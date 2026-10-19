"""Tests for catenary.py: parsing, catenary maths, checks, tables and the CLI."""

import contextlib
import io
import json
import math
import sys
import tempfile
import textwrap
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import catenary  # noqa: E402
from catenary import SolveError, SpecError, parse_spec, solve  # noqa: E402

EXAMPLES = ROOT / "examples"


def spec_from(text, source="test.cable"):
    return parse_spec(textwrap.dedent(text), source=source)


LEVEL = """
    span = 12 m
    weight = 0.35 kg/m
    sag = 0.6 m
"""


class ParsingTests(unittest.TestCase):

    def test_units_convert_to_si(self):
        spec = spec_from("""
            span = 12 ft
            weight = 1 lb/ft
            tension = 1 kN
        """)
        self.assertTrue(math.isclose(spec.span, 12 * 0.3048, rel_tol=1e-12))
        self.assertTrue(math.isclose(spec.weight, (0.45359237 / 0.3048) * catenary.G, rel_tol=1e-12))
        self.assertTrue(math.isclose(spec.tension, 1000.0, rel_tol=1e-12))

    def test_errors_carry_file_and_line_numbers(self):
        with self.assertRaises(SpecError) as ctx:
            spec_from("""
                span = 12 m
                colour = red
                weight = 0.3 kg
                sag = 0.6 m
            """, source="rope.cable")
        problems = ctx.exception.problems
        self.assertTrue(any(p.startswith("rope.cable:3:") and "unknown key" in p for p in problems))
        self.assertTrue(any(p.startswith("rope.cable:4:") and "unknown unit" in p for p in problems))

    def test_duplicate_key_points_at_first_occurrence(self):
        with self.assertRaises(SpecError) as ctx:
            spec_from("""
                span = 12 m
                span = 14 m
                weight = 0.3 kg/m
                sag = 0.6 m
            """)
        self.assertTrue(any(":2:" in p or ":3:" in p for p in ctx.exception.problems))
        self.assertTrue(any("first given on line 2" in p for p in ctx.exception.problems))

    def test_exactly_one_solve_mode(self):
        with self.assertRaises(SpecError) as ctx:
            spec_from("""
                span = 12 m
                weight = 0.3 kg/m
                sag = 0.6 m
                length = 12.2 m
            """)
        self.assertTrue(any("exactly one of sag, length or tension" in p
                            for p in ctx.exception.problems))

    def test_sag_mode_rejects_sloped_supports(self):
        with self.assertRaises(SpecError) as ctx:
            spec_from("""
                span = 12 m
                height_left = 2 m
                height_right = 3 m
                weight = 0.3 kg/m
                sag = 0.6 m
            """)
        self.assertTrue(any("equal support heights" in p for p in ctx.exception.problems))

    def test_price_per_foot_converts_to_metres(self):
        spec = spec_from(LEVEL + "price = 1 per ft\n")
        self.assertTrue(math.isclose(float(spec.price), 1 / 0.3048, rel_tol=1e-9))

    def test_span_range_is_inclusive(self):
        self.assertEqual(catenary.parse_span_range("10:30:5"), [10, 15, 20, 25, 30])
        with self.assertRaises(ValueError):
            catenary.parse_span_range("30:10:5")
        with self.assertRaises(ValueError):
            catenary.parse_span_range("10:30")


