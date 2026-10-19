# Catenary

**Offline hanging-cable planner: plain-text cable specs in, sag, tension, length, clearance, safety factor, cut lengths, cost and ASCII profiles out.**

Describe a clothesline, a guy line, an overhead wire or a rope bridge in a short text file. Catenary solves the curve the cable actually makes, checks it against your limits, and tells you how much cable to cut. It is one Python file, uses only the standard library, and makes no network calls.

## Features

- **Plain-text `.cable` specs** with `key = value` lines, `#` comments and friendly units (m, ft, cm, mm, kg/m, lb/ft, g/m, N, kN, lbf, kgf).
- **Line-numbered errors**: unknown keys, duplicate keys, bad units and missing values are reported as `file:line: message`, all at once.
- **Three ways to solve**: give the sag, the cable length or the horizontal tension, and the rest is worked out.
- **Unequal support heights**: sloped runs are solved in closed form, with the lowest point's position reported.
- **Exact support forces**: tension at each support, vertical reactions and the maximum tension, using `T² = H² + V²`.
- **Extra load**: add a uniform wet, iced or wind load on top of the cable's own mass.
- **Ground clearance**: height of the lowest cable point above the ground, with an optional minimum.
- **Safety factor** against a breaking strength, with an optional minimum.
- **Tension limit** check for hardware or anchors with a rated maximum.
- **Cut plan**: cable length plus anchor allowance per end, rounded up to the centimetre.
- **Cost in exact cents** from a price per metre or per foot.
- **Span table** at constant horizontal tension, to see how sag and clearance change across spans.
- **ASCII side view** of the cable between its supports.
- **JSON output** for scripts, and **CI exit codes**: `0` pass, `1` a check failed, `2` input error.

## Quick start

Save this as `clothesline.cable`, or use the copy in `examples/`:

```text
# Garden clothesline strung between two posts
name = Garden clothesline
span = 12 m
height_left = 1.8 m
height_right = 1.8 m
weight = 0.35 kg/m        # polypropylene line
extra = 0.05 kg/m         # allowance for wet washing
sag = 0.6 m               # target sag at mid-span
strength = 1.2 kN         # breaking strength from the datasheet
safety = 4                # minimum strength / max tension
min_clearance = 1.0 m     # lowest point must stay above a child's head
anchor = 0.3 m            # cable wrapped around each post
price = 1.90 per m
```

Then run:

```sh
python3 catenary.py check examples/clothesline.cable
python3 catenary.py solve examples/clothesline.cable --profile
python3 catenary.py table examples/clothesline.cable --spans 8:14:2
```

`solve` prints a report with every derived value, one PASS or FAIL line per limit, and a final `RESULT:` line. `--profile` adds a side view. `table` shows how the same cable behaves on other spans.

## Spec reference

| Key | Meaning | Units |
| --- | --- | --- |
| `name` | Label for reports | text |
| `span` | Horizontal distance between supports | length |
| `height_left`, `height_right` | Support heights above ground (default 0) | length |
| `weight` | Cable mass per unit length (required) | kg/m, g/m, lb/ft, lb/m |
| `extra` | Additional uniform load | same as `weight` |
| `sag`, `length`, `tension` | Give exactly one. Sag needs equal support heights. | m, m, N/kN/lbf/kgf |
| `strength` | Breaking strength | N, kN, lbf, kgf |
| `safety` | Minimum strength / maximum tension (needs `strength`) | number |
| `max_tension` | Largest allowed tension | N, kN, lbf, kgf |
| `min_clearance` | Smallest allowed height of the lowest point above ground | length |
| `anchor` | Extra cable per end for wrapping or termination | length |
| `price` | Cost of the cut cable | `1.90 per m` or `0.60 per ft` |

## Testing

The tests use only `unittest`, so they run with either runner:

```sh
python3 -m pytest -q tests
python3 -m unittest discover -s tests -v
```

They check the unit conversions, error line numbers, the closed-form level catenary, round trips between sag, length and tension, a sloped curve against numerical arc length, cut rounding and exact cents, the safety, clearance and tension checks, the span table, the profile, and the CLI exit codes.

## Method

For a cable with horizontal tension `H` and load `w` per metre, the catenary parameter is `a = H / w`. Writing `t = span / (2a)`, the sloped-support geometry has a closed form. The lowest point sits at `span/2 - a·φ`, where `sinh φ = dh / (span · sinh(t) / t)` and `dh` is the height difference. Length, sag and support tensions follow directly. Sag and length targets are solved by bisection on `t`, which is monotonic in both.

## Limits

- Cables are treated as inextensible and perfectly flexible. Stretch, thermal change and creep are not modelled.
- Loads are uniform along the cable. Point loads such as a hanging lamp are not modelled.
- Sag can only be specified for level supports. Use length or tension for sloped runs.
- With sloped supports, the lowest point can fall outside the span (a taut cable). The sag is then reported as `0`, and the vertical reaction at the low support is negative, meaning the cable pulls it up.
- Results are a planning aid. Check anything load-bearing against your hardware's ratings and local codes.

## Layout

```
catenary.py            the planner (CLI and library)
examples/              sample specs
tests/test_catenary.py unit and CLI tests
```
