#!/usr/bin/env python3
"""router.py — two-layer autorouter for bbnet islands (geometry only).

Connectivity truth stays in the island YAML and the derived netlist; this
module computes WHERE each recorded wire should physically run, in the
spirit of a PCB autorouter mapped onto solderable-breadboard reality:

- Layer 0 (TOP) is the board surface. Wires lie flat in the hole grid,
  one wire per lattice cell — crossings are congestion, not allowed to
  persist. Part bodies are hard obstacles.
- Layer 1 (BOT) is the underside. A wire changes layers only at its own
  solder joints — a solderable breadboard has no mid-route via: an
  underside wire is soldered from beneath at both end holes and its
  whole body hangs in free air below the board, competing for nothing.
  A TOP-blocked jumper therefore becomes an end-to-end underside run,
  and `underside: true` declares one explicitly.
- Leads and interlinks fly OFF the board through the air — they are
  never lattice-routed; each is a straight run from its terminal hole
  to its exit edge (the renderer adds arrow / ghost-bus symbology).
- Jumper conflicts resolve by PathFinder-style negotiated congestion:
  every wire routes by A* through a shared cost map; overused resources
  gain escalating history costs and the offenders re-route until no
  resource is over capacity.

The router is deterministic (stable wire ordering, tie-broken heaps) so
rendered layouts are reproducible in CI.
"""
from __future__ import annotations

import heapq
import re
from collections import defaultdict
from dataclasses import dataclass, field

from bbnet.geometry import (
    HoleAddr, LEFT_HOLES, RIGHT_HOLES, RailAddr, parse_local,
)
from bbnet.model import Island, PinRef, XIsland, dip_right_col

TOP, BOT = 0, 1

# cost model — the knobs that encode the "flat single layer" philosophy
TOP_BASE = 1.0        # one top-layer lattice step
BEND_COST = 1.5       # each direction change (wire kink)
SOLDER_SOFT = 2.0     # passing over another wire's solder point
PASSIVE_SOFT = 3.0    # passing over a passive body span
RAIL_RUN_SOFT = 4.0   # running ALONG a rail strip (per cell) — wires
                      # should cross rail bands perpendicular and land,
                      # never ride them lengthwise (the strip already
                      # conducts; a wire over it only buries landings)
PRES_FAC = 2.0        # present-congestion multiplier, grows per round
PAIR_BONUS = 0.6      # discount for hugging a declared twisted-pair mate
DOMAIN_SOFT = 1.5     # penalty/cell for entering a rival domain's shadow
HIST_INC = 1.0        # history increment per overused resource per round
MAX_ITERS = 60
EDGE_CAP = 99         # edge exit columns hold many labelled leads
CELL_CAP = 2          # hard limit: two insulated wires may share a
                      # channel cell; CROWD_SOFT keeps singles preferred
CROWD_SOFT = 0.8      # extra cost per wire already in the cell


@dataclass(frozen=True)
class Cell:
    layer: int
    x: int
    y: int


@dataclass
class RoutedWire:
    key: str
    kind: str                    # jumper | lead | interlink
    colour: str
    label: str
    path: list = field(default_factory=list)     # [Cell]
    fail: bool = False
    # cross-island wires are routed twice — once on the island that
    # declares the interlink, once as a stub on the island it lands on.
    # Both halves carry the same `link` id ("<island>#<index>") so a
    # panel can stitch them into one wire across the seam; `link_end` is
    # "a" on the declaring half and "b" on the stub.
    link: str = ""
    link_end: str = ""

    @property
    def underside(self):
        return any(c.layer == BOT for c in self.path)


@dataclass
class RouteStats:
    wires: int = 0
    routed: int = 0
    failed: int = 0
    underside: int = 0    # end-to-end under-board runs (no mid-route
                          # layer change exists on a solderable board)
    total_cells: int = 0
    max_overuse: int = 0
    iterations: int = 0
    # half-row endpoints with no clean hole left (DRC B13 input) —
    # [(kind, label, row, half, rank, hole)]
    landings: list = field(default_factory=list)
    # top-layer body cells the router had to buy through a soft penalty
    # (DRC B18 input) — [(label, row, col, what)]
    obstructions: list = field(default_factory=list)


