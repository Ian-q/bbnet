#!/usr/bin/env python3
"""model.py — bbnet data model: parts library, island records, derivation.

Loading: parts_lib_from() (footprints), island_from() (one YAML dict ->
Island with parts landed on half-row nodes). Derivation: derive() in the
second half of this file — union-find over tie-point nodes; jumpers and
interlinks merge nodes, passives become edges between derived nets.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from bbnet.geometry import (
    AddrError, BOARDS, Board, HoleAddr, RailAddr, col_at_span,
    LEFT_HOLES, RIGHT_HOLES, parse_local,
)


class ModelError(ValueError):
    pass


# Island-file schema version. Bump only on a BREAKING change to the YAML
# contract; a file may declare `schema_version:` to pin what it was
# written against. Absence means 1 -- every file predating the key stays
# valid, which is why the key is optional rather than required.
SCHEMA_VERSION = 1


class UnsupportedSchemaVersion(ModelError):
    """An island file declares a schema this build cannot read."""


CAP_KINDS = {"ceramic", "electrolytic", "tantalum", "film"}
# Two-terminal inline parts, written as `passives:` with from/to. `other`
# is the escape hatch for anything with two legs and no rule of its own.
PASSIVE_KINDS = ({"resistor", "diode", "led", "inductor", "ferrite",
                  "fuse", "other"} | CAP_KINDS)

# Parts with more than two terminals, written as `devices:` with a named
# `pins:` map. The pinout fixes the ORDER terminals are stored in and the
# names the YAML must use — a device naming a pin outside its kind's
# pinout is an error, because a mistyped leg on a three-legged part is a
# wiring bug the netlist would otherwise absorb silently.
DEVICE_PINOUTS = {
    "mosfet":    ("G", "D", "S"),
    "bjt":       ("B", "C", "E"),
    "regulator": ("IN", "GND", "OUT"),
    "pot":       ("A", "W", "B"),
    "switch":    ("A", "B"),
    "relay":     ("A1", "A2", "COM", "NO", "NC"),
}
DEVICE_KINDS = frozenset(DEVICE_PINOUTS)

# Terminals whose connection depends on state, and the state they are in
# when nothing is driving them: kind -> ((pins, default), ...).
#
# The derived netlist is the DE-ENERGIZED state. That is a deliberate
# convention and it earns its keep: de-energized is exactly what a
# multimeter reads on an unpowered board, so the netlist stays
# continuity-testable against the hardware. A netlist you cannot check
# against the thing on the bench is worth much less.
#
# A relay's coil is not in here — it is always its own pair, energized or
# not. A MOSFET/BJT channel is: enhancement-mode parts are off with no
# gate drive, which is why their D-S and C-E default open.
SWITCHED_SETS = {
    "switch": ((("A", "B"), "open"),),
    "relay":  ((("COM", "NC"), "closed"), (("COM", "NO"), "open")),
    "mosfet": ((("D", "S"), "open"),),
    "bjt":    ((("C", "E"), "open"),),
}
SWITCH_STATES = ("open", "closed")

# The board surface is level 0 and the underside is level -1, so the
# older `side: top|bottom` is just a coarser spelling of `level`. Keeping
# both spellings working matters: every island YAML written so far says
# `side`, and underside mounting is still the right answer sometimes.
SIDE_LEVELS = {"top": 0, "bottom": -1}


def device_indices(kind, pinout, normally, where):
    """pin -> net_index for one device, with switched sets resolved.

    Every pin starts on its own net. A switched set that is CLOSED in
    the de-energized state merges its pins onto one, which is how a
    normally-closed switch ends up being a piece of wire as far as the
    netlist is concerned — because on the unpowered bench, it is."""
    idx = {p: i for i, p in enumerate(pinout)}
    sets = SWITCHED_SETS.get(kind, ())
    if normally is not None:
        if kind != "switch":
            raise ModelError(
                f"{where}: `normally:` only applies to a switch — a "
                f"{kind}'s de-energized state is a property of the part, "
                "not a choice")
        if normally not in SWITCH_STATES:
            raise ModelError(f"{where}: normally {normally!r} not in "
                             f"{list(SWITCH_STATES)}")
    for pins, default in sets:
        state = normally if (normally and kind == "switch") else default
        if state != "closed":
            continue
        keep = min(idx[p] for p in pins)
        merged = {idx[p] for p in pins}
        for p, i in list(idx.items()):
            if i in merged:
                idx[p] = keep
    return idx


def link_positions(addrs, where):
    """Every slot a bar spanning `addrs` physically covers, in order.

    A bar is rigid and straight, so its slots are contiguous along one
    axis: either across a row (`20a`..`20e`, within one half — the
    ravine is a gap no bar bridges without leaving the grid) or down a
    hole column (`20a`..`24a`). Anything else is not a shape a 1xN part
    can take, and saying so here beats discovering it at the bench."""
    if len(addrs) < 2:
        raise ModelError(f"{where}: a link spans at least two positions")
    for a in addrs:
        if not isinstance(a, HoleAddr) or not a.hole:
            raise ModelError(
                f"{where}: link position {a} must name a hole "
                "(`20a`), not a half-row or a rail")
    if len({a.island for a in addrs}) != 1:
        raise ModelError(f"{where}: a link cannot span two islands")

    rows = {a.row for a in addrs}
    cols = {(a.half, a.hole) for a in addrs}
    if len(rows) == 1:
        half = {a.half for a in addrs}
        if len(half) != 1:
            raise ModelError(
                f"{where}: link spans the ravine — a rigid bar cannot "
                "cross it, use two bars and a jumper")
        holes = LEFT_HOLES if addrs[0].half == "L" else RIGHT_HOLES
        idx = sorted(holes.index(a.hole) for a in addrs)
        row, half = addrs[0].row, addrs[0].half
        return [HoleAddr(addrs[0].island, row, half, holes[i])
                for i in range(idx[0], idx[-1] + 1)]
    if len(cols) == 1:
        half, hole = cols.pop()
        lo, hi = min(rows), max(rows)
        return [HoleAddr(addrs[0].island, r, half, hole)
                for r in range(lo, hi + 1)]
    raise ModelError(
        f"{where}: link positions are not in one line — a bar runs "
        "along a row or down a hole column, not diagonally")


def level_of(spec, where):
    """Resolve `side:` and `level:` on one placed part to a single level.

    Either spelling alone is fine. Given both, they must agree — a part
    claiming `side: bottom` at `level: 2` is not a thing that can be
    built, and silently preferring one would bury the contradiction in
    whichever field the reader did not look at."""
    side = spec.get("side", "top")
    if side not in SIDE_LEVELS:
        raise ModelError(f"{where}: side {side!r} not 'top' or 'bottom'")
    if "level" not in spec or spec["level"] is None:
        return side, SIDE_LEVELS[side]
    level = spec["level"]
    if not isinstance(level, int) or isinstance(level, bool):
        raise ModelError(f"{where}: level {level!r} must be an integer")
    if "side" in spec and SIDE_LEVELS[side] != level:
        raise ModelError(
            f"{where}: side {side!r} and level {level} disagree — "
            f"side {side!r} means level {SIDE_LEVELS[side]}; drop one")
    return ("bottom" if level < 0 else "top"), level


# ---------------------------------------------------------------- parts lib

@dataclass
class Footprint:
    kind: str                       # "dip" | "sil"
    pin_names: list[str]            # ordered walk (dip: down L, back up R)
    seeds: dict[str, str] = field(default_factory=dict)
    pin_signals: str | None = None  # "<source>:<mcu>" -> seed the net
                                    # name from the external pin table
    span: int | None = None         # dip pin-row spacing in 0.1" units
                                    # (ravine counts 3); None = assume the
                                    # symmetric mirror around the ravine
    overhang: tuple | None = None   # (rows_above_pin1, rows_below_last):
                                    # physical body extent beyond the pin
                                    # rows (clicks tower over their
                                    # sockets). Soft keep-out only — the
                                    # body hovers, so wires beneath are
                                    # possible but discouraged


def parts_lib_from(d):
    lib = {}
    for pid, spec in (d or {}).items():
        kind = spec.get("kind")
        if kind not in ("dip", "sil"):
            raise ModelError(f"parts.{pid}: kind must be dip|sil, got {kind!r}")
        names = [str(n) for n in (spec.get("pins") or [])]
        if not names or len(names) != len(set(names)):
            raise ModelError(f"parts.{pid}: pins must be a non-empty list "
                             "of unique names")
        if kind == "dip" and len(names) % 2:
            raise ModelError(f"parts.{pid}: dip needs an even pin count")
        span = spec.get("span")
        if span is not None:
            if kind != "dip" or not isinstance(span, int) or span < 4:
                raise ModelError(f"parts.{pid}: span must be an int >= 4 "
                                 "on a dip footprint (0.1\" units, ravine "
                                 "counts 3)")
        over = spec.get("overhang")
        if over is not None:
            if (not isinstance(over, list) or len(over) != 2
                    or not all(isinstance(v, int) and v >= 0 for v in over)):
                raise ModelError(f"parts.{pid}: overhang must be "
                                 "[rows_above, rows_below], ints >= 0")
            over = tuple(over)
        lib[pid] = Footprint(
            kind, names,
            {str(k): str(v) for k, v in (spec.get("seeds") or {}).items()},
            spec.get("pin_signals"), span, over)
    return lib


DIP_MIRROR = dict(zip(LEFT_HOLES, reversed(RIGHT_HOLES)))


def dip_right_col(part):
    """Hole column of a placed dip part's right pin row: span-aware when
    the footprint declares one, symmetric mirror otherwise."""
    if part.anchor is None or part.anchor.hole is None:
        return None
    if part.fp is not None and part.fp.span:
        return col_at_span(part.anchor.hole, part.fp.span)
    return DIP_MIRROR.get(part.anchor.hole)


def land(fp, pin1):
    """Pin name -> half-row landing, walking the footprint from pin 1."""
    pins = {}
    if fp.kind == "dip":
        if pin1.half != "L":
            raise ModelError(f"dip pin1 must land on an L node, got {pin1}")
        n = len(fp.pin_names) // 2
        for i, name in enumerate(fp.pin_names):
            if i < n:
                pins[name] = HoleAddr(pin1.island, pin1.row + i, "L", None)
            else:
                pins[name] = HoleAddr(
                    pin1.island, pin1.row + (2 * n - 1 - i), "R", None)
    else:  # sil
        for i, name in enumerate(fp.pin_names):
            pins[name] = HoleAddr(pin1.island, pin1.row + i, pin1.half, None)
    return pins


# ------------------------------------------------------------- island model

@dataclass(frozen=True)
class PinRef:
    ref: str
    pin: str

    def __str__(self):
        return f"{self.ref}.{self.pin}"


@dataclass(frozen=True)
class XIsland:
    island: str
    text: str

    def __str__(self):
        return f"{self.island}:{self.text}"


@dataclass
class PlacedPart:
    ref: str
    part_id: str | None
    value: str
    pins: dict[str, HoleAddr]
    fp: Footprint | None
    seeds: dict[str, str]
    internal_ties: list[tuple[str, str]]
    anchor: HoleAddr | None = None   # pin1 as written (keeps its hole
                                     # letter; landed pins are node-level)


@dataclass
class Passive:
    ref: str
    kind: str
    value: str
    a: object
    b: object
    island: str
    side: str = "top"    # "top" | "bottom" — underside mounting lets
                         # two passives share crossing hole pairs
    rating: str = ""     # optional voltage rating ("25V") — B11 checks
                         # it against the net's working voltage
    level: int = 0       # build level; 0 is the board surface and -1 is
                         # the underside, so `side` is just a spelling of
                         # this (see level_of)
    # polarity convention: for electrolytics, `a` (YAML `from:`) is the
    # + terminal — rendered as +/- on the build sheet, enforced by B10


@dataclass
class Riser:
    """A stacking pin soldered into a hole, presenting a socket at
    `level` above the board — the male-into-the-board, female-on-top
    part that lets the build go upward instead of underneath.

    A riser adds NO net. It is electrically the same node as the hole it
    sits in, because `HoleAddr.node_key()` is (row, half) and knows
    nothing about height. That invariant is what keeps levels cheap:
    only link bars merge nodes, and the riser bin never appears in the
    netlist at all. What a riser does is make a level *reachable* at one
    specific hole, which is a mechanical fact the DRC needs and the
    derivation does not."""
    at: object
    level: int
    island: str
    kind: str = "stacking-header"
    note: str = ""


LINK_FABS = ("pcb-rail", "bent-wire")


@dataclass
class Link:
    """A rigid 1xN bar plugged into risers at one level — the thing that
    replaces a fistful of jumpers with one exact-length part.

    All N positions are ONE conductor, which is what makes a bar useful
    (a single 3V3 tap fans out to five things) and also what makes it
    dangerous: the pins you did not want still physically exist. Each
    position is therefore one of three things, and the YAML has to say
    which, because only intent distinguishes a deliberate fan-out from
    an accidental short:

      connect  bonded on purpose; needs a riser reaching `level`
      clip     pin snipped off; touches nothing, needs nothing
      float    pin present, no riser beneath — hangs in free air

    `positions` holds every slot the bar physically covers, in order,
    including the floats the author never mentioned."""
    ref: str
    level: int
    positions: list        # [HoleAddr] every slot, in order
    connects: list         # [HoleAddr] bonded on purpose
    clipped: list          # [HoleAddr] pins removed
    island: str
    stock: int = 0         # positions per bar as bought (0 = unstated)
    fab: str = "pcb-rail"
    note: str = ""

    @property
    def length(self):
        return len(self.positions)

    def floats(self):
        named = {(a.row, a.half, a.hole)
                 for a in self.connects + self.clipped}
        return [a for a in self.positions
                if (a.row, a.half, a.hole) not in named]

    def terminals(self):
        """Only the bonded positions carry the net. A clipped pin is not
        there and a floating one touches nothing, so neither belongs in
        the netlist — but both stay in `positions` because the DRC and
        the kitting sheet still have to account for them."""
        return [Terminal(f"p{i + 1}", a, 0)
                for i, a in enumerate(self.connects)]


@dataclass
class Terminal:
    """One leg of a placed part.

    `net_index` is what makes this general: terminals sharing an index
    are ONE conductor inside the part. A resistor's two legs get 0 and 1
    (two nets); a 1xN link bar's N pins all get 0 (one net). Everything
    between those is expressible without the derivation caring which
    kind of part it is looking at."""
    name: str
    addr: object
    net_index: int


@dataclass
class Device:
    """A placed part with more than two terminals.

    Two-terminal parts keep their own `Passive` form (`from`/`to` reads
    better than a pins map for a resistor, and every geometry rule that
    exists today is inherently two-ended). Both feed ONE derivation path
    via `terminal_groups()`, so a device's legs join nets by exactly the
    same machinery as a passive's."""
    ref: str
    kind: str
    value: str
    terminals: list
    island: str
    side: str = "top"
    rating: str = ""
    level: int = 0
    normally: str = ""   # switches only: the state the netlist assumes

    def addr_of(self, pin):
        for t in self.terminals:
            if t.name == pin:
                return t.addr
        return None


