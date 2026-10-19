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
import re
import zlib

from bbnet.geometry import HoleAddr, RailAddr
from bbnet.model import body_cover, dip_right_col, face_toward, pin_hole
from bbnet.router import BOT, TOP, route_design

# px geometry
CELL = 15        # px per row
HOLE_DX = 15     # px per hole column
RAIL_W = 12
RAV_W = 22
EDGE_W = 10
GAP = 6
NUM_W = 13       # reserved row-number strip just inside the rails —
                 # it is why the row numbers are painted last
MARGIN = 235     # label margins either side

# --------------------------------------------- header and footer bands
#
# Everything outside the hole grid — the island title, the rail names,
# the built-in end-jumper pads and their caption, the hole-column
# letters — lives in a band above row 1 and another below the last row.
# Both bands were badly over-subscribed: four lines of furniture inside
# 12px, so "3V3" and "GND" printed as one word and the end-jumper
# caption ran straight across the column letters f-j.
#
# Each line owns its own row now. The rail names additionally alternate
# between two sub-lines, because a rail strip is 12px wide, its
# neighbour's centre is 18px away, and a name like "5V_DIGITAL" is far
# wider than either — no single line can hold a pair side by side.
HEAD = 70                    # band above row 1
FOOT = 62                    # band below the last row

HDR_TITLE = HEAD - 56        # island name, outside the casing
HDR_BOARD = HEAD - 50        # casing rect, top edge
HDR_RAIL = (HEAD - 40, HEAD - 29)     # strip names, alternating
HDR_PADS = HEAD - 17         # end-jumper pads; caption baseline is +3
HDR_COLS = HEAD - 5          # hole-column letters

FTR_COLS = -42               # all four are relative to px.height
FTR_PADS = -30
FTR_BOARD = -22              # casing rect, bottom edge
FTR_RAIL = (-16, -5)         # net names, alternating, below the casing

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
        self.height = HEAD + lattice.rows * CELL + FOOT

    def x(self, xi):
        return round(self.cx[xi], 1)

    def y(self, row):
        return round(HEAD + (row - 0.5) * CELL, 1)

    def col_x(self, name):
        return self.x(self.lat.x_of(name))


# ---------------------------------------------------------- view modes
#
# The sheet has four views of the same board, switched from the dock:
#
#   names   every leg labelled with its pin name (VIN, SDA, C/B/E) - the
#           default, and what prints
#   numbers the part's own pin numbers (1..N in footprint order; a
#           transistor's legs counted in its `legs:` datasheet order)
#   holes   the breadboard hole each leg stands in (`4d`), plus both
#           ends of every passive
#   nets    every node strip and rail tinted by the net it carries, so a
#           whole 3V3 row reads red, not just the wire into it; legs are
#           labelled with their net. Click a strip to light that net up
#           across the whole sheet
#
# A label carries all its variants as tspans and CSS shows one, so the
# switch is instant and needs no re-render. With no script running (a
# print, a saved copy) the names view is what shows.

VIEWS = ("name", "num", "hole", "net")
NET_PALETTE = ("#1f77b4", "#2ca02c", "#9467bd", "#ff7f0e", "#17becf",
               "#8c564b", "#e377c2", "#bcbd22", "#3949ab", "#00897b",
               "#c0ca33", "#6d4c41")


class NetView:
    """What the view modes need from the derived design: the net on a
    node, the net on a pin, and a stable colour per net."""

    def __init__(self, design, rail_tints):
        self.design = design
        self.tints = rail_tints

    def key_net(self, key):
        nid = self.design.nid_of_key.get(key)
        return None if nid is None else self.design.nets[nid]

    def pin_net(self, ref, pin):
        nid = self.design.pin_nid.get((ref, str(pin)))
        return None if nid is None else self.design.nets[nid]

    def colour(self, net):
        if net.name in self.tints:
            return self.tints[net.name]
        return NET_PALETTE[zlib.crc32(net.name.encode()) % len(NET_PALETTE)]


def _vspans(variants):
    """tspans for a label's views; `variants` maps a VIEWS entry to its
    text and must hold "name". A view it does not mention shows the
    name."""
    name = variants["name"]
    out = []
    for v in VIEWS:
        txt = variants.get(v, name)
        out.append(f'<tspan class="vn vn-{v}">{esc(txt)}</tspan>')
    return "".join(out)


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
        return px.col_x(f"rail:{ep.strip}"), _rail_y(px, row)
    return None


def _rail_y(px, row):
    """y of the rail hole numbered with `row`: a half-size board's rail
    holes sit a fraction of a pitch below the main grid."""
    off = getattr(px.lat.board, "rail_offset", 0.0)
    return round(px.y(row) + off * CELL, 1)


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


