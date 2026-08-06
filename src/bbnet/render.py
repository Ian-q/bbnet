#!/usr/bin/env python3
"""render.py — SVG/HTML board views from the bbnet model + autorouter.

`bbnet render` draws every island as an SVG board grid: parts with
per-pin labels, passives as badges, and every jumper/lead/interlink as a
ROUTED orthogonal polyline from the two-layer autorouter (router.py) in
its real wire colour. Top-layer runs are full pipes; end-to-end
underside runs render translucent (KiCad-style — there is no mid-route
via on a solderable board); leads and interlinks fly off-board with
arrow / ghost-bus symbology. A per-island kitting
table gives cut lengths so wires can be prepared before soldering.

Output is deterministic (the router is; coordinates are rounded and the
lane jitter is CRC-keyed), so the committed LAYOUT.html is reproducible
and `bbnet check` guards it in-sync the same way as REPORT.md.
Click any wire (or its kitting row) to isolate it; print styles put one
board per page with checkbox kitting tables for the bench.
"""
from __future__ import annotations

import html
import zlib

from bbnet.geometry import HoleAddr, RailAddr
from bbnet.model import dip_right_col
from bbnet.router import BOT, TOP, route_design

# px geometry
CELL = 15        # px per row
HOLE_DX = 15     # px per hole column
RAIL_W = 12
RAV_W = 22
EDGE_W = 10
GAP = 6
NUM_W = 13       # reserved row-number strip just inside the rails —
                 # gutter wires must never paint over the numbers
MARGIN = 235     # label margins either side
HEAD = 46

WIRE_COLOURS = {
    "RED": "#d62728", "BLK": "#222222", "YEL": "#d4a800",
    "BLU": "#1f77b4", "GRN": "#2ca02c", "WHT": "#b0b0b0",
    "ORG": "#ff8c00", "PUR": "#9467bd", "GRY": "#808080",
    "BRN": "#8c564b", "": "#e91e63",
}
# Built-in rail tints. A project on other rail names supplies its own via
# colours.yaml `rail_tints:`; these three are the near-universal ones.
DEFAULT_RAIL_TINTS = {"3V3": "#d62728", "GND": "#444444", "5V": "#d62728"}
MM_PER_CELL = 2.54
BOARD_BG = "#f7f5ef"    # casing colour — must match the board rect
GHOST_DX = 34           # px from the board edge to the ghost bus that
                        # interlinks land on (off-island connections)


def esc(s):
    return html.escape(str(s), quote=True)


class PxMap:
    """Lattice column/row -> pixel coordinates."""

    def __init__(self, lattice):
        self.lat = lattice
        x = MARGIN
        self.cx = []
        for name in lattice.cols:
            w = (EDGE_W if name.startswith("edge") else
                 RAIL_W if name.startswith("rail:") else
                 RAV_W if name == "ravine" else
                 9 if name.startswith("gutter") else HOLE_DX)
            if name == "gutterL":
                x += NUM_W        # row-number strip before the gutter
            self.cx.append(x + w / 2)
            x += w + (GAP if name.startswith("rail:") else 0)
            if name == "gutterR":
                x += NUM_W        # row-number strip after the gutter
        self.width = x + MARGIN
        self.height = HEAD + lattice.rows * CELL + 30

    def x(self, xi):
        return round(self.cx[xi], 1)

    def y(self, row):
        return round(HEAD + (row - 0.5) * CELL, 1)

    def col_x(self, name):
        return self.x(self.lat.x_of(name))


def _pin_cols(part):
    """Per-pin hole column letters for a footprint part (render only)."""
    if part.anchor is None or part.fp is None:
        return {}
    c0 = part.anchor.hole
    if part.fp.kind == "sil":
        return {pn: c0 for pn in part.pins}
    right = dip_right_col(part)
    n = len(part.fp.pin_names) // 2
    cols = {}
    for i, pn in enumerate(part.fp.pin_names):
        cols[pn] = c0 if i < n else right
    return cols


def _endpoint_px(px, ep, other_row=None):
    if isinstance(ep, HoleAddr):
        if ep.hole:
            return px.col_x(ep.hole), px.y(ep.row)
        col = "c" if ep.half == "L" else "h"
        return px.col_x(col), px.y(ep.row)
    if isinstance(ep, RailAddr):
        row = getattr(ep, "row", 0) or other_row or 1
        return px.col_x(f"rail:{ep.strip}"), px.y(row)
    return None


def _wire_points(px, wire):
    """Path cells -> [(x, y, layer)], collinear points dropped."""
    pts = [(px.x(c.x), px.y(c.y), c.layer) for c in wire.path]
    out = [pts[0]]
    for p in pts[1:]:
        if len(out) >= 2:
            a, b = out[-2], out[-1]
            if (a[2] == b[2] == p[2]
                    and ((a[0] == b[0] == p[0]) or (a[1] == b[1] == p[1]))):
                out[-1] = p
                continue
        out.append(p)
    return out


LEAD_CH_PX = 5.4    # 9px ui-monospace advance — sizes label truncation
LEAD_MIN_CH = 10    # below this an edge label says nothing useful, so
                    # it is dropped rather than shown as a stub


def _fit_label(text, budget_px):
    """Clip an edge label to the room it has: (shown, full-or-'').

    The caller puts `full` in a <title>, and the wire's own group title
    plus the kitting table always carry the untruncated text — so a
    dropped or clipped label loses nothing, it just moves to hover. A
    narrow seam therefore spends its gutter on wires, not on stubs of
    text nobody can read."""
    n = int(budget_px / LEAD_CH_PX)
    if n >= len(text):
        return text, ""
    if n < LEAD_MIN_CH:
        return "", text
    return text[:n - 1] + "…", text


def _edge_label_sides(wires, lattice, skip_links=()):
    """Which board edges this island prints labels off, and how many."""
    n = {}
    for w in wires:
        if (w.link and w.link in skip_links) or not w.path:
            continue
        for e in (w.path[0], w.path[-1]):
            if lattice.is_edge(e.x):
                side = lattice.name(e.x)
                n[side] = n.get(side, 0) + 1
    return n


LANE = 4.0


def _jitter(key):
    # Lane offset ±4.0, an INTEGER on purpose. Hole centres land on a
    # half-pixel (MARGIN and every column width are whole numbers, and
    # each centre picks up a +w/2), so a whole-number lane offset keeps
    # every displaced pipe on that same half-pixel phase and it renders
    # crisp instead of straddling two device pixels. The old 3.4 put
    # displaced pipes on .9, which is why co-runs looked softer than the
    # wires that happened not to move.
    #
    # Two co-run pipes now sit 8.0px apart, comfortably past the 6.5px
    # casing reach (3.7 casing + 2.8 colour half-widths), so the
    # separation guarantee the old value bought at 6.8 is strengthened,
    # not traded away.
    h = zlib.crc32(key.encode("utf-8"))   # stable across processes
    return ((h % 3) - 1) * LANE, (((h >> 4) % 3) - 1) * LANE


def _co_run_keys(wires):
    """Wires that share >= 2 top-layer cells with one other wire are
    genuine co-runs and keep a small lane offset so both stay visible;
    everything else sits dead-centre on the grid (Flow-Free style —
    plain perpendicular crossings need no offset, the casing separates
    them)."""
    cell_users = {}
    for w in wires:
        for c in w.path:
            if c.layer == TOP:
                cell_users.setdefault((c.x, c.y), set()).add(w.key)
    pair_n = {}
    for users in cell_users.values():
        us = sorted(users)
        for i in range(len(us)):
            for j in range(i + 1, len(us)):
                pair_n[(us[i], us[j])] = pair_n.get((us[i], us[j]), 0) + 1
    return {k for pair, n in pair_n.items() if n >= 2 for k in pair}


def _pipe_d(pts, r=5.0):
    """SVG path with rounded elbows — a Flow-Free pipe. pts are (x, y)
    pixel pairs; interior corners get quadratic fillets of radius r."""
    if len(pts) < 2:
        return ""
    d = [f"M {pts[0][0]},{pts[0][1]}"]
    for i in range(1, len(pts) - 1):
        (x0, y0), (x1, y1), (x2, y2) = pts[i - 1], pts[i], pts[i + 1]
        l1 = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
        l2 = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
        if not l1 or not l2:
            continue
        f1, f2 = min(r, l1 / 2) / l1, min(r, l2 / 2) / l2
        ax, ay = x1 - (x1 - x0) * f1, y1 - (y1 - y0) * f1
        bx, by = x1 + (x2 - x1) * f2, y1 + (y2 - y1) * f2
        d.append(f"L {round(ax, 1)},{round(ay, 1)} "
                 f"Q {x1},{y1} {round(bx, 1)},{round(by, 1)}")
    d.append(f"L {pts[-1][0]},{pts[-1][1]}")
    return " ".join(d)


PASSIVE_COLS = {"resistor": "#a67c2e", "ceramic": "#c46210",
                "electrolytic": "#2e6da4"}