@dataclass
class Jumper:
    a: object
    b: object
    colour: str
    note: str
    island: str
    pair: str | None = None   # twisted-pair label — paired wires co-run
    offgrid: bool = False     # board's built-in end-jumper pads (solder
                              # blob outside the grid): merges nets but
                              # consumes no routing space
    underside: bool = False   # wire body runs beneath the board (free
                              # air, not hole-tunneling) — an "airwire":
                              # the router draws it dashed point-to-
                              # point and it consumes no channel space


@dataclass
class Lead:
    at: object
    colour: str
    net: str | None
    label: str
    island: str
    pair: str | None = None   # twisted-pair label — paired wires co-run


@dataclass
class Island:
    name: str
    board: Board
    rails: dict[str, str]
    rails_bridged: bool | None
    parts: list[PlacedPart]
    passives: list[Passive]
    jumpers: list[Jumper]
    leads: list[Lead]
    interlinks: list[Jumper]
    bench_only: list[str]
    schema_version: int = SCHEMA_VERSION
    devices: list[Device] = field(default_factory=list)
    risers: list[Riser] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)

    def sockets(self):
        """(row, half, hole) -> set of levels reachable there.

        The mechanical answer to "can something land here at level N",
        which link bars (PR C) need and derivation never does."""
        out = {}
        for r in self.risers:
            out.setdefault((r.at.row, r.at.half, r.at.hole),
                           set()).add(r.level)
        return out

    def terminal_groups(self):
        """Every inline part on this island as (part, [Terminal]) — the
        ONE view derivation and DRC walk, so a three-legged device joins
        nets by the same code that joins a resistor's two.

        Passives are yielded as their two-terminal equivalent rather
        than kept on a separate path; `a`/`b` become terminals 0 and 1,
        which is exactly what they already mean. Both element types
        carry ref/kind/value/rating, so callers need no isinstance."""
        for q in self.passives:
            yield q, [Terminal("a", q.a, 0), Terminal("b", q.b, 1)]
        for dv in self.devices:
            yield dv, list(dv.terminals)
        # A link bar is not a special case in here — it is a group whose
        # terminals all share net_index 0, which is exactly what "one
        # conductor across N positions" means. Derivation needs no
        # branch for it.
        for lk in self.links:
            yield lk, lk.terminals()