class Lattice:
    """Column map for one island: [edgeL][rails?][gutter?][a-e][ravine]
    [f-j][gutter?][rails?][edgeR]. Every board geometry has both hole
    halves and the DIP ravine (mini-170 = 17 rows x 10 holes); rail
    strips and their gutters are board-dependent. A gutter is the
    ~2-wire-wide bare lane between the inner rail and the hole field:
    routable on both layers, no holes — so no solder points, no vias,
    no terminals — just legal running room."""

    def __init__(self, island: Island):
        self.board = island.board
        self.rows = island.board.rows
        cols = ["edgeL"]
        if island.board.rails:
            cols += ["rail:top+", "rail:top-", "gutterL"]
        cols += list(LEFT_HOLES) + ["ravine"] + list(RIGHT_HOLES)
        if island.board.rails:
            cols += ["gutterR", "rail:bot+", "rail:bot-"]
        cols += ["edgeR"]
        self.cols = cols
        self._x = {c: i for i, c in enumerate(cols)}

    def name(self, x):
        return self.cols[x]

    def x_of(self, name):
        return self._x[name]

    def half(self, x):
        n = self.name(x)
        if n in LEFT_HOLES:
            return "L"
        if n in RIGHT_HOLES:
            return "R"
        return None

    def is_hole_col(self, x):
        return self.half(x) is not None

    def is_edge(self, x):
        return self.name(x) in ("edgeL", "edgeR")

    def is_rail(self, x):
        return self.name(x).startswith("rail:")

    def in_board(self, x):
        return not self.is_edge(x)


@dataclass
class _Wire:
    key: str
    kind: str
    colour: str
    label: str
    sources: list                 # [(x, y)] TOP cells
    targets: list                 # [(x, y)] TOP cells
    allowed: set                  # terminal cells exempt from body blocks
    nid: object = None            # derived net id (None = unknown)
    domain: str | None = None     # noise domain from rules['domains']
    pair: str | None = None       # twisted-pair label


def _local(addr):
    """Local display form of an endpoint (no island prefix)."""
    if isinstance(addr, HoleAddr):
        return f"{addr.row}{addr.hole or addr.half}"
    if isinstance(addr, RailAddr):
        return f"rail:{addr.strip}"
    return str(addr)


