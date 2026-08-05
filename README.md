# bbnet

A netlist, design-rule-check, autorouter, and printable build-sheet engine
for **hand-built breadboards** — solderable or solderless.

You write down what is physically on each board — parts, rail
assignments, jumpers, off-board leads, cross-board interlinks — as YAML.
bbnet derives the netlist from that geometry, runs a design-rule check
against it, routes the wires, and renders a build sheet (an HTML board
view plus a per-board kitting table) that you work from at the bench.
`bbnet check` is meant to run in CI: a design-rule violation, or a build
sheet that has drifted from what the YAML now says, both fail the gate.

## Why this exists

KiCad's data model has no concept of a breadboard row or a rail strip —
it assumes copper you route, not holes you jumper by hand — and its
edit-verify cycle is far slower than bench-pace iteration (move a part,
re-check, move again). Without a tool built for the actual medium,
nothing tracks what is really on the board: wiring drifts from intent
silently, and the only record of a bench build is the bench itself.
bbnet is a purpose-built alternative: the YAML *is* the record, the DRC
is the check, and the rendered build sheet is what regenerates from it —
never hand-maintained, never silently stale.

## Quickstart

```
pip install -e .
```

The fastest proof it works is the test suite, which exercises the engine
against the synthetic fixture corpus committed at `tests/fixtures/`:

```
$ pytest -q
........................................................................ [ 45%]
........................................................................ [ 91%]
..............                                                           [100%]
158 passed in 0.59s
```

Trying the CLI directly against that same fixture corpus is worth doing
once, because it demonstrates a real piece of the engine's contract
rather than just "it works":

```
$ bbnet check --data-dir tests/fixtures --pinmap tests/fixtures/pinmap.csv
DRC rail-split [warning]: rail '3V3' spans 2 disconnected strips — bridge them or rename one
DRC rail-split [warning]: rail 'GND' spans 3 disconnected strips — bridge them or rename one
DRC pinmap-xcheck [warning]: U1.7: bench uses a pin the pin table leaves unallocated (demo-mcu pin 7) — update the pin table or add to bench_only
DRC: 0 error(s), 3 warning(s)
bbnet check OK: DRC has no errors, REPORT.md + LAYOUT.html in sync
```

