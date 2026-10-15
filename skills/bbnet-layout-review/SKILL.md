---
name: bbnet-layout-review
description: Use to tidy a bbnet breadboard layout that is electrically right but awkward to build - wires that zig-zag, cross a carrier gap, fly over a whole board, or detour inside a node; a chip that would read better turned 180 degrees; a part on the wrong half. Proposes re-layouts that provably keep the circuit (`bbnet score` fingerprint) and lower the score, as a reviewed diff - never a silent rewrite. Triggers on "tidy the layout", "clean up the build sheet", "layout review", "zig-zag wires", "rotate the IC", "swap sides".
---

# bbnet layout review — tidy without changing the circuit

A first layout is written to get the circuit right. This review makes it
easy to BUILD: fewer, shorter, straighter wires, nothing threaded through
a carrier gap, parts turned the way their wiring wants. It is a separate
pass, ideally a separate agent, because the author of a layout is the
worst judge of how it reads.

The rule that makes it safe: **a tidy-up keeps the circuit.** `bbnet
score` prints a fingerprint of the circuit with every position stripped
out (which pins, leads and seeded nets share each net; a non-polar
passive's two ends are interchangeable). Every proposal must leave it
byte-identical. The same command prints the metrics, and a proposal must
make them better.

## Inputs

- The data dir (island YAML + `layout.yaml`), plus `--pinmap` if the
  project's footprints declare `pin_signals:` (see the `bbnet` skill).
- Optionally a focus: one island, one rail, or "the stage-2 boards".

## Procedure

1. **Work on a copy.** `cp -R <data-dir> <scratch>/review` and edit only
   the copy. The real data dir is not touched until the operator
   approves.
2. **Baseline.** `bbnet score --data-dir <copy> --json > before.json`.
   Note the fingerprint and `rank`. If `errors` is not 0, stop: fix the
   design first, a review is not the place.
3. **Look at it.** `bbnet render --data-dir <copy>` and look at
   `LAYOUT.html` (headless Chrome screenshot, then Read the PNG): the
   assembly view first, then each upright sheet. The metrics find most
   problems; the picture finds the rest (labels colliding, a wire
   parallel to a part leg for its whole length, a bundle that crosses
   itself).
4. **Skip frozen islands.** An island with `frozen: <reason>` (shown
   in the score table) is built, or deliberately fixed. Do not move
   anything on it; you may still change the OTHER end of a wire that
   lands on it.
5. **Propose one move at a time** from the vocabulary below. After each:
   `bbnet score --json` on the copy. **Keep the move only if** the
   fingerprint is unchanged, `errors` is still 0, `warnings` did not
   rise, and `rank` is lexicographically lower (or equal, with a reason
   a builder would accept, e.g. "every output now reads top to bottom
   in bit order"). Otherwise undo it. Never batch untested moves.
6. **Stop** when no move in the vocabulary improves anything, or after
   the moves that matter most - a short list of real wins beats twenty
   one-cell nudges.
7. **Report** (below). Do not commit, do not copy files back.

## Move vocabulary

Cheapest and safest first.

- **Re-hole a wire end within its node.** The five holes of a half-row
  are one node: moving an end to another free hole in the same half-row
  never changes the circuit. Use it to point an off-board wire's end at
  the channel edge (column a for `channel: below`, j for `above`), or to
  stop a wire walking along its own row (`in_node_runs`).
- **Turn a two-terminal part end for end** (swap `from:`/`to:`), or
  move it to free holes of the same two nodes. Non-polar only - the
  fingerprint refuses a flipped diode or electrolytic, as it should.
- **Move a part's net to another free row**: relocate the part AND every
  wire end and passive end on its rows by the same offset. Mechanical
  but error-prone; the fingerprint is the check.
- **Turn a chip or module 180 degrees.** For a `dip`, pin 1 moves to the
  diagonally opposite corner; for a `sil`, to the other end of the
  strip; for a 3-leg device (`pins: {E:, B:, C:}`), swap the outer legs'
  rows (a TO-92 then faces the other way - say so in its `value:`, e.g.
  "flat face away"). Then remap every endpoint on the old pin rows to
  the new ones. Worth it when most of the part's wiring leaves from the
  far side.
- **Move a part to the other half** of the board (across the ravine), or
  a whole sub-circuit along the rows, when its wiring all leaves one way.
- **Layout-level (`layout.yaml`), propose only - never apply silently:**
  a rail's `channel:` side (pick by the per-rail sum of `fly_cols` both
  ways), or the order of boards on a rail (shortens channel runs, but it
  is a physical rearrangement of the box). These change how the build is
  mounted, so they go to the operator as a recommendation with the
  numbers.

Out of bounds: anything that changes a net (adding, removing or
re-targeting a component or wire), a part's `value:` other than an
orientation note, any `frozen:` island, and rules/colour config.

## The metrics, in rank order

`errors` (must stay 0) · `route_failures` · `gap_crossings` (a link whose
halves leave a rail by opposite edges, threading the gap between
carriers) · `underside` · `warnings` · `in_node_runs` · `fly_cols` (how
far off-board wires fly across their own board to an edge) · `wires` ·
`wire_mm` (cut length). Earlier beats later: one gap crossing removed is
worth more than any amount of cut length.

## Report

1. Before/after `totals` side by side, and the fingerprint (identical,
   stated explicitly).
2. Each kept move: island, what moved (old -> new holes), why, and the
   metric it improved.
3. Layout-level recommendations with their numbers, clearly separate.
4. The patch: `diff -ru <data-dir> <copy>` restricted to the YAML, saved
   to a file whose absolute path you give.
5. What you looked at but left alone, and why (e.g. "teensy-pwr is
   frozen"; "rotating U_GRP saves 2 cells but puts SER under the Click
   overhang").

The operator applies an approved patch, then runs `bbnet report`,
`render` and `check` on the real data dir and commits the YAML with the
regenerated `REPORT.md`/`LAYOUT.html`, as the `bbnet` skill requires.