class CatenaryMathTests(unittest.TestCase):

    def test_level_sag_matches_closed_form(self):
        sol = solve(spec_from(LEVEL))
        a, span = sol.a, 12.0
        self.assertTrue(math.isclose(sol.sag, 0.6, rel_tol=1e-9))
        self.assertTrue(math.isclose(sol.sag, a * (math.cosh(span / (2 * a)) - 1), rel_tol=1e-9))
        self.assertTrue(math.isclose(sol.length, 2 * a * math.sinh(span / (2 * a)), rel_tol=1e-9))
        self.assertTrue(math.isclose(sol.H, sol.w * a, rel_tol=1e-12))

    def test_level_length_round_trip_recovers_sag(self):
        first = solve(spec_from(LEVEL))
        second = solve(spec_from(f"""
            span = 12 m
            weight = 0.35 kg/m
            length = {first.length!r} m
        """))
        self.assertTrue(math.isclose(second.sag, first.sag, rel_tol=1e-7))
        self.assertTrue(math.isclose(second.H, first.H, rel_tol=1e-7))

    def test_tension_round_trip_recovers_sag(self):
        first = solve(spec_from(LEVEL))
        second = solve(spec_from(f"""
            span = 12 m
            weight = 0.35 kg/m
            tension = {first.H!r} N
        """))
        self.assertTrue(math.isclose(second.sag, first.sag, rel_tol=1e-9))
        self.assertTrue(math.isclose(second.length, first.length, rel_tol=1e-9))

    def test_support_tension_satisfies_pythagoras(self):
        sol = solve(spec_from(LEVEL))
        self.assertTrue(math.isclose(sol.t_left ** 2, sol.H ** 2 + sol.v_left ** 2, rel_tol=1e-9))
        self.assertTrue(math.isclose(sol.t_right ** 2, sol.H ** 2 + sol.v_right ** 2, rel_tol=1e-9))

    def test_sloped_curve_meets_both_supports_and_has_right_length(self):
        spec = spec_from("""
            span = 9 m
            height_left = 6 m
            height_right = 9 m
            weight = 0.12 kg/m
            length = 10.2 m
        """)
        sol = solve(spec)
        a, x0, dh, span = sol.a, sol.lowest_x, 3.0, 9.0

        def y(x):
            return -sol.h_left + a * (math.cosh((x - x0) / a) - 1)

        self.assertTrue(math.isclose(y(0.0), 0.0, abs_tol=1e-9))
        self.assertTrue(math.isclose(y(span), dh, rel_tol=1e-9))

        steps = 4000
        h = span / steps
        total = 0.0
        for i in range(steps + 1):
            x = i * h
            slope = math.sinh((x - x0) / a)          # dy/dx of the catenary
            weight = 1 if i in (0, steps) else (4 if i % 2 else 2)
            total += weight * math.sqrt(1 + slope * slope)
        arc = total * h / 3
        self.assertTrue(math.isclose(sol.length, arc, rel_tol=1e-6))
        self.assertTrue(math.isclose(sol.length, 10.2, rel_tol=1e-9))

    def test_taut_sloped_cable_with_lowest_point_past_the_support(self):
        # Tension 500 N on 9 m with a 6 m rise: the lowest point lies far left of the span
        spec = spec_from("""
            span = 9 m
            height_left = 0 m
            height_right = 6 m
            weight = 0.12 kg/m
            tension = 500 N
        """)
        sol = solve(spec)
        a, x0, span, dh = sol.a, sol.lowest_x, 9.0, 6.0
        self.assertLess(x0, 0)
        self.assertAlmostEqual(sol.sag, 0.0, places=9)
        self.assertAlmostEqual(sol.clearance, 0.0, places=9)

        def y(x):
            return -sol.h_left + a * (math.cosh((x - x0) / a) - 1)

        self.assertTrue(math.isclose(y(0.0), 0.0, abs_tol=1e-9))
        self.assertTrue(math.isclose(y(span), dh, rel_tol=1e-9))

        steps = 4000
        h = span / steps
        total = 0.0
        for i in range(steps + 1):
            slope = math.sinh((i * h - x0) / a)
            factor = 1 if i in (0, steps) else (4 if i % 2 else 2)
            total += factor * math.sqrt(1 + slope * slope)
        self.assertTrue(math.isclose(sol.length, total * h / 3, rel_tol=1e-6))
        self.assertTrue(math.isclose(sol.v_left + sol.v_right, sol.w * sol.length, rel_tol=1e-9))
        self.assertLess(sol.v_left, 0)  # the cable pulls the low support up

    def test_higher_support_carries_higher_tension(self):
        sol = solve(spec_from("""
            span = 9 m
            height_left = 6 m
            height_right = 9 m
            weight = 0.12 kg/m
            length = 10.2 m
        """))
        self.assertGreater(sol.t_right, sol.t_left)
        self.assertTrue(math.isclose(sol.tension_max, sol.t_right))

    def test_length_shorter_than_chord_is_rejected(self):
        spec = spec_from("""
            span = 9 m
            height_left = 6 m
            height_right = 9 m
            weight = 0.12 kg/m
            length = 9.3 m
        """)
        with self.assertRaises(SolveError):
            solve(spec)