# ---------------------------------------------------------------- panels

DEFAULT_SEAM_PX = 260   # drawn gutter between two butted boards. The
                        # boards touch on the bench; the gutter is label
                        # room (each board's edge leads print into it),
                        # so it is a render knob, not a physical fact.


@dataclass
class Panel:
    """A group of islands that sit physically adjacent on the bench,
    ordered left-to-right as placed. A panel renders as ONE board view:
    interlinks with both ends inside it are drawn whole across the seam
    instead of dying on each island's ghost bus."""
    name: str
    islands: list[str]
    seam: int = DEFAULT_SEAM_PX
    note: str = ""

    def side_of(self, mine, other):
        """Which edge of island `mine` its panel-mate `other` sits past."""
        return "R" if self.islands.index(other) > self.islands.index(mine) \
            else "L"


def panels_from(d, island_names):
    """layout.yaml -> [Panel]. Islands named by no panel render alone."""
    panels, home = [], {}
    for i, p in enumerate(d.get("panels") or []):
        if not isinstance(p, dict):
            raise ModelError(f"layout.yaml panel #{i + 1}: must be a "
                             "mapping with name:/islands:")
        name = str(p.get("name") or "").strip()
        if not name:
            raise ModelError(f"layout.yaml panel #{i + 1}: missing 'name:'")
        if any(q.name == name for q in panels):
            raise ModelError(f"layout.yaml: duplicate panel name {name!r}")
        members = [str(x) for x in (p.get("islands") or [])]
        if len(members) < 2:
            raise ModelError(
                f"layout.yaml panel {name!r}: needs at least two islands "
                "— a lone board is already rendered as its own island")
        for m in members:
            if m not in island_names:
                raise ModelError(f"layout.yaml panel {name!r}: unknown "
                                 f"island {m!r} (have "
                                 f"{sorted(island_names)})")
            if m in home:
                raise ModelError(
                    f"layout.yaml: island {m!r} is in two panels "
                    f"({home[m]!r} and {name!r}) — a board sits in one "
                    "place")
            home[m] = name
        if len(set(members)) != len(members):
            raise ModelError(f"layout.yaml panel {name!r}: island listed "
                             "twice")
        seam = p.get("seam", DEFAULT_SEAM_PX)
        if isinstance(seam, bool) or not isinstance(seam, int) or seam < 0:
            raise ModelError(f"layout.yaml panel {name!r}: seam must be a "
                             f"non-negative integer (px), got {seam!r}")
        panels.append(Panel(name, members, seam, str(p.get("note", ""))))
    return panels