DEVICE_COL = "#5b3fa8"
RISER_COL = "#0d7a6f"
LINK_COL = "#b3452b"

# Per-level drawing offset. Small and diagonal on purpose: enough that a
# bar at level 1 visibly floats clear of the board it spans, not so much
# that it stops pointing at the holes it actually plugs into. The board
# grid stays the ground truth — this is a depth CUE, not a projection.
LEVEL_DX, LEVEL_DY = 5.0, -5.0


def _lift(pt, level):
    return (round(pt[0] + LEVEL_DX * level, 1),
            round(pt[1] + LEVEL_DY * level, 1))


def _level_layer(add, px, island):
    """Risers, link bars and multi-terminal devices, painted low level
    first so higher ones land on top — the same order you build them in.

    Everything above the surface is drawn lifted and leader-lined back
    to its hole, because the one question this view has to answer is
    "what is stacked on what", and a bar drawn flat on its holes answers
    the opposite."""
    risers = getattr(island, "risers", ())
    links = getattr(island, "links", ())
    devices = getattr(island, "devices", ())
    if not (risers or links or devices):
        return

    levels = sorted({r.level for r in risers}
                    | {lk.level for lk in links}
                    | {d.level for d in devices})
    add('<g class="lyr-level">')
    for lv in levels:
        add(f'<g class="lvl" data-level="{lv}">')
        for r in (x for x in risers if x.level == lv):
            base = _endpoint_px(px, r.at)
            if base is None:
                continue
            top = _lift(base, lv)
            # the post, then the socket at its top — a riser is the one
            # thing here that is genuinely vertical
            add(f'<line x1="{base[0]}" y1="{base[1]}" x2="{top[0]}" '
                f'y2="{top[1]}" stroke="{RISER_COL}" stroke-width="1.6" '
                f'stroke-opacity="0.75"/>')
            add(f'<circle cx="{top[0]}" cy="{top[1]}" r="3.1" '
                f'fill="#fff" stroke="{RISER_COL}" stroke-width="1.6">'
                f'<title>riser {r.at} → level {lv}'
                f'{" — " + r.note if r.note else ""}</title></circle>')
        for lk in (x for x in links if x.level == lv):
            pts = [p for p in (_endpoint_px(px, a) for a in lk.positions)
                   if p is not None]
            if len(pts) < 2:
                continue
            a, b = _lift(pts[0], lv), _lift(pts[-1], lv)
            add(f'<line x1="{a[0]}" y1="{a[1]}" x2="{b[0]}" y2="{b[1]}" '
                f'stroke="{LINK_COL}" stroke-width="7" '
                f'stroke-linecap="round" stroke-opacity="0.32"/>')
            add(f'<line x1="{a[0]}" y1="{a[1]}" x2="{b[0]}" y2="{b[1]}" '
                f'stroke="{LINK_COL}" stroke-width="2.6" '
                f'stroke-linecap="round">'
                f'<title>{esc(lk.ref)} 1x{lk.length} @ level {lv} '
                f'({esc(lk.fab)})</title></line>')
            bonded = {(x.row, x.half, x.hole) for x in lk.connects}
            snipped = {(x.row, x.half, x.hole) for x in lk.clipped}
            for addr, pt in zip(lk.positions, pts):
                key = (addr.row, addr.half, addr.hole)
                top = _lift(pt, lv)
                if key in bonded:
                    # a bonded pin really does reach the board
                    add(f'<line x1="{pt[0]}" y1="{pt[1]}" x2="{top[0]}" '
                        f'y2="{top[1]}" stroke="{LINK_COL}" '
                        f'stroke-width="1.4" stroke-opacity="0.7"/>')
                    add(f'<circle cx="{top[0]}" cy="{top[1]}" r="2.6" '
                        f'fill="{LINK_COL}"/>')
                elif key in snipped:
                    add(f'<circle cx="{top[0]}" cy="{top[1]}" r="2.4" '
                        f'fill="none" stroke="{LINK_COL}" '
                        f'stroke-width="1.2" stroke-dasharray="2 2"/>')
                else:
                    # a float: pin present, touching nothing. Drawn hollow
                    # and unconnected so the build sheet says what B15
                    # says — this pin is hanging in the air.
                    add(f'<circle cx="{top[0]}" cy="{top[1]}" r="2.4" '
                        f'fill="#fff" stroke="{LINK_COL}" '
                        f'stroke-width="1.2"/>')
            add(f'<text x="{(a[0]+b[0])/2}" y="{(a[1]+b[1])/2 - 7}" '
                f'class="pv" text-anchor="middle" fill="{LINK_COL}">'
                f'{esc(lk.ref)}</text>')
        for dv in (x for x in devices if x.level == lv):
            pts = [p for p in (_endpoint_px(px, t.addr)
                               for t in dv.terminals) if p is not None]
            if len(pts) < 2:
                continue
            lifted = [_lift(p, lv) for p in pts]
            path = " ".join(f"{p[0]},{p[1]}" for p in lifted)
            add(f'<polyline points="{path}" fill="none" '
                f'stroke="{DEVICE_COL}" stroke-width="2.4" '
                f'stroke-linejoin="round" stroke-linecap="round">'
                f'<title>{esc(dv.ref)} {esc(dv.kind)} '
                f'{esc(dv.value)}</title></polyline>')
            for t, p in zip(dv.terminals, lifted):
                add(f'<circle cx="{p[0]}" cy="{p[1]}" r="2.4" '
                    f'fill="#fff" stroke="{DEVICE_COL}" '
                    f'stroke-width="1.4"><title>{esc(dv.ref)}.'
                    f'{esc(t.name)}</title></circle>')
            mid = lifted[len(lifted) // 2]
            lab = f"{dv.ref} {dv.value}".strip()
            add(f'<text x="{mid[0]}" y="{mid[1] - 8}" class="pv" '
                f'text-anchor="middle" fill="{DEVICE_COL}">'
                f'{esc(lab)}</text>')
        add('</g>')
    add('</g>')


def _badge_y(badges, mx, my, hw, hh):
    """Nudge a value badge vertically until it overlaps no earlier
    badge (crossing passive pairs stack labels otherwise)."""
    for off in (0, -13, 13, -26, 26, -39, 39):
        cand = my + off
        if not any(abs(mx - bx) < hw + bw and abs(cand - by) < hh + bh
                   for (bx, by, bw, bh) in badges):
            badges.append((mx, cand, hw, hh))
            return cand
    badges.append((mx, my, hw, hh))
    return my


def _ep_name(ep):
    if isinstance(ep, RailAddr):
        at = f"@{ep.row}" if getattr(ep, "row", 0) else ""
        return f"rail:{ep.strip}{at}"
    if getattr(ep, "hole", None):
        return f"{ep.row}{ep.hole}"
    return f"{ep.row}{ep.half}"


def _rail_marker(add, px, island, strip, row, rail_tints):
    """Unambiguous rail landing: a solid bar filling the band width at
    the landing row, in the rail's tint — 'solder to THIS strip'."""
    net = island.rails.get(strip, "")
    tint = rail_tints.get(net, "#999")
    x = px.col_x(f"rail:{strip}")
    y = px.y(row)
    add(f'<rect x="{x-RAIL_W/2+1.5}" y="{y-2.4}" width="{RAIL_W-3}" '
        f'height="4.8" rx="2.4" fill="{tint}"><title>lands on the '
        f'{esc(strip)} strip ({esc(net)})</title></rect>')


def wire_length_mm(wire):
    """Cut-length estimate: geometric path length + service slack (+
    board-thickness dips for an underside run)."""
    mm = 0.0
    for a, b in zip(wire.path, wire.path[1:]):
        mm += ((a.x - b.x) ** 2 + (a.y - b.y) ** 2) ** 0.5 * MM_PER_CELL
    mm += 25 + (12 if wire.underside else 0)
    return int(round(max(mm, MM_PER_CELL) / 5.0)) * 5


def render_island(island, wires, stats, lattice, rail_tints):
    """One island, standalone: its body wrapped in its own <svg>."""
    body, px = island_body(island, wires, stats, lattice,
                           rail_tints=rail_tints)
    return (f'<svg viewBox="0 0 {px.width} {px.height}" '
            f'width="{px.width}" height="{px.height}" '
            f'xmlns="http://www.w3.org/2000/svg">' + body + "</svg>")


def island_body(island, wires, stats, lattice, skip_links=(),
                label_px=None, *, rail_tints):
    """SVG content for one island in its own coordinates, plus its
    PxMap. A panel translates several of these into one <svg>;
    `skip_links` names cross-island links the panel draws whole across
    the seam, so this island must not draw its half of them, and
    `label_px` ({side: px}) narrows the edge labels on a side that now
    shares a seam gutter with the neighbouring board instead of owning
    a full margin."""
    px = PxMap(lattice)
    S = []
    add = S.append
    bx0 = px.col_x("edgeL") - EDGE_W / 2 + 4
    bx1 = px.col_x("edgeR") + EDGE_W / 2 - 4
    add(f'<rect x="{bx0}" y="{HEAD-26}" width="{bx1-bx0}" '
        f'height="{px.height-HEAD+16}" rx="8" fill="#f7f5ef" '
        f'stroke="#c8c2b2"/>')
    add(f'<text x="{bx0+6}" y="{HEAD-32}" class="ttl">{esc(island.name)}'
        f'  <tspan class="sub">({esc(island.board.name)}) — '
        f'{stats.routed}/{stats.wires} wires routed, '
        f'{stats.underside} underside</tspan></text>')

    # rails
    for strip in island.board.rails:
        net = island.rails.get(strip, "")
        tint = rail_tints.get(net, "#999")
        x = px.col_x(f"rail:{strip}")
        add(f'<rect x="{x-RAIL_W/2}" y="{HEAD-4}" width="{RAIL_W}" '
            f'height="{lattice.rows*CELL+8}" rx="4" fill="{tint}" '
            f'fill-opacity="0.13" stroke="{tint}" stroke-opacity="0.6"/>')
        add(f'<text x="{x}" y="{HEAD-8}" class="rail" fill="{tint}" '
            f'text-anchor="middle">{esc(strip)}</text>')
        add(f'<text x="{x}" y="{px.height-6}" class="rail" fill="{tint}" '
            f'text-anchor="middle">{esc(net)}</text>')
        # repeat the net name down the band so a zoomed-in view still
        # says which strip is which (top/bottom labels are far away on
        # a 63-row board)
        for rr in range(12, lattice.rows - 4, 16):
            yy = px.y(rr)
            add(f'<text x="{x}" y="{yy}" class="rail" fill="{tint}" '
                f'fill-opacity="0.55" text-anchor="middle" '
                f'transform="rotate(90 {x} {yy})">{esc(net)}</text>')

    # built-in rail end-jumper pads (top = PWR pair, bottom = GND pair).
    # The PWR label has three possible states, and only one of them may
    # ever claim "one net": if BOTH + strips are declared and their net
    # names differ, bridging them shorts two supplies -- warn. If BOTH
    # are declared and equal, the model has verified they are one net,
    # and the bridge is the intended rail-common tie -- show that
    # affordance. If FEWER THAN TWO are declared, the model has verified
    # nothing about the missing strip -- it may carry no net at all, or
    # one this design just never mentions -- so claiming "one net" would
    # assert a fact the design never established; name whichever
    # position is missing instead, which is neutral and true either way.
    if island.board.rails:
        gnd_bridged = any(
            getattr(j, "offgrid", False) for j in island.jumpers)
        plus_positions = [p for p in ("top+", "bot+") if p in island.rails]
        plus_nets = {island.rails[p] for p in plus_positions}
        rx = px.col_x("ravine")
        for (yy, kind) in ((HEAD - 14, "PWR"), (px.height - 16, "GND")):
            for dx in (-5, 5):
                add(f'<circle cx="{rx+dx}" cy="{yy}" r="3.2" fill="#fff" '
                    f'stroke="#8a8a8a" stroke-width="1.4"/>')
            if kind == "PWR" and len(plus_nets) > 1:
                add(f'<text x="{rx+14}" y="{yy+3}" class="pv" '
                    f'fill="#b0413e">PWR pads — NEVER bridge '
                    f'(+ rails are different nets)</text>')
            elif kind == "PWR" and len(plus_positions) == 2:
                add(f'<text x="{rx+14}" y="{yy+3}" class="pv" '
                    f'fill="#888">PWR end-jumper pads '
                    f'(+ rails are one net)</text>')
            elif kind == "PWR":
                missing = [p for p in ("top+", "bot+")
                           if p not in island.rails]
                add(f'<text x="{rx+14}" y="{yy+3}" class="pv" '
                    f'fill="#888">PWR end-jumper pads '
                    f'({"/".join(missing)} not wired in this design)</text>')
            elif gnd_bridged:
                add(f'<line x1="{rx-5}" y1="{yy}" x2="{rx+5}" y2="{yy}" '
                    f'stroke="#222" stroke-width="4" '
                    f'stroke-linecap="round"/>')
                add(f'<text x="{rx+14}" y="{yy+3}" class="pv" '
                    f'fill="#222">GND end-jumper: solder blob here '
                    f'(rail-common tie)</text>')
            else:
                add(f'<text x="{rx+14}" y="{yy+3}" class="pv" '
                    f'fill="#888">GND end-jumper pads (unbridged)</text>')

    # mounting-hole keep-outs (screws in the ravine)
    if island.board.ravine_keepouts:
        rx = px.col_x("ravine")
        rows_ko = sorted(island.board.ravine_keepouts)
        clusters, cur = [], [rows_ko[0]]
        for r in rows_ko[1:]:
            if r == cur[-1] + 1:
                cur.append(r)
            else:
                clusters.append(cur)
                cur = [r]
        clusters.append(cur)
        for cl in clusters:
            y0 = px.y(cl[0]) - CELL / 2
            add(f'<rect x="{rx-8}" y="{y0}" width="16" '
                f'height="{len(cl)*CELL}" rx="4" fill="#b0413e" '
                f'fill-opacity="0.10" stroke="#b0413e" '
                f'stroke-opacity="0.35" stroke-dasharray="3 2">'
                f'<title>mounting-hole keep-out (rows '
                f'{cl[0]}-{cl[-1]}): no straddling parts, no ravine '
                f'crossings</title></rect>')
            cy = (px.y(cl[0]) + px.y(cl[-1])) / 2
            add(f'<circle cx="{rx}" cy="{cy}" r="5.5" fill="#e8e4d8" '
                f'stroke="#8a8a8a" stroke-width="1.5"/>')
            add(f'<line x1="{rx-3.6}" y1="{cy}" x2="{rx+3.6}" y2="{cy}" '
                f'stroke="#8a8a8a" stroke-width="1.4"/>')

    # hole grid + row numbers (in the reserved strip inside the rails —
    # gutter/hole wires can never route over them)
    hole_cols = [i for i in range(len(lattice.cols))
                 if lattice.half(i) is not None]
    try:
        nx_l = px.col_x("gutterL") - 4.5 - NUM_W / 2
        nx_r = px.col_x("gutterR") + 4.5 + NUM_W / 2
    except (KeyError, ValueError):
        nx_l = px.x(hole_cols[0]) - 14      # rail-less boards: no gutter
        nx_r = px.x(hole_cols[-1]) + 14
    # Every fifth row carries a full-width guide and a darker, bolder
    # number. Counting holes by eye is the single most error-prone thing
    # about working from a printed sheet, and an unbroken column of
    # identical grey digits gives the eye nothing to land on — the
    # decade lines are what let you find row 43 without counting.
    for r in range(1, lattice.rows + 1):
        y = px.y(r)
        major = (r % 5 == 0)
        if major:
            add(f'<line x1="{round(nx_l + 6, 1)}" y1="{y}" '
                f'x2="{round(nx_r - 6, 1)}" y2="{y}" stroke="#000" '
                f'stroke-opacity="0.055" stroke-width="1"/>')
        cls = "rn maj" if major else "rn"
        add(f'<text x="{round(nx_l, 1)}" y="{y+3}" class="{cls}" '
            f'text-anchor="middle">{r}</text>')
        add(f'<text x="{round(nx_r, 1)}" y="{y+3}" class="{cls}" '
            f'text-anchor="middle">{r}</text>')
        for xi in hole_cols:
            add(f'<circle cx="{px.x(xi)}" cy="{y}" r="1.6" fill="#c9c3b3"/>')

    # Hole-column letters, top AND bottom. These did not exist before,
    # which meant the one axis you address every wire by (`43h`) was the
    # one the sheet never labelled — you counted columns from the ravine
    # every time.
    for xi in hole_cols:
        letter = lattice.name(xi)
        for yy in (HEAD - 6, px.height - 12):
            add(f'<text x="{px.x(xi)}" y="{yy}" class="cn" '
                f'text-anchor="middle">{esc(letter)}</text>')

    # parts
    for part in island.parts:
        pins = part.pins
        rows = [a.row for a in pins.values()]
        y0, y1 = px.y(min(rows)) - CELL / 2 + 2, px.y(max(rows)) + CELL / 2 - 2
        cols = _pin_cols(part)
        if cols:
            xs = [px.col_x(c) for c in cols.values()]
        else:
            xs = [px.col_x(a.hole) for a in pins.values() if a.hole]
        if not xs:
            continue
        add('<g class="lyr-part">')
        x0, x1 = min(xs) - 7, max(xs) + 7
        if part.fp is not None and part.fp.overhang:
            up, down = part.fp.overhang
            oy0 = px.y(max(1, min(rows) - up)) - CELL / 2 + 2
            oy1 = px.y(max(rows) + down) + CELL / 2 - 2
            add(f'<rect x="{x0-2}" y="{oy0}" width="{x1-x0+4}" '
                f'height="{oy1-oy0}" rx="6" fill="#2b3a4a" '
                f'fill-opacity="0.03" stroke="#2b3a4a" '
                f'stroke-opacity="0.35" stroke-dasharray="4 3">'
                f'<title>{esc(part.ref)} physical body (hovers on '
                f'headers — soft keep-out)</title></rect>')
        add(f'<rect x="{x0}" y="{y0}" width="{x1-x0}" height="{y1-y0}" '
            f'rx="5" class="part"><title>{esc(part.ref)}: '
            f'{esc(part.value)}</title></rect>')
        for pn, a in pins.items():
            if cols:
                cx = px.col_x(cols[pn])
            elif a.hole:
                cx = px.col_x(a.hole)
            else:
                continue
            anchor = "start" if cx - x0 < x1 - cx else "end"
            tx = x0 + 4 if anchor == "start" else x1 - 4
            if x1 - x0 < 40:   # narrow part: label beside the pin dot
                add(f'<circle cx="{cx}" cy="{px.y(a.row)}" r="2.4" '
                    f'fill="#333"/>')
            add(f'<text x="{tx}" y="{px.y(a.row)+3}" class="pin" '
                f'text-anchor="{anchor}">{esc(pn)}</text>')
        midx, midy = (x0 + x1) / 2, (y0 + y1) / 2
        add(f'<text x="{midx}" y="{midy}" class="pref" text-anchor="middle" '
            f'transform="rotate(90 {midx} {midy})">{esc(part.ref)}</text>')
        add('</g>')

    # passives — rail-to-rail entry decouplers stagger down from row 1.
    # Value badges are DEFERRED and drawn after the wire pipes so they
    # float above the pipe field like map labels (each in its own group
    # sharing the passive's data-w/data-pk, so toggles + click-isolate
    # still treat badge and body as one).
    rail_rail_n = 0
    badges = []
    deferred_badges = []
    for q in island.passives:
        rows = [e.row for e in (q.a, q.b) if getattr(e, "row", 0)]
        if not rows:
            rail_rail_n += 1
            rows = [1 + rail_rail_n * 1.4]
        pa = _endpoint_px(px, q.a, other_row=rows[0])
        pb = _endpoint_px(px, q.b, other_row=rows[-1])
        if pa is None or pb is None:
            continue
        side_bot = getattr(q, "side", "top") == "bottom"
        pkey = f"p:{island.name}:{q.ref}"
        add(f'<g class="wire lyr-passive" data-w="{esc(pkey)}" '
            f'data-pk="{esc(q.kind)}">')
        col = PASSIVE_COLS.get(q.kind, "#666")
        polar = q.kind == "electrolytic"
        val = " ".join(x for x in (q.value, getattr(q, "rating", ""))
                       if x)
        title = f"{q.ref} {val}" + (" — UNDERSIDE" if side_bot else "")
        if polar:
            title += f" — polarized: + at {_ep_name(q.a)}"
        lyr = "seg-bot" if side_bot else "seg-top"
        dash = (' stroke-dasharray="6 4" stroke-opacity="0.85"'
                if side_bot else "")
        add(f'<line class="{lyr}" x1="{pa[0]}" y1="{pa[1]}" '
            f'x2="{pb[0]}" y2="{pb[1]}" stroke="{col}" '
            f'stroke-width="2.8" stroke-linecap="round"{dash}>'
            f'<title>{esc(title)}</title></line>')
        if polar:
            # +/- glyphs just beyond each terminal, along the body axis
            dx, dy = pb[0] - pa[0], pb[1] - pa[1]
            ln = max((dx * dx + dy * dy) ** 0.5, 1)
            ux, uy = dx / ln, dy / ln
            add(f'<text x="{round(pa[0]-ux*9, 1)}" '
                f'y="{round(pa[1]-uy*9+3, 1)}" class="pol" '
                f'text-anchor="middle" fill="{col}">+</text>')
            add(f'<text x="{round(pb[0]+ux*9, 1)}" '
                f'y="{round(pb[1]+uy*9+3, 1)}" class="pol" '
                f'text-anchor="middle" fill="{col}">&#8722;</text>')
        a_rail = isinstance(q.a, RailAddr)
        b_rail = isinstance(q.b, RailAddr)
        if a_rail:
            _rail_marker(add, px, island, q.a.strip,
                         getattr(q.a, "row", 0) or rows[0], rail_tints)
        if b_rail:
            _rail_marker(add, px, island, q.b.strip,
                         getattr(q.b, "row", 0) or rows[-1], rail_tints)
        # bias the badge toward the hole end so it never buries the rail
        # landing; lift it off the line entirely when the span is short
        t = 0.72 if a_rail and not b_rail else \
            0.28 if b_rail and not a_rail else 0.5
        mx = pa[0] + (pb[0] - pa[0]) * t
        my = pa[1] + (pb[1] - pa[1]) * t
        lab = f"{q.ref} {val}".strip()
        if abs(pb[0] - pa[0]) < len(lab) * 5.2 + 20:
            my -= 11
        my = _badge_y(badges, mx, my, len(lab) * 2.6 + 5, 8)
        bdash = ' stroke-dasharray="3 2"' if side_bot else ""
        deferred_badges.append(
            f'<g class="wire lyr-passive lyr-label" data-w="{esc(pkey)}" '
            f'data-pk="{esc(q.kind)}">'
            f'<rect x="{mx-len(lab)*2.6-3}" y="{my-6.5}" '
            f'width="{len(lab)*5.2+6}" height="13" rx="3" '
            f'fill="#fff" stroke="{col}"{bdash}/>'
            f'<text x="{mx}" y="{my+3.5}" class="pv" text-anchor="middle" '
            f'fill="{col}">{esc(lab)}</text></g>')
        add('</g>')

    _level_layer(add, px, island)

    # routed wires — Flow-Free pipes: fat rounded strokes snapped to
    # the grid, a board-coloured casing under each top run so plain
    # crossings separate visually; underside runs are translucent
    # (KiCad-style), leads fly off thin with an arrow, interlinks land
    # on a ghost bus past the edge. Each wire is grouped so
    # click-to-highlight can isolate it. Edge labels get greedy
    # per-side lanes (sorted by row) so labels never stack.
    wires = [w for w in wires
             if not (w.link and w.link in skip_links)]

    def room_on(side):
        return (label_px or {}).get(side, MARGIN)

    def print_side(side):
        """Where an edge label actually prints. A seam-facing side has
        only the gutter to write in, and a stub of text there is worth
        less than the whole label in this board's own outer margin — so
        it flips. The hole address inside every label (16b vs 16i) keeps
        the two sides' labels apart, and an arrow marks the flip."""
        other = "edgeR" if side == "edgeL" else "edgeL"
        if room_on(side) - 48 >= LEAD_MIN_CH * LEAD_CH_PX:
            return side
        return other if room_on(other) > room_on(side) else side

    wants = []
    for w in wires:
        for e in (w.path[0], w.path[-1]):
            if lattice.is_edge(e.x):
                wants.append((print_side(lattice.name(e.x)),
                              px.y(e.y) + 3, w.key, e.x, e.y))
    label_y = {}
    last = {}
    for side, want, key, ex, ey in sorted(wants):
        ly = max(want, last.get(side, -1e9) + 10.5)
        last[side] = ly
        label_y[(key, ex, ey)] = ly
    co_run = _co_run_keys(wires)
    ghost_rows = {}     # side -> [y px] of interlink ghost-bus landings
    for w in wires:
        colour = WIRE_COLOURS.get(w.colour, WIRE_COLOURS[""])
        jx, jy = _jitter(w.key) if w.key in co_run else (0.0, 0.0)
        fly = w.key.startswith("fly:")
        add(f'<g class="wire" data-w="{esc(w.key)}" '
            f'data-kind="{esc(w.kind)}">')
        pts = _wire_points(px, w)
        title = f"{w.colour or '?'}: {w.label}"
        if w.underside:
            title += " — UNDERSIDE run (solder from beneath, both ends)"
        if fly:
            title += " — flies off-board"
        if w.fail:
            title += " — UNROUTED"
        gx = None
        if fly and w.kind == "interlink" and len(pts) >= 2:
            # extend to the ghost bus just past the board edge: this
            # wire physically continues onto another island
            ex, ey, _l = pts[-1]
            sgn = 1 if ex > pts[0][0] else -1
            gx = round(ex + sgn * GHOST_DX, 1)
            pts[-1] = (gx, ey, TOP)
            ghost_rows.setdefault("R" if sgn > 0 else "L", []).append(ey)
        if fly and w.kind == "lead":
            # a lead is drawn as a LOCAL glyph at its hole (45° arrow),
            # not a line — several leads on one row would otherwise
            # collapse into an unreadable collinear train
            pts = pts[:1]
        i = 0
        while i < len(pts) - 1:
            j = i
            while j + 1 < len(pts) and pts[j + 1][2] == pts[i][2]:
                j += 1
            seg = pts[i:j + 1]
            if len(seg) >= 2:
                d = _pipe_d([(p[0] + jx, p[1] + jy) for p in seg])
                lyr = "seg-bot" if seg[0][2] == BOT else "seg-top"
                caps = ('fill="none" stroke-linecap="round" '
                        'stroke-linejoin="round"')
                if w.fail:
                    add(f'<path class="{lyr}" d="{d}" {caps} '
                        f'stroke="{colour}" stroke-width="3" '
                        f'stroke-dasharray="2 6">'
                        f'<title>{esc(title)}</title></path>')
                elif seg[0][2] == BOT:
                    # KiCad-style: underside pipes are solid but drawn
                    # translucent (CSS, driven by the active-layer
                    # radio) — no casing, so top pipes read as above
                    add(f'<path class="{lyr}" d="{d}" {caps} '
                        f'stroke="{colour}" stroke-width="5.6">'
                        f'<title>{esc(title)}</title></path>')
                elif fly:
                    # interlink fly-over: airborne above everything —
                    # no casing (it does not sit on the surface)
                    add(f'<path class="{lyr}" d="{d}" {caps} '
                        f'stroke="{colour}" stroke-width="4.4" '
                        f'stroke-opacity="0.9">'
                        f'<title>{esc(title)}</title></path>')
                else:
                    # casing: a hairline board-colour outline — wide
                    # enough to read as a break at crossings, narrow
                    # enough not to bite into a parallel neighbour pipe
                    add(f'<path class="{lyr}" d="{d}" {caps} '
                        f'stroke="{BOARD_BG}" stroke-width="7.4"/>')
                    add(f'<path class="{lyr}" d="{d}" {caps} '
                        f'stroke="{colour}" stroke-width="5.6">'
                        f'<title>{esc(title)}</title></path>')
            i = max(j, i + 1)
        if fly and w.kind == "lead" and w.path:
            # the lead glyph: a 45° up-and-away arrow rising from the
            # hole toward its label's margin side
            hx, hy = px.x(w.path[0].x), px.y(w.path[0].y)
            sgn = 1 if w.path[-1].x > w.path[0].x else -1
            tx2, ty2 = round(hx + sgn * 10.5, 1), round(hy - 10.5, 1)
            add(f'<path class="seg-top lead-fly" '
                f'd="M {round(hx+sgn*3.5, 1)},{round(hy-3.5, 1)} '
                f'L {tx2},{ty2} '
                f'M {round(tx2-sgn*5.5, 1)},{ty2} L {tx2},{ty2} '
                f'L {tx2},{round(ty2+5.5, 1)}" fill="none" '
                f'stroke="{colour}" stroke-width="2.6" '
                f'stroke-linecap="round" stroke-linejoin="round">'
                f'<title>{esc(title)}</title></path>')
        ends = [wpt for wpt in (w.path[0], w.path[-1])
                if lattice.is_edge(wpt.x)]
        for e in ends:
            side = lattice.name(e.x)
            pside = print_side(side)
            ly = label_y.get((w.key, e.x, e.y), px.y(e.y) + 3)
            gpad = GHOST_DX + 8 if (gx is not None and pside == side) else 8
            # flipped labels carry an arrow toward the edge the wire
            # really leaves by — added AFTER the fit so truncation can
            # never eat the one glyph that says "this exits the far side"
            mark = "" if pside == side else (
                " →" if pside == "edgeL" else "← ")
            shown, full = _fit_label(
                w.label, room_on(pside) - gpad - 6 - len(mark) * LEAD_CH_PX)
            if not shown:      # no room at all — hover carries it
                continue
            shown = shown + mark if pside == "edgeL" else mark + shown
            tip = f"<title>{esc(full)}</title>" if full else ""
            anchor = ' text-anchor="end"' if pside == "edgeL" else ""
            lx = px.col_x(pside) + (-gpad if pside == "edgeL" else gpad)
            # halo: in a panel a label may sit in the seam gutter, where
            # the stitched wires' lanes run right over it
            halo = ' class="lead lyr-label halo"' if label_px else \
                ' class="lead lyr-label"'
            add(f'<text x="{round(lx, 1)}" y="{ly}"{halo}'
                f'{anchor}>{esc(shown)}{tip}</text>')
        for i_end, c in ((0, w.path[0]), (len(w.path) - 1, w.path[-1])):
            cname = lattice.name(c.x)
            if cname.startswith("rail:"):
                _rail_marker(add, px, island, cname[5:], c.y, rail_tints)
            if fly and i_end > 0:
                # far end is off-board: the interlink puck lands on the
                # ghost bus; a lead just fades out past its arrow
                if gx is not None:
                    add(f'<circle class="seg-top" cx="{gx}" '
                        f'cy="{px.y(c.y)}" r="4.8" fill="{colour}">'
                        f'<title>{esc(title)}</title></circle>')
                continue
            nb = w.path[1] if i_end == 0 else w.path[-2]
            if len(w.path) > 1 and nb.layer == BOT:
                # hollow puck: the run leaves this terminal UNDERNEATH
                add(f'<circle class="seg-top" cx="{px.x(c.x)+jx}" '
                    f'cy="{px.y(c.y)+jy}" r="4.8" fill="{colour}">'
                    f'<title>terminal — the run departs on the '
                    f'UNDERSIDE here — {esc(title)}</title></circle>')
                add(f'<circle class="seg-top" cx="{px.x(c.x)+jx}" '
                    f'cy="{px.y(c.y)+jy}" r="1.9" fill="#fff"/>')
            else:
                # Flow-Free endpoint puck (solid = surface connection)
                add(f'<circle class="seg-top" cx="{px.x(c.x)+jx}" '
                    f'cy="{px.y(c.y)+jy}" r="4.8" fill="{colour}"/>')
        add('</g>')

    # ghost buses: the off-board rails interlinks land on — connected
    # to something real, just not part of THIS island
    for side, ys in sorted(ghost_rows.items()):
        bx = round(px.col_x("edgeR" if side == "R" else "edgeL")
                   + (GHOST_DX if side == "R" else -GHOST_DX), 1)
        y0, y1 = min(ys) - 12, max(ys) + 12
        add(f'<line class="ghost" x1="{bx}" y1="{y0}" x2="{bx}" '
            f'y2="{y1}" stroke="#8a8a8a" stroke-width="3.4" '
            f'stroke-dasharray="7 5" stroke-linecap="round" '
            f'opacity="0.55"><title>off-board bus — these wires '
            f'continue beyond this island</title></line>')

    S.extend(deferred_badges)   # value badges float above the pipes

    # edge labels stack downward when a side is crowded and can end up
    # below the last row — grow the canvas rather than clip them (the
    # board art above keeps the height it was laid out with)
    if label_y:
        px.height = max(px.height, round(max(label_y.values()) + 14, 1))
    return "\n".join(S), px


# ------------------------------------------------------------- panels

def seam_links(panel, routed):
    """Cross-island wires the panel can draw whole: {link: (a, b)} where
    each half is (island, wire). A link qualifies only when BOTH halves
    were routed and both islands sit in this panel — an interlink to a
    board outside the panel keeps its ghost-bus stub."""
    halves = {}
    for name in panel.islands:
        for w in routed[name][0]:
            if w.link and w.path:
                halves.setdefault(w.link, {})[w.link_end] = (name, w)
    return {k: (v["a"], v["b"]) for k, v in sorted(halves.items())
            if "a" in v and "b" in v}


def seam_stack(links):
    """Solder order for a panel's stitched wires: {link: ordinal}, 1 = go
    down first. Sorted by cut length, shortest first.

    A butted pair has no free surface to route a cross-board bundle
    through — both ends of every wire are pinned by a part, so the runs
    fan across each other no matter where they land. Rather than pay for
    that with underside runs or tunnels (both expensive to change on a
    solderable board, and these boards exist to iterate), the bundle is
    built as a STACK on the top face: the shortest wire lies flat, and
    each longer one arches over what is already down, board to board
    through the air. Length order is what makes that buildable — a short
    wire can always be laid under a long one, never the reverse.
    """
    return {link: i + 1 for i, link in enumerate(sorted(
        links, key=lambda k: (seam_length_mm(*(h[1] for h in links[k])),
                              k)))}


def _seam_pipe(pts, colour, under, title, key):
    """One stitched wire: an airborne 3-segment run over the seam.

    Painted in stack order by the caller, so a later (longer) wire draws
    over the ones beneath it; the board-colour casing is what makes that
    read as a crossing rather than a merge."""
    d = _pipe_d(pts)
    lyr = "seg-bot" if under else "seg-top"
    caps = ('fill="none" stroke-linecap="round" '
            'stroke-linejoin="round"')
    out = [f'<g class="wire" data-w="{esc(key)}" data-kind="interlink">']
    if not under:
        out.append(f'<path class="{lyr}" d="{d}" {caps} '
                   f'stroke="{BOARD_BG}" stroke-width="7.4" '
                   f'stroke-opacity="0.75"/>')
    out.append(f'<path class="{lyr}" d="{d}" {caps} stroke="{colour}" '
               f'stroke-width="{4.4 if not under else 5.6}" '
               f'stroke-opacity="0.9"><title>{esc(title)}</title></path>')
    for (cx, cy) in (pts[0], pts[-1]):
        out.append(f'<circle class="seg-top" cx="{cx}" cy="{cy}" '
                   f'r="4.8" fill="{colour}">'
                   f'<title>{esc(title)}</title></circle>')
        if under:   # hollow puck — the run departs UNDERNEATH here
            out.append(f'<circle class="seg-top" cx="{cx}" cy="{cy}" '
                       f'r="1.9" fill="#fff"/>')
    out.append("</g>")
    return "".join(out)


def _seam_geometry(links, maps, offsets):
    """Lane x per link inside the seam gutter, plus the endpoint pixels.

    Each stitched wire runs out to its own board's facing edge, down a
    private vertical lane in the gutter, then in at the far board's
    destination row. Lanes are handed out in source-row order so
    neighbouring wires stay neighbours instead of crossing."""
    geom = {}
    for link, ((an, aw), (bn, bw)) in links.items():
        pxa, pxb = maps[an], maps[bn]
        ax = pxa.x(aw.path[0].x) + offsets[an]
        ay = pxa.y(aw.path[0].y)
        aex = pxa.x(aw.path[-1].x) + offsets[an]
        bx = pxb.x(bw.path[0].x) + offsets[bn]
        by = pxb.y(bw.path[0].y)
        bex = pxb.x(bw.path[-1].x) + offsets[bn]
        geom[link] = [ax, ay, aex, bex, bx, by]
    # gutter lanes: keep clear of both boards' edges, order by source row
    order = sorted(geom, key=lambda k: (geom[k][1], geom[k][5], k))
    for i, link in enumerate(order):
        g = geom[link]
        lo, hi = sorted((g[2], g[3]))
        span = max(hi - lo, 1.0)
        geom[link].append(round(lo + span * (i + 1) / (len(order) + 1), 1))
    return geom


def render_panel(panel, islands, routed, rail_tints):
    """Several physically adjacent boards drawn in ONE svg, in the order
    they sit on the bench, with their shared interlinks crossing the
    seam as real wires."""
    links = seam_links(panel, routed)
    skip = set(links)
    # a seam-facing edge no longer owns a full label margin — it shares
    # the gutter with the board opposite, and only with the sides that
    # actually print labels there (usually just one, which then gets the
    # whole gutter rather than an arbitrary half)
    sides = {n: _edge_label_sides(routed[n][0], routed[n][2], skip)
             for n in panel.islands}
    budgets = {}
    for i, name in enumerate(panel.islands):
        b = {}
        for j, side, facing in ((i - 1, "edgeL", "edgeR"),
                                (i + 1, "edgeR", "edgeL")):
            if 0 <= j < len(panel.islands):
                share = 2 if sides[panel.islands[j]].get(facing) else 1
                b[side] = max((panel.seam - 12) / share, 3 * LEAD_CH_PX)
        budgets[name] = b
    bodies, maps, offsets = {}, {}, {}
    x = 0.0
    height = 0.0
    for name in panel.islands:
        wires, stats, lattice = routed[name]
        body, px = island_body(islands[name], wires, stats, lattice, skip,
                               budgets[name], rail_tints=rail_tints)
        bodies[name], maps[name], offsets[name] = body, px, x
        height = max(height, px.height)
        # the two facing label margins collapse into one shared gutter
        x += px.width - (2 * MARGIN - panel.seam)
    width = round(x + (2 * MARGIN - panel.seam), 1)

    S = [f'<g transform="translate({round(offsets[n], 1)},0)">'
         f'{bodies[n]}</g>' for n in panel.islands]
    geom = _seam_geometry(links, maps, offsets)
    stack = seam_stack(links)
    # paint shortest-first: the drawing then stacks exactly the way the
    # bench builds it, each longer wire arching over the ones already down
    for link in sorted(links, key=lambda k: stack[k]):
        (an, aw), (bn, bw) = links[link]
        ax, ay, aex, bex, bx, by, lane = geom[link]
        pts = [(ax, ay), (aex, ay), (lane, ay),
               (lane, by), (bex, by), (bx, by)]
        pts = [p for i, p in enumerate(pts) if i == 0 or p != pts[i - 1]]
        title = (f"{aw.colour or '?'}: {aw.label} — crosses the seam "
                 f"to {bn} · stack {stack[link]}/{len(links)}")
        if aw.underside or bw.underside:
            title += " — UNDERSIDE run (solder from beneath, both ends)"
        S.append(_seam_pipe(
            pts, WIRE_COLOURS.get(aw.colour, WIRE_COLOURS[""]),
            aw.underside or bw.underside, title, aw.key))
    return (f'<svg viewBox="0 0 {width} {round(height, 1)}" '
            f'width="{width}" height="{round(height, 1)}" '
            f'xmlns="http://www.w3.org/2000/svg">' + "\n".join(S)
            + "</svg>"), links


def _route_text(wire, lattice):
    """Work-instruction summary for the kitting route column."""
    if wire.underside:
        return "UNDERSIDE — solder from beneath at both holes"
    if wire.key.startswith("fly:"):
        # destination island is already in the wire label — keep short
        return ("off-board fly" if wire.kind == "interlink"
                else "flies up and away ↗")
    return "flat"


def seam_length_mm(a_wire, b_wire):
    """Cut length for a stitched cross-board wire: both routed halves,
    the rows they span at the seam, and ONE service slack — the halves
    are two views of one physical wire, not two wires."""
    def run(w):
        return sum(((p.x - q.x) ** 2 + (p.y - q.y) ** 2) ** 0.5
                   for p, q in zip(w.path, w.path[1:]))
    cells = (run(a_wire) + run(b_wire)
             + abs(a_wire.path[0].y - b_wire.path[0].y))
    mm = (cells * MM_PER_CELL + 25
          + (12 if (a_wire.underside or b_wire.underside) else 0))
    return int(round(max(mm, MM_PER_CELL) / 5.0)) * 5


def kitting_table(island, wires, lattice, seam=None):
    """Cut list for one island. In a panel, `seam` ({link: (a, b)}) folds
    each stitched wire into a single row on the island that declares it —
    the far board's stub is the same physical wire, and listing it twice
    would have the bench cut two. Each seam row also carries its stack
    ordinal, which is a work instruction, not a label: lay them in that
    order and every wire goes down over an already-finished bundle."""
    by_link = {}
    for link, (a, b) in (seam or {}).items():
        by_link[a[1].key] = ("a", link, a, b)
        by_link[b[1].key] = ("b", link, a, b)
    stack = seam_stack(seam) if seam else {}

    def order(w):
        """Seam wires float to the top of the table IN STACK ORDER — the
        number is only an instruction if the rows follow it. Everything
        else keeps the colour grouping (you cut by colour, not by
        sequence)."""
        st = by_link.get(w.key)
        if st and st[0] == "a":
            return (0, stack[st[1]], "", 0)
        return (1, 0, w.colour, -len(w.path))

    rows = []
    shown = 0
    for w in sorted(wires, key=order):
        stitched = by_link.get(w.key)
        if stitched and stitched[0] == "b":
            continue          # counted on the declaring island
        profile = _route_text(w, lattice)
        cut = f"{wire_length_mm(w)} mm"
        if stitched:
            _end, link, (_an, aw), (bn, bw) = stitched
            profile = (f"over the seam → {esc(bn)} · "
                       f"stack {stack[link]}/{len(stack)}")
            cut = f"{seam_length_mm(aw, bw)} mm"
        if w.fail:
            profile = "UNROUTED"
        shown += 1
        rows.append(
            f"<tr class='krow' data-w='{esc(w.key)}' "
            f"data-kind='{esc(w.kind)}'>"
            "<td class='cb'></td>"
            f"<td><i style='background:"
            f"{WIRE_COLOURS.get(w.colour, WIRE_COLOURS[''])}'></i>"
            f"{esc(w.colour or '?')}</td><td>{esc(w.kind)}</td>"
            f"<td>{esc(w.label)}</td><td>{profile}</td>"
            f"<td>{cut}</td></tr>")
    for q in island.passives:
        col = PASSIVE_COLS.get(q.kind, "#666")
        if q.kind == "electrolytic":
            route = f"{_ep_name(q.a)}(+) → {_ep_name(q.b)}(−)"
        else:
            route = f"{_ep_name(q.a)} → {_ep_name(q.b)}"
        if getattr(q, "side", "top") == "bottom":
            route += " — UNDERSIDE"
        val = " ".join(x for x in (q.value, getattr(q, "rating", ""))
                       if x)
        rows.append(
            f"<tr class='krow' data-w='p:{esc(q.island)}:{esc(q.ref)}' "
            f"data-kind='{esc(q.kind)}'>"
            "<td class='cb'></td>"
            f"<td><i style='background:{col}'></i>—</td>"
            f"<td>{esc(q.kind)}</td><td>{esc(q.ref)} {esc(val)}</td>"
            f"<td>{route}</td><td>—</td></tr>")
    return (f"<details open><summary>kitting table — {island.name} "
            f"({shown} wires + {len(island.passives)} passives)"
            "</summary><table>"
            "<tr><th>✓</th><th>colour</th><th>kind</th><th>wire</th>"
            "<th>route</th><th>cut</th></tr>" + "".join(rows)
            + "</table></details>")


CSS = """
body{font-family:ui-sans-serif,system-ui;background:#fbfaf7;color:#222;
     margin:16px 208px 16px 16px}
h1{font-size:20px} h2{font-size:15px;margin:26px 0 6px}
h2 .sub{font-weight:400;color:#8a8272}
details.help{font:12px ui-monospace;margin:6px 0 14px;max-width:900px}
details.help summary{cursor:pointer;color:#7a7263}
.island{display:grid;grid-template-columns:minmax(0,1fr) 6px
     minmax(300px,var(--kitw,34%));gap:0 8px;align-items:start;
     margin-bottom:30px}
.island h2{grid-column:1/-1}
.island .board{position:relative}
.island .board svg{max-width:100%;height:auto}
.island .split{cursor:col-resize;width:6px;align-self:stretch;
     background:#e5e0d2;border-radius:3px;touch-action:none}
.island .split:hover,.island .split:active{background:#c9b98a}
.zreset{position:absolute;top:8px;right:8px;display:none;
     font:11px ui-monospace;padding:2px 8px;border:1px solid #c9c2ad;
     border-radius:6px;background:#fffdf5;cursor:pointer}
.board.zoomed .zreset{display:block}
.board.zoomed svg{cursor:grab}
.board.zoomed svg:active{cursor:grabbing}
.board.zoomed{user-select:none}
.island .kit{position:sticky;top:12px;max-height:calc(100vh - 24px);
     overflow:auto}
.island .kit details{margin:0}
svg{background:#fff;border:1px solid #ddd;border-radius:10px}
svg .ttl{font:700 14px ui-sans-serif}
svg .sub{font:400 11px ui-sans-serif;fill:#888}
svg .rn{font:9px ui-monospace;fill:#7c766a}
svg .rn.maj{font:bold 9.5px ui-monospace;fill:#3f3a31}
svg .cn{font:bold 9.5px ui-monospace;fill:#3f3a31}
svg .rail{font:700 9px ui-monospace}
svg .part{fill:#2b3a4a;fill-opacity:.08;stroke:#2b3a4a;stroke-opacity:.55}
svg .pin{font:8px ui-monospace;fill:#37474f}
svg .pref{font:700 11px ui-sans-serif;fill:#2b3a4a}
svg .pv{font:8.5px ui-monospace}
svg .pol{font:700 11px ui-monospace}
svg .lead{font:9px ui-monospace;fill:#333}
svg .halo{paint-order:stroke;stroke:#fff;stroke-width:3px;
     stroke-linejoin:round}
.leg{font:12px ui-monospace}
.leg span{display:block;margin:2px 0}
.leg i,td i{display:inline-block;width:22px;height:8px;border-radius:3px;
     vertical-align:middle;margin-right:4px}
details{margin:10px 0 24px;font:12px ui-monospace}
table{border-collapse:collapse;margin-top:6px}
td,th{border:1px solid #ddd;padding:2px 8px;text-align:left;
     vertical-align:top}
td:last-child,th:last-child{white-space:nowrap}
.kit th{position:sticky;top:0;background:#f3f1ea;z-index:1}
td.cb{width:16px;height:12px;padding:0}
svg .wire{cursor:pointer}
.krow{cursor:pointer}
body.focus svg .wire:not(.sel){opacity:.08}
body.focus .krow:not(.sel) td{opacity:.35}
.krow.sel td{background:#fff3c4}
.layers{position:fixed;top:0;right:0;bottom:0;width:168px;z-index:5;
  background:#f3f1ea;border-left:1px solid #ddd7c6;padding:12px 14px;
  font:12px ui-monospace;overflow-y:auto}
.layers b{display:block;margin:12px 0 4px;font:700 10.5px ui-sans-serif;
  text-transform:uppercase;letter-spacing:.07em;color:#7a7263}
.layers b:first-child{margin-top:0}
.layers label{display:block;margin:3px 0;cursor:pointer;user-select:none;
  white-space:nowrap}
.layers input{vertical-align:-2px;margin-right:5px}
body.hide-part svg .lyr-part{display:none}
body.hide-res svg .lyr-passive[data-pk="resistor"]{display:none}
body.hide-res .krow[data-kind="resistor"]{display:none}
body.hide-cap svg .lyr-passive[data-pk="ceramic"],
body.hide-cap svg .lyr-passive[data-pk="electrolytic"]{display:none}
body.hide-cap .krow[data-kind="ceramic"],
body.hide-cap .krow[data-kind="electrolytic"]{display:none}
body.hide-jumper svg .wire[data-kind="jumper"]{display:none}
body.hide-jumper .krow[data-kind="jumper"]{display:none}
body.hide-lead svg .wire[data-kind="lead"]{display:none}
body.hide-lead .krow[data-kind="lead"]{display:none}
body.hide-interlink svg .wire[data-kind="interlink"]{display:none}
body.hide-interlink .krow[data-kind="interlink"]{display:none}
body.hide-top svg .seg-top{display:none}
body.hide-bot svg .seg-bot{display:none}
svg .seg-bot{opacity:.4}
body.act-bot svg .seg-bot{opacity:1}
body.act-bot svg .seg-top{opacity:.18}
body.hide-label svg .lyr-label{display:none}
body.hide-label svg .pin{display:none}
@media (max-width:1150px){
  body{margin:16px}
  .layers{position:static;width:auto;border-left:none;
    border-bottom:1px solid #ddd7c6}
  .layers label,.layers .leg span{display:inline-block;margin-right:12px}
  .island{grid-template-columns:1fr}
  .island .split{display:none}
  .island .kit{position:static;max-height:none}
}
@media print{
  body{background:#fff;margin:0}
  svg{border:none;break-inside:avoid}
  .island{display:block;break-before:page}
  .island .split,.zreset{display:none}
  .island .kit{position:static;max-height:none;overflow:visible}
  details{break-inside:avoid}
  .layers{display:none}
}
"""

JS = """
<script>
(function () {
  document.querySelectorAll('.layers input[data-h]').forEach(function (cb) {
    cb.addEventListener('change', function () {
      document.body.classList.toggle('hide-' + cb.dataset.h, !cb.checked);
    });
  });
  document.querySelectorAll('.layers input[name="actlyr"]').forEach(
    function (rb) {
      rb.addEventListener('change', function () {
        document.body.classList.toggle('act-bot', rb.value === 'bot');
      });
    });
  // pane resizer: drag the divider to trade board width for kitting
  // width — the board SVG rescales so it always stays fully visible
  document.querySelectorAll('.island .split').forEach(function (sp) {
    sp.addEventListener('pointerdown', function (e) {
      e.preventDefault();
      sp.setPointerCapture(e.pointerId);
      const isl = sp.closest('.island');
      function mv(ev) {
        const r = isl.getBoundingClientRect();
        const w = Math.min(Math.max(r.right - ev.clientX - 4, 260),
                           r.width - 320);
        isl.style.setProperty('--kitw', w + 'px');
      }
      function up() {
        sp.removeEventListener('pointermove', mv);
        sp.removeEventListener('pointerup', up);
      }
      sp.addEventListener('pointermove', mv);
      sp.addEventListener('pointerup', up);
    });
  });
  // board zoom: pinch / ctrl+scroll over a board zooms THAT board only
  // — never the page. Chrome/Firefox deliver pinch as ctrl+wheel;
  // Safari delivers it as gesture* events, so both are captured (this
  // is what used to let the page itself zoom). While zoomed, the board
  // owns the wheel outright — scroll pans it, and the page scrolls
  // only when the cursor is off the board or zoom is reset.
  document.querySelectorAll('.island .board').forEach(function (bd) {
    const svg = bd.querySelector('svg');
    if (!svg) return;
    const vb0 = svg.getAttribute('viewBox').split(' ').map(Number);
    let sc = 1, vx = vb0[0], vy = vb0[1];
    const btn = document.createElement('button');
    btn.className = 'zreset';
    btn.textContent = '\\u27f2 reset zoom';
    bd.appendChild(btn);
    function apply() {
      const w = vb0[2] / sc, h = vb0[3] / sc;
      vx = Math.min(Math.max(vx, vb0[0]), vb0[0] + vb0[2] - w);
      vy = Math.min(Math.max(vy, vb0[1]), vb0[1] + vb0[3] - h);
      svg.setAttribute('viewBox', vx + ' ' + vy + ' ' + w + ' ' + h);
      bd.classList.toggle('zoomed', sc > 1.001);
    }
    function zoomAt(cx, cy, factor) {
      const r = svg.getBoundingClientRect();
      const fx = (cx - r.left) / r.width, fy = (cy - r.top) / r.height;
      const ax = vx + fx * vb0[2] / sc, ay = vy + fy * vb0[3] / sc;
      sc = Math.min(Math.max(factor, 1), 10);
      vx = ax - fx * vb0[2] / sc;
      vy = ay - fy * vb0[3] / sc;
      apply();
    }
    btn.addEventListener('click', function () {
      sc = 1; vx = vb0[0]; vy = vb0[1]; apply();
    });
    bd.addEventListener('wheel', function (e) {
      // Chromium (Brave/Chrome) latches a scroll gesture after the
      // FIRST uncancelled wheel event: the rest of the stream arrives
      // non-cancelable and preventDefault is silently ignored. So a
      // zoomed board must cancel EVERY event — scroll-chaining at the
      // pan clamp handed whole gestures (momentum included) to the
      // page, which read as "can't pan vertically" in Brave. To scroll
      // the page, move the cursor off the board or reset the zoom.
      if (!e.cancelable) return;   // gesture already owned by the page
      if (e.ctrlKey || e.metaKey) {
        e.preventDefault();
        zoomAt(e.clientX, e.clientY, sc * Math.exp(-e.deltaY * 0.01));
        return;
      }
      if (sc <= 1.001) return;   // not zoomed — the page scrolls
      e.preventDefault();
      const r = svg.getBoundingClientRect();
      vx += e.deltaX * (vb0[2] / sc) / r.width;
      vy += e.deltaY * (vb0[3] / sc) / r.height;
      apply();   // clamps vx/vy to the board bounds
    }, {passive: false});
    let gsc = 1;   // Safari trackpad pinch
    bd.addEventListener('gesturestart', function (e) {
      e.preventDefault(); gsc = sc;
    });
    bd.addEventListener('gesturechange', function (e) {
      e.preventDefault(); zoomAt(e.clientX, e.clientY, gsc * e.scale);
    });
    bd.addEventListener('gestureend', function (e) { e.preventDefault(); });
    // drag-to-pan while zoomed; a real drag suppresses the
    // click-to-isolate that would otherwise fire on release.
    // Pointer capture is taken only once the pointer has actually
    // MOVED past the threshold, never on pointerdown: capturing to the
    // board retargets the following click away from the wire that was
    // under the cursor, so a zoomed board would swallow every
    // click-to-isolate (.board is not .wire, so the handler read it as
    // "clicked the background" and cleared the selection). The board is
    // user-select:none while zoomed, so no preventDefault is needed to
    // stop a drag selecting text.
    let drag = null, moved = false;
    bd.addEventListener('pointerdown', function (e) {
      if (sc <= 1.001 || e.button !== 0) return;
      drag = {x: e.clientX, y: e.clientY, id: e.pointerId, cap: false};
      moved = false;
    });
    bd.addEventListener('pointermove', function (e) {
      if (!drag) return;
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      if (!moved) {
        if (Math.abs(dx) + Math.abs(dy) <= 3) return;   // still a click
        moved = true;
        try { bd.setPointerCapture(drag.id); drag.cap = true; } catch (_) {}
      }
      e.preventDefault();
      const r = svg.getBoundingClientRect();
      vx -= dx * (vb0[2] / sc) / r.width;
      vy -= dy * (vb0[3] / sc) / r.height;
      drag.x = e.clientX;
      drag.y = e.clientY;
      apply();
    });
    bd.addEventListener('pointerup', function () {
      if (drag && drag.cap) {
        try { bd.releasePointerCapture(drag.id); } catch (_) {}
      }
      drag = null;
    });
    bd.addEventListener('click', function (e) {
      if (moved) { e.stopPropagation(); moved = false; }
    }, true);
  });
  let last = null;
  document.addEventListener('click', function (e) {
    const g = e.target.closest('.wire, .krow');
    const key = g ? g.getAttribute('data-w') : null;
    document.querySelectorAll('.sel').forEach(
      el => el.classList.remove('sel'));
    if (key && key !== last) {
      document.querySelectorAll('.wire, .krow').forEach(el => {
        if (el.getAttribute('data-w') === key) el.classList.add('sel');
      });
      document.body.classList.add('focus');
      last = key;
    } else {
      document.body.classList.remove('focus');
      last = null;
    }
  });
})();
</script>
"""


def _section(title, board_svg, kit_html):
    return (f'<section class="island"><h2>{title}</h2>'
            f'<div class="board">{board_svg}</div>'
            '<div class="split" title="drag to resize"></div>'
            f'<div class="kit">{kit_html}</div></section>')


def render_html(islands, design=None, rules=None, panels=(), colours=None,
                title=None):
    # A project's own rail names are supplied via colours.yaml `rail_tints:`,
    # merged over the three near-universal defaults so a caller only has to
    # override what it actually renames.
    rail_tints = dict(DEFAULT_RAIL_TINTS,
                      **((colours or {}).get("rail_tints") or {}))
    heading = title or "Breadboard layout — routed build sheet"
    routed = route_design(islands, design, rules, panels)
    legend = "".join(
        f'<span><i style="background:{WIRE_COLOURS[c]}"></i>{c}</span>'
        for c in ("RED", "BLK", "YEL", "BLU", "GRN", "WHT", "ORG", "PUR",
                  "GRY", "BRN"))
    blocks = []
    total_under = 0
    total_seam = 0
    # panels first — the boards that sit together on the bench read as
    # one build sheet; then whatever islands stand alone
    for panel in panels or ():
        svg, links = render_panel(panel, islands, routed, rail_tints)
        total_seam += len(links)
        kit = []
        for name in panel.islands:
            wires, stats, lattice = routed[name]
            total_under += stats.underside
            kit.append(kitting_table(islands[name], wires, lattice, links))
        note = f' <span class="sub">— {esc(panel.note)}</span>' \
            if panel.note else ""
        blocks.append(_section(
            f'{esc(panel.name)}{note} <span class="sub">'
            f'[{esc(" | ".join(panel.islands))}]</span>',
            svg, "".join(kit)))
    grouped = {n for p in (panels or ()) for n in p.islands}
    for name in sorted(islands):
        if name in grouped:
            continue
        wires, stats, lattice = routed[name]
        total_under += stats.underside
        blocks.append(_section(
            esc(name),
            render_island(islands[name], wires, stats, lattice, rail_tints),
            kitting_table(islands[name], wires, lattice)))
    law = ("full pipe = focused layer · translucent pipe = the other "
           "layer (KiCad-style; the focus radio flips which, the "
           "front/back checkboxes hide) · ⊙ = dive to the underside, "
           "○ = surface back to top (through a free half-row) · hollow "
           "puck = that terminal's run departs UNDERNEATH · pipes snap "
           "to the grid; a crossing pipe carries a board-colour casing "
           "· solid bar across a rail band = that end solders to THAT "
           "strip · hover any wire/part for its note · layout quality: "
           f"{total_under} underside run(s) total")
    if total_seam:
        law += (f" · {total_seam} wire(s) cross a seam between butted "
                "boards: those are drawn whole — out to the edge, down a "
                "lane in the seam gutter, in to the real hole on the "
                "neighbour — and kitted ONCE, on the board that declares "
                "them (arrangement: layout.yaml) · seam wires are a "
                "STACK, not a route: solder them in the kitting table's "
                "stack order (shortest first), each arching over the "
                "bundle already down, and cut each long enough to clear "
                "it rather than pulling it taut")
    law += (" · click a wire or kitting row to isolate it; click again "
            "to release · pinch / ctrl+scroll over a board to zoom just "
            "that board; while zoomed, scroll or drag to pan — move the "
            "cursor off the board (or ⟲ reset) to scroll the page "
            "· drag the divider between board and kitting table to "
            "resize the panes")
    show = "".join(
        f'<label><input type="checkbox" checked data-h="{h}">{t}</label>'
        for h, t in (("part", "components"), ("res", "resistors"),
                     ("cap", "capacitors"),
                     ("jumper", "jumpers"), ("lead", "leads"),
                     ("interlink", "interlinks"),
                     ("top", "front (top runs)"),
                     ("bot", "back (underside)"), ("label", "labels")))
    # KiCad-style right-hand dock: active-layer focus on top, then the
    # visibility checkboxes, then the wire-colour legend
    dock = ('<div class="layers"><b>focus</b>'
            '<label><input type="radio" name="actlyr" value="top" '
            'checked>front</label>'
            '<label><input type="radio" name="actlyr" value="bot">'
            'back</label>'
            f'<b>show</b>{show}'
            f'<b>wire colours</b><div class="leg">{legend}</div></div>')
    return (f'<meta charset="utf-8">\n<style>{CSS}</style>\n'
            f"<h1>{esc(heading)}</h1>\n"
            "<!-- GENERATED by: bbnet render — do not edit by hand -->\n"
            f"{dock}\n"
            '<details class="help"><summary>how to read this sheet'
            f"</summary><p>{law}</p></details>\n"
            + "\n".join(blocks) + JS)