class _IslandRouter:
    def __init__(self, island: Island, remote_stubs, net_ctx=None,
                 pair_side=None):
        self.isl = island
        self.lat = Lattice(island)
        # net context (from the derived Design): endpoint -> nid,
        # nid -> domain
        self.net_ctx = net_ctx or _NetCtx()
        # panel-mate island -> "L"/"R": which edge that board sits past
        self.pair_side = pair_side or {}
        self._build_occupancy()
        self.wires = self._collect_wires(remote_stubs)
        self.landings = self._scan_landings(remote_stubs)

    # -------------------------------------------------- occupancy & costs

    def _endpoint_halfrows(self, ep):
        if isinstance(ep, HoleAddr):
            return {(ep.row, ep.half)}
        if isinstance(ep, PinRef):
            for part in self.isl.parts:
                if part.ref == ep.ref and ep.pin in part.pins:
                    a = part.pins[ep.pin]
                    return {(a.row, a.half)}
        return set()

    def _build_occupancy(self):
        lat, isl = self.lat, self.isl
        self.occupied_halfrows = set()
        self.solder_cells = set()
        self.body_cells = set()
        self.passive_cells = set()
        self.level_cells = {}      # level -> cells claimed at that level

        def occupy(ep):
            self.occupied_halfrows.update(self._endpoint_halfrows(ep))
            if isinstance(ep, HoleAddr) and ep.hole:
                self.solder_cells.add((lat.x_of(ep.hole), ep.row))

        for part in isl.parts:
            for a in part.pins.values():
                occupy(a)
                # A module's pins are usually recorded as bare half-rows
                # (`39L`) because the net does not care which hole. The
                # PIN does: a DIP/click footprint puts every left pin in
                # the anchor column and every right pin in the far one,
                # so those holes are physically full even though the
                # address never said so. Register them, or a half-row
                # endpoint elsewhere gets drawn landing on top of a pin.
                col = self._pin_col(part, a)
                if col is not None:
                    self.solder_cells.add((col, a.row))
            self.body_cells |= self._body(part)
            self.passive_cells |= self._overhang_cells(part)
        for q in isl.passives:
            occupy(q.a)
            occupy(q.b)
            self._claim_span(getattr(q, "level", 0), self._span(q.a, q.b))
        for dv in getattr(isl, "devices", ()):
            for t in dv.terminals:
                occupy(t.addr)
            # A three-legged part has no single body axis the way a
            # resistor does, so its keep-out is the span across its
            # outermost legs — the TO-92 tab really does sit over the
            # holes between G and S.
            if len(dv.terminals) > 1:
                self._claim_span(dv.level,
                                 self._span(dv.terminals[0].addr,
                                            dv.terminals[-1].addr))
        # A riser is soldered into its hole like any other leg, so it
        # claims that hole against B1 — the socket it presents is above
        # the board, but the pin is IN it.
        for r in getattr(isl, "risers", ()):
            occupy(r.at)
        # A bar's body covers every slot it spans, at ITS level — not the
        # surface. That is the point: the board underneath stays free for
        # routing, which is why building upward buys anything at all.
        for lk in getattr(isl, "links", ()):
            self._claim_span(lk.level,
                             {(lat.x_of(a.hole), a.row)
                              for a in lk.positions})
        for j in isl.jumpers + isl.interlinks:
            occupy(j.a)
            occupy(j.b)
        for w in isl.leads:
            occupy(w.at)

    def _claim_span(self, level, cells):
        """File a body's footprint under the level it sits at.

        Level 0 is the board surface, and those cells are what the
        autorouter has to route around. A body lifted to level 1 stops
        competing for surface channel entirely — that is the whole point
        of building upward — and the underside (level -1) never competed
        for it in the first place, which is why `side: bottom` was
        already excluded here before levels existed."""
        if level == 0:
            self.passive_cells |= cells
        else:
            self.level_cells.setdefault(level, set()).update(cells)

    def _pin_col(self, part, addr):
        """Lattice x of a part pin recorded as a bare half-row, or None
        when the address already names a hole (or the part has no
        footprint to infer from). A dip/click straddles the ravine with
        its two pin rows at the anchor column and `dip_right_col`;
        anything else keeps every pin in the anchor column."""
        if getattr(addr, "hole", None) or part.fp is None \
                or part.anchor is None or part.anchor.hole is None:
            return None
        lat = self.lat
        if part.fp.kind == "dip":
            return lat.x_of(dip_right_col(part) if addr.half == "R"
                            else part.anchor.hole)
        # single-column part: the anchor speaks only for its own half
        if addr.half == part.anchor.half:
            return lat.x_of(part.anchor.hole)
        return None

    def _body(self, part):
        """TOP-layer cells covered by a part body."""
        lat = self.lat
        pins = list(part.pins.values())
        rows = [a.row for a in pins]
        # An anchor pinned to a bare half-row (`20L`) names no column, so
        # there is no footprint-derived span to compute — fall through to
        # the pin-derived one. `_overhang_cells` below guards the same
        # case; `_body` only ever escaped it because nothing routed a
        # design like that until DRC gained geometry-dependent rules.
        if part.fp is not None and part.anchor is not None \
                and part.anchor.hole is not None:
            c0 = part.anchor.hole
            if part.fp.kind == "dip":
                x0, x1 = lat.x_of(c0), lat.x_of(dip_right_col(part))
            else:
                x0 = x1 = lat.x_of(c0)
        else:
            xs = [lat.x_of(a.hole) for a in pins if a.hole]
            if not xs:
                return set()
            x0, x1 = min(xs), max(xs)
        x0, x1 = min(x0, x1), max(x0, x1)
        return {(x, y)
                for x in range(x0, x1 + 1)
                for y in range(min(rows), max(rows) + 1)}

    def _overhang_cells(self, part):
        """Cells under the part's hovering body beyond its pin rows —
        soft keep-out (the body stands off on headers/sockets, so a wire
        beneath is possible but discouraged)."""
        fp = part.fp
        if fp is None or not fp.overhang or part.anchor is None \
                or part.anchor.hole is None:
            return set()
        lat = self.lat
        rows = [a.row for a in part.pins.values()]
        up, down = fp.overhang
        x0 = lat.x_of(part.anchor.hole)
        x1 = lat.x_of(dip_right_col(part)) if fp.kind == "dip" else x0
        x0, x1 = min(x0, x1), max(x0, x1)
        cells = set()
        for y in range(max(1, min(rows) - up),
                       min(lat.rows, max(rows) + down) + 1):
            if min(rows) <= y <= max(rows):
                continue           # pin rows are the HARD body already
            for x in range(x0, x1 + 1):
                cells.add((x, y))
        return cells

    def _span(self, a, b):
        """Cells under an axially placed passive body (soft cost)."""
        lat = self.lat
        if not (isinstance(a, HoleAddr) and isinstance(b, HoleAddr)
                and a.hole and b.hole):
            return set()
        ax, bx = lat.x_of(a.hole), lat.x_of(b.hole)
        if ax == bx:
            lo, hi = sorted((a.row, b.row))
            return {(ax, y) for y in range(lo + 1, hi)}
        if a.row == b.row:
            lo, hi = sorted((ax, bx))
            return {(x, a.row) for x in range(lo + 1, hi)}
        return set()

    # ------------------------------------------------------ wire building

    def _halfrow_choices(self, ep):
        """Candidate holes for an endpoint written as a bare half-row
        (`39L`, `40R`) -> [(cell, rank)], best first.

        A half-row says "any hole in this node", which is electrically
        true and physically not: a hole with a leg already in it is
        taken (B1), and a hole under a part body cannot be reached from
        the top at all. Ranking them means the drawn landing is one the
        bench could actually solder — before this, the picker took the
        first/last hole in the row and cheerfully drew the wire into an
        occupied one.

        rank 0 = free and open on top · 1 = free but under a body
        (underside only) · 2 = already occupied. Nothing is dropped, so
        a fully-claimed node still routes and B13 reports it."""
        lat = self.lat
        holes = LEFT_HOLES if ep.half == "L" else RIGHT_HOLES
        out = []
        for h in holes:
            cell = (lat.x_of(h), ep.row)
            if cell in self.solder_cells:
                rank = 2
            elif cell in self.body_cells:
                rank = 1
            else:
                rank = 0
            out.append((cell, rank))
        return sorted(out, key=lambda t: (t[1], t[0]))

    def _terminal(self, ep):
        """Endpoint -> (cells, halfrows)."""
        lat = self.lat
        if isinstance(ep, PinRef):
            for part in self.isl.parts:
                if part.ref == ep.ref and ep.pin in part.pins:
                    return self._terminal(part.pins[ep.pin])
            return [], set()
        if isinstance(ep, HoleAddr):
            if ep.hole:
                return [(lat.x_of(ep.hole), ep.row)], {(ep.row, ep.half)}
            ranked = self._halfrow_choices(ep)
            best = ranked[0][1]
            return ([c for c, r in ranked if r == best],
                    {(ep.row, ep.half)})
        if isinstance(ep, RailAddr):
            x = lat.x_of(f"rail:{ep.strip}")
            if getattr(ep, "row", 0):
                return [(x, ep.row)], set()     # pinned at a height
            return [(x, y) for y in range(1, lat.rows + 1)], set()
        return [], set()

    def _airwire(self, kind, colour, label, a_cell, b_cell,
                 link="", link_end=""):
        """A wire whose body hangs beneath the board (underside: true):
        free air, no channel competition, drawn point-to-point."""
        (ax, ay), (bx, by) = a_cell, b_cell
        rw = RoutedWire(f"air:{len(self.airwires):03d}:{label}", kind,
                        colour or "", label, link=link, link_end=link_end)
        rw.path = [Cell(TOP, ax, ay), Cell(BOT, ax, ay),
                   Cell(BOT, bx, by), Cell(TOP, bx, by)]
        self.airwires.append(rw)

    def _flywire(self, kind, colour, label, cells, side,
                 link="", link_end=""):
        """Off-board wire (lead or interlink): it rises from its hole
        and flies through the air, occupying NO surface channel — never
        routed, just a straight run to its exit edge at the terminal's
        row (the renderer adds the arrow / ghost-bus symbology)."""
        lo, hi = min(cells), max(cells)
        if side is None:
            xl = self.lat.x_of("edgeL")
            xr = self.lat.x_of("edgeR")
            side = "L" if (lo[0] - xl) <= (xr - hi[0]) else "R"
        (ax, ay) = lo if side == "L" else hi
        ex = self.lat.x_of("edgeL" if side == "L" else "edgeR")
        rw = RoutedWire(f"fly:{len(self.flywires):03d}:{label}", kind,
                        colour or "", label, link=link, link_end=link_end)
        rw.path = [Cell(TOP, ax, ay), Cell(TOP, ex, ay)]
        self.flywires.append(rw)

    def _scan_landings(self, remote_stubs):
        """Wire endpoints written as a bare half-row whose best remaining
        hole is compromised -> [(kind, label, row, half, rank, hole)].

        rank 1 = every free hole in the node is under a part body, so the
        wire can only be landed from beneath; rank 2 = every hole already
        has a leg in it. Both are decisions the model should be making
        explicitly rather than leaving to whichever hole the picker
        happens to reach for. DRC B13 turns these into instructions."""
        out = []

        def check(ep, kind, label):
            if not isinstance(ep, HoleAddr) or ep.hole:
                return
            (cell, rank) = self._halfrow_choices(ep)[0]
            if rank:
                out.append((kind, label, ep.row, ep.half, rank,
                            self.lat.name(cell[0])))

        for j in self.isl.jumpers:
            if not j.offgrid:
                label = f"{_local(j.a)}→{_local(j.b)}"
                check(j.a, "jumper", label)
                check(j.b, "jumper", label)
        for j in self.isl.interlinks:
            if not j.offgrid:
                check(j.a, "interlink", f"{_local(j.a)} ⇒ {j.b}")
        for w in self.isl.leads:
            check(w.at, "lead", f"{_local(w.at)} · {w.label}")
        for origin, text, _c, _p, _n, _u, _link in remote_stubs:
            try:
                ep = parse_local(text, self.isl.name, self.isl.board,
                                 self.isl.rails)
            except Exception:
                continue
            check(ep, "interlink", f"{_local(ep)} ⇐ {origin}")
        return out

    def _collect_wires(self, remote_stubs):
        wires = []
        self.airwires = []
        self.flywires = []
        ctx = self.net_ctx

        def add(kind, a_cells, b_cells, colour, label, allowed,
                nid=None, pair=None):
            key = f"{kind}:{len(wires):03d}:{label}"
            wires.append(_Wire(key, kind, colour or "", label,
                               a_cells, b_cells, allowed,
                               nid, ctx.domain_of(nid), pair))

        for j in self.isl.jumpers:
            if j.offgrid:      # solder blob at the end-jumper pads —
                continue       # nothing to route
            a, _ah = self._terminal(j.a)
            b, _bh = self._terminal(j.b)
            if not a or not b:
                continue
            label = (f"{_local(j.a)}→{_local(j.b)}"
                     + (f" · {j.note}" if j.note else ""))
            if getattr(j, "underside", False):
                self._airwire("jumper", j.colour, label, a[0], b[0])
                continue
            add("jumper", a, b, j.colour, label, set(a) | set(b),
                ctx.nid_of(self.isl.name, j.a), j.pair)

        for w in self.isl.leads:
            a, _ah = self._terminal(w.at)
            if not a:
                continue
            self._flywire("lead", w.colour,
                          f"{_local(w.at)} · {w.label}", a, None)

        for idx, j in enumerate(self.isl.interlinks):
            if j.offgrid:      # off-board tie (e.g. supply star point)
                continue       # — merges nets, nothing to route or kit
            a, _ah = self._terminal(j.a)
            if not a or not isinstance(j.b, XIsland):
                continue
            side = self.pair_side.get(j.b.island)
            link = f"{self.isl.name}#{idx}"
            label = f"{_local(j.a)} ⇒ {j.b}"
            if getattr(j, "underside", False):
                ex = self.lat.x_of("edgeR" if side != "L" else "edgeL")
                self._airwire("interlink", j.colour, label,
                              a[0], (ex, a[0][1]), link, "a")
                continue
            self._flywire("interlink", j.colour, label, a, side, link, "a")

        for origin, text, colour, pair, nid, under, link in remote_stubs:
            try:
                ep = parse_local(text, self.isl.name, self.isl.board,
                                 self.isl.rails)
            except Exception:
                continue
            a, _ah = self._terminal(ep)
            if not a:
                continue
            side = self.pair_side.get(origin)
            label = f"{_local(ep)} ⇐ {origin}"
            if under:
                ex = self.lat.x_of("edgeR" if side != "L" else "edgeL")
                self._airwire("interlink", colour, label,
                              a[0], (ex, a[0][1]), link, "b")
                continue
            self._flywire("interlink", colour, label, a, side, link, "b")

        # sort pairs adjacent (mates route back-to-back, so attraction
        # sees the partner's fresh path) while keeping a total order
        return sorted(wires, key=lambda w: (w.pair or w.key, w.key))

    # ----------------------------------------------------------- routing

    def _passable(self, wire, layer, x, y):
        lat = self.lat
        if not (1 <= y <= lat.rows and 0 <= x < len(lat.cols)):
            return False
        # a mounting screw fills the ravine at keep-out rows, through the
        # board: impassable on both layers
        if lat.name(x) == "ravine" and y in lat.board.ravine_keepouts:
            return False
        if layer == BOT:
            return lat.in_board(x)
        if lat.is_edge(x):
            return (wire.kind in ("lead", "interlink")
                    and (x, y) in wire._target_set)
        if (x, y) in self.body_cells and (x, y) not in wire.allowed:
            return False
        return True

    def _astar(self, wire, usage, hist):
        """Single-layer A*: wires route on the TOP surface only. A wire
        changes layers only at its own solder joints — a solderable
        breadboard has no mid-route via, so there is no dive through a
        spare hole. A wire that cannot route on TOP becomes an
        end-to-end underside run in route() instead of failing."""
        lat = self.lat
        targets = wire._target_set
        tx = sorted(targets)

        def h(x, y):
            return min(abs(x - a) + abs(y - b) for a, b in tx)

        pres_fac = self._pres_fac
        mates = self._pair_mates.get(wire.key, ())

        def others(key):
            """Effective rival occupants: pair mates never count (a
            twisted pair sharing a channel is the point)."""
            users = usage.get(key, ())
            n = 0
            for k in users:
                if k == wire.key or k in mates:
                    continue
                n += 1
            return n

        def res_mult(key, cap):
            over = max(0, others(key) + 1 - cap)
            return 1.0 + hist.get(key, 0.0) + pres_fac * over

        best = {}
        heap = []
        tie = 0
        for (x, y) in wire.sources:
            if not self._passable(wire, TOP, x, y):
                continue
            # the start cell is a consumed resource too — charge its
            # entry cost, or wires sharing a multi-cell terminal (rail
            # strips, half-rows) deadlock on a cell A* never "enters"
            ck = ("cell", TOP, x, y)
            cap = EDGE_CAP if lat.is_edge(x) else CELL_CAP
            g0 = TOP_BASE * res_mult(ck, cap) + CROWD_SOFT * others(ck)
            if (x, y) in self.solder_cells and (x, y) not in wire.allowed:
                g0 += SOLDER_SOFT
            st = (x, y, None)
            if g0 < best.get(st, 1e18):
                best[st] = g0
                heapq.heappush(heap, (g0 + h(x, y), g0, tie, st, None))
            tie += 1
        parents = {}
        goal = None
        while heap:
            _f, g, _t, st, par = heapq.heappop(heap)
            if best.get(st, 1e18) < g - 1e-9:
                continue
            if st not in parents:
                parents[st] = par
            x, y, d = st
            if (x, y) in targets:
                goal = st
                break
            for nd, (dx, dy) in enumerate(((1, 0), (-1, 0),
                                           (0, 1), (0, -1))):
                nx, ny = x + dx, y + dy
                if not self._passable(wire, TOP, nx, ny):
                    continue
                ck = ("cell", TOP, nx, ny)
                cap = EDGE_CAP if lat.is_edge(nx) else CELL_CAP
                cost = TOP_BASE * res_mult(ck, cap) + CROWD_SOFT * others(ck)
                if (nx, ny) in wire._repel:
                    cost += DOMAIN_SOFT
                if (nx, ny) in wire._attract:
                    cost = max(cost - PAIR_BONUS, 0.25)
                if (nx, ny) in self.solder_cells \
                        and (nx, ny) not in wire.allowed:
                    cost += SOLDER_SOFT
                if (nx, ny) in self.passive_cells:
                    cost += PASSIVE_SOFT
                if nd >= 2 and lat.is_rail(nx):
                    cost += RAIL_RUN_SOFT   # vertical step in a rail
                if d is not None and d != nd:
                    cost += BEND_COST
                ns = (nx, ny, nd)
                ng = g + cost
                if ng < best.get(ns, 1e18) - 1e-9:
                    best[ns] = ng
                    parents[ns] = st
                    heapq.heappush(
                        heap, (ng + h(nx, ny), ng, tie, ns, st))
                    tie += 1
        if goal is None:
            return None
        cells = []
        st = goal
        while st is not None:
            x, y = st[0], st[1]
            if not cells or cells[-1] != (TOP, x, y):
                cells.append((TOP, x, y))
            st = parents.get(st)
        cells.reverse()
        return cells

    def _resources(self, cells):
        return [("cell", layer, x, y) for (layer, x, y) in cells]

    def route(self):
        usage = defaultdict(set)
        hist = defaultdict(float)
        paths = {}
        nbrs = {}          # wire key -> neighborhood of its TOP cells
        stats = RouteStats(wires=len(self.wires))
        self._pair_mates = {}
        by_pair = defaultdict(list)
        for w in self.wires:
            w._target_set = set(w.targets)
            if w.pair:
                by_pair[w.pair].append(w.key)
        for keys in by_pair.values():
            for k in keys:
                self._pair_mates[k] = frozenset(x for x in keys if x != k)
        domain_by_key = {w.key: w.domain for w in self.wires}

        def neighborhood(cells):
            out = set()
            for (layer, x, y) in cells:
                if layer != TOP:
                    continue
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        out.add((x + dx, y + dy))
            return out

        def cap_of(key):
            if key[0] == "cell":
                return EDGE_CAP if self.lat.is_edge(key[2]) else CELL_CAP
            return 1

        def overused(key, users):
            eff = len(users)
            seen_pairs = set()
            for k in users:
                mates = self._pair_mates.get(k)
                if mates and any(m in users for m in mates):
                    pid = frozenset({k} | mates)
                    if pid in seen_pairs:
                        eff -= 1
                    else:
                        seen_pairs.add(pid)
            return eff - cap_of(key)

        dirty = list(self.wires)
        for it in range(1, MAX_ITERS + 1):
            stats.iterations = it
            # escalate the present-congestion factor (PathFinder): early
            # rounds may share optimistically, later rounds must separate
            self._pres_fac = min(PRES_FAC * (1.35 ** (it - 1)), 60.0)
            for w in dirty:
                if paths.get(w.key):
                    for r in self._resources(paths[w.key]):
                        usage[r].discard(w.key)
                # twisted-pair attraction: hug an already-routed mate
                w._attract = set()
                for mk in self._pair_mates.get(w.key, ()):
                    if paths.get(mk):
                        w._attract |= nbrs.get(mk, set())
                # noise-domain repulsion: keep out of rival domains'
                # shadow (only wires with a *different* named domain)
                w._repel = set()
                if w.domain:
                    for ok, dom in domain_by_key.items():
                        if (dom and dom != w.domain and paths.get(ok)):
                            w._repel |= nbrs.get(ok, set())
                cells = self._astar(w, usage, hist)
                if cells is None:
                    paths[w.key] = None
                    continue
                paths[w.key] = cells
                nbrs[w.key] = neighborhood(cells)
                for r in self._resources(cells):
                    usage[r].add(w.key)
            over = {}
            worst = 0
            for r, users in usage.items():
                ov = overused(r, users)
                if ov > 0:
                    over[r] = users
                    worst = max(worst, ov)
            if not over:
                stats.max_overuse = 0
                break
            stats.max_overuse = worst
            offenders = set()
            for r, users in over.items():
                hist[r] += HIST_INC
                offenders |= users
            dirty = [w for w in self.wires if w.key in offenders]

        routed = []
        for w in self.wires:
            cells = paths.get(w.key)
            rw = RoutedWire(w.key, w.kind, w.colour, w.label)
            if cells is None and w.sources and w.targets:
                # TOP-blocked: the wire becomes an end-to-end underside
                # run — soldered from beneath at both holes; there is
                # no mid-route layer change on a solderable board
                (ax, ay), (bx, by) = min(
                    ((s, t) for s in w.sources for t in w.targets),
                    key=lambda st: (abs(st[0][0] - st[1][0])
                                    + abs(st[0][1] - st[1][1]),
                                    st))
                rw.path = [Cell(TOP, ax, ay), Cell(BOT, ax, ay),
                           Cell(BOT, bx, by), Cell(TOP, bx, by)]
                stats.routed += 1
                stats.underside += 1
            elif cells is None:
                rw.fail = True
                rw.path = []
                stats.failed += 1
            else:
                rw.path = [Cell(*c) for c in cells]
                stats.routed += 1
                stats.total_cells += len(rw.path)
            routed.append(rw)
        for rw in self.airwires:    # declared underside runs
            stats.wires += 1
            stats.routed += 1
            stats.underside += 1
            routed.append(rw)
        for rw in self.flywires:    # off-board leads/interlinks
            stats.wires += 1
            stats.routed += 1
            routed.append(rw)
        stats.landings = self.landings
        stats.obstructions = self._obstructions(routed)
        return routed, stats, self.lat

    def _obstructions(self, routed):
        """Top-layer cells where a routed wire body ends up lying over a
        hole that already has a leg in it, or across a passive body.

        Both are SOFT costs during search (SOLDER_SOFT / PASSIVE_SOFT),
        and deliberately so: a router that hard-blocked them would fail
        to route rather than hand back a buildable-but-ugly answer. But
        soft means the penalty CAN be paid, and until now nothing said
        when it had been — the wire just came out of the negotiation
        lying across a hole the builder still has to solder into. This
        is the record of what was given up, for DRC B18 to report.

        Underside runs never appear here, and both kinds fall out of the
        two exemptions below rather than needing a guard of their own —
        which is worth saying, because an explicit `rw.underside` check
        reads like it is protecting something and would be dead code:

        - a DECLARED `underside: true` wire is an airwire, never lattice
          routed, so it is not in `self.wires` and has no `allowed` entry;
        - a wire the router PROMOTED underside after finding the top face
          blocked has only its own two terminals on TOP, and a wire's own
          terminals are exempt anyway.

        Either way the body hangs in free air below the board and competes
        for nothing up here, which is exactly why sending a blocked wire
        down there is the standard fix.
        """
        allowed = {w.key: w.allowed for w in self.wires}
        out = []
        for rw in routed:
            free = allowed.get(rw.key)
            if free is None or rw.fail:
                continue
            for c in rw.path:
                if c.layer != TOP or (c.x, c.y) in free:
                    continue
                if (c.x, c.y) in self.solder_cells:
                    what = "a soldered leg"
                elif (c.x, c.y) in self.passive_cells:
                    what = "a passive body"
                else:
                    continue
                out.append((rw.label, c.y, self.lat.name(c.x), what))
        return out