_PINREF_RE = re.compile(r"([A-Za-z_]\w*)\.([\w+\-]+)")
_LOCAL_RE = re.compile(r"\d+[a-jLR]")


def parse_endpoint(text, island_name, board, rails):
    """Endpoint of a jumper/passive/lead: address, pin-ref, or cross-island."""
    t = str(text).strip()
    if ":" in t and not t.lower().startswith("rail:"):
        other, rest = t.split(":", 1)
        return XIsland(other.strip(), rest.strip())
    m = _PINREF_RE.fullmatch(t)
    if m and not _LOCAL_RE.fullmatch(t):
        return PinRef(m.group(1), m.group(2))
    return parse_local(t, island_name, board, rails)


def island_from(d, parts_lib):
    name = d.get("island")
    if not name:
        raise ModelError("island file missing 'island:' name")
    sv = d.get("schema_version", SCHEMA_VERSION)
    # Strict on purpose: this field exists so a file's declared schema can
    # be TRUSTED, so int(sv) (which silently truncates 1.5 -> 1 and
    # accepts True/False, since bool is an int subclass in Python) is not
    # good enough. Only a genuine, un-quoted integer is accepted -- no
    # str fallback, since "1.0" vs 1.0 vs 1 should not depend on whether
    # the author happened to quote it.
    if isinstance(sv, bool) or not isinstance(sv, int):
        raise ModelError(
            f"{name}: schema_version must be a plain integer (e.g. "
            f"`schema_version: 1`), got {sv!r}")
    if sv < 1:
        raise ModelError(
            f"{name}: schema_version must be >= 1 (e.g. "
            f"`schema_version: 1`), got {sv!r}")
    if sv > SCHEMA_VERSION:
        raise UnsupportedSchemaVersion(
            f"{name}: file declares schema_version {sv}, but this bbnet "
            f"reads up to {SCHEMA_VERSION} — upgrade bbnet, or pin the "
            "revision this file was written against")
    board = BOARDS.get(d.get("board"))
    if board is None:
        raise ModelError(f"{name}: unknown board {d.get('board')!r} "
                         f"(have {sorted(BOARDS)})")
    rails = {str(k): str(v) for k, v in (d.get("rails") or {}).items()}
    for pos in rails:
        if pos not in board.rails:
            raise ModelError(f"{name}: board {board.name} has no rail "
                             f"strip {pos!r} (have {board.rails})")

    def ep(text):
        try:
            return parse_endpoint(text, name, board, rails)
        except AddrError as e:
            raise ModelError(str(e)) from e

    parts, refs = [], set()
    for p in d.get("parts") or []:
        ref = p.get("ref")
        if not ref or ref in refs:
            raise ModelError(f"{name}: part ref {ref!r} missing or duplicate")
        refs.add(ref)
        fp = None
        anchor = None
        if "part" in p:
            fp = parts_lib.get(p["part"])
            if fp is None:
                raise ModelError(f"{name}.{ref}: unknown part {p['part']!r}")
            pin1 = ep(p.get("pin1", ""))
            if not isinstance(pin1, HoleAddr):
                raise ModelError(f"{name}.{ref}: pin1 must be a hole address")
            pins = land(fp, pin1)
            anchor = pin1
            if (fp.kind == "dip" and fp.span and pin1.hole
                    and col_at_span(pin1.hole, fp.span) is None):
                raise ModelError(
                    f"{name}.{ref}: pin1 {pin1.hole!r} + span {fp.span} "
                    "lands the right pin row in the ravine or off-grid")
        else:
            pins = {}
            for pn, addr in (p.get("pins") or {}).items():
                a = ep(addr)
                if not isinstance(a, HoleAddr):
                    raise ModelError(f"{name}.{ref}.{pn}: pins must be "
                                     "hole addresses")
                pins[str(pn)] = a
            if not pins:
                raise ModelError(f"{name}.{ref}: needs part:+pin1: or pins:")
        for pn, a in pins.items():
            if a.row > board.rows:
                raise ModelError(f"{name}.{ref}.{pn}: lands on row {a.row}, "
                                 f"off the {board.name} ({board.rows} rows)")
        halves = {a.half for a in pins.values()}
        if halves == {"L", "R"} and board.ravine_keepouts:
            rows_used = {a.row for a in pins.values()}
            span_rows = set(range(min(rows_used), max(rows_used) + 1))
            hit = sorted(span_rows & board.ravine_keepouts)
            if hit:
                raise ModelError(
                    f"{name}.{ref}: body straddles the ravine over "
                    f"mounting-hole row(s) {hit} — move the part clear "
                    "of the screw keep-outs")
        ties = []
        for t in (p.get("internal_ties") or []):
            if len(t) != 2:
                raise ModelError(f"{name}.{ref}: internal tie {t!r} must be "
                                 "a [pin, pin] pair")
            ties.append(tuple(map(str, t)))
        for ta, tb in ties:
            if ta not in pins or tb not in pins:
                raise ModelError(f"{name}.{ref}: internal tie {ta}-{tb} "
                                 "names an unplaced pin")
        seeds = dict((fp.seeds if fp else {}),
                     **{str(k): str(v)
                        for k, v in (p.get("seeds") or {}).items()})
        parts.append(PlacedPart(ref, p.get("part"), str(p.get("value", "")),
                                pins, fp, seeds, ties, anchor))

    passives = []
    for q in d.get("passives") or []:
        kind = q.get("kind")
        if kind not in PASSIVE_KINDS:
            raise ModelError(f"{name}.{q.get('ref')}: kind {kind!r} not in "
                             f"{sorted(PASSIVE_KINDS)}")
        side, level = level_of(q, f"{name}.{q.get('ref')}")
        passives.append(Passive(q.get("ref", "?"), kind,
                                str(q.get("value", "")),
                                ep(q["from"]), ep(q["to"]), name, side,
                                str(q.get("rating", "")), level))

    devices = []
    for dv in d.get("devices") or []:
        ref = dv.get("ref", "?")
        kind = dv.get("kind")
        if kind not in DEVICE_KINDS:
            raise ModelError(
                f"{name}.{ref}: device kind {kind!r} not in "
                f"{sorted(DEVICE_KINDS)} — two-terminal parts go under "
                "`passives:` with from/to, not here")
        side, level = level_of(dv, f"{name}.{ref}")
        pinout = DEVICE_PINOUTS[kind]
        placed = {str(k): v for k, v in (dv.get("pins") or {}).items()}
        # Both directions are errors, and for the same reason: on a part
        # whose legs are not interchangeable, a name the pinout does not
        # know (or a leg left unplaced) is a wiring mistake the netlist
        # would otherwise absorb without complaint.
        unknown = sorted(set(placed) - set(pinout))
        if unknown:
            raise ModelError(f"{name}.{ref}: {kind} has no pin(s) "
                             f"{unknown} — expected {list(pinout)}")
        missing = [p for p in pinout if p not in placed]
        if missing:
            raise ModelError(f"{name}.{ref}: {kind} leaves pin(s) "
                             f"{missing} unplaced — expected {list(pinout)}")
        if ref in refs:
            raise ModelError(f"{name}: device ref {ref!r} duplicates a "
                             "part or device ref on this island")
        refs.add(ref)
        normally = dv.get("normally")
        idx = device_indices(kind, pinout,
                             (str(normally) if normally else None),
                             f"{name}.{ref}")
        terminals = [Terminal(p, ep(placed[p]), idx[p]) for p in pinout]
        devices.append(Device(ref, kind, str(dv.get("value", "")),
                              terminals, name, side,
                              str(dv.get("rating", "")), level,
                              (str(normally) if normally else "")))

    risers = []
    for rs in d.get("risers") or []:
        where = f"{name}.riser@{rs.get('at')}"
        level = rs.get("level")
        if not isinstance(level, int) or isinstance(level, bool):
            raise ModelError(f"{where}: level {level!r} must be an integer")
        if level <= 0:
            raise ModelError(
                f"{where}: level {level} — a riser exists to reach ABOVE "
                "the board; level 0 is the surface itself and negative "
                "levels are the underside, which needs no riser")
        at = ep(rs["at"])
        if not isinstance(at, HoleAddr) or not at.hole:
            raise ModelError(
                f"{where}: a riser is soldered into ONE hole, so its "
                "address must name the hole (`20a`, not `20L` or a rail)")
        risers.append(Riser(at, level, name,
                            str(rs.get("kind", "stacking-header")),
                            str(rs.get("note", ""))))

    links = []
    for lk in d.get("links") or []:
        ref = lk.get("ref", "?")
        where = f"{name}.{ref}"
        level = lk.get("level")
        if not isinstance(level, int) or isinstance(level, bool):
            raise ModelError(f"{where}: level {level!r} must be an integer")
        if level == 0:
            raise ModelError(
                f"{where}: level 0 is the board surface — a bar sitting "
                "flat on the board is a jumper, write it under `jumpers:`")
        connects = [ep(x) for x in (lk.get("connects") or [])]
        clipped = [ep(x) for x in (lk.get("clipped") or [])]
        if len(connects) < 2:
            raise ModelError(
                f"{where}: a link needs at least two `connects:` — one "
                "bonded position is a riser with a bar balanced on it")
        # Clipped positions extend the span like bonded ones do: snipping
        # a pin does not shorten the PCB it was on, so an end position
        # with its pin removed still says how long the bar is.
        positions = link_positions(connects + clipped, where)
        both = ({(a.row, a.half, a.hole) for a in connects}
                & {(a.row, a.half, a.hole) for a in clipped})
        if both:
            at = ", ".join(f"{r}{h}" for r, _hf, h in sorted(both))
            raise ModelError(
                f"{where}: position(s) {at} are listed as BOTH connected "
                "and clipped — a snipped pin cannot carry the net")
        fab = str(lk.get("fab", "pcb-rail"))
        if fab not in LINK_FABS:
            raise ModelError(f"{where}: fab {fab!r} not in "
                             f"{list(LINK_FABS)}")
        stock = lk.get("stock", 0)
        if not isinstance(stock, int) or isinstance(stock, bool) \
                or stock < 0:
            raise ModelError(f"{where}: stock {stock!r} must be a "
                             "non-negative integer (positions per bar)")
        links.append(Link(ref, level, positions, connects, clipped,
                          name, stock, fab, str(lk.get("note", ""))))

    jumpers = [Jumper(ep(j["from"]), ep(j["to"]), str(j.get("colour", "")),
                      str(j.get("note", "")), name,
                      (str(j["pair"]) if j.get("pair") else None),
                      bool(j.get("offgrid", False)),
                      bool(j.get("underside", False)))
               for j in d.get("jumpers") or []]
    leads = [Lead(ep(w["at"]), str(w.get("colour", "")),
                  (str(w["net"]) if w.get("net") else None),
                  str(w.get("label", "")), name,
                  (str(w["pair"]) if w.get("pair") else None))
             for w in d.get("leads") or []]
    inter = [Jumper(ep(j["from"]), ep(j["to"]), str(j.get("colour", "")),
                    str(j.get("note", "")), name,
                    (str(j["pair"]) if j.get("pair") else None),
                    bool(j.get("offgrid", False)),
                    bool(j.get("underside", False)))
             for j in d.get("interlinks") or []]

    rb = d.get("rails_bridged")
    return Island(name, board, rails,
                  (bool(rb) if rb is not None else None),
                  parts, passives, jumpers, leads, inter,
                  [str(x) for x in (d.get("bench_only") or [])],
                  sv, devices, risers, links)


