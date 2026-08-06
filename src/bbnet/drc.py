#!/usr/bin/env python3
"""drc.py — bbnet design-rule checks over a derived Design.

Rules (each encodes a real breadboard bug class):
  B1 occupancy     two legs in one hole; >5 members on a half-row
  B2 rail-short    one net carrying two rail names; split-rail assertions
  B3 signal-short  one net seeded with two pinmap signals (unless tied)
  B4 floating      must-connect pin alone on its node
  B5 requirements  declarative per-pin constraints (rules.yaml) — unmet
                   requirements double as the placement todo/BOM list
  B6 colour        jumper/lead colour vs colours.yaml vocabulary+classes
  B7 pinmap-xcheck  bench vs the external pin-allocation table
  B8 passive-span  a passive placed tighter than its leads can bend
  B9 passive-overlap  two same-face passive bodies crossing or lying
                   along each other — move one to side: bottom
  B10 cap-polarity electrolytics are polarized, from = "+" by
                   convention; flag one reversed across GND and power
  B11 voltage-rating a rated part across a known power net: error when
                   the rail exceeds the rating, warn on thin derating
  B12 in-node detour  a wire landing in one hole of a half-row and then
                   crawling across its own node to leave — the holes are
                   one conductor, so the crawl is wasted wire over holes
                   it never uses (needs routed geometry; waivable)
  B13 half-row landing  an endpoint left as a bare half-row (`39L`) whose
                   only remaining holes are taken or under a part body —
                   pin the hole, and say `underside: true` if the module
                   is sitting on it (needs routed geometry)
  B14 link support  a link bar bonded at a position with no riser
                   reaching its level: the bar cannot land there
  B15 link stray pin  an unclipped bar position the author never listed
                   as connected, sitting on a riser — the pin is bonding
                   a net into the bar that nobody asked it to
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from bbnet.geometry import HOLE_TENTHS, HoleAddr, RailAddr
from bbnet.model import CAP_KINDS, ModelError


@dataclass
class Violation:
    rule: str
    severity: str    # "error" | "warning"
    message: str


@dataclass
class TodoItem:
    pin: str
    instruction: str


# ------------------------------------------------------ value normalization

_R_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([rRkK]|[mM])?")
_C_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([pnuµ]?)[fF]?")


def res_ohms(s):
    """'10k'->1e4, '470'/'470R'->470, '1M'->1e6 (M = mega). None if not R."""
    m = _R_RE.fullmatch(str(s).strip())
    if not m:
        return None
    mult = {"r": 1, "k": 1e3, "m": 1e6, None: 1}[
        m.group(2).lower() if m.group(2) else None]
    return float(m.group(1)) * mult


def cap_farads(s):
    """'100n'/'100nF'->1e-7, '4.7u'->4.7e-6, '22p'->2.2e-11. None if not C."""
    m = _C_RE.fullmatch(str(s).strip())
    if not m:
        return None
    exponent_map = {"p": -12, "n": -9, "u": -6, "µ": -6, "": 0}
    exponent = exponent_map[m.group(2)]
    return float(m.group(1) + "e" + str(exponent))


def value_matches(spec, actual, kind):
    if spec is None:
        return True
    if not str(actual).strip():
        return False
    if kind == "resistor":
        a, b = res_ohms(spec), res_ohms(actual)
    elif kind in CAP_KINDS:
        a, b = cap_farads(spec), cap_farads(actual)
    else:
        a = b = None
    if a is not None and b is not None:
        return abs(a - b) <= 1e-9 * max(abs(a), abs(b))
    return str(spec).casefold() == str(actual).casefold()


# ------------------------------------------------- B5 requirements engine

def _edges_of(design, nid):
    for e in design.edges:
        if e.a_nid == nid:
            yield e, e.b_nid
        elif e.b_nid == nid:
            yield e, e.a_nid


def _net_named(design, name, near=None):
    """Unique net named `name`; when `near` (a Net) is given and the bare
    name is globally ambiguous, restrict to nets sharing an island with
    `near` — separate islands legitimately have their own GND/3V3 domains
    (e.g. a standalone supply board not yet wired into the system)."""
    hits = design.nets_named(name)
    if near is not None and len(hits) > 1:
        islands = {k[1] for k in near.keys}
        hits = [n for n in hits if islands & {k[1] for k in n.keys}]
    return hits[0] if len(hits) == 1 else None


def _find_edge(design, net, to_name, kinds, value):
    """Is there a passive of `kinds` between `net` and the net named
    to_name (resolved near `net`), with a matching value? Returns
    (found, reason-if-not)."""
    target = _net_named(design, to_name, near=net)
    if target is None:
        return False, f"no unique net named {to_name!r} on the bench"
    for e, other in _edges_of(design, net.nid):
        if other == target.nid and e.kind in kinds \
                and value_matches(value, e.value, e.kind):
            return True, ""
    return False, "not placed"


def _norm_req(req):
    if isinstance(req, str):
        return req, {}
    if not isinstance(req, dict) or len(req) != 1:
        raise ValueError(
            f"requirement must be a string or single-key mapping, got {req!r}")
    (key, arg), = req.items()
    return key, (arg if isinstance(arg, dict) else {"_": arg})


def check_requirement(design, ref, pin, req):
    """-> (ok, todo_instruction or None). Raises KeyError if pin unplaced."""
    net = design.net_of_pin(ref, pin)
    key, arg = _norm_req(req)
    where = f"{ref}.{pin} net ({net.name})"
    if key == "must-connect":
        ok = (len(net.pins) + len(net.leads) >= 2 or bool(net.rail_seeds)
              or any(True for _ in _edges_of(design, net.nid)))
        return ok, None if ok else f"{where}: wire it up — must-connect"
    if key == "no-power":
        ok = not net.rail_seeds
        return ok, None if ok else None    # violation only; nothing to place
    if key == "logic":
        domain = str(arg.get("_", arg.get("domain", "")))
        bad_rails = [r for r in net.rail_seeds if r != domain]
        if bad_rails:
            return False, None
        for e, other in _edges_of(design, net.nid):
            other_net = design.net_by_id(other)
            if e.kind == "resistor" and other_net.rail_seeds \
                    and domain not in other_net.rail_seeds:
                return False, None
        return True, None
    if key in ("pullup", "pulldown"):
        to = str(arg.get("to", "GND" if key == "pulldown" else ""))
        value = arg.get("value")
        found, _why = _find_edge(design, net, to, {"resistor"}, value)
        desc = f"{value or ''} pull-{'up' if key == 'pullup' else 'down'}" \
               f" → {to}".strip()
        return found, None if found else f"{where}: place {desc}"
    if key == "series":
        to = str(arg["to"])
        value = arg.get("value")
        found, _why = _find_edge(design, net, to, {"resistor"}, value)
        return found, None if found else \
            f"{where}: place series {value or 'resistor'} → {to}"
    if key in ("decouple", "filter-cap"):
        to = str(arg.get("to", "GND"))
        kinds = set(arg.get("kind") or CAP_KINDS)
        value = arg.get("value")
        found, _why = _find_edge(design, net, to, kinds, value)
        desc = "|".join(sorted(kinds)) + (f" {value}" if value else "") \
               + f" cap → {to}"
        return found, None if found else f"{where}: place {desc}"
    return False, None   # unknown vocabulary word — reported by caller


def _pin_reqs(design, rules):
    """Expand rules['pins'] (incl. wildcards) -> [(ref, pin, req)] plus
    warnings for unknown refs/pins."""
    out, warnings = [], []
    placed = {ref: part for isl in design.islands.values()
              for part in isl.parts for ref in [part.ref]}
    for key, reqs in (rules.get("pins") or {}).items():
        ref, _, pin = key.partition(".")
        if ref not in placed:
            warnings.append(Violation(
                "requirements", "warning",
                f"rules.yaml: {key}: ref {ref!r} not on the bench"))
            continue
        pins = list(placed[ref].pins) if pin == "*" else [pin]
        for p in pins:
            if p not in placed[ref].pins:
                warnings.append(Violation(
                    "requirements", "warning",
                    f"rules.yaml: {key}: {ref} has no placed pin {p!r}"))
                continue
            out.extend((ref, p, r) for r in reqs)
    return out, warnings


def rule_requirements(design, rules, colours):
    violations, todos = [], []
    reqs, warnings = _pin_reqs(design, rules)
    violations.extend(warnings)
    for ref, pin, req in reqs:
        try:
            key, _arg = _norm_req(req)
        except ValueError as e:
            violations.append(Violation(
                "requirements", "error", f"rules.yaml {ref}.{pin}: {e}"))
            continue
        if key == "must-connect":
            ok, _todo = check_requirement(design, ref, pin, req)
            if not ok:
                violations.append(Violation(
                    "floating", "error",
                    f"{ref}.{pin} is floating "
                    f"(net {design.net_of_pin(ref, pin).name})"))
            continue
        ok, todo = check_requirement(design, ref, pin, req)
        if not ok:
            violations.append(Violation(
                "requirements", "error",
                f"{ref}.{pin}: unmet {key} requirement {req!r}"))
            if todo:
                todos.append(TodoItem(f"{ref}.{pin}", todo))
    return violations, todos


def rule_occupancy(design, rules, colours):
    out = []
    for hk, occ in sorted(design.hole_members.items()):
        if len(occ) > 1:
            isl, row, half, hole = hk
            out.append(Violation(
                "occupancy", "error",
                f"{isl}:{row}{hole}: {len(occ)} things in one hole "
                f"({', '.join(occ)})"))
    for nk, occ in sorted(design.node_members.items(), key=str):
        if nk[0] == "row" and len(occ) > 5:
            out.append(Violation(
                "occupancy", "error",
                f"{nk[1]}:{nk[2]}{nk[3]}: {len(occ)} members on a 5-hole "
                f"half-row ({', '.join(occ)})"))
    return out


def rule_rails(design, rules, colours):
    out = []
    for net in design.nets:
        if len(net.rail_seeds) > 1:
            out.append(Violation(
                "rail-short", "error",
                f"net {net.name!r} merges rails "
                f"{' + '.join(net.rail_seeds)} — check for a misplaced "
                "jumper"))
    by_name = {}
    for net in design.nets:
        for r in net.rail_seeds:
            by_name.setdefault(r, []).append(net)
    for rname, nets in sorted(by_name.items()):
        if len(nets) > 1:
            out.append(Violation(
                "rail-split", "warning",
                f"rail {rname!r} spans {len(nets)} disconnected strips — "
                "bridge them or rename one"))
    for isl in design.islands.values():
        if isl.board.split_rails and isl.rails and isl.rails_bridged is None:
            out.append(Violation(
                "rail-split", "warning",
                f"{isl.name}: {isl.board.name} rails have a mid-board "
                "break — declare rails_bridged: true|false after checking "
                "the physical bridges"))
        elif isl.board.split_rails and isl.rails \
                and isl.rails_bridged is False:
            out.append(Violation(
                "rail-split", "warning",
                f"{isl.name}: rails_bridged: false — row-level addressing "
                "cannot tell which rail segment a wire lands on; bridge "
                "the rails or split the model into two islands"))
    # seed-short: a net carrying two differently-named seeds of ANY kind
    # (rail, lead, part) is a wiring short or a naming clash. Without this,
    # a 12V lead jumpered to a GND lead passes green on a rail-less board
    # (mini-170 has no rail strips, so rail-short above can never fire).
    # Pure signal-vs-signal conflicts belong to B3; ties: waives here too.
    ties = [frozenset(map(str, t)) for t in (rules.get("ties") or [])]
    for net in design.nets:
        names = sorted({n for n, _src in net.seeds})
        if len(names) < 2:
            continue
        if set(names) <= set(net.signal_seeds):
            continue                      # B3 signal-short owns this net
        if len(net.rail_seeds) > 1:
            continue                      # rail-short above already errored
        if any(frozenset(names) <= t for t in ties):
            continue
        out.append(Violation(
            "seed-short", "error",
            f"net {net.name!r} merges differently-named seeds "
            f"({', '.join(names)}) — short, naming clash, or add a "
            "ties: entry"))
    return out


def rule_signal_short(design, rules, colours):
    ties = [frozenset(map(str, t)) for t in (rules.get("ties") or [])]
    out = []
    for net in design.nets:
        sigs = net.signal_seeds
        if len(sigs) < 2:
            continue
        if any(frozenset(sigs) <= t for t in ties):
            continue
        out.append(Violation(
            "signal-short", "error",
            f"net {net.name!r} carries {len(sigs)} pinmap signals "
            f"({', '.join(sigs)}) — short, or add a ties: entry"))
    return out


def rule_colour(design, rules, colours):
    vocab = set(colours.get("vocabulary") or [])
    classes = [(re.compile(c["match"]), set(c.get("colours") or []))
               for c in (colours.get("classes") or [])]
    out = []

    def check(colour, net_name, what):
        if not colour:
            out.append(Violation("colour", "warning",
                                 f"{what}: missing colour"))
            return
        if vocab and colour not in vocab:
            out.append(Violation(
                "colour", "warning",
                f"{what}: colour {colour!r} not in colours.yaml vocabulary"))
            return
        for rx, allowed in classes:
            if rx.search(net_name):
                if allowed and colour not in allowed:
                    out.append(Violation(
                        "colour", "warning",
                        f"{what}: net {net_name!r} wants "
                        f"{sorted(allowed)}, wire is {colour}"))
                break

    for j, nid in design.jumpers:
        net = design.net_by_id(nid)
        check(j.colour, net.name, f"{j.island}: jumper {j.a}→{j.b}")
    for net in design.nets:
        for w in net.leads:   # leads were resolved onto nets in derive()
            check(w.colour, net.name, f"{w.island}: lead {w.label or w.at}")
    return out


def rule_pinmap_xcheck(design, rules, colours):
    out = []
    pm_pins = {}
    for row in design.signal_rows:
        sig = (row.signal or "").strip()
        pm_pins.setdefault(row.mcu, {})[str(row.pin)] = sig
    for isl in design.islands.values():
        waived = set(isl.bench_only)
        for part in isl.parts:
            if not (part.fp and part.fp.pin_signals):
                continue
            mcu = part.fp.pin_signals.split(":", 1)[1]
            allocated = pm_pins.get(mcu, {})
            for pn in part.pins:
                if f"{part.ref}.{pn}" in waived or pn in part.seeds:
                    continue   # waived, or a seeded power pin (GND/VIN/3V3)
                net = design.net_of_pin(part.ref, pn)
                # "used" = wired to anything beyond its own landing node
                used = (len(net.keys) > 1
                        or len(net.pins) + len(net.leads) >= 2
                        or net.rail_seeds)
                sig = allocated.get(pn, "")
                if used and (not sig or sig.startswith("(")):
                    out.append(Violation(
                        "pinmap-xcheck", "warning",
                        f"{part.ref}.{pn}: bench uses a pin the pin table "
                        f"leaves unallocated ({mcu} pin {pn}) — update the "
                        "pin table or add to bench_only"))
                if sig and not sig.startswith("(") and net.rail_seeds:
                    out.append(Violation(
                        "pinmap-xcheck", "warning",
                        f"{part.ref}.{pn} ({sig}) is tied to rail "
                        f"{'+'.join(net.rail_seeds)} — short? (waive via "
                        "bench_only if intentional)"))
    return out


# minimum body spans for through-hole passives, in 0.1-inch units: an
# axial resistor bends down to "down 1, over 2" (sqrt(5) ~ 2.24) and is
# perfect at 3-4 tenths (rail->col a, or across the ravine e->f); the
# small ceramics bend to one diagonal (sqrt(2) ~ 1.41). Radial
# electrolytics sit on native 0.1" lead spacing — exempt. Rail
# endpoints are exempt too: the builder picks any hole along the
# strip, so the span is theirs to lengthen.
PASSIVE_MIN_SPAN = {"resistor": 2.2, "ceramic": 1.4}


def rule_passive_span(design, rules, colours):
    spans = dict(PASSIVE_MIN_SPAN)
    spans.update(rules.get("passive_min_span") or {})
    waived = set(rules.get("passive_span_waivers") or [])
    out = []
    for isl in design.islands.values():
        for q in isl.passives:
            need = spans.get(q.kind)
            if need is None or f"{isl.name}:{q.ref}" in waived:
                continue
            if not (isinstance(q.a, HoleAddr) and q.a.hole
                    and isinstance(q.b, HoleAddr) and q.b.hole):
                continue
            dr = abs(q.a.row - q.b.row)
            dc = abs(HOLE_TENTHS[q.a.hole] - HOLE_TENTHS[q.b.hole])
            span = (dr * dr + dc * dc) ** 0.5
            if span < need:
                out.append(Violation(
                    "passive-span", "error",
                    f"{isl.name}: {q.ref} {q.value} "
                    f"{q.a.row}{q.a.hole}->{q.b.row}{q.b.hole}: span "
                    f"{span:.2f} (x0.1in) is tighter than a {q.kind}'s "
                    f"leads can bend (min {need}) — go 'down 1 over 2', "
                    "one diagonal, or land a leg on a rail"))
    return out


# nominal x-positions (0.1" units, col a = 0) of the rail strips, for
# passive body geometry: the inner strip of each pair is 4 tenths off
# its hole column ("GND rail to col a is the perfect resistor span").
_RAIL_X = {"top+": -5, "top-": -4, "bot+": 15, "bot-": 16}


def _passive_xy(ep, other):
    """Endpoint -> (x_tenths, row) for body geometry; None if the
    position isn't pinned (a bare half-row reference leaves the hole —
    and so the body line — to the builder; rail-to-rail passives sit
    anywhere along the strips)."""
    if isinstance(ep, HoleAddr):
        if not ep.hole:
            return None
        return (HOLE_TENTHS[ep.hole], ep.row)
    if isinstance(ep, RailAddr):
        if getattr(ep, "row", 0):
            return (_RAIL_X[ep.strip], ep.row)  # pinned at a height
        if isinstance(other, HoleAddr) and other.hole:
            return (_RAIL_X[ep.strip], other.row)   # straight across
    return None


def _segs_touch(p1, p2, p3, p4):
    """True if closed segments [p1,p2] and [p3,p4] share any point."""
    def cross(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    def on(a, b, c):
        return (cross(a, b, c) == 0
                and min(a[0], b[0]) <= c[0] <= max(a[0], b[0])
                and min(a[1], b[1]) <= c[1] <= max(a[1], b[1]))

    d1, d2 = cross(p3, p4, p1), cross(p3, p4, p2)
    d3, d4 = cross(p1, p2, p3), cross(p1, p2, p4)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)) \
            and d1 != 0 and d2 != 0 and d3 != 0 and d4 != 0:
        return True
    return on(p3, p4, p1) or on(p3, p4, p2) \
        or on(p1, p2, p3) or on(p1, p2, p4)


def rule_passive_overlap(design, rules, colours):
    out = []
    for isl in design.islands.values():
        placed = []
        for q in isl.passives:
            a = _passive_xy(q.a, q.b)
            b = _passive_xy(q.b, q.a)
            if a is None or b is None:
                continue        # rail-to-rail: builder places it freely
            side = getattr(q, "side", "top")
            for (pref, pside, pa, pb) in placed:
                if pside == side and _segs_touch(a, b, pa, pb):
                    out.append(Violation(
                        "passive-overlap", "error",
                        f"{isl.name}: {q.ref} and {pref} lie across "
                        f"each other on the {side} face — move one to "
                        "the other side (side: bottom) or re-hole it"))
            placed.append((q.ref, side, a, b))
    return out


# Default net-name -> nominal working voltage. A project on other supply
# names overrides this wholesale via rules.yaml `net_voltages:`; without
# a match B10/B11 cannot judge a part, so an unmatched net is None
# (unknown) rather than 0 (safe-looking).
_DEFAULT_NET_VOLTS = (
    (re.compile(r"^GND$"), 0.0),
    (re.compile(r"^3V3"), 3.3),
    (re.compile(r"^5V"), 5.0),
    (re.compile(r"^12V"), 12.0),
)


_UNSET = object()


def _net_volts_table(rules):
    spec = (rules or {}).get("net_voltages", _UNSET)
    if spec is _UNSET:
        return _DEFAULT_NET_VOLTS    # key absent: built-in table
    if not spec:
        # key present but null/[]/{}  -- an empty vocabulary would leave
        # B10/B11 silently unable to judge every net on the board (the
        # same silent-no-op class the table-driven lookup exists to
        # avoid), so treat it as a probable authoring mistake rather
        # than a deliberate "no rails" declaration. To disable the
        # rules on purpose, omit net_voltages entirely instead.
        raise ModelError(
            "rules.yaml: net_voltages is present but empty (null, [], "
            "or {}) -- omit the key to use the built-in 3V3/5V/12V/GND "
            "table, or list at least one {match, volts} entry")
    return tuple((re.compile(str(e["match"])), float(e["volts"]))
                 for e in spec)


def _net_volts(name, rules=None):
    for rx, v in _net_volts_table(rules):
        if rx.match(name or ""):
            return v
    return None


def _is_power_net(name, rules):
    """Any voltage-known net that is not ground."""
    v = _net_volts(name, rules)
    return v is not None and v > 0.0


def rule_cap_polarity(design, rules, colours):
    """B10: an electrolytic's `from` endpoint is its + terminal (model
    convention, rendered as +/- on the build sheet). One sitting
    reversed across GND and a power net is a vented can waiting for
    power-on."""
    out = []
    for e in design.edges:
        if e.kind != "electrolytic":
            continue
        plus = design.nets[e.a_nid].name
        minus = design.nets[e.b_nid].name
        if _net_volts(plus, rules) == 0.0 and _is_power_net(minus, rules):
            out.append(Violation(
                "cap-polarity", "error",
                f"{e.ref} {e.value}: + terminal (from=) lands on {plus} "
                f"while - lands on {minus} — reversed electrolytic "
                "(convention: from = +; swap from/to)"))
    return out


def rule_voltage_rating(design, rules, colours):
    """B11: a part with a rating: across two voltage-known nets must be
    rated above the working voltage — and electrolytic lifetime wants
    >= 2x derating, so a thin margin (<1.5x) warns."""
    out = []
    for e in design.edges:
        rating = getattr(e, "rating", "")
        if not rating:
            continue
        m = re.match(r"(\d+(?:\.\d+)?)\s*[vV]?$", rating.strip())
        if not m:
            out.append(Violation(
                "voltage-rating", "warning",
                f"{e.ref}: rating {rating!r} not parseable (use '25V')"))
            continue
        rv = float(m.group(1))
        va = _net_volts(design.nets[e.a_nid].name, rules)
        vb = _net_volts(design.nets[e.b_nid].name, rules)
        if va is None or vb is None:
            continue                    # signal net — working V unknown
        work = abs(va - vb)
        if not work:
            continue
        if rv < work:
            out.append(Violation(
                "voltage-rating", "error",
                f"{e.ref} {e.value} rated {rv:g}V sits across {work:g}V "
                f"({design.nets[e.a_nid].name} <-> "
                f"{design.nets[e.b_nid].name}) — over its rating"))
        elif rv < 1.5 * work:
            out.append(Violation(
                "voltage-rating", "warning",
                f"{e.ref} {e.value} rated {rv:g}V across {work:g}V — "
                "thin derating; prefer >= 2x for electrolytic life"))
    return out


def rule_in_node_detour(design, rules, colours, routed=None):
    """B12: a wire that lands in one hole of a half-row and then crawls
    across its own node to leave.

    The five holes of a half-row are ONE conductor, so which hole a wire
    lands in is free choice. Landing in the wrong one buys nothing and
    costs three ways: extra wire, a body laid over holes it never uses
    (awkward to solder into later), and channel cells the neighbours
    wanted. `61h -> 40R` crawling h→i→j just to reach the gutter is the
    canonical shape — solder it at 61j and it leaves straight out.

    Advisory, because the fix is to re-land a wire: on an as-built run
    that means desoldering, and the call is the bench's. Waive a
    deliberate one with `in_node_waivers: ["<island>:<row><hole>"]`.

    Needs routed geometry (the router resolves half-row endpoints to a
    real hole), so it no-ops when `routed` is absent."""
    if not routed:
        return []
    from bbnet import router as _router
    waived = set(rules.get("in_node_waivers") or ())
    out = []
    for iname, (wires, _stats, lat) in sorted(routed.items()):
        for w, _end, row, half, holes in _router.in_node_runs(wires, lat):
            if f"{iname}:{row}{holes[0]}" in waived:
                continue
            # the best landing is the furthest hole along the crawl that
            # nothing else already occupies — a hole with a leg in it is
            # not available no matter how much wire it would save (B1)
            better = [h for h in holes[1:]
                      if not design.hole_members.get((iname, row, half, h))]
            if not better:
                continue
            target = better[-1]
            saved = holes.index(target)
            covered = ", ".join(f"{row}{h}" for h in holes[:saved])
            out.append(Violation(
                "in-node detour", "warning",
                f"{iname}: {w.kind} {w.label.split(' · ')[0]} lands at "
                f"{row}{holes[0]} then runs {saved} hole(s) along its own "
                f"half-row before leaving — land at {row}{target} instead "
                f"(same node, {saved} cell(s) shorter, frees {covered})"))
    return out


def rule_halfrow_landing(design, rules, colours, routed=None):
    """B13: a wire endpoint written as a bare half-row (`39L`) whose only
    remaining holes are compromised.

    `39L` means "any hole in this node", which is electrically true and
    physically not. When R7's leg holds 39a and the IMU click's body
    covers 39b-e, the only landings left are under the module — the wire
    has to be soldered from BENEATH, and the model should say so instead
    of letting the picker choose a hole and draw a wire the bench cannot
    actually run. Pin the hole and add `underside: true`.

    Warning, not error: the wire IS connectable, and which hole plus
    which face is a bench decision. Silent without routed geometry."""
    if not routed:
        return []
    why = {1: ("every free hole is under a part body — solder from "
               "beneath: pin the hole and set underside: true"),
           2: ("every hole in the node already has a leg in it — free "
               "one, or move the wire to another member of this net")}
    out = []
    for iname, (_wires, stats, _lat) in sorted(routed.items()):
        for kind, label, row, half, rank, hole in getattr(
                stats, "landings", ()):
            out.append(Violation(
                "half-row landing", "warning",
                f"{iname}: {kind} {label} lands anywhere in {row}{half} "
                f"and the router picked {row}{hole} — {why[rank]}"))
    return out


def rule_link_support(design, rules, colours):
    """B14: a link bar bonded where it cannot actually land.

    A bar sits on risers. A `connects:` position with no riser reaching
    the bar's level has nothing to plug into, so the netlist claims a
    bond the hardware does not have — the worst kind of model error,
    because everything downstream believes it."""
    out = []
    for iname in sorted(design.islands):
        isl = design.islands[iname]
        sockets = isl.sockets()
        for lk in isl.links:
            for a in lk.connects:
                have = sockets.get((a.row, a.half, a.hole), ())
                if lk.level not in have:
                    at = f"level {lk.level}"
                    got = (f"only {sorted(have)}" if have
                           else "no riser at all")
                    out.append(Violation(
                        "link support", "error",
                        f"{iname}: {lk.ref} bonds at {a.row}{a.hole} "
                        f"({at}) but that hole has {got} — add "
                        f"`risers: [{{at: {a.row}{a.hole}, "
                        f"level: {lk.level}}}]` or move the bar"))
    return out


def rule_link_stray_pin(design, rules, colours):
    """B15: a bar pin bonding a net nobody asked it to.

    Every position on a 1xN bar is the same conductor, and all N pins
    physically exist whether or not the design wanted them. A position
    the author listed under neither `connects:` nor `clipped:` is a
    float — fine, and the common case, because a pin with no riser
    beneath it hangs in free air touching nothing.

    Put a riser under that float, though, and the pin lands: the hole's
    net is now bonded into the bar, silently, by a pin whose only reason
    for existing is that the bar was sold that long. That is the one way
    this system bites, so it is an error. Snip the pin (`clipped:`) or
    say you meant it (`connects:`)."""
    out = []
    for iname in sorted(design.islands):
        isl = design.islands[iname]
        sockets = isl.sockets()
        for lk in isl.links:
            for a in lk.floats():
                if lk.level in sockets.get((a.row, a.half, a.hole), ()):
                    out.append(Violation(
                        "link stray pin", "error",
                        f"{iname}: {lk.ref} covers {a.row}{a.hole} "
                        f"without listing it, and a riser there reaches "
                        f"level {lk.level} — that pin bonds "
                        f"{a.row}{a.hole} into the bar. Add it to "
                        f"`connects:` if you meant it, else `clipped:`"))
    return out


def rule_link_stock(design, rules, colours):
    """B16: a bar cut longer than the stock it is cut from.

    A purchasing fact rather than a wiring fault, hence a warning: the
    design is electrically fine, you just cannot build it out of the bag
    you have."""
    out = []
    for iname in sorted(design.islands):
        isl = design.islands[iname]
        for lk in isl.links:
            if lk.stock and lk.length > lk.stock:
                out.append(Violation(
                    "link stock", "warning",
                    f"{iname}: {lk.ref} spans {lk.length} positions but "
                    f"stock is 1x{lk.stock} — bars cut DOWN, not up; "
                    f"buy longer stock or split the run"))
    return out


def run_all(design, rules, colours, routed=None):
    """Run every rule -> (violations, todos), stable order B1..B16.

    `routed` (island -> (wires, stats, lattice) from router.route_design)
    enables the geometry-dependent rules; without it B12 and B13 are
    skipped and everything else is unaffected."""
    violations, todos = rule_requirements(design, rules, colours)
    for rule in (rule_occupancy, rule_rails, rule_signal_short,
                 rule_colour, rule_pinmap_xcheck, rule_passive_span,
                 rule_passive_overlap, rule_cap_polarity,
                 rule_voltage_rating, rule_link_support,
                 rule_link_stray_pin, rule_link_stock):
        violations.extend(rule(design, rules, colours))
    violations.extend(rule_in_node_detour(design, rules, colours, routed))
    violations.extend(rule_halfrow_landing(design, rules, colours, routed))
    return violations, todos