`--pinmap` reads a CSV (header row exactly `mcu,pin,signal`) and
registers its rows as the signal source named `pinmap` — the same name
`pin_signals: pinmap:<mcu>` already refers to. The fixture corpus
includes one footprint (`mcu-demo`, placed as `U1` in
`tests/fixtures/demo-left.yaml`) that declares
`pin_signals: pinmap:demo-mcu`, and `tests/fixtures/pinmap.csv` is that
source's table: header only, no data rows, because this fixture's MCU
has no pin allocated yet. That's not a placeholder waiting to be filled
in — it's the shipped example for `bbnet`'s own B7 (`pinmap-xcheck`)
warning above, which fires precisely because pin 7 is used on the bench
but the pin table (this CSV) leaves it unallocated. A header-only file
is a legitimate table, not an empty one; see
[Configuration semantics](#configuration-semantics) for why an *absent*
source is a different, harder failure than an empty one.

`bbnet report` and `bbnet render` regenerate `REPORT.md` and
`LAYOUT.html` the same way; `bbnet todo` prints unmet `rules.yaml`
requirements (`no unmet requirements — nothing to place` for this
fixture); `bbnet bom` rolls up parts and passives per island. All five
subcommands accept `--pinmap`.

Drop `--pinmap` from that same command and it raises instead:

```
$ bbnet check --data-dir tests/fixtures
...
bbnet.signals.UnknownSignalSource: footprint declares signal source 'pinmap', but no such source is registered (registered: none)
```

This is not a bug — it is the *raise for contracts* rule (see
[Configuration semantics](#configuration-semantics) below) firing for
real. `mcu-demo` names a `pinmap` signal source and nothing supplied
one, so the engine refuses to derive a design from it rather than
silently deriving one that never checked what it claims to check
(DRC B3/B7 would both go quiet while the run still reported clean). A
project with no footprint declaring `pin_signals:` never needs `--pinmap`
at all, and the plain `bbnet <command> --data-dir <dir>` just works — the
raise only fires when a footprint names a source and the flag is
missing, never unconditionally.

A host application that embeds the engine (rather than driving it
through this CLI) registers a source the same way, in Python, before
calling in — see `tests/helpers.py`'s `registry()` for the two-line
version the test suite itself uses.

## The data model

- **Islands** — one YAML file per physical board (`demo-left.yaml`,
  `demo-right.yaml` in the fixtures): its board type, rail net
  assignments, placed parts, jumpers, off-board leads, and interlinks to
  other islands.
- **`parts.yaml`** — the footprint library: named part shapes (`dip`,
  `sil`, ...) with pin counts/spans, referenced from islands by name.
- **`colours.yaml`** — the wire-colour vocabulary and the classes that
  constrain which colours a given net may use (DRC B6), plus rail-strip
  tint overrides for the rendered build sheet.
- **`rules.yaml`** — declarative per-pin requirements (decoupling caps,
  ties, etc. — DRC B5) and the net-name → nominal-voltage table used by
  cap-polarity and voltage-rating checks (B10/B11).
- **`layout.yaml`** — panels: boards that sit side by side on the bench
  and render as one stitched build sheet, with cross-board interlinks
  drawn once across the seam rather than twice as ghost stubs.

### Addressing

- Rows are numbered top to bottom; each row splits into two independent
  nodes at the ravine — holes `a`–`e` are the **left** node, `f`–`j` the
  **right**.
- `43L` / `43R` — the canonical half-row node address.
- `43c` — a hole address; canonicalizes to `43L` for netlisting, but the
  letter is kept for hole-occupancy checking (DRC B1) so two parts can't
  claim the same physical hole on the same node.
- `rail:5V` — a rail strip by declared net name (must be unambiguous);
  `rail:top+` — by position; `rail:top+@5` — pins a rail's drawn position
  to a row height (geometry only).
- `U1.29` — pin sugar: a part ref plus pin name, resolved through that
  part's placement.
- `gps-imu:4L` — a cross-island address (`<island>:<local address>`),
  for a lead or jumper that lands on another board.

## Design-rule checks

Each rule encodes a real breadboard bug class (from `src/bbnet/drc.py`'s
own index):

| Rule | Checks |
|------|--------|
| B1 occupancy | two legs in one hole; more than 5 members on a half-row |
| B2 rail-short | one net carrying two rail names; split-rail assertions |
| B3 signal-short | one net seeded with two pinmap signals (unless tied) |
| B4 floating | a must-connect pin alone on its node |
| B5 requirements | declarative per-pin constraints (`rules.yaml`) — unmet ones double as the placement todo/BOM list |
| B6 colour | jumper/lead colour vs. `colours.yaml` vocabulary + classes |
| B7 pinmap-xcheck | bench placement vs. an external pin-allocation table |
| B8 passive-span | a passive placed tighter than its leads can bend |
| B9 passive-overlap | two same-face passive bodies crossing or lying along each other |
| B10 cap-polarity | electrolytics are polarized (`from` = "+" by convention); flags one reversed across GND and power |
| B11 voltage-rating | a rated part across a known power net: error when the rail exceeds the rating, warning on thin derating |

## Configuration semantics

Three deliberately different answers live in this engine, and a
stranger's first config edit will trip whichever one they guess wrong:

- **`colours.yaml`'s `rail_tints:` MERGES** over the built-in defaults.
  This is presentation only — a rail name with no tint just renders
  grey, so merging is safe and additive.
- **`rules.yaml`'s `net_voltages:` REPLACES wholesale.** This is a
  vocabulary, not a style sheet: merging a regex table has no
  well-defined precedence when two patterns could both match a net name,
  and a silently-shadowed pattern would change a *safety* verdict
  (cap-polarity, voltage-rating). Declaring one entry means you now own
  the entire table — including `GND` — not just the entry you added.
- **An unregistered signal source RAISES.** A host application that
  wants `pin_signals:` seeding must register the source by name before
  calling into the engine (the CLI's own hook for this is `--pinmap`, see
  [Quickstart](#quickstart)). Returning an empty list for an unregistered
  name instead would silently mute two DRC rules (B3, B7) while the run
  still reports clean — a check that finds nothing must stay
  distinguishable from a check that never ran.

**Merge for presentation, replace for vocabulary, raise for contracts.**

## Status

This engine was extracted from a private parent repository, where it
tracks the breadboard build of a real vehicle's control-unit prototype.
No issue history, commit history, or the host's own board data came
with it — this is a clean start. The worked example at
`tests/fixtures/` (a two-board panel, `demo-left` + `demo-right`,
stitched across a seam) is the place to start reading: it is a complete,
DRC-clean design exercising placements, rails, jumpers, passives of
three kinds, an off-board lead, a cross-board interlink, and a panel.

A companion Claude Code skill for the authoring loop lives at
[`skills/bbnet/SKILL.md`](skills/bbnet/SKILL.md).

## Licence

AGPL-3.0-or-later. See [LICENSE](LICENSE).