# -------------------------------------------------------------- derivation

@dataclass
class Net:
    nid: int
    name: str
    seeds: list           # [(seed_name, source_desc)]
    keys: list            # node keys in this net, sorted
    pins: list            # [(ref, pin_name)]
    leads: list           # [Lead]
    rail_seeds: list      # distinct seed names that came from rail strips
    signal_seeds: list    # distinct seed names from the signal source


@dataclass
class Edge:
    ref: str
    kind: str
    value: str
    a_nid: int
    b_nid: int
    rating: str = ""


@dataclass
class Design:
    islands: dict
    nets: list
    edges: list
    pin_nid: dict         # (ref, pin) -> nid
    jumpers: list         # [(Jumper, nid)] — both ends same net by merge
    node_members: dict    # node_key -> [member desc strings]
    hole_members: dict    # (island,row,half,hole) -> [member desc strings]
    nid_of_key: dict      # node_key -> nid
    signal_rows: list
    # ref -> {terminal name -> nid}, for parts with more than two
    # terminals. `edges` stays two-ended on purpose: every geometry rule
    # it feeds (span, overlap, polarity, rating) is inherently about two
    # legs and a body between them, and a three-legged part has no such
    # single body axis to measure.
    device_nids: dict = field(default_factory=dict)

    def net_by_id(self, nid):
        return self.nets[nid]

    def net_of_pin(self, ref, pin):
        return self.nets[self.pin_nid[(ref, str(pin))]]

    def nets_named(self, name):
        return [n for n in self.nets if n.name == name]


