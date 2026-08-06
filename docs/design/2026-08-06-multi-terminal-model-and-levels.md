# Multi-terminal parts and build levels

**Status:** approved 2026-08-06 · **Supersedes:** nothing · **Implements:** slices 1–2 of the 2.5D roadmap

## The problem

A breadboard with real component count becomes a rat's nest. Wires cross,
the board fills up, and the only escape bbnet models today is the
underside (`side: bottom`) — which makes things *worse* to reason about,
because some wires now run top-to-bottom and some bottom-to-top and the
build sheet has to keep telling you which face you are looking at. DRC
B13 exists precisely because the bench got forced underside once
(`51i ⇒ 39c`, where R7's leg holds 39a and the click body covers the
rest) and the model had to be taught to say so.

The alternative is to build **upward** instead of underneath. Stacking
pins — male into the board, female receptacle on top, at standard heights
with plastic spacers, the way Raspberry Pi header risers work — lift a
node to a known level. Rigid **1×N link bars** then do the connecting up
there: exact lengths, orthogonal, no floppy wire. Everything stays on the
top face where you can see and reach it.

The model cannot express any of this today. Nor can it express a 2N7000,
which is already on the bench.

## Two asks, one refactor

These arrived as separate requests and turn out to be the same change:

- a **1×5 link bar** is 5 terminals that are *one conductor*
- a **2N7000** is 3 terminals that are *three different nets*
- a **pot** is 3 terminals where one is electrically distinct from the ends

`Passive` today is strictly two endpoints (`a`, `b`). All three want a
generalized placed multi-terminal object. Doing them together is cheaper
than doing either alone.

## Design

### Terminal groups

A placed object carries an ordered list of terminals. Each terminal has a
name, a hole, and a **terminal net index**. Terminals sharing an index
are one conductor.

| Object | Terminals | Indices |
|---|---|---|
| Resistor | 2 | `0, 1` |
| 2N7000 (TO-92) | 3 — G/D/S | `0, 1, 2` |
| Pot | 3 — A/W/B | `0, 1, 2` |
| **1×5 link bar** | 5 | `0, 0, 0, 0, 0` |

A link bar is not a special case in the engine — it is a terminal group
whose terminals all share index `0`. That is the whole trick.

`Passive` stays as the two-terminal convenience form, so no existing
island YAML changes. It becomes sugar over a terminal group.

### Levels

`level: <int>` on any placed object. `0` is the board surface.

A **riser** at a hole makes levels `1..N` reachable *at that hole only*.

The load-bearing property: **risers add no nets.** A riser is
electrically the same node as the hole it sits in — it is purely a
mechanical enabler. Only link bars merge nodes. This keeps derivation
simple and keeps the riser bin out of the netlist.

`side: bottom` becomes `level: -1` internally. Today's underside runs
keep working untouched, and "which face" stops being a separate idea from
"which level".

### Switched sets — the de-energized-state rule

A terminal group may declare a **switched set**: terminal indices whose
connection depends on state, plus `default: open | closed`.

| Part | Always-independent | Switched set | Default |
|---|---|---|---|
| SPST switch | — | `{1, 2}` | as declared (NO/NC) |
| Relay | coil `{A1, A2}` | `{COM, NC}` / `{COM, NO}` | closed / open |
| N-MOSFET | gate | channel `{D, S}` | open (enhancement) |

**The derived netlist is the de-energized state.** That is a deliberate
convention and it earns its keep: the de-energized state is exactly what
you measure with a multimeter on an unpowered board, so the netlist bbnet
derives stays continuity-testable at the bench. A netlist you cannot
check against the hardware is worth much less on a project like this.

Consequence worth stating plainly: the derived netlist now carries a
state assumption, which nothing else in bbnet does. It is confined to
switched sets and it is always the measurable state.

This also gives the DRC a genuinely new question to ask — a default-closed
switched set bridging two differently-named rails is a real hazard, and
now visible.

### Fabrication stays undecided

A link carries `fab: pcb-rail | bent-wire`.

Both candidate physical approaches — ordering 1×N rails from JLCPCB, or
3D-printing a bending gauge so solid-core wire bends to exact repeatable
geometry — produce the same modelled thing: *a rigid link with an exact
geometry at a level*. Everything upstream works either way, so the
physical call can be made later, or both can coexist in one bag.

Stock is cuttable: you buy 50× 1×5 and cut them down. So a link has a
stock length and a cut length, and kitting emits the cut list ("cut a
1×5 → 1×3").

## New DRC rules

Three new bug classes, and one correction to an existing rule.

**Unsupported link.** A bar needs at least two *supported* positions —
ones with a riser reaching its level — or it is floating on one pin and
will pivot. Intermediate positions are a different matter, see below.
*Error.*

**Middle-pin short.** The rule that earns the whole system, and the one
whose physics has to be got right.

A 1×5 is one conductor and all five pins are the same length. If a middle
position has **no riser**, that pin simply hangs in the air at level N
touching nothing — harmless, and the common case. If a middle position
**does** have a riser, that pin is now bonded to whatever net the riser's
hole belongs to. Join positions 1 and 5 across a riser you put down for
some unrelated net at position 3, and you have silently shorted that net
into yours.

So the rule is not "middle pins are dangerous", it is: an *unclipped*
middle pin sitting over a riser whose hole carries a **different net** is
an *error*. Pins actually snipped off are declared `clipped: [2, 3]` and
are exempt. Getting this distinction wrong in either direction makes the
rule useless — flag every middle pin and it is noise, flag none and it
misses the only way this system can bite you.

**Cut/stock sanity.** Bar length must not exceed the stock length being
bought. *Warning* — it is a purchasing fact, not a wiring fault.

**B9 passive-overlap becomes level-aware.** Today it flags two same-face
bodies crossing. Once levels exist, crossing at *different* levels is
legal and is the entire point of the system. Levels do not weaken B9 —
they make it correct.

## Non-goals for this slice

- Automatic placement of bars (router proposing or allocating them)
- The agent-review and human-draw-on-the-sheet workflow
- The full visual rework of `island_body()`
- The bending-gauge STL generator

All of these are downstream slices and all of them need this foundation
to have something to work against. Render work here is the minimum that
makes levels *testable and visible*, not the legibility pass.

Level shifters and other multi-pin modules are **already** expressible
via the existing footprint path (`kind: dip | sil`) and are not part of
this change. Terminal groups are for discrete and inline parts, not
modules.

## Delivery

Five PRs, each independently green:

| PR | Content |
|---|---|
| **A** | Terminal groups in `model.py`; `Passive` becomes sugar; named-pin discretes (TO-92/TO-220); inductor/ferrite/fuse kinds; pots; derivation through terminal indices |
| **B** | `level:` on placed objects; risers; `side: bottom` → `level: -1`; per-level occupancy in `router.py` |
| **C** | Link bars: shared-index groups, span, `clipped:`, kitting cut list; DRC unsupported-link, middle-pin-short, cut/stock |
| **D** | Switched sets: switches, relays, FET channels, NO/NC default; DRC default-closed rail bridge; B9 made level-aware |
| **E** | Minimal 2.5D render: per-level z-order and offset so stacking reads; level shown in kitting |

Each PR keeps the fixture corpus DRC-clean and the exact-warning-set test
honest. ET-embed's pin is bumped once at the end, not per PR, so its
bench data moves in a single reviewable step.