class _NetCtx:
    """Optional bridge to the derived Design: endpoint -> net id,
    half-row -> net id, net id -> noise domain (rules['domains']).
    Without a Design everything degrades to the net-blind behavior."""

    def __init__(self, design=None, rules=None):
        self.design = design
        self._domains = []
        for d in (rules or {}).get("domains") or []:
            try:
                self._domains.append(
                    (re.compile(str(d["match"])), str(d["domain"])))
            except (KeyError, re.error):
                continue
        self._dom_cache = {}

    def nid_of(self, island, ep):
        if self.design is None:
            return None
        if isinstance(ep, HoleAddr):
            return self.design.nid_of_key.get(
                ("row", island, ep.row, ep.half))
        if isinstance(ep, RailAddr):
            return self.design.nid_of_key.get(("rail", island, ep.strip))
        if isinstance(ep, PinRef):
            return self.design.pin_nid.get((ep.ref, ep.pin))
        return None


    def domain_of(self, nid):
        if nid is None or self.design is None or not self._domains:
            return None
        if nid not in self._dom_cache:
            name = self.design.net_by_id(nid).name
            dom = None
            for rx, d in self._domains:
                if rx.search(name):
                    dom = d
                    break
            self._dom_cache[nid] = dom
        return self._dom_cache[nid]


