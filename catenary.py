#!/usr/bin/env python3
"""Catenary: offline hanging-cable planner driven by plain-text cable specs.

Solves the catenary curve of a cable hung between two supports. Give the span,
the cable's mass per metre and exactly one of sag, cable length or horizontal
tension; the rest follows. Unequal support heights are supported. Standard
library only.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass, field, replace
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

G = 9.80665                  # m/s^2, standard gravity
FT = 0.3048                  # m per foot
LB = 0.45359237              # kg per pound
LBF = 4.4482216152605        # N per pound-force
MAX_SPANS = 500
SOLVE_T_MIN, SOLVE_T_MAX = 1e-6, 300.0  # search range for t = span / (2a)

LENGTH_UNITS = {"m": 1.0, "cm": 0.01, "mm": 0.001, "ft": FT}
LOAD_UNITS = {"kg/m": 1.0, "g/m": 0.001, "lb/m": LB, "lb/ft": LB / FT}  # kg per metre
TENSION_UNITS = {"n": 1.0, "kn": 1000.0, "lbf": LBF, "kgf": G}          # newtons

LENGTH_KEYS = ("span", "height_left", "height_right", "sag", "length", "anchor", "min_clearance")
LOAD_KEYS = ("weight", "extra")
TENSION_KEYS = ("tension", "strength", "max_tension")
MODE_KEYS = ("sag", "length", "tension")
KEYS = ("name", "price", "safety") + LENGTH_KEYS + LOAD_KEYS + TENSION_KEYS

QUANTITY_RE = re.compile(r"([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*([A-Za-z]+(?:/[A-Za-z]+)?)")
PRICE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*per\s*(m|ft)", re.IGNORECASE)


class SpecError(Exception):
    """A cable file has problems. Carries one message per problem."""

    def __init__(self, problems):
        super().__init__("; ".join(problems))
        self.problems = list(problems)


class SolveError(ValueError):
    """A valid spec has no physical solution, e.g. a cable shorter than its span."""


@dataclass
class Spec:
    name: str = "cable"
    span: float = 0.0             # m
    height_left: float = 0.0      # m above ground
    height_right: float = 0.0     # m above ground
    weight: float = 0.0           # N/m, cable mass
    extra: float = 0.0            # N/m, extra uniform load (wet, ice, ...)
    sag: float | None = None      # m
    length: float | None = None   # m, cable length between supports
    tension: float | None = None  # N, horizontal tension
    strength: float | None = None # N, breaking strength
    safety: float | None = None   # minimum strength / max tension
    max_tension: float | None = None  # N
    min_clearance: float | None = None  # m above ground at the lowest point
    anchor: float = 0.0           # m of extra cable per end, for cutting
    price: Decimal | None = None  # money per metre of cut cable


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class Solution:
    spec: Spec
    solved_from: str
    w: float               # N/m, total load per metre
    a: float               # catenary parameter H / w, m
    H: float               # horizontal tension, N
    lowest_x: float        # lowest point, m from the left support
    h_left: float          # height of left support above the lowest point, m
    sag: float             # chord-to-cable drop at the lowest point on the span, m (0 if beyond a support)
    length: float          # cable length between supports, m
    t_left: float          # tension at left support, N
    t_right: float         # tension at right support, N
    v_left: float          # vertical reaction at left support, N (negative = pulled up)
    v_right: float         # vertical reaction at right support, N (negative = pulled up)
    clearance: float       # lowest cable point above ground, m
    cut_length: Decimal    # m, length plus anchors, rounded up to cm
    cost: Decimal | None
    safety_factor: float | None
    checks: list[Check] = field(default_factory=list)

    @property
    def tension_max(self) -> float:
        return max(self.t_left, self.t_right)

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)


# ---------------------------------------------------------------- parsing

def parse_quantity(key, value, units):
    """Parse '12 m' style values into SI using the given unit table."""
    match = QUANTITY_RE.fullmatch(value.strip())
    if match is None:
        raise ValueError(f"{key} needs a number and one unit ({', '.join(units)}), got {value!r}")
    unit = match.group(2).lower()
    if unit not in units:
        raise ValueError(f"{key}: unknown unit {match.group(2)!r} (use {', '.join(units)})")
    result = float(match.group(1)) * units[unit]
    if not math.isfinite(result):
        raise ValueError(f"{key} must be a finite number, got {value!r}")
    return result


def parse_number(key, value):
    try:
        number = float(value)
    except ValueError:
        raise ValueError(f"{key} needs a plain number, got {value!r}") from None
    if not math.isfinite(number):
        raise ValueError(f"{key} must be a finite number, got {value!r}")
    return number


def parse_price(value):
    """'1.90 per m' or '0.60 per ft' -> money per metre, exact as Decimal."""
    match = PRICE_RE.fullmatch(value.strip())
    if match is None:
        raise ValueError(f"price needs the form '1.90 per m' or '0.60 per ft', got {value!r}")
    price = Decimal(match.group(1))
    if match.group(2).lower() == "ft":
        price = price / Decimal(repr(FT))
    return price


def _apply(spec, key, value):
    if key == "name":
        if not value:
            raise ValueError("name cannot be empty")
        spec.name = value
    elif key in LENGTH_KEYS:
        setattr(spec, key, parse_quantity(key, value, LENGTH_UNITS))
    elif key in LOAD_KEYS:
        setattr(spec, key, parse_quantity(key, value, LOAD_UNITS) * G)
    elif key in TENSION_KEYS:
        setattr(spec, key, parse_quantity(key, value, TENSION_UNITS))
    elif key == "safety":
        spec.safety = parse_number(key, value)
    else:  # price
        spec.price = parse_price(value)


def _validate(spec, source, given):
    problems = []
    modes = [key for key in MODE_KEYS if key in given]
    if len(modes) != 1:
        problems.append(f"{source}: give exactly one of sag, length or tension (found {len(modes)})")
    if "span" not in given or spec.span <= 0:
        problems.append(f"{source}: span must be given and greater than 0")
    if "weight" not in given or spec.weight <= 0:
        problems.append(f"{source}: weight must be given and greater than 0")
    for key in ("sag", "length", "tension", "strength", "safety", "max_tension"):
        if key in given and getattr(spec, key) <= 0:
            problems.append(f"{source}: {key} must be greater than 0")
    if "safety" in given and "strength" not in given:
        problems.append(f"{source}: safety needs a strength to check against")
    if "anchor" in given and spec.anchor < 0:
        problems.append(f"{source}: anchor cannot be negative")
    if "price" in given and spec.price <= 0:
        problems.append(f"{source}: price must be greater than 0")
    if "sag" in modes and spec.height_left != spec.height_right:
        problems.append(f"{source}: sag needs equal support heights; "
                        "give length or tension for sloped supports")
    return problems


def parse_spec(text, source="<spec>"):
    """Parse cable spec text. Raises SpecError listing every problem found."""
    spec = Spec()
    problems = []
    first_line = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if "=" not in line:
            problems.append(f"{source}:{lineno}: expected 'key = value', got {line!r}")
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        key = key.lower()
        if key not in KEYS:
            problems.append(f"{source}:{lineno}: unknown key {key!r}")
        elif key in first_line:
            problems.append(f"{source}:{lineno}: duplicate key {key!r} "
                            f"(first given on line {first_line[key]})")
        else:
            first_line[key] = lineno
            try:
                _apply(spec, key, value)
            except ValueError as exc:
                problems.append(f"{source}:{lineno}: {exc}")
    if not problems:
        problems = _validate(spec, source, first_line)
    if problems:
        raise SpecError(problems)
    return spec


def load_spec(path):
    with open(path, encoding="utf-8") as handle:
        return parse_spec(handle.read(), source=path)


def parse_span_range(text):
    """'10:30:5' -> [10, 15, 20, 25, 30] metres, inclusive of the stop value."""
    parts = text.split(":")
    if len(parts) != 3:
        raise ValueError("--spans must look like START:STOP:STEP, for example 10:30:5")
    start, stop, step = (float(part) for part in parts)
    if not (0 < start <= stop and step > 0):
        raise ValueError("--spans needs 0 < START <= STOP and STEP > 0")
    count = int(math.floor((stop - start) / step + 1e-9)) + 1
    if count > MAX_SPANS:
        raise ValueError(f"--spans gives {count} spans; the limit is {MAX_SPANS}")
    return [round(start + i * step, 9) for i in range(count)]


# ---------------------------------------------------------------- maths

def _sinhc(t):
    """sinh(t) / t, stable near zero."""
    if abs(t) < 1e-6:
        return 1.0 + t * t / 6.0
    return math.sinh(t) / t


def _solve_increasing(f, what):
    """Root of an increasing f on the solver range, by bisection."""
    lo, hi = SOLVE_T_MIN, SOLVE_T_MAX
    if f(lo) > 0 or f(hi) < 0:
        raise SolveError(f"{what} is outside the range this span can give")
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if f(mid) < 0:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def _catenary_t(spec, w, dh):
    """Solve t = span / (2a), where a = H / w is the catenary parameter."""
    span = spec.span
    if spec.tension is not None:
        t = span * w / (2.0 * spec.tension)
        if not SOLVE_T_MIN <= t <= SOLVE_T_MAX:
            raise SolveError(f"tension {spec.tension:.2f} N gives a shape outside the solver range")
        return t
    if spec.length is not None:
        chord = math.hypot(span, dh)
        if spec.length <= chord:
            raise SolveError(f"length {spec.length:.4f} m must be longer than the "
                             f"support chord {chord:.4f} m")
        # sinh(t)/t = sqrt(S^2 - dh^2) / span for a cable of length S
        ratio = math.sqrt(spec.length ** 2 - dh ** 2) / span
        return _solve_increasing(lambda t: _sinhc(t) - ratio, "length")
    # sag: for level supports the sag depends on t alone, sag = (span/t) sinh^2(t/2)
    return _solve_increasing(lambda t: span / t * math.sinh(t / 2.0) ** 2 - spec.sag, "sag")


def solve(spec):
    """Solve the cable geometry and run its limit checks for a validated spec."""
    span = spec.span
    w = spec.weight + spec.extra
    dh = spec.height_right - spec.height_left
    solved_from = next(key for key in MODE_KEYS if getattr(spec, key) is not None)

    t = _catenary_t(spec, w, dh)
    a = span / (2.0 * t)
    phi = math.asinh(dh / (span * _sinhc(t)))       # tilt caused by unequal supports
    x0 = span / 2.0 - a * phi                       # lowest point, from left support
    h_left = 2.0 * a * math.sinh((t - phi) / 2.0) ** 2
    x_c = min(max(x0, 0.0), span)                   # lowest point actually on the cable
    y_c = -h_left + 2.0 * a * math.sinh((x_c - x0) / (2.0 * a)) ** 2
    sag = dh * x_c / span - y_c

    t_left = w * a * math.cosh(t - phi)
    t_right = w * a * math.cosh(t + phi)
    # Signed: a negative reaction means the cable pulls that support up. This
    # happens when the lowest point lies beyond that support.
    v_left = w * a * math.sinh(t - phi)
    v_right = w * a * math.sinh(t + phi)
    length = (v_left + v_right) / w
    clearance = spec.height_left + y_c
    tension_max = max(t_left, t_right)

    cut_length = Decimal(repr(round(length + 2.0 * spec.anchor, 9))).quantize(
        Decimal("0.01"), rounding=ROUND_CEILING)
    cost = None
    if spec.price is not None:
        cost = (cut_length * spec.price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    checks = []
    if spec.max_tension is not None:
        checks.append(Check("max tension", tension_max <= spec.max_tension,
                            f"{tension_max:.2f} N against {spec.max_tension:.2f} N limit"))
    safety_factor = None
    if spec.strength is not None:
        safety_factor = spec.strength / tension_max
        if spec.safety is None:
            checks.append(Check("safety factor", True, f"{safety_factor:.2f} (no minimum set)"))
        else:
            checks.append(Check("safety factor", safety_factor >= spec.safety,
                                f"{safety_factor:.2f} against {spec.safety:g} minimum"))
    if spec.min_clearance is not None:
        checks.append(Check("clearance", clearance >= spec.min_clearance,
                            f"{clearance:.3f} m against {spec.min_clearance:.3f} m minimum"))

    return Solution(
        spec=spec, solved_from=solved_from, w=w, a=a, H=w * a, lowest_x=x0,
        h_left=h_left, sag=sag, length=length, t_left=t_left, t_right=t_right,
        v_left=v_left, v_right=v_right, clearance=clearance, cut_length=cut_length,
        cost=cost, safety_factor=safety_factor, checks=checks,
    )


def span_table(spec, spans):
    """Solve each span with the same horizontal tension as the base spec."""
    base = solve(spec)
    return [solve(replace(spec, span=span, sag=None, length=None, tension=base.H))
            for span in spans]


def cable_height(sol, x):
    """Height of the cable above ground at distance x from the left support."""
    a = sol.a
    return (sol.spec.height_left - sol.h_left
            + 2.0 * a * math.sinh((x - sol.lowest_x) / (2.0 * a)) ** 2)


# ---------------------------------------------------------------- output

def format_report(sol):
    s = sol.spec
    rows = [
        ("solved from", f"{sol.solved_from} ({getattr(s, sol.solved_from):.4f} "
                        f"{'N' if sol.solved_from == 'tension' else 'm'})"),
        ("span", f"{s.span:.3f} m"),
        ("supports above ground", f"{s.height_left:.3f} m left, {s.height_right:.3f} m right"),
        ("load per metre", f"{sol.w:.3f} N/m ({sol.w / G:.3f} kg/m)"),
        ("horizontal tension", f"{sol.H:.2f} N"),
        ("catenary parameter a", f"{sol.a:.4f} m"),
        ("sag", f"{sol.sag:.4f} m"),
        ("lowest point", f"{sol.lowest_x:.3f} m from left support"),
        ("cable length", f"{sol.length:.4f} m"),
        ("support tension", f"{sol.t_left:.2f} N left, {sol.t_right:.2f} N right"),
        ("max tension", f"{sol.tension_max:.2f} N"),
        ("reaction (vertical)", f"{sol.v_left:.2f} N left, {sol.v_right:.2f} N right"),
        ("lowest clearance", f"{sol.clearance:.3f} m above ground"),
        ("cut length", f"{sol.cut_length} m (anchors {s.anchor:.3f} m each end)"),
    ]
    if sol.cost is not None:
        rows.append(("cable cost", f"{sol.cost} (at {s.price} per m)"))
    lines = [s.name, ""]
    lines += [f"  {label:<24}{value}" for label, value in rows]
    if sol.checks:
        lines += ["", "Checks"]
        lines += [f"  [{'PASS' if c.ok else 'FAIL'}] {c.name}: {c.detail}" for c in sol.checks]
    lines += ["", f"RESULT: {'PASS' if sol.ok else 'FAIL'}"]
    return "\n".join(lines)


def render_profile(sol, width=60, height=12):
    """ASCII side view of the cable. '.' is ground, L and R are the supports."""
    spec = sol.spec
    xs = [spec.span * i / (width - 1) for i in range(width)]
    ys = [cable_height(sol, x) for x in xs]
    tops = ys + [spec.height_left, spec.height_right, 0.0]
    lo, hi = min(tops), max(tops)
    scale = (hi - lo) or 1.0

    def row(y):
        return round((hi - y) / scale * (height - 1))

    grid = [[" "] * width for _ in range(height)]
    for col in range(width):
        grid[row(0.0)][col] = "."
    for col, y in enumerate(ys):
        grid[row(y)][col] = "*"
    grid[row(spec.height_left)][0] = "L"
    grid[row(spec.height_right)][-1] = "R"
    lines = ["".join(cells).rstrip() for cells in grid]
    lines.append(f"span {spec.span:.2f} m, height {lo:.2f} to {hi:.2f} m, "
                 f"sag {sol.sag:.3f} m")
    return "\n".join(lines)


def to_dict(sol):
    s = sol.spec
    return {
        "name": s.name,
        "solved_from": sol.solved_from,
        "span_m": s.span,
        "load_n_per_m": sol.w,
        "horizontal_tension_n": sol.H,
        "catenary_parameter_m": sol.a,
        "sag_m": sol.sag,
        "lowest_point_m": sol.lowest_x,
        "length_m": sol.length,
        "tension_left_n": sol.t_left,
        "tension_right_n": sol.t_right,
        "tension_max_n": sol.tension_max,
        "reaction_left_n": sol.v_left,
        "reaction_right_n": sol.v_right,
        "clearance_m": sol.clearance,
        "cut_length_m": str(sol.cut_length),
        "cost": str(sol.cost) if sol.cost is not None else None,
        "safety_factor": sol.safety_factor,
        "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in sol.checks],
        "ok": sol.ok,
    }


def format_table(rows):
    header = (f"{'span(m)':>8}  {'sag(m)':>8}  {'length(m)':>10}  "
              f"{'Tmax(N)':>9}  {'clear(m)':>9}  result")
    lines = [header]
    for sol in rows:
        lines.append(f"{sol.spec.span:>8.3f}  {sol.sag:>8.4f}  {sol.length:>10.4f}  "
                     f"{sol.tension_max:>9.2f}  {sol.clearance:>9.3f}  "
                     f"{'PASS' if sol.ok else 'FAIL'}")
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="catenary", description="Plan hanging cables from plain-text specs.")
    sub = parser.add_subparsers(dest="command", required=True)

    check_p = sub.add_parser("check", help="lint a cable file without solving it")
    check_p.add_argument("file")

    solve_p = sub.add_parser("solve", help="solve a cable file and print the report")
    solve_p.add_argument("file")
    solve_p.add_argument("--json", action="store_true", help="print JSON instead of text")
    solve_p.add_argument("--profile", action="store_true", help="add an ASCII side view")

    table_p = sub.add_parser("table", help="sag and tension across spans at constant tension")
    table_p.add_argument("file")
    table_p.add_argument("--spans", required=True, metavar="START:STOP:STEP",
                         help="span range in metres, for example 10:30:5")
    table_p.add_argument("--json", action="store_true", help="print JSON instead of text")

    args = parser.parse_args(argv)
    try:
        spans = parse_span_range(args.spans) if args.command == "table" else None
        spec = load_spec(args.file)
        if args.command == "check":
            print(f"OK: {args.file}")
            return 0
        if args.command == "solve":
            sol = solve(spec)
            if args.json:
                print(json.dumps(to_dict(sol), indent=2))
            else:
                print(format_report(sol))
                if args.profile:
                    print()
                    print(render_profile(sol))
            return 0 if sol.ok else 1
        rows = span_table(spec, spans)
        if args.json:
            print(json.dumps([to_dict(sol) for sol in rows], indent=2))
        else:
            print(format_table(rows))
        return 0 if all(sol.ok for sol in rows) else 1
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except SpecError as exc:
        for problem in exc.problems:
            print(problem, file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