class ChecksAndCostTests(unittest.TestCase):

    def test_cut_length_rounds_up_and_cost_is_exact_cents(self):
        sol = solve(spec_from("""
            span = 12 m
            weight = 0.35 kg/m
            length = 12.401 m
            anchor = 0.3 m
            price = 1.90 per m
        """))
        self.assertEqual(sol.cut_length, Decimal("13.01"))
        self.assertEqual(sol.cost, Decimal("24.72"))

    def test_safety_factor_failure_sets_result_to_fail(self):
        sol = solve(spec_from(LEVEL + "strength = 100 N\nsafety = 4\n"))
        self.assertFalse(sol.ok)
        self.assertTrue(any(c.name == "safety factor" and not c.ok for c in sol.checks))

    def test_clearance_limit(self):
        sol = solve(spec_from(LEVEL + "height_left = 1.8 m\nheight_right = 1.8 m\n"
                                      "min_clearance = 1.5 m\n"))
        self.assertTrue(math.isclose(sol.clearance, 1.2, rel_tol=1e-9))
        self.assertFalse(sol.ok)

    def test_max_tension_limit_passes_when_slack_enough(self):
        sol = solve(spec_from(LEVEL + "max_tension = 1000 N\n"))
        self.assertTrue(sol.ok)

    def test_example_files_pass(self):
        for name in ("clothesline.cable", "sloped-wire.cable"):
            with self.subTest(name=name):
                sol = solve(catenary.load_spec(EXAMPLES / name))
                self.assertTrue(sol.ok, [c.detail for c in sol.checks if not c.ok])


class TableAndProfileTests(unittest.TestCase):

    def test_span_table_holds_horizontal_tension_and_sag_grows(self):
        spec = spec_from(LEVEL)
        base = solve(spec)
        rows = catenary.span_table(spec, [8, 10, 12, 14])
        for row in rows:
            self.assertTrue(math.isclose(row.H, base.H, rel_tol=1e-9))
        sags = [row.sag for row in rows]
        self.assertEqual(sags, sorted(sags))
        self.assertTrue(math.isclose(rows[2].sag, 0.6, rel_tol=1e-9))

    def test_profile_draws_both_supports_and_cable(self):
        sol = solve(spec_from(LEVEL))
        picture = catenary.render_profile(sol)
        self.assertIn("L", picture)
        self.assertIn("R", picture)
        self.assertIn("*", picture)
        self.assertIn("sag 0.600", picture)


class CommandLineTests(unittest.TestCase):

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = catenary.main(argv)
        return code, out.getvalue(), err.getvalue()

    def write(self, directory, name, text):
        path = Path(directory) / name
        path.write_text(textwrap.dedent(text), encoding="utf-8")
        return str(path)

    def test_exit_codes_for_pass_fail_and_bad_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = self.write(tmp, "good.cable", LEVEL + "strength = 1 kN\nsafety = 2\n")
            fail = self.write(tmp, "fail.cable", LEVEL + "strength = 10 N\nsafety = 2\n")
            bad = self.write(tmp, "bad.cable", "span = twelve\n")
            code, out, _ = self.run_cli(["solve", good])
            self.assertEqual(code, 0)
            self.assertIn("RESULT: PASS", out)
            code, out, _ = self.run_cli(["solve", fail])
            self.assertEqual(code, 1)
            self.assertIn("RESULT: FAIL", out)
            code, _, err = self.run_cli(["solve", bad])
            self.assertEqual(code, 2)
            self.assertIn(":1:", err)
            code, _, _ = self.run_cli(["solve", str(Path(tmp) / "missing.cable")])
            self.assertEqual(code, 2)

    def test_check_command_is_quiet_on_valid_file(self):
        code, out, _ = self.run_cli(["check", str(EXAMPLES / "clothesline.cable")])
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("OK:"))

    def test_json_output_is_parseable_and_complete(self):
        code, out, _ = self.run_cli(["solve", str(EXAMPLES / "clothesline.cable"), "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertTrue(data["ok"])
        self.assertEqual(data["solved_from"], "sag")
        # 0.6 m sag on 12 m gives a cable of about 12.079 m; +0.6 m anchors, rounded up
        self.assertTrue(math.isclose(data["length_m"], 12.0793, abs_tol=1e-3))
        self.assertEqual(data["cut_length_m"], "12.68")
        self.assertEqual(data["cost"], "24.09")

    def test_table_command_flags_spans_that_break_clearance(self):
        # Constant tension: sag grows with span, so the 1.0 m clearance fails at 14 m only
        code, out, _ = self.run_cli(["table", str(EXAMPLES / "clothesline.cable"),
                                     "--spans", "8:14:2"])
        rows = out.strip().splitlines()[1:]
        self.assertEqual(len(rows), 4)
        self.assertTrue(rows[0].endswith("PASS"))
        self.assertTrue(rows[-1].endswith("FAIL"))
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
