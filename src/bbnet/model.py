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
PASSIVE_KINDS = {"resistor", "diode", "led", "other"} | CAP_KINDS


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
    # polarity convention: for electrolytics, `a` (YAML `from:`) is the
    # + terminal — rendered as +/- on the build sheet, enforced by B10


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
        side = q.get("side", "top")
        if side not in ("top", "bottom"):
            raise ModelError(f"{name}.{q.get('ref')}: side {side!r} not "
                             "'top' or 'bottom'")
        passives.append(Passive(q.get("ref", "?"), kind,
                                str(q.get("value", "")),
                                ep(q["from"]), ep(q["to"]), name, side,
                                str(q.get("rating", ""))))

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
                  sv)


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
        for part in isl.parts:
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
            if endpoint.pin not in part.pins:
                raise ModelError(f"{endpoint.ref} has no pin "
                                 f"{endpoint.pin!r} placed")
            return part.pins[endpoint.pin]
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
        for q in isl.passives:
            a, b = resolve(q.a, isl), resolve(q.b, isl)
            ka = touch(a, f"{q.ref}.a")
            kb = touch(b, f"{q.ref}.b")
            passive_recs.append((q, ka, kb))

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

    return Design(islands, nets, edges, pin_nid, jumpers,
                  node_members, hole_members,
                  {k: nid_of_key[uf.find(k)] for k in uf.parent},
                  signals.all_rows())