def _level_layer(add, px, island, badges, writable):
    """Risers, link bars and multi-terminal devices, painted low level
    first so higher ones land on top — the same order you build them in.

    Everything above the surface is drawn lifted and leader-lined back
    to its hole, because the one question this view has to answer is
    "what is stacked on what", and a bar drawn flat on its holes answers
    the opposite.

    Returns its deferred badges, and nudges them against `badges` —
    which the passive layer has already written into. Both layers label
    the same board."""
    risers = getattr(island, "risers", ())
    links = getattr(island, "links", ())
    devices = getattr(island, "devices", ())
    if not (risers or links or devices):
        return []
    deferred = []

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
            lab = _stack_label(lk.ref, f"1x{lk.length}")
            hw = len(lab) * 2.6 + 5
            mx = _badge_x(writable, (a[0] + b[0]) / 2, hw)
            my = _badge_y(badges, mx, (a[1] + b[1]) / 2 - 7, hw, 8)
            deferred.append(
                f'<g class="lyr-level lyr-label"'
                f'{_lbl(island.name, "d", lk.ref)}>'
                f'{_badge_svg(mx, my, lab, LINK_COL)}</g>')
        for dv in (x for x in devices if x.level == lv):
            pts = [p for p in (_endpoint_px(px, t.addr)
                               for t in dv.terminals) if p is not None]
            if len(pts) < 2:
                continue
            lifted = [_lift(p, lv) for p in pts]
            face = face_toward(dv)
            if face:
                # the flat face, drawn as a bar alongside the legs on the
                # side it points to (column a is left on the upright sheet)
                off = -8 if face == "a" else 8
                ys = [p[1] for p in lifted]
                fx = round(lifted[0][0] + off, 1)
                add(f'<line class="flatface" x1="{fx}" '
                    f'y1="{round(min(ys) - 4, 1)}" x2="{fx}" '
                    f'y2="{round(max(ys) + 4, 1)}" stroke="{DEVICE_COL}" '
                    f'stroke-width="3" stroke-linecap="round"><title>'
                    f'{esc(dv.ref)}: flat face toward column {face}'
                    '</title></line>')
            path = " ".join(f"{p[0]},{p[1]}" for p in lifted)
            add(f'<polyline points="{path}" fill="none" '
                f'stroke="{DEVICE_COL}" stroke-width="2.4" '
                f'stroke-linejoin="round" stroke-linecap="round">'
                f'<title>{esc(dv.ref)} {esc(dv.kind)} '
                f'{esc(dv.value)}</title></polyline>')
            # Each leg carries its pin name (D/G/S, C/B/E, IN/GND/OUT):
            # three identical dots left the builder guessing which leg
            # of a TO-92 went in which hole. Names longer than three
            # characters are cut to three; the hover title has the rest.
            order = list(dv.legs) or [t.name for t in dv.terminals]
            for t, p in zip(dv.terminals, lifted):
                short = str(t.name)[:3]
                v = {"name": short}
                if t.name in order:
                    v["num"] = str(order.index(t.name) + 1)
                if getattr(t.addr, "hole", None):
                    v["hole"] = f"{t.addr.row}{t.addr.hole}"
                r = 4.6 if len(short) == 1 else 6.4
                add(f'<circle cx="{p[0]}" cy="{p[1]}" r="{r}" '
                    f'fill="#fff" stroke="{DEVICE_COL}" '
                    f'stroke-width="1.4"><title>{esc(dv.ref)}.'
                    f'{esc(t.name)}</title></circle>')
                add(f'<text x="{p[0]}" y="{p[1] + 2.1}" class="dpin '
                    f'lyr-label" text-anchor="middle">{_vspans(v)}'
                    f'<title>{esc(dv.ref)}.{esc(t.name)}</title></text>')
            mid = lifted[len(lifted) // 2]
            lab = _stack_label(dv.ref, dv.value)
            hw = len(lab) * 2.6 + 5
            # Above the legs first; on a crowded cell, beside them
            # (right, then left), then below. Only if every spot is
            # taken does it fall back to the plain vertical nudge.
            xs = [p[0] for p in lifted]
            cands = [(mid[0], mid[1] - 8)]
            cands += [(max(xs) + hw + 9, p[1]) for p in lifted]
            cands += [(min(xs) - hw - 9, p[1]) for p in lifted]
            cands += [(mid[0], max(p[1] for p in lifted) + 14)]
            for cx, cy in cands:
                mx = _badge_x(writable, cx, hw)
                if not _badge_hits(badges, mx, cy, hw, 8):
                    my = cy
                    badges.append((mx, my, hw, 8))
                    break
            else:
                mx = _badge_x(writable, mid[0], hw)
                my = _badge_y(badges, mx, mid[1] - 8, hw, 8)
            deferred.append(
                f'<g class="lyr-level lyr-label"'
                f'{_lbl(island.name, "d", dv.ref)}>'
                f'{_badge_svg(mx, my, lab, DEVICE_COL)}</g>')
        add('</g>')
    add('</g>')
    return deferred


def _badge_y(badges, mx, my, hw, hh):
    """Nudge a value badge vertically until it overlaps no earlier
    badge (crossing passive pairs stack labels otherwise).

    `badges` is island-scoped, not layer-scoped: passives and the level
    stack both write labels onto the same board and so must compete for
    the same space. Keeping a list per layer is how R9's badge and Q2's
    end up on top of each other."""
    for off in (0, -13, 13, -26, 26, -39, 39):
        cand = my + off
        if not any(abs(mx - bx) < hw + bw and abs(cand - by) < hh + bh
                   for (bx, by, bw, bh) in badges):
            badges.append((mx, cand, hw, hh))
            return cand
    badges.append((mx, my, hw, hh))
    return my


def _device_leg_plates(px, island):
    """(x, y, half-w, half-h) of every lettered device leg, so value
    badges placed BEFORE the device layer already steer clear of them."""
    out = []
    for dv in getattr(island, "devices", ()):
        for t in dv.terminals:
            p = _endpoint_px(px, t.addr)
            if p is None:
                continue
            p = _lift(p, dv.level)
            r = 4.6 if len(str(t.name)[:3]) == 1 else 6.4
            out.append((p[0], p[1], r + 1, r + 1))
    return out


def _badge_hits(badges, mx, my, hw, hh):
    """True if a badge plate at (mx, my) would overlap one already placed."""
    return any(abs(mx - bx) < hw + bw and abs(my - by) < hh + bh
               for (bx, by, bw, bh) in badges)


def _badge_x(writable, mx, hw):
    """Slide a badge plate back inside the writable band.

    NUM_W reserves a strip down each side for the row numbers, and the
    numbers paint last — so a badge that drifts into one does not cover
    it, it gets covered BY it, and you lose the value and the row at
    once. The strips are the sheet's coordinate system; nothing else
    gets to sit in them."""
    lo, hi = writable
    if lo + hw > hi - hw:      # board narrower than the badge: centre it
        return (lo + hi) / 2
    return min(max(mx, lo + hw), hi - hw)


def _badge_svg(mx, my, lab, col, dash=""):
    """A value badge: white plate + centred text, sized off the label's
    character count (the sheet is ui-monospace throughout)."""
    return (f'<rect x="{mx-len(lab)*2.6-3}" y="{my-6.5}" '
            f'width="{len(lab)*5.2+6}" height="13" rx="3" '
            f'fill="#fff" stroke="{col}"{dash}/>'
            f'<text x="{mx}" y="{my+3.5}" class="pv" text-anchor="middle" '
            f'fill="{col}">{esc(lab)}</text>')


# A board label has to fit ON the board. Passive values are short by
# nature ("4.7k", "100n"), but a device or link `value:` is free text
# and is routinely a whole sentence — "2N7000 (level-shift buffer for
# the reset line, replaces the flying lead)" is a realistic one.
# Printed at full length and centred on the part, that single string
# crosses the entire board, the row numbers, and every label in the
# margin beyond it.
STACK_LABEL_PX = 104


def _stack_label(ref, value):
    """What a level-stack badge says. The ref always survives — it is
    the thing you look up in the report and the kitting table; the value
    is truncated to fit beside it. Nothing is lost: the untruncated text
    is on the part's own <title>, which is where a long description was
    always going to be read from anyway."""
    value = (value or "").strip()
    room = int(STACK_LABEL_PX / LEAD_CH_PX) - len(ref) - 1
    if not value or room < 4:
        return ref
    if len(value) <= room:
        return f"{ref} {value}"
    # Break at the last word boundary rather than mid-token. A hard cut
    # gives "2N7000 (level-…", which spends half the badge on a
    # fragment of a sentence; breaking gives "2N7000…", which spends it
    # on the part number — the one thing in a device value that is worth
    # reading at arm's length from the board.
    cut = value[:room - 1]
    space = cut.rfind(" ")
    if space > 0:
        cut = cut[:space]
    return f"{ref} {cut.rstrip(' -(,;:')}…"


# ------------------------------------------------------ movable labels
#
# Automatic label placement is greedy and sometimes lands a label on
# something worth seeing. Every label a person would want to nudge
# carries a STABLE id in `data-lbl`, and `<data-dir>/labels.yaml` maps
# ids to a hand-dragged (dx, dy) applied as a translate on that label's
# group - plate, text and halo move together.
#
# The id scheme, chosen to survive unrelated edits (never the router's
# sequence-numbered wire keys, which renumber when a wire is added):
#
#   <island>/p/<ref>         a passive's value badge
#   <island>/d/<ref>         a device's or link bar's badge
#   <island>/d/<ref>.<pin>   a narrow part's outboard pin label
#   <island>/w/<endpoint>    a wire's edge label, keyed by its on-board
#                            endpoint as written in the YAML (`1c`, `40R`,
#                            `rail:5V`)
#
# Two labels that still collide (two leads out of one hole) take `#2`,
# `#3`... in paint order. The island prefix keeps ids unique across a
# panel, where several islands share one svg.

def _lbl(island_name, kind, ref):
    """The data-lbl attribute for one movable label."""
    return f' data-lbl="{esc(f"{island_name}/{kind}/{ref}")}"'


def _wire_anchor(w, lattice):
    """The board endpoint an edge label belongs to: as DECLARED in the
    YAML (`2R` stays `2R` whichever hole the router lands it in, so the
    id survives a neighbour taking that hole). Falls back to the routed
    start hole for a wire built without one."""
    if getattr(w, "anchor", ""):
        return w.anchor
    c = w.path[0]
    name = lattice.name(c.x)
    return f"{name}@{c.y}" if name.startswith("rail:") else f"{c.y}{name}"


_LBL_RE = re.compile(r' data-lbl="([^"]*)"')


def _num(v):
    return f"{round(float(v), 1):g}"


def _finish_labels(labels, offsets):
    """Make every data-lbl id in `labels` unique (`#2` suffixes, paint
    order) and apply any hand-dragged offset as a translate on its group.

    With no offsets the markup is untouched beyond the dedupe, so an
    absent or empty labels.yaml renders byte-identically."""
    seen = {}
    offsets = offsets or {}

    def fix(m):
        base = html.unescape(m.group(1))
        n = seen.get(base, 0) + 1
        seen[base] = n
        lid = base if n == 1 else f"{base}#{n}"
        out = f' data-lbl="{esc(lid)}"'
        off = offsets.get(lid)
        if off and (float(off[0]) or float(off[1])):
            out += f' transform="translate({_num(off[0])},{_num(off[1])})"'
        return out

    return [_LBL_RE.sub(fix, s) for s in labels]


def label_ids(page):
    """Every movable-label id rendered in a page (or svg fragment)."""
    return {html.unescape(m.group(1)) for m in _LBL_RE.finditer(page)}


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
    y = _rail_y(px, row)
    if y != px.y(row):
        # the wire arrives on its row; the hole is a little lower
        add(f'<line x1="{x}" y1="{px.y(row)}" x2="{x}" y2="{y}" '
            f'stroke="{tint}" stroke-width="2"/>')
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


def render_island(island, wires, stats, lattice, rail_tints,
                  label_offsets=None, nets=None):
    """One island, standalone: its body wrapped in its own <svg>."""
    body, labels, px = island_body(island, wires, stats, lattice,
                                   rail_tints=rail_tints,
                                   label_offsets=label_offsets, nets=nets)
    return (f'<svg viewBox="0 0 {px.width} {px.height}" '
            f'width="{px.width}" height="{px.height}" '
            f'xmlns="http://www.w3.org/2000/svg">'
            + body + "\n" + "\n".join(labels) + "</svg>")


# --------------------------------------------------------- island layers
#
# An island is painted as a stack of layers, back to front: casing,
# rails, grid, parts, passives, the level stack, then the routed wires
# on top. Each layer is its own function, so the paint ORDER is legible
# in one screenful (island_body, at the end of this section) instead of
# being an emergent property of a 470-line function, and so a change to
# how a part is drawn cannot silently reach into how a wire is. They
# all share one `add` sink and one PxMap.


def _board_frame(add, px, island, stats):
    """Casing rectangle + the island's title line."""
    bx0 = px.col_x("edgeL") - EDGE_W / 2 + 4
    bx1 = px.col_x("edgeR") + EDGE_W / 2 - 4
    add(f'<rect x="{bx0}" y="{HDR_BOARD}" width="{bx1-bx0}" '
        f'height="{px.height+FTR_BOARD-HDR_BOARD}" rx="8" fill="#f7f5ef" '
        f'stroke="#c8c2b2"/>')
    add(f'<text x="{bx0+6}" y="{HDR_TITLE}" class="ttl">{esc(island.name)}'
        f'  <tspan class="sub">({esc(island.board.name)}) — '
        f'{stats.routed}/{stats.wires} wires routed, '
        f'{stats.underside} underside</tspan></text>')


def _rail_bands(add, px, island, lattice, rail_tints):
    """Rail strips: tinted band, strip name, net name — and the net
    repeated down the band so a zoomed-in view still says which strip is
    which (the top/bottom labels are far away on a 63-row board)."""
    for i, strip in enumerate(island.board.rails):
        net = island.rails.get(strip, "")
        tint = rail_tints.get(net, "#999")
        x = px.col_x(f"rail:{strip}")
        lane = i % 2               # rails come in adjacent +/- pairs
        add(f'<rect x="{x-RAIL_W/2}" y="{HEAD-4}" width="{RAIL_W}" '
            f'height="{lattice.rows*CELL+8}" rx="4" fill="{tint}" '
            f'fill-opacity="0.13" stroke="{tint}" stroke-opacity="0.6"/>')
        # the strip's own holes, so its gaps and its offset from the
        # main rows show on the sheet
        for rr in range(1, lattice.rows + 1):
            if island.board.has_rail_hole(rr):
                add(f'<circle cx="{x}" cy="{_rail_y(px, rr)}" r="1.4" '
                    f'fill="{tint}" fill-opacity="0.55"/>')
        add(f'<text x="{x}" y="{HDR_RAIL[lane]}" class="rail" '
            f'fill="{tint}" text-anchor="middle">{esc(strip)}</text>')
        add(f'<text x="{x}" y="{px.height+FTR_RAIL[lane]}" class="rail" '
            f'fill="{tint}" text-anchor="middle">{esc(net)}</text>')
        for rr in range(12, lattice.rows - 4, 16):
            yy = px.y(rr)
            add(f'<text x="{x}" y="{yy}" class="rail" fill="{tint}" '
                f'fill-opacity="0.55" text-anchor="middle" '
                f'transform="rotate(90 {x} {yy})">{esc(net)}</text>')


def _end_jumper_pads(add, px, island):
    """The board's built-in rail end-jumper pads (top = PWR pair, bottom
    = GND pair).

    The PWR label has three possible states, and only one of them may
    ever claim "one net": if BOTH + strips are declared and their net
    names differ, bridging them shorts two supplies -- warn. If BOTH are
    declared and equal, the model has verified they are one net, and the
    bridge is the intended rail-common tie -- show that affordance. If
    FEWER THAN TWO are declared, the model has verified nothing about
    the missing strip -- it may carry no net at all, or one this design
    just never mentions -- so claiming "one net" would assert a fact the
    design never established; name whichever position is missing
    instead, which is neutral and true either way."""
    if not island.board.rails:
        return
    gnd_bridged = any(
        getattr(j, "offgrid", False) for j in island.jumpers)
    plus_positions = [p for p in ("top+", "bot+") if p in island.rails]
    plus_nets = {island.rails[p] for p in plus_positions}
    rx = px.col_x("ravine")
    for (yy, kind) in ((HDR_PADS, "PWR"), (px.height + FTR_PADS, "GND")):
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


def _ravine_keepouts(add, px, island):
    """Mounting-hole keep-outs (screws in the ravine), drawn as one
    capsule per run of consecutive rows."""
    if not island.board.ravine_keepouts:
        return
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


def _net_layer(add, px, island, lattice, nets):
    """The nets view: every node strip (five holes of one row-half) and
    every rail band tinted by the net it carries. Hidden unless the nets
    view is on; painted under the parts so legs and wires stay readable.
    Each tint carries data-net, which is what click-to-light-a-net keys
    on, so the same net on two boards lights together."""
    if nets is None:
        return
    add('<g class="lyr-net">')
    left = [c for c in lattice.cols if lattice.half(lattice.x_of(c)) == "L"]
    right = [c for c in lattice.cols if lattice.half(lattice.x_of(c)) == "R"]
    for r in range(1, lattice.rows + 1):
        for half, cols in (("L", left), ("R", right)):
            net = nets.key_net(("row", island.name, r, half))
            if net is None:
                continue
            x0 = px.col_x(cols[0]) - HOLE_DX / 2 + 1
            x1 = px.col_x(cols[-1]) + HOLE_DX / 2 - 1
            add(f'<rect class="net" data-net="{net.nid}" x="{x0}" '
                f'y="{px.y(r) - CELL / 2 + 1.5}" width="{x1 - x0}" '
                f'height="{CELL - 3}" rx="3" fill="{nets.colour(net)}">'
                f'<title>{esc(net.name)}</title></rect>')
    for strip in island.board.rails:
        net = nets.key_net(("rail", island.name, strip))
        if net is None:
            continue
        x = px.col_x(f"rail:{strip}")
        add(f'<rect class="net" data-net="{net.nid}" x="{x - RAIL_W / 2}" '
            f'y="{HEAD - 4}" width="{RAIL_W}" '
            f'height="{lattice.rows * CELL + 8}" rx="4" '
            f'fill="{nets.colour(net)}"><title>{esc(net.name)}</title>'
            '</rect>')
    add('</g>')


def _hole_cols(lattice):
    return [i for i in range(len(lattice.cols))
            if lattice.half(i) is not None]


def _number_strip_x(px, lattice):
    """x of the two reserved row-number strips, just inside the rails."""
    try:
        return (px.col_x("gutterL") - 4.5 - NUM_W / 2,
                px.col_x("gutterR") + 4.5 + NUM_W / 2)
    except (KeyError, ValueError):
        cols = _hole_cols(lattice)      # rail-less boards: no gutter
        return px.x(cols[0]) - 14, px.x(cols[-1]) + 14


def _hole_grid(add, px, lattice):
    """The hole dots and a full-width guide every fifth row.

    The decade lines are what let you find row 43 without counting from
    1 — counting holes by eye is the single most error-prone thing about
    working from a printed sheet. The numbers themselves are a separate,
    later layer (see _row_numbers)."""
    hole_cols = _hole_cols(lattice)
    nx_l, nx_r = _number_strip_x(px, lattice)
    for r in range(1, lattice.rows + 1):
        y = px.y(r)
        if r % 5 == 0:
            add(f'<line x1="{round(nx_l + 6, 1)}" y1="{y}" '
                f'x2="{round(nx_r - 6, 1)}" y2="{y}" stroke="#000" '
                f'stroke-opacity="0.055" stroke-width="1"/>')
        for xi in hole_cols:
            add(f'<circle cx="{px.x(xi)}" cy="{y}" r="1.6" fill="#c9c3b3"/>')


def _row_numbers(px, lattice):
    """The row numbers, as deferred text painted ABOVE the wires.

    NUM_W reserves a strip for them inside the rails, but reserving
    board space cannot protect them: every wire bound for an edge has to
    cross that strip to get there, and the wire layer paints after. The
    numbers are the sheet's coordinate system — you read a hole address
    off them — so they go on top, haloed, the way a map draws its labels
    over its roads.

    Every fifth number is darker and bolder: an unbroken column of
    identical grey digits gives the eye nothing to land on."""
    nx_l, nx_r = _number_strip_x(px, lattice)
    out = []
    for r in range(1, lattice.rows + 1):
        y = px.y(r)
        cls = "rn maj halo" if r % 5 == 0 else "rn halo"
        for nx in (nx_l, nx_r):
            out.append(f'<text x="{round(nx, 1)}" y="{y+3}" class="{cls}" '
                       f'text-anchor="middle">{r}</text>')
    return out


def _column_letters(add, px, lattice):
    """Hole-column letters, top AND bottom. These did not exist before,
    which meant the one axis you address every wire by (`43h`) was the
    one the sheet never labelled — you counted columns from the ravine
    every time."""
    for xi in _hole_cols(lattice):
        letter = lattice.name(xi)
        for yy in (HDR_COLS, px.height + FTR_COLS):
            add(f'<text x="{px.x(xi)}" y="{yy}" class="cn" '
                f'text-anchor="middle">{esc(letter)}</text>')


def _net_label(net, room):
    """A net's name cut to `room` characters for a pin label. An unnamed
    net (`N$island:4R`) is only a node id, so it reads as a dash: the
    tint and its hover title already say all there is."""
    if net.name.startswith("N$"):
        return "–"
    if len(net.name) <= room:
        return net.name
    return net.name[:max(room - 1, 1)] + "…"


def _part_pin_variants(part, pn, a, col, nets, room=12):
    """{view: text} for one footprint pin; `room` is how many characters
    the label has before it runs into the one opposite."""
    v = {"name": str(pn)}
    if part.fp is not None and pn in part.fp.pin_names:
        v["num"] = str(part.fp.pin_names.index(pn) + 1)
    if col:
        v["hole"] = f"{a.row}{col}"
    if nets is not None:
        net = nets.pin_net(part.ref, pn)
        if net is not None:
            v["net"] = _net_label(net, room)
    return v


def _parts(add, px, island, nets=None):
    """Footprint parts: the body rect, its soft overhang keep-out, a
    per-pin label, and the ref rotated down the middle.

    Returns the OUTBOARD pin labels as deferred text. A wide part's pin
    labels sit inside its own body, which the router keeps clear, so
    they are safe painted here; a narrow part's have to go outside it,
    onto board that wires do route across — so those go on top."""
    deferred = []
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
        cover = body_cover(part)
        if cover:
            # the module's PCB, lying flat beside its header: drawn as
            # solid board so the holes under it read as gone
            cx = [px.col_x(h) for _r, _hf, h in cover] + [min(xs), max(xs)]
            add(f'<rect x="{min(cx) - 7}" y="{y0}" '
                f'width="{max(cx) - min(cx) + 14}" height="{y1 - y0}" '
                f'rx="4" fill="#2b3a4a" fill-opacity="0.10" '
                f'stroke="#2b3a4a" stroke-opacity="0.45">'
                f'<title>{esc(part.ref)}: PCB lies flat over '
                f'{len({h for _r, _hf, h in cover})} column(s) beside the '
                f'header - those holes are buried</title></rect>')
        add(f'<rect x="{x0}" y="{y0}" width="{x1-x0}" height="{y1-y0}" '
            f'rx="5" class="part"><title>{esc(part.ref)}: '
            f'{esc(part.value)}</title></rect>')
        # A single-column part (a SIL header, a 3-terminal regulator) is
        # 14px wide and a pin name is 20-32px, so there is no "inside"
        # to label into: text placed there lands on the ref rotated down
        # the middle, and X1/X2/X3 came out unreadable. Narrow parts
        # label OUTBOARD instead — away from the ravine, which is the
        # side with board left on it — and take a halo, since out there
        # they cross the hole grid and whatever routes past.
        narrow = x1 - x0 < 40
        # the ravine is to the R half's left and the L half's right, so
        # "away from the ravine" is right for R and left for L
        out_right = narrow and {a.half for a in pins.values()} == {"R"}
        for pn, a in pins.items():
            if cols:
                col = cols[pn]
            elif a.hole:
                col = a.hole
            else:
                continue
            cx = px.col_x(col)
            # a wide part's labels share its body with the row opposite
            room = 7 if narrow else max(int(((x1 - x0) / 2 - 6) / 4.8), 3)
            spans = _vspans(_part_pin_variants(part, pn, a, col, nets, room))
            if narrow:
                add(f'<circle cx="{cx}" cy="{px.y(a.row)}" r="2.4" '
                    f'fill="#333"/>')
                anchor = "start" if out_right else "end"
                tx = x1 + 3 if out_right else x0 - 3
                # keeps lyr-part so the components toggle still hides it
                deferred.append(
                    f'<g class="lyr-part"'
                    f'{_lbl(island.name, "d", f"{part.ref}.{pn}")}>'
                    f'<text x="{tx}" '
                    f'y="{px.y(a.row)+3}" class="pin halo" '
                    f'text-anchor="{anchor}">{spans}</text></g>')
            else:
                anchor = "start" if cx - x0 < x1 - cx else "end"
                tx = x0 + 4 if anchor == "start" else x1 - 4
                add(f'<text x="{tx}" y="{px.y(a.row)+3}" class="pin" '
                    f'text-anchor="{anchor}">{spans}</text>')
        midx, midy = (x0 + x1) / 2, (y0 + y1) / 2
        add(f'<text x="{midx}" y="{midy}" class="pref" text-anchor="middle" '
            f'transform="rotate(90 {midx} {midy})">{esc(part.ref)}</text>')
        add('</g>')
    return deferred


# Diodes and LEDs: `from` is the ANODE, `to` the CATHODE - the band
# end of a diode, the short leg / flat of an LED - the same "from is
# the + end" convention electrolytics use.
BANDED_KINDS = {"diode", "led"}


def _passives(add, px, island, rail_tints, badges, writable):
    """Two-terminal passives. Rail-to-rail entry decouplers stagger down
    from row 1.

    Returns the value badges, which are DEFERRED: the caller emits them
    after the wire pipes so they float above the pipe field like map
    labels. Each badge carries the passive's own data-w/data-pk, so
    layer toggles and click-isolate still treat badge and body as one."""
    rail_rail_n = 0
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
        banded = q.kind in BANDED_KINDS
        val = " ".join(x for x in (q.value, getattr(q, "rating", ""))
                       if x)
        title = f"{q.ref} {val}" + (" — UNDERSIDE" if side_bot else "")
        if polar:
            title += f" — polarized: + at {_ep_name(q.a)}"
        if banded:
            title += (f" — anode at {_ep_name(q.a)}, cathode (band) at "
                      f"{_ep_name(q.b)}")
        lyr = "seg-bot" if side_bot else "seg-top"
        dash = (' stroke-dasharray="6 4" stroke-opacity="0.85"'
                if side_bot else "")
        if getattr(q, "dia", 0):
            # a radial can: draw its real footprint, so the holes it
            # buries read as buried on the build sheet
            r_t = q.dia / 2 / 2.54            # radius in 0.1" pitches
            pitch_x = px.col_x("b") - px.col_x("a")
            add(f'<ellipse class="{lyr}" cx="{(pa[0] + pb[0]) / 2}" '
                f'cy="{(pa[1] + pb[1]) / 2}" rx="{round(r_t * pitch_x, 1)}" '
                f'ry="{round(r_t * CELL, 1)}" fill="{col}" '
                f'fill-opacity="0.18" stroke="{col}" stroke-width="1.4"'
                f'{dash}><title>{esc(title)} - body dia {q.dia:g} mm'
                '</title></ellipse>')
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
        if banded:
            # the cathode band, drawn where it sits on the part: across
            # the body near the `to` end, plus a K just beyond that leg
            dx, dy = pb[0] - pa[0], pb[1] - pa[1]
            ln = max((dx * dx + dy * dy) ** 0.5, 1)
            ux, uy = dx / ln, dy / ln
            bx, by = pb[0] - ux * min(7, ln / 3), pb[1] - uy * min(7, ln / 3)
            nx, ny = -uy * 5, ux * 5
            add(f'<line class="{lyr} band" x1="{round(bx - nx, 1)}" '
                f'y1="{round(by - ny, 1)}" x2="{round(bx + nx, 1)}" '
                f'y2="{round(by + ny, 1)}" stroke="{col}" '
                f'stroke-width="3.2"><title>{esc(title)}</title></line>')
            add(f'<text x="{round(pb[0]+ux*9, 1)}" '
                f'y="{round(pb[1]+uy*9+3, 1)}" class="pol" '
                f'text-anchor="middle" fill="{col}">K</text>')
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
        hw = len(lab) * 2.6 + 5
        mx = _badge_x(writable, mx, hw)
        my = _badge_y(badges, mx, my, hw, 8)
        bdash = ' stroke-dasharray="3 2"' if side_bot else ""
        deferred_badges.append(
            f'<g class="wire lyr-passive lyr-label" data-w="{esc(pkey)}" '
            f'data-pk="{esc(q.kind)}"{_lbl(island.name, "p", q.ref)}>'
            f'{_badge_svg(mx, my, lab, col, bdash)}</g>')
        add('</g>')
        # the holes view names both legs' holes, just beyond each end
        dx, dy = pb[0] - pa[0], pb[1] - pa[1]
        ln = max((dx * dx + dy * dy) ** 0.5, 1)
        ux, uy = dx / ln, dy / ln
        for ep, (ex, ey), sgn in ((q.a, pa, -1), (q.b, pb, 1)):
            if not getattr(ep, "hole", None):
                continue
            deferred_badges.append(
                f'<text x="{round(ex + sgn * ux * 10, 1)}" '
                f'y="{round(ey + sgn * uy * 10 + 3, 1)}" '
                f'class="vo vo-hole pin halo" text-anchor="middle" '
                f'fill="{col}">{esc(_ep_name(ep))}</text>')
    return deferred_badges


# ------------------------------------------------------- the wire layer


def _edge_label_lanes(px, wires, lattice, label_px):
    """Greedy per-side lanes for the edge labels, plus the two side
    policies the wire layer needs.

    Labels are laid out sorted by row and pushed down 10.5px at a time,
    so a crowded side stacks downward instead of overprinting itself."""

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
    return label_y, print_side, room_on


def _pipe_segments(add, w, pts, colour, jx, jy, fly, title):
    """One wire's pipes: each maximal run of same-layer points becomes a
    rounded Flow-Free path."""
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


def _lead_glyph(add, px, w, colour, title):
    """A lead is drawn as a LOCAL glyph at its hole — a 45° up-and-away
    arrow rising toward its label's margin side — not as a line: several
    leads on one row would otherwise collapse into an unreadable
    collinear train."""
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


def _edge_labels(px, w, lattice, label_y, print_side, room_on, gx,
                 label_px, island_name=""):
    """The off-board text for whichever of this wire's ends land on a
    board edge — DEFERRED like every other label.

    On a wide seam these print inside the shared gutter rather than
    flipping to the outer margin, and the panel draws its stitched
    interlinks through that same gutter afterwards. The halo here was
    always a half-measure against that: it survives a thin stroke
    crossing the glyphs, not an opaque 7.4px casing painted on top."""
    out = []
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
        out.append(f'<g{_lbl(island_name, "w", _wire_anchor(w, lattice))}>'
                   f'<text x="{round(lx, 1)}" y="{ly}"{halo}'
                   f'{anchor}>{esc(shown)}{tip}</text></g>')
    return out


def _end_pucks(add, px, island, w, lattice, colour, title, jx, jy, fly, gx,
               rail_tints):
    """Both ends of one wire: a rail marker where it lands on a strip,
    and a Flow-Free puck — solid for a surface connection, hollow when
    the run departs underneath, off on the ghost bus when it flies."""
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


def _ghost_buses(add, px, ghost_rows):
    """The off-board rails interlinks land on — connected to something
    real, just not part of THIS island."""
    for side, ys in sorted(ghost_rows.items()):
        bx = round(px.col_x("edgeR" if side == "R" else "edgeL")
                   + (GHOST_DX if side == "R" else -GHOST_DX), 1)
        y0, y1 = min(ys) - 12, max(ys) + 12
        add(f'<line class="ghost" x1="{bx}" y1="{y0}" x2="{bx}" '
            f'y2="{y1}" stroke="#8a8a8a" stroke-width="3.4" '
            f'stroke-dasharray="7 5" stroke-linecap="round" '
            f'opacity="0.55"><title>off-board bus — these wires '
            f'continue beyond this island</title></line>')


def _wire_layer(add, px, island, wires, lattice, label_px, rail_tints):
    """Every routed run, as Flow-Free pipes: fat rounded strokes snapped
    to the grid, a board-coloured casing under each top run so plain
    crossings separate visually; underside runs are translucent
    (KiCad-style), leads fly off thin with an arrow, interlinks land on
    a ghost bus past the edge. Each wire is one group so click-to-
    highlight can isolate it.

    Returns the edge-label lanes — a crowded side can stack labels
    below the last row and only the caller can grow the canvas — and the
    edge labels themselves, for the caller's label layer."""
    label_y, print_side, room_on = _edge_label_lanes(
        px, wires, lattice, label_px)
    co_run = _co_run_keys(wires)
    edge_labels = []
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
            pts = pts[:1]
        _pipe_segments(add, w, pts, colour, jx, jy, fly, title)
        if fly and w.kind == "lead" and w.path:
            _lead_glyph(add, px, w, colour, title)
        edge_labels += _edge_labels(px, w, lattice, label_y, print_side,
                                    room_on, gx, label_px, island.name)
        _end_pucks(add, px, island, w, lattice, colour, title, jx, jy,
                   fly, gx, rail_tints)
        add('</g>')
    _ghost_buses(add, px, ghost_rows)
    return label_y, edge_labels


def island_body(island, wires, stats, lattice, skip_links=(),
                label_px=None, *, rail_tints, label_offsets=None,
                nets=None):
    """SVG content for one island in its own coordinates, plus its
    PxMap. A panel translates several of these into one <svg>;
    `skip_links` names cross-island links the panel draws whole across
    the seam, so this island must not draw its half of them, and
    `label_px` ({side: px}) narrows the edge labels on a side that now
    shares a seam gutter with the neighbouring board instead of owning
    a full margin. `label_offsets` ({label id: (dx, dy)}, from
    labels.yaml) moves hand-dragged labels; see _finish_labels.

    The body of this function is the island's paint order, back to
    front — read it as the layer stack, and each layer's own function
    for how that layer draws."""
    px = PxMap(lattice)
    S = []
    add = S.append

    _board_frame(add, px, island, stats)
    _rail_bands(add, px, island, lattice, rail_tints)
    _end_jumper_pads(add, px, island)
    _ravine_keepouts(add, px, island)
    _hole_grid(add, px, lattice)
    _net_layer(add, px, island, lattice, nets)
    _column_letters(add, px, lattice)
    # one badge-collision list for the whole island: passives and the
    # level stack label the same board and must not stack on each other
    # lettered device legs are labels too: registered first, so no
    # value badge (a device's own name plate included) lands on one
    badges = _device_leg_plates(px, island)
    nx_l, nx_r = _number_strip_x(px, lattice)
    writable = (nx_l + NUM_W / 2 + 2, nx_r - NUM_W / 2 - 2)
    labels = _parts(add, px, island, nets)
    labels += _passives(add, px, island, rail_tints, badges, writable)
    labels += _level_layer(add, px, island, badges, writable)
    label_y, edge_labels = _wire_layer(
        add, px, island,
        [w for w in wires if not (w.link and w.link in skip_links)],
        lattice, label_px, rail_tints)
    labels += edge_labels
    labels = _finish_labels(labels, label_offsets)

    # The label layer, painted last. Everything here names something —
    # a value, a pin, a row — and the sheet is unusable if a pipe lands
    # on top of it, so labels float over the pipe field the way a map
    # draws its place names over its roads. The layers below defer them
    # here rather than drawing them in place.
    labels += _row_numbers(px, lattice)

    # edge labels stack downward when a side is crowded and can end up
    # below the last row — grow the canvas rather than clip them (the
    # board art above keeps the height it was laid out with)
    if label_y:
        px.height = max(px.height, round(max(label_y.values()) + 14, 1))
    return "\n".join(S), labels, px


# ------------------------------------------------------------- panels

def seam_links(panel, routed):
    """Cross-island wires the panel can draw whole: {link: (a, b)} where
    each half is (island, wire). A link qualifies only when BOTH halves
    were routed and both islands sit in this panel — an interlink to a
    board outside the panel keeps its ghost-bus stub.

    Both halves must also leave by the edges that FACE each other. A
    rail `channel:` sends every half out one long edge, so on a channel
    rail a panel-mate's wire usually leaves away from the seam; it is
    run in the assembly view's channel, and here it keeps its labelled
    stub at each end rather than being drawn back across a board."""
    halves = {}
    for name in panel.islands:
        for w in routed[name][0]:
            if w.link and w.path:
                halves.setdefault(w.link, {})[w.link_end] = (name, w)

    def faces(name, wire, other):
        side = ("L" if wire.path[-1].x <= routed[name][2].x_of("edgeL")
                else "R")
        return side == panel.side_of(name, other)

    return {k: (v["a"], v["b"]) for k, v in sorted(halves.items())
            if "a" in v and "b" in v
            and faces(v["a"][0], v["a"][1], v["b"][0])
            and faces(v["b"][0], v["b"][1], v["a"][0])}


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


def render_panel(panel, islands, routed, rail_tints, label_offsets=None,
                 nets=None):
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
    bodies, labels, maps, offsets = {}, {}, {}, {}
    x = 0.0
    height = 0.0
    for name in panel.islands:
        wires, stats, lattice = routed[name]
        body, labs, px = island_body(islands[name], wires, stats, lattice,
                                     skip, budgets[name],
                                     rail_tints=rail_tints,
                                     label_offsets=label_offsets,
                                     nets=nets)
        bodies[name], labels[name] = body, labs
        maps[name], offsets[name] = px, x
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

    # Each board's label layer goes on LAST, after the seam wires — a
    # stitched interlink is drawn by the panel, after every body, so a
    # label deferred only within its own body still ends up underneath
    # it. Row numbers on the seam-facing rows were exactly this.
    S += [f'<g transform="translate({round(offsets[n], 1)},0)">'
          f'{chr(10).join(labels[n])}</g>' for n in panel.islands]
    return (f'<svg viewBox="0 0 {width} {round(height, 1)}" '
            f'width="{width}" height="{round(height, 1)}" '
            f'xmlns="http://www.w3.org/2000/svg">' + "\n".join(S)
            + "</svg>"), links


# ------------------------------------------------------------ assembly

ASM_LANE = 7         # px between parallel wires in a channel
ASM_CLEAR = 14       # px from the carriers' edge to the first lane
ASM_NAME_H = 22      # band above the top channel for the board names


def island_anchor(name):
    """Fragment id of an island's upright build sheet (its focus view)."""
    return "isl-" + "".join(c if c.isalnum() or c in "-_" else "-"
                             for c in name)


def _asm_exit_side(wire, lattice):
    """Which long edge of the CARRIED board a wire half leaves by:
    'bot' for the column-a edge (upright left), 'top' for the far one.
    A half with no routed path is treated as leaving at the bottom."""
    if not wire.path:
        return "bot"
    return "bot" if wire.path[-1].x <= lattice.x_of("edgeL") else "top"


def _asm_pack(spans):
    """Greedy interval packing: {key: lane}, lane 0 nearest the boards.
    Shortest spans are placed first, so a short hop between neighbours
    hugs the carriers and the long runs ride outside it - the order a
    bundle is laid in, and the order with the fewest crossings."""
    lanes, out = [], {}
    for key, lo, hi in sorted(spans, key=lambda t: (t[2] - t[1], t[0])):
        for i, used in enumerate(lanes):
            if all(hi + 6 < a or lo - 6 > b for a, b in used):
                used.append((lo, hi))
                out[key] = i
                break
        else:
            lanes.append([(lo, hi)])
            out[key] = len(lanes) - 1
    return out, len(lanes)


def render_rail(rail, islands, routed, rail_tints):
    """The ASSEMBLY view of one rail: every board turned 90 degrees
    counter-clockwise the way its carrier holds it (row 1 left, column a
    down), side by side in mounting order, and every wire between two of
    them drawn the way it is run in the box - out over the board's long
    edge, into a channel above or below the rail, along it, and back in.

    The boards here are the same drawing as their upright build sheets,
    turned; labels are left off at this scale. Each board's name links
    to its upright sheet, which is where it is assembled."""
    boards = []
    for name in rail.islands:
        wires, stats, lattice = routed[name]
        body, _labels, px = island_body(islands[name], wires, stats,
                                        lattice, rail_tints=rail_tints)
        ux0 = px.col_x("edgeL") - EDGE_W / 2 + 4
        ux1 = px.col_x("edgeR") + EDGE_W / 2 - 4
        uy0 = HDR_BOARD
        uy1 = HEAD + lattice.rows * CELL + FOOT + FTR_BOARD
        boards.append(dict(name=name, body=body, px=px, lat=lattice,
                           wires=wires, ux0=ux0, ux1=ux1, uy0=uy0,
                           uy1=uy1, w=uy1 - uy0, h=ux1 - ux0))

    # links with both halves on this rail, and halves whose far end is
    # elsewhere (another rail, or no rail) - those get a labelled stub
    halves, stubs = {}, []
    for i, b in enumerate(boards):
        for w in b["wires"]:
            if w.kind != "interlink" or not w.link:
                continue
            halves.setdefault(w.link, {})[w.link_end] = (i, w)
    links = {}
    for k, v in sorted(halves.items()):
        if "a" in v and "b" in v:
            links[k] = (v["a"], v["b"])
        else:
            stubs.extend(v.values())

    tallest = max(b["h"] for b in boards)
    # first pass: x positions, so channel spans are known
    x = 0.0
    for b in boards:
        b["X0"] = x
        x += b["w"] + rail.gap
    width_core = x - rail.gap

    def at(i, wire):
        b = boards[i]
        if wire.path:
            ux = b["px"].x(wire.path[-1].x)
            uy = b["px"].y(wire.path[-1].y)
        else:
            ux, uy = b["ux0"], b["px"].y(1)
        return b["X0"] + (uy - b["uy0"]), ux   # Y is fixed up below

    def gap_x(i, toward):
        """The gap beside board i on the side facing board `toward`."""
        b = boards[i]
        if toward < i:
            return b["X0"] - rail.gap / 2
        return b["X0"] + b["w"] + rail.gap / 2

    spans = {"top": [], "bot": []}
    plan = {}
    gap_use = {}
    for k, ((ia, wa), (ib, wb)) in links.items():
        sa = _asm_exit_side(wa, boards[ia]["lat"])
        sb = _asm_exit_side(wb, boards[ib]["lat"])
        xa, _ = at(ia, wa)
        xb, _ = at(ib, wb)
        if sa == sb:
            spans[sa].append((k, min(xa, xb), max(xa, xb)))
            plan[k] = (sa, sb, None)
        else:
            gx = gap_x(ib, ia)
            gap_use.setdefault(round(gx, 1), []).append(k)
            spans[sa].append((k + "#a", min(xa, gx), max(xa, gx)))
            spans[sb].append((k + "#b", min(xb, gx), max(xb, gx)))
            plan[k] = (sa, sb, gx)
    lane_top, n_top = _asm_pack(spans["top"])
    lane_bot, n_bot = _asm_pack(spans["bot"])
    # each side reserves stub room only for the stubs that leave by it,
    # so a channel rail carries no empty band on its quiet side
    stub_sides = {_asm_exit_side(w, boards[i]["lat"]) for i, w in stubs}
    top_band = (ASM_CLEAR + max(n_top, 1) * ASM_LANE
                + (96 if "top" in stub_sides else 0))
    bot_band = (ASM_CLEAR + max(n_bot, 1) * ASM_LANE
                + (96 if "bot" in stub_sides else 0))
    mid = ASM_NAME_H + top_band + tallest / 2     # the rail's centreline

    def place(i, wire):
        """Assembly coordinates of a half's exit, plus its edge y."""
        b = boards[i]
        X, ux = at(i, wire)
        Y0 = mid - b["h"] / 2
        Y = Y0 + (b["ux1"] - ux)
        side = _asm_exit_side(wire, b["lat"])
        edge = Y0 if side == "top" else Y0 + b["h"]
        return X, Y, edge, side

    def lane_y(side, lane):
        if side == "top":
            return mid - tallest / 2 - ASM_CLEAR - lane * ASM_LANE
        return mid + tallest / 2 + ASM_CLEAR + lane * ASM_LANE

    height = round(ASM_NAME_H + top_band + tallest + bot_band, 1)
    width = round(width_core, 1)
    S = [f'<line x1="0" y1="{round(mid, 1)}" x2="{width}" '
         f'y2="{round(mid, 1)}" stroke="#b9b4a8" stroke-width="26" '
         'stroke-opacity="0.35"><title>DIN rail</title></line>']
    for i, b in enumerate(boards):
        Y0 = mid - b["h"] / 2
        e = round(b["X0"] - b["uy0"], 1)
        f = round(b["ux1"] + Y0, 1)
        cid = f'asmclip-{island_anchor(rail.name)}-{i}'
        S.append(
            f'<clipPath id="{cid}"><rect x="{b["ux0"]}" y="{b["uy0"]}" '
            f'width="{round(b["h"], 1)}" height="{round(b["w"], 1)}"/>'
            '</clipPath>'
            f'<g class="asm-board" data-island="{esc(b["name"])}" '
            f'transform="matrix(0 -1 1 0 {e} {f})">'
            f'<g clip-path="url(#{cid})">{b["body"]}</g></g>')
        S.append(
            f'<a href="#{island_anchor(b["name"])}">'
            f'<text class="asm-name" x="{round(b["X0"] + b["w"] / 2, 1)}" '
            f'y="{round(ASM_NAME_H - 6, 1)}" text-anchor="middle">'
            f'{esc(b["name"])} ↗<title>open the upright build sheet '
            f'for {esc(b["name"])}</title></text></a>')

    gap_seen = {}
    for k in sorted(links, key=lambda k: k):
        (ia, wa), (ib, wb) = links[k]
        ax, ay, aedge, sa = place(ia, wa)
        bx, by, bedge, sb = place(ib, wb)
        _sa, _sb, gx = plan[k]
        if gx is None:
            ly = lane_y(sa, (lane_top if sa == "top" else lane_bot)[k])
            pts = [(ax, ay), (ax, ly), (bx, ly), (bx, by)]
        else:
            la = lane_y(sa, (lane_top if sa == "top" else
                             lane_bot)[k + "#a"])
            lb = lane_y(sb, (lane_top if sb == "top" else
                             lane_bot)[k + "#b"])
            key = round(gx, 1)
            n = len(gap_use[key])
            j = gap_seen.get(key, 0)
            gap_seen[key] = j + 1
            gxx = gx + (j - (n - 1) / 2) * min(ASM_LANE, rail.gap / max(n, 1))
            pts = [(ax, ay), (ax, la), (gxx, la), (gxx, lb), (bx, lb),
                   (bx, by)]
        pts = [(round(p[0], 1), round(p[1], 1)) for p in pts]
        pts = [p for n_, p in enumerate(pts) if n_ == 0 or p != pts[n_ - 1]]
        under = wa.underside or wb.underside
        title = (f"{wa.colour or '?'}: {wa.label} - "
                 f"{boards[ia]['name']} -> {boards[ib]['name']}, via the "
                 + ("channel " + ("above" if sa == "top" else "below")
                    if gx is None else
                    "channels on both sides, through the gap"))
        S.append(_seam_pipe(pts, WIRE_COLOURS.get(wa.colour,
                                                  WIRE_COLOURS[""]),
                            under, title, wa.key))

    for i, w in stubs:
        X, Y, edge, side = place(i, w)
        far = (w.label or "").split("→")[-1].strip() or "elsewhere"
        sgn = -1 if side == "top" else 1
        end = edge + sgn * (ASM_CLEAR + (n_top if side == "top" else
                                         n_bot) * ASM_LANE + 6)
        colour = WIRE_COLOURS.get(w.colour, WIRE_COLOURS[""])
        S.append(f'<g class="wire" data-w="{esc(w.key)}" '
                 'data-kind="interlink">'
                 f'<path d="M{round(X, 1)},{round(Y, 1)} '
                 f'L{round(X, 1)},{round(end, 1)}" stroke="{colour}" '
                 'stroke-width="3" stroke-dasharray="4 3" fill="none">'
                 f'<title>{esc(w.label)} (off this rail)</title></path>'
                 f'<text class="asm-stub" x="{round(X + 3, 1)}" '
                 f'y="{round(end, 1)}" transform="rotate('
                 f'{-90 if side == "top" else 90} {round(X, 1)} '
                 f'{round(end, 1)})">{esc(_fit_label(far, 88)[0])}</text>'
                 '</g>')
    svg = (f'<svg viewBox="0 0 {width} {height}" width="{width}" '
           f'height="{height}" xmlns="http://www.w3.org/2000/svg">'
           + "\n".join(S) + "</svg>")
    return svg, links, stubs


def assembly_table(rail, links, stubs, routed):
    """What the assembly view adds to the cut lists: the boards in rail
    order (each a link to its upright sheet) and every board-to-board
    wire with the side it runs on."""
    rows = []
    for i, name in enumerate(rail.islands):
        rows.append(f"<tr><td>{i + 1}</td><td><a href=\"#"
                    f"{island_anchor(name)}\">{esc(name)}</a></td></tr>")
    out = ['<table class="kt"><tr><th>#</th><th>board, in rail order '
           '(left to right)</th></tr>' + "".join(rows) + "</table>"]
    if links:
        r = []
        for k, ((ia, wa), (ib, wb)) in links.items():
            sa = _asm_exit_side(wa, routed[rail.islands[ia]][2])
            sb = _asm_exit_side(wb, routed[rail.islands[ib]][2])
            via = ({"top": "above", "bot": "below"}[sa] if sa == sb
                   else "both sides, through a gap")
            r.append(f"<tr class='krow' data-w='{esc(wa.key)}'><td>"
                     f"{esc(wa.colour or '?')}</td><td>"
                     f"{esc(rail.islands[ia])}</td><td>"
                     f"{esc(rail.islands[ib])}</td><td>{via}</td></tr>")
        out.append('<table class="kt"><tr><th>wire</th><th>from</th>'
                   '<th>to</th><th>channel</th></tr>' + "".join(r)
                   + "</table>")
    if stubs:
        out.append(f'<p class="sub">{len(stubs)} wire(s) leave this rail '
                   "(dashed stubs) - they are kitted on their own boards' "
                   "sheets.</p>")
    return "".join(out)


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


def _part_row(island, ref, value, where, kind):
    """One kitting row for a placed part or device: ref, part number and
    every leg's hole. These were missing from the table entirely, so a
    builder reading the cut list never saw the chip it had to fetch."""
    return (f"<tr class='krow' data-w='d:{esc(island.name)}:{esc(ref)}' "
            f"data-kind='part'><td class='cb'></td><td>—</td>"
            f"<td>{esc(kind)}</td><td>{esc(ref)} {esc(value)}</td>"
            f"<td>{esc(where)}</td><td>—</td></tr>")


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
        elif q.kind in BANDED_KINDS:
            route = f"{_ep_name(q.a)}(A) → {_ep_name(q.b)}(K, band)"
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
    # parts and devices: what to fetch and where each leg goes. A module
    # with a long header gets its pin-1 hole instead of every pin.
    n_parts = 0
    for part in island.parts:
        legs = []
        for name, addr in part.pins.items():
            hole = pin_hole(part, addr)
            legs.append(f"{name} {addr.row}{hole}" if hole
                        else f"{name} {_ep_name(addr)}")
        anchor = part.anchor
        where = (" · ".join(legs) if len(legs) <= 8 else
                 f"pin 1 at {_ep_name(anchor) if anchor else legs[0]} "
                 f"({len(legs)} pins)")
        rows.append(_part_row(island, part.ref,
                              part.value or part.part_id or "", where,
                              "part"))
        n_parts += 1
    for dv in getattr(island, "devices", ()):
        legs = sorted(dv.terminals,
                      key=lambda t: (getattr(t.addr, "row", 0),
                                     getattr(t.addr, "hole", "") or ""))
        where = " · ".join(f"{t.name} {_ep_name(t.addr)}" for t in legs)
        face = face_toward(dv)
        if face:
            where += f" — flat face toward column {face}"
        elif dv.legs:
            where += (f" — ⚠ legs not in a straight {'-'.join(dv.legs)} "
                      "line: check the placement")
        rows.append(_part_row(island, dv.ref, dv.value or dv.kind, where,
                              dv.kind))
        n_parts += 1
    extra = f" + {n_parts} parts" if n_parts else ""
    return (f"<details open><summary>kitting table — {island.name} "
            f"({shown} wires + {len(island.passives)} passives{extra})"
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
.island.stacked{grid-template-columns:1fr}
.asm-name{font:600 12px ui-monospace,monospace;fill:#3b5b8c;cursor:pointer}
.asm-name:hover{text-decoration:underline}
.asm-stub{font:8px ui-monospace,monospace;fill:#6b6455}
h2 a.up{font:400 11px ui-monospace,monospace;margin-left:.6em;color:#3b5b8c}
.island.stacked .split{display:none}
.island.stacked .kit{position:static;max-height:none}
.kitpos{font:11px ui-monospace;padding:1px 8px;margin-left:10px;
     border:1px solid #c9c2ad;border-radius:6px;background:#fffdf5;
     cursor:pointer;vertical-align:2px}
svg{background:#fff;border:1px solid #ddd;border-radius:10px}
svg .ttl{font:700 14px ui-sans-serif}
svg .sub{font:400 11px ui-sans-serif;fill:#888}
svg .rn{font:9px ui-monospace;fill:#7c766a}
svg .rn.maj{font:bold 9.5px ui-monospace;fill:#3f3a31}
svg .cn{font:bold 9.5px ui-monospace;fill:#3f3a31}
svg .rail{font:700 9px ui-monospace}
svg .part{fill:#2b3a4a;fill-opacity:.08;stroke:#2b3a4a;stroke-opacity:.55}
svg .pin{font:8px ui-monospace;fill:#37474f}
svg .dpin{font:700 5.6px ui-monospace;fill:#5b3fa8;pointer-events:none}
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
.vn-num,.vn-hole,.vn-net,.vo{display:none}
body.view-num .vn-name,body.view-hole .vn-name,body.view-net .vn-name{
  display:none}
body.view-num .vn-num,body.view-hole .vn-hole,body.view-net .vn-net,
body.view-hole .vo-hole{display:inline}
svg .lyr-net{display:none}
body.view-net svg .lyr-net{display:inline}
svg .lyr-net .net{fill-opacity:.3;cursor:pointer}
body.view-net svg .pin{font-weight:700}
body.netfocus svg .lyr-net .net:not(.nsel){fill-opacity:.07}
body.netfocus svg .lyr-net .net.nsel{fill-opacity:.62}
.netname{font:700 12px ui-monospace;margin:6px 0;word-break:break-all}
svg [data-lbl]{-webkit-user-select:none;user-select:none}
svg [data-lbl].lbl-armed{cursor:move;
  filter:drop-shadow(0 0 1.5px #e91e63) drop-shadow(0 0 1.5px #e91e63)}
.lblpanel{position:fixed;left:12px;bottom:12px;z-index:6;max-width:420px;
  background:#fffdf5;border:1px solid #c9b98a;border-radius:8px;
  padding:8px 10px;font:12px ui-monospace;box-shadow:0 2px 10px #0002}
.lblpanel button{font:11px ui-monospace;margin:0 4px 0 0;padding:2px 8px;
  border:1px solid #c9c2ad;border-radius:6px;background:#fff;cursor:pointer}
.lblpanel .n{font-weight:700;margin-right:8px}
.lblpanel .hint{color:#7a7263;font-size:10.5px;margin-top:4px}
.lblpanel textarea{display:block;width:100%;height:110px;margin-top:6px;
  font:10.5px ui-monospace}
.lblpanel .msg{color:#b0413e;margin-top:4px}
@media (max-width:1150px){
  body{margin:16px}
  .layers{position:static;width:auto;border-left:none;
    border-bottom:1px solid #ddd7c6}
  .layers label,.layers .leg span{display:inline-block;margin-right:12px}
  .island{grid-template-columns:1fr}
  .island .split{display:none}
  .island .kit{position:static;max-height:none}
  .kitpos{display:none}
}
@media print{
  body{background:#fff;margin:0}
  svg{border:none;break-inside:avoid}
  .island{display:block;break-before:page}
  .island .split,.zreset,.kitpos{display:none}
  .island .kit{position:static;max-height:none;overflow:visible}
  details{break-inside:avoid}
  .layers,.lblpanel{display:none}
  .vn-num,.vn-hole,.vn-net,.vo,svg .lyr-net{display:none!important}
  .vn-name{display:inline!important}
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
  // ---- view modes: names / numbers / holes / nets (see VIEWS). The
  // choice is remembered per sheet; clicking a net tint lights that net
  // on every board and names it in the dock.
  (function () {
    const radios = document.querySelectorAll('.layers input[name="view"]');
    if (!radios.length) return;
    const h1 = document.getElementById('top');
    const KEY = 'bbnet-view:' + (h1 ? h1.textContent : '').slice(0, 120);
    const label = document.createElement('div');
    label.className = 'netname';
    radios[radios.length - 1].closest('label').after(label);
    function clearNet() {
      document.querySelectorAll('.nsel').forEach(function (el) {
        el.classList.remove('nsel');
      });
      document.body.classList.remove('netfocus');
      label.textContent = '';
    }
    function setView(v) {
      ['name', 'num', 'hole', 'net'].forEach(function (k) {
        document.body.classList.toggle('view-' + k, k === v);
      });
      radios.forEach(function (rb) { rb.checked = rb.value === v; });
      if (v !== 'net') clearNet();
    }
    radios.forEach(function (rb) {
      rb.addEventListener('change', function () {
        setView(rb.value);
        try { localStorage.setItem(KEY, rb.value); } catch (e) {}
      });
    });
    let v0 = 'name';
    try { v0 = localStorage.getItem(KEY) || 'name'; } catch (e) {}
    setView(v0);
    let lit = null;
    document.addEventListener('click', function (e) {
      if (!document.body.classList.contains('view-net')) return;
      const r = e.target.closest && e.target.closest('svg .lyr-net .net');
      if (!r) return;
      e.stopPropagation();
      const id = r.getAttribute('data-net');
      clearNet();
      if (id === lit) { lit = null; return; }
      lit = id;
      document.querySelectorAll('svg .lyr-net .net[data-net="' + id + '"]')
        .forEach(function (el) { el.classList.add('nsel'); });
      document.body.classList.add('netfocus');
      const t = r.querySelector('title');
      label.textContent = t ? t.textContent : '';
    }, true);
  })();
  document.querySelectorAll('.layers input[name="actlyr"]').forEach(
    function (rb) {
      rb.addEventListener('change', function () {
        document.body.classList.toggle('act-bot', rb.value === 'bot');
      });
    });
  // kitting position: a wide, short board (a multi-board panel) shrinks
  // to a sliver beside the kitting table, so such boards start with the
  // table BELOW and get the full page width. A per-board button flips
  // it; the choice is remembered per board title.
  document.querySelectorAll('.island').forEach(function (isl) {
    const svg = isl.querySelector('.board svg'), h2 = isl.querySelector('h2');
    if (!svg || !h2 || !isl.querySelector('.kit')) return;
    const vb = svg.getAttribute('viewBox').split(' ').map(Number);
    const key = 'bbnet-kitpos:' + h2.textContent.slice(0, 80);
    let below = vb[2] / vb[3] > 2.2;
    try {
      const v = localStorage.getItem(key);
      if (v) below = v === 'below';
    } catch (e) {}
    const btn = document.createElement('button');
    btn.className = 'kitpos';
    function set(b) {
      below = b;
      isl.classList.toggle('stacked', b);
      btn.textContent = b ? 'kitting table beside' : 'kitting table below';
    }
    btn.addEventListener('click', function () {
      set(!below);
      try { localStorage.setItem(key, below ? 'below' : 'beside'); }
      catch (e) {}
    });
    h2.appendChild(btn);
    set(below);
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
  // ---- movable labels. Double-click a label to arm it, drag it clear
  // of whatever it covers; click elsewhere or Esc to let go;
  // alt+double-click puts a label back where the layout put it. Moves
  // are pending (kept in localStorage per sheet) until saved to
  // labels.yaml: `bbnet serve` writes it for you, a file:// page offers
  // the YAML to paste instead.
  (function () {
    const lbls = {};
    document.querySelectorAll('svg [data-lbl]').forEach(function (g) {
      const t = g.transform && g.transform.baseVal.consolidate();
      g._base = t ? [t.matrix.e, t.matrix.f] : [0, 0];
      lbls[g.getAttribute('data-lbl')] = g;
    });
    if (!Object.keys(lbls).length) return;
    const served = !!document.querySelector('meta[name="bbnet-serve"]');
    const h1 = document.getElementById('top');
    const KEY = 'bbnet-labels:' + (h1 ? h1.textContent : '').slice(0, 120);
    let pend = {};    // id -> {dx, dy, reset}
    try { pend = JSON.parse(localStorage.getItem(KEY) || '{}') || {}; }
    catch (e) { pend = {}; }
    Object.keys(pend).forEach(function (id) {
      if (!lbls[id]) delete pend[id];
    });
    function r1(v) { return Math.round(v * 10) / 10; }
    function at(id) {
      const g = lbls[id], p = pend[id] || {dx: 0, dy: 0};
      const b = p.reset ? [0, 0] : g._base;
      return [r1(b[0] + p.dx), r1(b[1] + p.dy)];
    }
    function show(id) {
      const o = at(id);
      if (o[0] || o[1]) {
        lbls[id].setAttribute('transform',
                              'translate(' + o[0] + ',' + o[1] + ')');
      } else {
        lbls[id].removeAttribute('transform');
      }
    }
    function store() {
      try {
        if (Object.keys(pend).length) {
          localStorage.setItem(KEY, JSON.stringify(pend));
        } else {
          localStorage.removeItem(KEY);
        }
      } catch (e) {}
    }
    const panel = document.createElement('div');
    panel.className = 'lblpanel';
    panel.hidden = true;
    panel.innerHTML = '<span class="n"></span>' +
      (served ? '<button data-a="save">Save</button>' : '') +
      '<button data-a="copy">Copy YAML</button>' +
      '<button data-a="discard">Discard</button>' +
      '<div class="hint">' + (served ? 'Save writes labels.yaml and ' +
        'LAYOUT.html, then reloads' : 'paste into labels.yaml beside ' +
        'the island YAML, then bbnet render') +
      ' · alt+double-click a label to reset it</div>' +
      '<textarea readonly hidden></textarea><div class="msg"></div>';
    document.body.appendChild(panel);
    const msg = panel.querySelector('.msg');
    const ta = panel.querySelector('textarea');
    function refresh() {
      const n = Object.keys(pend).length;
      panel.hidden = !n;
      panel.querySelector('.n').textContent =
        n + (n === 1 ? ' label' : ' labels') + ' moved';
      if (!n) { ta.hidden = true; msg.textContent = ''; }
    }
    function yaml() {
      const lines = ['# dragged label offsets, svg px in the upright sheet',
                     'offsets:'];
      Object.keys(lbls).sort().forEach(function (id) {
        const o = at(id);
        if (o[0] || o[1]) {
          lines.push('  ' + JSON.stringify(id) + ': [' + o[0] + ', ' +
                     o[1] + ']');
        }
      });
      if (lines.length === 2) lines[1] = 'offsets: {}';
      return lines.join(String.fromCharCode(10)) + String.fromCharCode(10);
    }
    panel.addEventListener('click', function (e) {
      e.stopPropagation();
      const a = e.target.getAttribute && e.target.getAttribute('data-a');
      if (a === 'discard') {
        pend = {};
        Object.keys(lbls).forEach(show);
        store(); refresh();
      } else if (a === 'copy') {
        const text = yaml();
        ta.value = text; ta.hidden = false; ta.select();
        msg.textContent = '';
        try {
          navigator.clipboard.writeText(text).then(function () {
            msg.textContent = 'copied - it replaces labels.yaml whole';
          }, function () { msg.textContent = 'select and copy the text'; });
        } catch (err) { msg.textContent = 'select and copy the text'; }
      } else if (a === 'save') {
        const body = {offsets: {}, reset: []};
        Object.keys(pend).forEach(function (id) {
          const p = pend[id];
          if (p.reset) body.reset.push(id);
          if (p.dx || p.dy) body.offsets[id] = [r1(p.dx), r1(p.dy)];
        });
        msg.textContent = 'saving...';
        fetch('labels', {method: 'POST',
                         headers: {'Content-Type': 'application/json'},
                         body: JSON.stringify(body)})
          .then(function (r) {
            return r.json().then(function (j) {
              if (!r.ok || !j.ok) throw new Error(j.error || r.status);
              pend = {}; store(); location.reload();
            });
          })
          .catch(function (err) { msg.textContent = 'save failed: ' + err; });
      }
    });
    Object.keys(pend).forEach(show);
    refresh();

    let armed = null, drag = null, held = null, replaying = false;
    function disarm() {
      if (armed) armed.classList.remove('lbl-armed');
      armed = null; drag = null;
    }
    // A label's single click still isolates its wire, but only once a
    // double-click is ruled out: the click is held back briefly and
    // replayed, and the second click of a double-click cancels it.
    document.addEventListener('click', function (e) {
      if (replaying) return;
      const g = e.target.closest && e.target.closest('svg [data-lbl]');
      if (armed && g !== armed) disarm();
      if (!g) return;
      e.stopPropagation();
      if (g === armed) return;          // an armed label's clicks are drags
      if (held) { clearTimeout(held); held = null; }
      if (e.detail >= 2) return;
      const t = e.target;
      held = setTimeout(function () {
        held = null; replaying = true;
        try { t.dispatchEvent(new MouseEvent('click', {bubbles: true})); }
        finally { replaying = false; }
      }, 280);
    }, true);
    document.addEventListener('dblclick', function (e) {
      const g = e.target.closest && e.target.closest('svg [data-lbl]');
      if (!g) return;
      e.preventDefault(); e.stopPropagation();
      if (held) { clearTimeout(held); held = null; }
      const id = g.getAttribute('data-lbl');
      disarm();
      if (e.altKey) {
        if (g._base[0] || g._base[1]) pend[id] = {dx: 0, dy: 0, reset: true};
        else delete pend[id];
        show(id); store(); refresh();
        return;
      }
      armed = g;
      g.classList.add('lbl-armed');
    }, true);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') disarm();
    });
    function local(g, e) {
      // the label's translate lives in its PARENT's user space, so the
      // pointer is mapped there - right at any zoom, pan or panel offset
      const m = g.parentNode.getScreenCTM().inverse();
      return new DOMPoint(e.clientX, e.clientY).matrixTransform(m);
    }
    // capture phase on the document: runs before the board's own
    // pointerdown, so dragging an armed label never starts a pan
    document.addEventListener('pointerdown', function (e) {
      if (!armed || e.button !== 0 || !armed.contains(e.target)) return;
      e.stopPropagation(); e.preventDefault();
      const id = armed.getAttribute('data-lbl');
      const p = pend[id] || {dx: 0, dy: 0};
      drag = {id: id, p0: local(armed, e), dx: p.dx, dy: p.dy,
              reset: !!p.reset, pid: e.pointerId};
      try { armed.setPointerCapture(e.pointerId); } catch (_) {}
    }, true);
    document.addEventListener('pointermove', function (e) {
      if (!drag || e.pointerId !== drag.pid) return;
      e.stopPropagation(); e.preventDefault();
      const q = local(armed, e);
      pend[drag.id] = {dx: r1(drag.dx + q.x - drag.p0.x),
                       dy: r1(drag.dy + q.y - drag.p0.y),
                       reset: drag.reset};
      show(drag.id);
    }, true);
    document.addEventListener('pointerup', function (e) {
      if (!drag || e.pointerId !== drag.pid) return;
      e.stopPropagation();
      try { armed.releasePointerCapture(e.pointerId); } catch (_) {}
      const p = pend[drag.id];
      if (p && !p.dx && !p.dy && !p.reset) delete pend[drag.id];
      drag = null;
      store(); refresh();
    }, true);
  })();
})();
</script>
"""


_UP = (' <a class="up" href="#top" title="back to the assembly view">'
       '↑ assembly</a>')


def _section(title, board_svg, kit_html):
    return (f'<section class="island"><h2>{title}</h2>'
            f'<div class="board">{board_svg}</div>'
            '<div class="split" title="drag to resize"></div>'
            f'<div class="kit">{kit_html}</div></section>')


def render_html(islands, design=None, rules=None, panels=(), colours=None,
                title=None, rails=(), label_offsets=None):
    """The whole build sheet. `label_offsets` ({label id: (dx, dy)}, the
    `offsets:` of labels.yaml) nudges hand-dragged labels; None or {}
    renders exactly as before any label was moved."""
    # A project's own rail names are supplied via colours.yaml `rail_tints:`,
    # merged over the three near-universal defaults so a caller only has to
    # override what it actually renames.
    rail_tints = dict(DEFAULT_RAIL_TINTS,
                      **((colours or {}).get("rail_tints") or {}))
    heading = title or "Breadboard layout — routed build sheet"
    routed = route_design(islands, design, rules, panels, rails)
    nets = NetView(design, rail_tints) if design is not None else None
    legend = "".join(
        f'<span><i style="background:{WIRE_COLOURS[c]}"></i>{c}</span>'
        for c in ("RED", "BLK", "YEL", "BLU", "GRN", "WHT", "ORG", "PUR",
                  "GRY", "BRN"))
    blocks = []
    total_under = 0
    total_seam = 0
    # the assembly view first: the whole box as it is mounted, each board
    # turned the way its carrier holds it; the upright sheets below are
    # where each board is built
    for rail in rails or ():
        svg, links, stubs = render_rail(rail, islands, routed, rail_tints)
        note = f' <span class="sub">- {esc(rail.note)}</span>' \
            if rail.note else ""
        blocks.append(
            f'<section class="island stacked assembly" id="asm-'
            f'{island_anchor(rail.name)}"><h2>Assembly - {esc(rail.name)}'
            f'{note} <span class="sub">(boards as carried: row 1 left, '
            'column a down; click a name for its upright sheet)</span>'
            f'</h2><div class="board">{svg}</div>'
            f'<div class="kit">{assembly_table(rail, links, stubs, routed)}'
            '</div></section>')
    on_rail = bool(rails)
    # panels first — the boards that sit together on the bench read as
    # one build sheet; then whatever islands stand alone
    for panel in panels or ():
        svg, links = render_panel(panel, islands, routed, rail_tints,
                                  label_offsets, nets)
        total_seam += len(links)
        kit = []
        for name in panel.islands:
            wires, stats, lattice = routed[name]
            total_under += stats.underside
            kit.append(kitting_table(islands[name], wires, lattice, links))
        note = f' <span class="sub">— {esc(panel.note)}</span>' \
            if panel.note else ""
        anchors = "".join(f'<span id="{island_anchor(n)}"></span>'
                          for n in panel.islands)
        blocks.append(_section(
            f'{anchors}{esc(panel.name)}{note} <span class="sub">'
            f'[{esc(" | ".join(panel.islands))}]</span>'
            + (_UP if on_rail else ""),
            svg, "".join(kit)))
    grouped = {n for p in (panels or ()) for n in p.islands}
    for name in sorted(islands):
        if name in grouped:
            continue
        wires, stats, lattice = routed[name]
        total_under += stats.underside
        blocks.append(_section(
            f'<span id="{island_anchor(name)}"></span>{esc(name)}'
            + (_UP if on_rail else ""),
            render_island(islands[name], wires, stats, lattice, rail_tints,
                          label_offsets, nets),
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
            "resize the panes · double-click a label to drag it clear of "
            "what it covers (alt+double-click resets it); moves are saved "
            "to labels.yaml")
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
    views = "".join(
        f'<label><input type="radio" name="view" value="{v}"'
        f'{" checked" if v == "name" else ""}>{t}</label>'
        for v, t in (("name", "pin names"), ("num", "pin numbers"),
                     ("hole", "holes"), ("net", "nets")))
    dock = ('<div class="layers"><b>view</b>' + views + '<b>focus</b>'
            '<label><input type="radio" name="actlyr" value="top" '
            'checked>front</label>'
            '<label><input type="radio" name="actlyr" value="bot">'
            'back</label>'
            f'<b>show</b>{show}'
            f'<b>wire colours</b><div class="leg">{legend}</div></div>')
    return (f'<meta charset="utf-8">\n<style>{CSS}</style>\n'
            f"<h1 id=\"top\">{esc(heading)}</h1>\n"
            "<!-- GENERATED by: bbnet render — do not edit by hand -->\n"
            f"{dock}\n"
            '<details class="help"><summary>how to read this sheet'
            f"</summary><p>{law}</p></details>\n"
            + "\n".join(blocks) + JS)