def route_design(islands: dict, design=None, rules=None, panels=()):
    """Route every island; returns {name: (wires, stats, lattice)}.

    With a derived Design (and optional rules with a `domains:` list),
    routing is net-aware: declared `pair:` wires co-run as twisted
    pairs and wires whose nets match different `domains:` classes keep
    out of each other's shadow. `panels` (layout.yaml) fixes the exit
    edge for islands that sit side by side, so the two halves of a
    cross-board wire run toward each other and meet at the seam."""
    ctx = _NetCtx(design, rules)
    sides = defaultdict(dict)
    for p in panels or ():
        for mine in p.islands:
            for other in p.islands:
                if other != mine:
                    sides[mine][other] = p.side_of(mine, other)
    stubs = defaultdict(list)
    for isl in islands.values():
        for idx, j in enumerate(isl.interlinks):
            if getattr(j, "offgrid", False):
                continue
            if isinstance(j.b, XIsland) and j.b.island in islands:
                stubs[j.b.island].append(
                    (isl.name, j.b.text, j.colour, j.pair,
                     ctx.nid_of(isl.name, j.a),
                     getattr(j, "underside", False),
                     f"{isl.name}#{idx}"))
    out = {}
    for name in sorted(islands):
        r = _IslandRouter(islands[name], sorted(
            stubs.get(name, []),
            key=lambda t: (t[0], t[1], t[2], t[3] or "")), ctx,
            sides.get(name))
        out[name] = r.route()
    return out


def in_node_runs(wires, lat):
    """Every routed endpoint that walks across its OWN half-row before
    leaving it: [(wire, end, row, half, [holes traversed, in order])].

    The five holes of a half-row are one node, so a wire that lands in
    one and then crawls to another has picked the wrong hole — the run
    is redundant copper laid over holes it never uses, and it burns
    channel cells its neighbours could have had. Geometry only; whether
    a better hole is actually free is DRC's call (B12).

    Underside airwires are skipped: their path is a layer hop at a fixed
    hole and their body hangs in free air, crossing nothing on top."""
    out = []
    for w in wires:
        if not w.path or w.fail or w.underside:
            continue
        for end, path in (("a", w.path), ("b", w.path[::-1])):
            x0, y0 = path[0].x, path[0].y
            half = lat.half(x0)
            if half is None:
                continue
            holes = [lat.name(x0)]
            for cell in path[1:]:
                if (cell.layer != TOP or cell.y != y0
                        or lat.half(cell.x) != half):
                    break
                if lat.name(cell.x) != holes[-1]:
                    holes.append(lat.name(cell.x))
            if len(holes) > 1:
                out.append((w, end, y0, half, holes))
    return out
