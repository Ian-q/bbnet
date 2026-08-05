---
name: bbnet
description: Use when authoring or editing a hand-built breadboard layout (solderable or solderless) tracked with bbnet — placing parts, wiring jumpers/leads, running DRC, or regenerating the build sheet. Triggers on "breadboard layout", "island YAML", "bbnet check/report/render/todo/bom", "DRC violation", "netlist for the breadboard", "kitting table", "LAYOUT.html", "REPORT.md drift".
---

# bbnet — breadboard layout authoring loop

bbnet turns a YAML record of what is physically on a breadboard into a
derived netlist, a design-rule check, an autorouted wire list, and a
printable build sheet. It does not design the circuit for you — it is
the oracle that tells you whether what you wrote down is consistent and
complete, and the tool that turns the record into bench instructions.

## The loop

If any footprint in `parts.yaml` declares `pin_signals: pinmap:<mcu>`,
every command below needs `--pinmap <path-to-csv>` appended — a CSV with
header row exactly `mcu,pin,signal`, registered as the `pinmap` signal
source that declaration refers to. If no footprint declares
`pin_signals:` at all, omit `--pinmap` entirely; it has nothing to feed.
Mixing the two up is a fast way to burn a cycle: forgetting `--pinmap`
when a footprint needs it raises `UnknownSignalSource` before any DRC
even runs (correct behaviour — see *Configuration semantics* below, not
a bug to route around).

1. **`bbnet todo --data-dir <dir> [--pinmap <csv>]`** — what still needs
   to be placed. Reads `rules.yaml`'s declarative per-pin requirements
   (decouple caps, pull-ups, etc.) and reports every one not yet
   satisfied on the bench, as a placement instruction. Empty output means
   every requirement is met.

2. **Edit the island YAML.** Each board is one YAML file (an "island"):
   its footprint placements, rail net assignments, jumpers, and off-board
   leads. Add/move a part, wire a jumper, assign a rail net — whatever
   the todo list or the circuit calls for.

3. **`bbnet check --data-dir <dir> [--pinmap <csv>]`** — the validating
   oracle. Runs the full DRC rule set (occupancy, shorts, floating pins,
   unmet requirements, colour convention, pinmap cross-check, passive
   placement/overlap, cap polarity, voltage rating — see the engine's
   own rule index in `src/bbnet/drc.py`) and reports errors/warnings.
   **A DRC violation is the feedback signal, not a failure to route
   around** — fix the YAML and re-run until `check` is clean.

   `check` also **guards `REPORT.md` and `LAYOUT.html` against drift**:
   it regenerates both from the current YAML and diffs them against the
   committed files. If they differ, the record (or the build sheet)
   silently disagrees with what the YAML now says — that fails `check`
   too, even with zero DRC violations. This is why step 4 is mandatory,
   not optional polish.

4. **`bbnet report` / `bbnet render --data-dir <dir> [--pinmap <csv>]`**
   — regenerate `REPORT.md` (netlist + violations + todo list, prose) and
   `LAYOUT.html` (autorouted board view + per-board kitting table, what
   you solder from at the bench).

5. **Commit the YAML and the regenerated `REPORT.md`/`LAYOUT.html`
   together, in the same commit.** Never commit an island edit without
   re-running `report`/`render` first — that is exactly the drift `check`
   exists to catch, and an out-of-sync build sheet sends you to the bench
   with wrong instructions.

`bbnet bom` rolls up parts and passives per island, including any unmet
requirements, for a shopping/kitting list — it takes `--pinmap` too, on
the same condition as the rest of the loop.

## Addressing syntax

- **Rows** are numbered top to bottom. Each row has two independent
  electrical nodes, split at the ravine: holes **a–e = left node**,
  **f–j = right node**.
- **Half-row node**: `43L` / `43R` — the canonical address.
- **Hole address**: `43c` — canonicalizes to `43L`; the letter survives
  only for hole-occupancy checking (DRC B1), so two parts can't claim
  the same physical hole even when they land on the same node.
- **Rail strip**: `rail:5V` (by declared net name, must be unambiguous)
  or `rail:top+` (by position); `rail:top+@5` pins a rail strip's
  drawing position to a row height (geometry only, no electrical
  meaning).
- **Pin sugar**: `U1.29` — part ref + pin name, resolved through that
  part's placement.
- **Cross-island**: `gps-imu:4L` — `<island>:<address>`, for a lead or
  jumper that lands on another board (e.g. an interlink cable between
  two islands sitting on the same panel).

## Configuration semantics — three deliberately different answers

A stranger's first config edit almost always trips one of these. State
the principle before editing `colours.yaml` or `rules.yaml`:

- **`colours.yaml`'s `rail_tints:` MERGES** over the built-in defaults.
  This is presentation only — a rail name with no tint just renders
  grey, so merging is safe and additive.
- **`rules.yaml`'s `net_voltages:` REPLACES wholesale.** This is a
  vocabulary, not a style sheet — merging a regex table has no
  well-defined precedence when two patterns could both match, and a
  silently-shadowed pattern would change a safety verdict (cap-polarity
  and voltage-rating DRC). Declaring one entry means you now own the
  *entire* table, including `GND`.
- **An unregistered signal source RAISES.** A host application that
  wants `pin_signals:` seeding must register the source by name before
  calling into the engine; from the CLI that's `--pinmap`. A missing
  registration is a broken configuration, not "no data." Returning an
  empty list instead would silently mute two DRC rules (signal-short,
  pinmap-xcheck) while the run still reports clean — worse than failing
  loudly.

**Merge for presentation, replace for vocabulary, raise for contracts.**

## Worked example

`tests/fixtures/` is a complete, DRC-clean two-board panel (`demo-left`
+ `demo-right`, stitched across a seam) — read it before writing a new
island from scratch. `tests/fixtures/REPORT.md` and `LAYOUT.html` show
what `report`/`render` actually produce from it. Its `mcu-demo` footprint
declares `pin_signals: pinmap:demo-mcu`, so every command against this
fixture needs `--pinmap tests/fixtures/pinmap.csv` — that file is
header-only (no allocations recorded yet for `demo-mcu`), which is why
`check` on the fixture reports a `pinmap-xcheck` warning rather than a
clean pass; that warning is expected, not a regression to chase.
