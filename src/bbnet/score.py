"""Layout score and circuit fingerprint - the yardstick for a tidy-up.

A layout review (skills/bbnet-layout-review) proposes moves that should
make a build sheet easier to build WITHOUT changing the circuit: turn a
chip 180 degrees, move a part to the other half, send a wire out the
other edge. Two numbers make that checkable rather than a matter of
taste:

- `fingerprint` - a hash of the circuit with every position stripped
  out: which component pins, leads and seeded nets share a net. Moving
  or turning a part, or re-laying a wire, leaves it unchanged; changing
  what is connected to what does not. A tidy-up must keep it.
- `metrics` - what makes a sheet hard to build, counted: wires that
  failed to route, wires that cross through the gap between carriers
  on a rail, wires forced underneath, DRC warnings, how far off-board
  wires fly across their own board to reach its edge, wire count and
  cut length. Lower is tidier, compared in that order (`rank`).
"""
from __future__ import annotations

import hashlib
import json

from bbnet.router import in_node_runs

# two-terminal parts whose ends are NOT interchangeable; any other
# passive may be turned end for end and is still the same circuit
POLAR_KINDS = {"diode", "led", "electrolytic", "tantalum", "other"}

# the comparison order, most important first
RANK = ("route_failures", "gap_crossings", "underside", "warnings",
        "in_node_runs", "fly_cols", "wires", "wire_mm")


def _passive_kinds(islands):
    out = {}
    for name, isl in islands.items():
        for q in isl.passives:
            out[(name, q.ref)] = q.kind
    return out


def fingerprint(design, islands):
    """sha256 of the position-free circuit: one entry per net, the
    sorted set of what sits on it. Wire ends are left out (a wire only
    joins nodes; its holes are layout), and so are net names, which are
    positional for unnamed nets ("N$island:12L"). A non-polar passive's
    ends both read `.~`, so turning it end for end changes nothing."""
    kinds = _passive_kinds(islands)
    nets = []
    for net in design.nets:
        members = set()
        for key in net.keys:
            island = key[1]
            for m in design.node_members.get(key, []):
                if m.startswith("wire→"):
                    continue
                ref, dot, pin = m.partition(".")
                if dot and pin in ("a", "b"):
                    kind = kinds.get((island, ref))
                    if kind is not None and kind not in POLAR_KINDS:
                        m = f"{ref}.~"
                members.add(f"{island}:{m}")
        for seed, _where in net.seeds:
            members.add(f"seed:{seed}")
        if members:
            nets.append(sorted(members))
    nets.sort()
    blob = json.dumps(nets, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _exit(wire, lattice):
    return "L" if wire.path and wire.path[-1].x <= lattice.x_of("edgeL") \
        else "R"


def rail_metrics(rail, routed):
    """Per rail: links with both halves on it, how many cross through a
    carrier gap (halves leaving by opposite long edges), and wires that
    leave the rail altogether."""
    halves = {}
    for name in rail.islands:
        wires, _stats, lattice = routed[name]
        for w in wires:
            if w.kind == "interlink" and w.link:
                halves.setdefault(w.link, {})[w.link_end] = (name, w,
                                                             lattice)
    links = gaps = off = 0
    for v in halves.values():
        if "a" in v and "b" in v:
            links += 1
            (_an, aw, al), (_bn, bw, bl) = v["a"], v["b"]
            if _exit(aw, al) != _exit(bw, bl):
                gaps += 1
        else:
            off += 1
    return {"rail": rail.name, "channel": rail.channel or "",
            "links": links, "gap_crossings": gaps, "off_rail": off}


def island_metrics(name, island, routed, violations):
    from bbnet.render import wire_length_mm
    wires, stats, lattice = routed[name]
    mine = [w for w in wires if w.kind == "jumper"]
    tag = f"{name}:"
    return {
        "island": name,
        "frozen": island.frozen,
        "route_failures": stats.failed,
        "underside": sum(1 for w in mine if w.underside),
        "warnings": sum(1 for v in violations
                        if v.severity == "warning" and tag in v.message),
        "in_node_runs": len(in_node_runs(mine, lattice)),
        # off-board wires (leads, interlink halves) fly straight from
        # their hole to an edge, over whatever is between: columns
        # crossed, summed. The rail channel side that makes this small
        # is the one the board's wiring already wants.
        "fly_cols": sum(abs(w.path[-1].x - w.path[0].x) for w in wires
                        if w.kind in ("lead", "interlink") and w.path
                        and not w.underside),
        "wires": len(mine),
        "wire_mm": sum(wire_length_mm(w) for w in mine if w.path),
    }


def score(design, islands, routed, violations, rails=()):
    """The whole report: fingerprint, per-island and per-rail metrics,
    and the totals in RANK order."""
    isl = [island_metrics(n, islands[n], routed, violations)
           for n in sorted(islands)]
    rl = [rail_metrics(r, routed) for r in rails]
    totals = {k: sum(i[k] for i in isl) for k in RANK if k != "gap_crossings"}
    totals["gap_crossings"] = sum(r["gap_crossings"] for r in rl)
    totals["errors"] = sum(1 for v in violations if v.severity == "error")
    return {
        "fingerprint": fingerprint(design, islands),
        "totals": {k: totals[k] for k in ("errors",) + RANK},
        "rank": [totals["errors"]] + [totals[k] for k in RANK],
        "rails": rl,
        "islands": isl,
    }


def format_text(s):
    t = s["totals"]
    out = [f"fingerprint {s['fingerprint']}",
           "totals: " + ", ".join(f"{k} {t[k]}" for k in t),
           ""]
    for r in s["rails"]:
        out.append(f"rail {r['rail']!r} (channel {r['channel'] or '-'}): "
                   f"{r['links']} links, {r['gap_crossings']} through a "
                   f"gap, {r['off_rail']} off the rail")
    out.append("")
    cols = ("route_failures", "underside", "warnings", "in_node_runs",
            "fly_cols", "wires", "wire_mm")
    out.append(f"{'island':<20} " + " ".join(f"{c[:8]:>8}" for c in cols))
    for i in s["islands"]:
        out.append(f"{i['island']:<20} "
                   + " ".join(f"{i[c]:>8}" for c in cols)
                   + (f"  FROZEN: {i['frozen']}" if i["frozen"] else ""))
    return "\n".join(out)