class _UF:
    def __init__(self):
        self.parent = {}

    def find(self, k):
        self.parent.setdefault(k, k)
        while self.parent[k] != k:
            self.parent[k] = self.parent[self.parent[k]]
            k = self.parent[k]
        return k

    def union(self, a, b):
        self.parent[self.find(a)] = self.find(b)


def derive(islands, signals):
    """Union-find over tie-point nodes -> Design. See module docstring.

    `signals` is a SignalRegistry; a footprint naming a source it does
    not know raises UnknownSignalSource at the footprint that names it.
    """
    refs = {}
    for isl in islands.values():
        # Devices are addressable by ref too: a jumper may say `Q1.G`,
        # and rules.yaml may put a `pulldown` on it. Their refs share one
        # namespace with parts because rules.yaml keys on bare refs and
        # cannot tell the two apart.
        for part in list(isl.parts) + list(isl.devices):
            if part.ref in refs:
                raise ModelError(f"ref {part.ref!r} used in both "
                                 f"{refs[part.ref][0].name} and {isl.name} — "
                                 "refs are global (rules.yaml keys on them)")
            refs[part.ref] = (isl, part)

    def resolve(endpoint, isl):
        if isinstance(endpoint, PinRef):
            if endpoint.ref not in refs:
                raise ModelError(f"{isl.name}: unknown ref in {endpoint}")
            part = refs[endpoint.ref][1]
            addr = (part.addr_of(endpoint.pin) if isinstance(part, Device)
                    else part.pins.get(endpoint.pin))
            if addr is None:
                raise ModelError(f"{endpoint.ref} has no pin "
                                 f"{endpoint.pin!r} placed")
            return addr
        if isinstance(endpoint, XIsland):
            target = islands.get(endpoint.island)
            if target is None:
                raise ModelError(f"{isl.name}: interlink to unknown island "
                                 f"{endpoint.island!r}")
            try:
                return parse_local(endpoint.text, target.name, target.board,
                                   target.rails)
            except AddrError as e:
                raise ModelError(str(e)) from e
        return endpoint

    uf = _UF()
    node_members, hole_members = {}, {}
    seeds_at = {}     # node_key -> [(seed_name, source, kind)]

    def touch(addr, desc):
        key = addr.node_key()
        uf.find(key)
        node_members.setdefault(key, []).append(desc)
        if isinstance(addr, HoleAddr) and addr.hole:
            hk = (addr.island, addr.row, addr.half, addr.hole)
            hole_members.setdefault(hk, []).append(desc)
        return key

    _pm_cache = {}

    def signal_map(source):
        """(mcu, pin) -> signal for one registered source, built once."""
        if source not in _pm_cache:
            pm = {}
            for row in signals.rows(source):
                sig = (row.signal or "").strip()
                if sig and not sig.startswith("("):
                    pm[(row.mcu, str(row.pin))] = sig
            _pm_cache[source] = pm
        return _pm_cache[source]

    pin_key, jumper_recs, lead_recs, passive_recs = {}, [], [], []
    device_recs = []
    for isl in islands.values():
        for pos, netname in isl.rails.items():
            key = RailAddr(isl.name, pos).node_key()
            uf.find(key)
            node_members.setdefault(key, [])
            seeds_at.setdefault(key, []).append(
                (netname, f"rail {pos} @ {isl.name}", "rail"))
        for part in isl.parts:
            source = mcu = None
            if part.fp and part.fp.pin_signals:
                source, mcu = part.fp.pin_signals.split(":", 1)
                # Validate the source HERE, unconditionally -- not only
                # when a pin below actually consults signal_map(source).
                # A footprint whose pins are all seed-overridden
                # (part.seeds) never takes that branch, so an unknown
                # source would otherwise derive cleanly and leave DRC
                # B3/B7 with nothing to find: the exact silent-empty
                # failure this registry exists to prevent.
                signal_map(source)
            for pn, addr in part.pins.items():
                key = touch(addr, f"{part.ref}.{pn}")
                pin_key[(part.ref, pn)] = key
                if pn in part.seeds:
                    seeds_at.setdefault(key, []).append(
                        (part.seeds[pn], f"{part.ref}.{pn} seed", "part"))
                elif mcu and (mcu, pn) in signal_map(source):
                    seeds_at.setdefault(key, []).append(
                        (signal_map(source)[(mcu, pn)],
                         f"{part.ref}.{pn} {source} {mcu}", "signal"))
            for ta, tb in part.internal_ties:
                uf.union(part.pins[ta].node_key(), part.pins[tb].node_key())
        for j in isl.jumpers + isl.interlinks:
            a, b = resolve(j.a, isl), resolve(j.b, isl)
            ka = touch(a, f"wire→{b}")
            kb = touch(b, f"wire→{a}")
            uf.union(ka, kb)
            jumper_recs.append((j, ka))
        for w in isl.leads:
            at = resolve(w.at, isl)
            key = touch(at, f"lead {w.label or w.net or w.colour}")
            if w.net:
                seeds_at.setdefault(key, []).append(
                    (w.net, f"lead {w.label!r} @ {isl.name}", "lead"))
            lead_recs.append((w, key))
        for part, terminals in isl.terminal_groups():
            by_index, keys = {}, []
            for t in terminals:
                key = touch(resolve(t.addr, isl), f"{part.ref}.{t.name}")
                keys.append(key)
                # A device leg is a named pin like any other, so it goes
                # in pin_nid: that is what net_of_pin() reads, and what
                # lets rules.yaml put a `pulldown` on a MOSFET gate. The
                # bench's Q1/Q2 gate pull-downs are exactly that shape.
                if isinstance(part, Device):
                    pin_key[(part.ref, t.name)] = key
                # Terminals sharing a net_index are one conductor inside
                # the part, so they merge here. Distinct indices stay
                # apart: this loop is what keeps a MOSFET's three legs on
                # three nets while a link bar's N pins collapse to one.
                if t.net_index in by_index:
                    uf.union(by_index[t.net_index], key)
                else:
                    by_index[t.net_index] = key
            # `edges` is the two-ended view the geometry rules walk, so
            # only genuine two-terminal passives belong in it. An OPEN
            # two-pin switch also has two terminals on two nets, but it
            # is not a body with legs either side and must not be handed
            # to B8/B10/B11 as though it were.
            if isinstance(part, Passive) and len(by_index) == 2:
                passive_recs.append((part, keys[0], keys[1]))
            else:
                device_recs.append((part, terminals, keys))

    groups = {}
    for key in list(uf.parent):
        groups.setdefault(uf.find(key), []).append(key)

    nets, nid_of_key = [], {}
    for root, keys in sorted(groups.items(), key=lambda kv: str(kv[0])):
        nid = len(nets)
        seeds, rail_s, sig_s = [], [], []
        for k in keys:
            for name, src, skind in seeds_at.get(k, []):
                seeds.append((name, src))
                if skind == "rail" and name not in rail_s:
                    rail_s.append(name)
                if skind == "signal" and name not in sig_s:
                    sig_s.append(name)
        names = sorted({n for n, _ in seeds})
        if len(names) == 1:
            name = names[0]
        elif names:
            name = "+".join(names)
        else:
            k = sorted(keys, key=str)[0]
            name = (f"N${k[1]}:{k[2]}{k[3]}" if k[0] == "row"
                    else f"N${k[1]}:rail:{k[2]}")
        nets.append(Net(nid, name, seeds, sorted(keys, key=str), [], [],
                        sorted(rail_s), sorted(sig_s)))
        for k in keys:
            nid_of_key[k] = nid

    pin_nid = {}
    for (ref, pn), key in pin_key.items():
        nid = nid_of_key[uf.find(key)]
        pin_nid[(ref, pn)] = nid
        nets[nid].pins.append((ref, pn))
    for w, key in lead_recs:
        nets[nid_of_key[uf.find(key)]].leads.append(w)

    edges = [Edge(q.ref, q.kind, q.value,
                  nid_of_key[uf.find(ka)], nid_of_key[uf.find(kb)],
                  getattr(q, "rating", ""))
             for q, ka, kb in passive_recs]
    jumpers = [(j, nid_of_key[uf.find(k)]) for j, k in jumper_recs]

    device_nids = {}
    for part, terminals, keys in device_recs:
        device_nids[part.ref] = {
            t.name: nid_of_key[uf.find(k)]
            for t, k in zip(terminals, keys)}

    return Design(islands, nets, edges, pin_nid, jumpers,
                  node_members, hole_members,
                  {k: nid_of_key[uf.find(k)] for k in uf.parent},
                  signals.all_rows(), device_nids)
