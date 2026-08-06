"""Router (two-layer PathFinder) contract tests.

The router computes wire GEOMETRY only — connectivity truth stays in the
island YAML / derived netlist. Layer 0 (TOP) is the board surface where
wires should stay flat and never share a lattice cell; layer 1 (BOT) is
the underside, reached ONLY at a wire's own solder joints — an underside
wire is end-to-end beneath the board (no mid-route "via" exists on a
solderable board).
"""
from pathlib import Path

import yaml

from bbnet import cli as bbnet
from bbnet import model
from bbnet import router
from helpers import registry

EMPTY_LIB = model.parts_lib_from({})
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _island(text, lib=EMPTY_LIB):
    return model.island_from(yaml.safe_load(text), lib)


def _route_one(text, lib=EMPTY_LIB):
    isl = _island(text, lib)
    result = router.route_design({isl.name: isl})
    return result[isl.name]


def _cells(wire):
    return {(c.layer, c.x, c.y) for c in wire.path}


# ------------------------------------------------------------ basic paths

def test_straight_route_same_row():
    wires, stats, lat = _route_one("""
island: t1
board: mini-170
jumpers:
  - {from: 5a, to: 5e, colour: RED}
""")
    assert stats.failed == 0 and stats.max_overuse == 0
    (w,) = wires
    assert not w.fail and not w.underside
    assert all(c.layer == router.TOP for c in w.path)
    assert all(c.y == 5 for c in w.path)
    names = [lat.name(c.x) for c in w.path]
    assert names[0] == "a" and names[-1] == "e"


def test_crossing_wires_share_at_most_one_cell():
    # A runs down column c, B runs across row 5 — their shortest paths
    # meet at (c,5). A single perpendicular crossing (one wire lying
    # over the other at one point) is legal breadboard practice, but a
    # parallel co-run is not: the crowding cost caps the overlap at the
    # crossing point, and nobody tunnels for it.
    wires, stats, _lat = _route_one("""
island: t2
board: mini-170
jumpers:
  - {from: 2c, to: 8c, colour: BLU}
  - {from: 5a, to: 5e, colour: GRN}
""")
    assert stats.failed == 0 and stats.max_overuse == 0
    assert stats.underside == 0
    a, b = wires
    assert len(_cells(a) & _cells(b)) <= 1


_WALL = """
  - ref: W9
    value: full-width wall component (blocks TOP row 8, a-j)
    pins: {P1: 8a, P2: 8b, P3: 8c, P4: 8d, P5: 8e,
           P6: 8f, P7: 8g, P8: 8h, P9: 8i, P10: 8j}
"""


def test_component_wall_forces_underside_run():
    # A part body occupies every top-layer cell of row 8 and B must
    # cross it to get from 4c to 12c; jumpers may not leave the board
    # edge, so the wire becomes an END-TO-END underside run: soldered
    # from beneath at 4c and 12c, body hanging under the board — never
    # a mid-route dive (no via exists on a solderable board).
    wires, stats, _lat = _route_one(f"""
island: t3
board: mini-170
parts:
{_WALL}
jumpers:
  - {{from: 4c, to: 12c, colour: BLU}}
""")
    assert stats.failed == 0 and stats.max_overuse == 0
    assert stats.underside == 1
    (b,) = wires
    layers = [c.layer for c in b.path]
    assert layers == [router.TOP, router.BOT, router.BOT, router.TOP]


def test_rail_endpoint_targets_the_strip():
    wires, stats, lat = _route_one("""
island: t5
board: full-830
rails: {top+: 3V3, top-: GND, bot+: 5V, bot-: GND}
jumpers:
  - {from: "rail:top+", to: 31a, colour: RED}
""")
    assert stats.failed == 0 and stats.max_overuse == 0
    (w,) = wires
    ends = {lat.name(w.path[0].x), lat.name(w.path[-1].x)}
    assert "rail:top+" in ends and "a" in ends


def test_part_body_blocks_top_layer():
    lib = model.parts_lib_from({
        "dip8": {"kind": "dip",
                 "pins": ["1", "2", "3", "4", "5", "6", "7", "8"]},
    })
    wires, stats, lat = _route_one("""
island: t7
board: full-830
parts:
  - {ref: U9, part: dip8, value: test dip, pin1: 10c}
jumpers:
  - {from: 12a, to: 12j, colour: YEL}
""", lib)
    assert stats.failed == 0 and stats.max_overuse == 0
    (w,) = wires
    body_cols = {lat.x_of(c) for c in "cdefgh"}
    for c in w.path:
        if c.layer == router.TOP and 10 <= c.y <= 13:
            assert c.x not in body_cols, f"path enters part body at {c}"


def test_routing_is_deterministic():
    text = """
island: t6
board: mini-170
jumpers:
  - {from: 8a, to: 8e, colour: RED}
  - {from: 4c, to: 12c, colour: BLU}
  - {from: 2a, to: 14a, colour: GRN}
"""
    first, _s1, _l1 = _route_one(text)
    second, _s2, _l2 = _route_one(text)
    assert [w.path for w in first] == [w.path for w in second]


# ------------------------------------------------------ fixture corpus

def test_fixture_corpus_routes_and_converges(monkeypatch):
    # Quality invariants on the committed fixture islands, asserted on
    # BOTH router modes: net-aware (design+rules — the mode that
    # generates the committed LAYOUT.html; the byte-diff sync guard
    # alone cannot tell a converged snapshot from a stuck one) and
    # net-blind (the degraded fallback when no Design is supplied).
    monkeypatch.setattr(bbnet, "g_signals", registry())
    _p, _c, rules, islands, sig, _pn = bbnet.load_data(FIXTURES)
    design = model.derive(islands, sig)
    for mode, result in (
            ("net-aware", router.route_design(islands, design, rules)),
            ("net-blind", router.route_design(islands))):
        assert set(result) == set(islands)
        for name, (wires, stats, _lat) in result.items():
            assert stats.failed == 0, f"{mode}/{name}: unroutable wires"
            assert stats.max_overuse == 0, \
                f"{mode}/{name}: congestion did not converge"
        # underside runs exist but are rare — the top surface is the
        # default home for wires
        total = sum(s.underside for _w, s, _l in result.values())
        assert total < 25, f"{mode}: underside-heavy layout ({total})"


def test_no_longitudinal_rail_runs(monkeypatch):
    # RAIL_RUN_SOFT: wires cross rail bands perpendicular and land on
    # them, but never RIDE a strip lengthwise (the strip conducts — a
    # wire over it only buries the landing markers on the build sheet).
    # Landing = 1 cell in the rail column; a crossing = 1 cell per pass;
    # >=3 consecutive same-rail-column cells means a longitudinal run.
    monkeypatch.setattr(bbnet, "g_signals", registry())
    _p, _c, rules, islands, sig, _pn = bbnet.load_data(FIXTURES)
    design = model.derive(islands, sig)
    for name, (wires, _stats, lat) in \
            router.route_design(islands, design, rules).items():
        for w in wires:
            run, prev = 0, None
            for c in w.path:
                if c.layer == router.TOP and lat.is_rail(c.x) \
                        and c.x == prev:
                    run += 1
                else:
                    run = 0
                prev = c.x if lat.is_rail(c.x) else None
                assert run < 2, \
                    f"{name}: {w.label} rides rail col {lat.name(c.x)}"


# ------------------------------------------------- net-aware routing

def _route_with_design(text, rules=None, lib=EMPTY_LIB):
    isl = _island(text, lib)
    design = model.derive({isl.name: isl}, registry())
    result = router.route_design({isl.name: isl}, design, rules)
    return result[isl.name]


def test_paired_wires_land_adjacent_exits():
    wires, stats, _lat = _route_with_design("""
island: t9
board: mini-170
leads:
  - {at: 6a, colour: YEL, label: P+, pair: pp}
  - {at: 7a, colour: GRN, label: P-, pair: pp}
""")
    assert stats.failed == 0 and stats.max_overuse == 0
    a, b = wires
    a_cells = {(c.x, c.y) for c in a.path}
    near = sum(1 for c in b.path
               if any(abs(c.x - x) <= 1 and abs(c.y - y) <= 1
                      for (x, y) in a_cells))
    assert near / len(b.path) >= 0.8


def test_domain_repulsion_keeps_analog_off_fast_shadow():
    # The fast wire runs straight across row 8; the analog wire's
    # endpoints sit on row 9, one cell inside the fast wire's shadow.
    # With domain rules its midsection must bow away (rows >= 10);
    # without them it would run straight along row 9.
    rules = {"domains": [{"match": "^SPI_", "domain": "fast"},
                         {"match": "^AVDD_T", "domain": "analog"}]}
    wires, stats, _lat = _route_with_design("""
island: t10
board: mini-170
jumpers:
  - {from: 8a, to: 8j, colour: GRN}
  - {from: 9a, to: 9j, colour: PUR}
leads:
  - {at: 8b, colour: GRN, net: SPI_TEST, label: fast net seed}
  - {at: 9b, colour: PUR, net: AVDD_TEST, label: analog net seed}
""", rules)
    assert stats.failed == 0 and stats.max_overuse == 0
    analog = next(w for w in wires if w.label.startswith("9a"))
    interior = [c for c in analog.path
                if c not in (analog.path[0], analog.path[-1])]
    assert interior, analog.path
    away = sum(1 for c in interior if c.y >= 10)
    assert away / len(interior) >= 0.6, [(c.x, c.y) for c in analog.path]


def test_dip_span_widens_the_body():
    # A 0.6"-wide dip (Teensy-style, span 6) with pin1 in column d has
    # its right pins in column H, not the mirror's G — the body keep-out
    # must cover d..h and leave i free.
    lib = model.parts_lib_from({
        "dip8w": {"kind": "dip", "span": 6,
                  "pins": ["1", "2", "3", "4", "5", "6", "7", "8"]},
    })
    wires, stats, lat = _route_one("""
island: t11
board: full-830
parts:
  - {ref: U9, part: dip8w, value: wide dip, pin1: 10d}
jumpers:
  - {from: 12a, to: 12j, colour: YEL}
""", lib)
    assert stats.failed == 0 and stats.max_overuse == 0
    (w,) = wires
    body_cols = {lat.x_of(c) for c in "defgh"}
    for c in w.path:
        if c.layer == router.TOP and 10 <= c.y <= 13:
            assert c.x not in body_cols, f"path enters part body at {c}"


def test_dip_span_into_ravine_is_a_model_error():
    lib = model.parts_lib_from({
        "dipbad": {"kind": "dip", "span": 4,
                   "pins": ["1", "2", "3", "4"]},
    })
    import pytest
    with pytest.raises(model.ModelError, match="span"):
        _island("""
island: t12
board: full-830
parts:
  - {ref: U9, part: dipbad, value: bad span, pin1: 10c}
""", lib)   # c + 0.4" lands in the ravine


def test_mounting_hole_keepouts():
    import pytest
    # a ravine-straddling dip may not cover a mounting-hole row
    lib = model.parts_lib_from({
        "dip8": {"kind": "dip",
                 "pins": ["1", "2", "3", "4", "5", "6", "7", "8"]},
    })
    with pytest.raises(model.ModelError, match="mounting-hole"):
        _island("""
island: t13
board: full-830
parts:
  - {ref: U9, part: dip8, value: over the screw, pin1: 30c}
""", lib)   # rows 30-33 straddle the ravine over the 31-33 keep-out
    # a row bridge at a keep-out row must detour around the screw, on
    # either layer — never through the ravine cell itself
    wires, stats, lat = _route_one("""
island: t14
board: full-830
rails: {top+: 3V3, top-: GND, bot+: 5V, bot-: GND}
jumpers:
  - {from: 62e, to: 62f, colour: RED}
""")
    assert stats.failed == 0 and stats.max_overuse == 0
    (w,) = wires
    rav = lat.x_of("ravine")
    assert all(not (c.x == rav and c.y in (62, 63)) for c in w.path)


def test_offgrid_end_jumper_merges_nets_without_routing():
    isl = _island("""
island: t15
board: full-830
rails: {top+: 3V3, top-: GND, bot+: 5V, bot-: GND}
jumpers:
  - {from: "rail:top-", to: "rail:bot-", colour: BLK, offgrid: true,
     note: built-in GND end-jumper}
""")
    design = model.derive({isl.name: isl}, registry())
    # electrically: one GND net spanning both rails
    (gnd,) = design.nets_named("GND")
    strips = {k[2] for k in gnd.keys if k[0] == "rail"}
    assert strips == {"top-", "bot-"}
    # geometrically: nothing routed for it
    wires, stats, _lat = router.route_design({isl.name: isl})[isl.name]
    assert wires == [] and stats.wires == 0


def test_underside_airwire():
    """underside: true = the wire body hangs beneath the board (free
    air): drawn as a dashed point-to-point run, no channel competition,
    always routable."""
    isl = _island("""
island: bb1
board: full-830
rails: {top+: 3V3, top-: GND, bot+: 5V, bot-: GND}
rails_bridged: true
jumpers:
  - {from: "rail:top+@3", to: 40a, colour: RED, underside: true}
""", EMPTY_LIB)
    wires, stats, lat = router.route_design({isl.name: isl})[isl.name]
    assert stats.failed == 0 and stats.underside == 1
    aw = [w for w in wires if w.key.startswith("air:")]
    assert len(aw) == 1
    layers = [c.layer for c in aw[0].path]
    assert layers == [router.TOP, router.BOT, router.BOT, router.TOP]
    assert aw[0].path[0].y == 3          # pinned rail height honoured


def test_offgrid_interlink_merges_but_routes_nothing():
    """An offgrid interlink (supply star tie) merges nets across
    islands without producing a routed wire on either sheet."""
    a = model.island_from(
        {"island": "bb1", "board": "mini-170",
         "leads": [{"at": "5c", "net": "GND", "colour": "BLK"}],
         "interlinks": [{"from": "5L", "to": "bb2:5L", "colour": "BLK",
                         "offgrid": True}]}, {})
    b = model.island_from(
        {"island": "bb2", "board": "mini-170",
         "leads": [{"at": "5c", "net": "GND", "colour": "BLK"}]}, {})
    islands = {"bb1": a, "bb2": b}
    design = model.derive(islands, registry())
    assert len(design.nets_named("GND")) == 1     # merged
    for name, (wires, stats, _lat) in \
            router.route_design(islands, design).items():
        assert not [w for w in wires if w.kind == "interlink"], name


def test_gutter_lanes():
    """Railed boards get a bare ~2-wire gutter lane between the inner
    rail and the hole field (both sides): routable running room on
    both layers, but no holes — so no vias and no terminals there.
    Rail-less boards have no gutters."""
    railed = _island("""
island: g1
board: full-830
rails: {top+: 3V3, top-: GND, bot+: 5V, bot-: GND}
rails_bridged: true
jumpers:
  - {from: 5a, to: 40a, colour: GRN}
  - {from: 6a, to: 39a, colour: WHT}
  - {from: 7a, to: 38a, colour: BLU}
""", EMPTY_LIB)
    wires, stats, lat = router.route_design(
        {railed.name: railed})[railed.name]
    assert "gutterL" in lat.cols and "gutterR" in lat.cols
    assert stats.failed == 0 and stats.max_overuse == 0
    # three parallel long runs from column a: with CELL_CAP=2 in the a
    # column, at least one run must spill somewhere legal — the gutter
    # gives them room without tunneling or riding the rails
    gx = lat.x_of("gutterL")
    used_gutter = any(c.x == gx for w in wires for c in w.path)
    assert used_gutter or stats.underside == 0
    bare = _island("""
island: g2
board: mini-170
jumpers:
  - {from: 5a, to: 8a, colour: GRN}
""", EMPTY_LIB)
    _w, _s, lat2 = router.route_design({bare.name: bare})[bare.name]
    assert not any(c.startswith("gutter") for c in lat2.cols)


def test_device_legs_occupy_their_holes():
    """A device leg has to claim its hole in the router the way a
    passive's does, or the autorouter will happily draw a wire straight
    through a MOSFET's gate."""
    from bbnet import model, router
    from bbnet.model import island_from
    isl = island_from({
        "island": "bb1", "board": "full-830",
        "rails": {"top+": "3V3", "top-": "GND", "bot+": "5V",
                  "bot-": "GND"},
        "devices": [{"ref": "Q1", "kind": "mosfet", "value": "2N7000",
                     "pins": {"G": "20a", "D": "21a", "S": "22a"}}]}, {})
    islands = {isl.name: isl}
    design = model.derive(islands, registry())
    routed = router.route_design(islands, design, {"ties": [], "pins": {}})
    _wires, _stats, lat = routed["bb1"]
    r = router._IslandRouter(isl, [], router._NetCtx())
    for row in (20, 21, 22):
        assert (lat.x_of("a"), row) in r.solder_cells


def _lvl_island(**over):
    d = {"island": "bb1", "board": "full-830",
         "rails": {"top+": "3V3", "top-": "GND", "bot+": "5V",
                   "bot-": "GND"}}
    d.update(over)
    return d


def _occupancy(d):
    from bbnet import model, router
    from bbnet.model import island_from
    isl = island_from(d, {})
    model.derive({isl.name: isl}, registry())
    return router._IslandRouter(isl, [], router._NetCtx())


def test_lifting_a_body_frees_the_surface_channel():
    """The point of building upward. A resistor lying on the board
    claims surface cells the autorouter must route around; the same
    resistor on risers at level 1 claims none of them, because nothing
    on the surface has to dodge a body that is no longer on it."""
    flat = _occupancy(_lvl_island(passives=[
        {"ref": "R1", "kind": "resistor", "value": "1k",
         "from": "20a", "to": "20e"}]))
    lifted = _occupancy(_lvl_island(passives=[
        {"ref": "R1", "kind": "resistor", "value": "1k",
         "from": "20a", "to": "20e", "level": 1}]))
    assert flat.passive_cells, "flat resistor should claim surface cells"
    assert lifted.passive_cells == set()
    assert lifted.level_cells[1] == flat.passive_cells


def test_underside_still_claims_no_surface_cells():
    """side: bottom was already excluded from surface keep-out before
    levels existed; routing it through level -1 must not change that."""
    under = _occupancy(_lvl_island(passives=[
        {"ref": "R1", "kind": "resistor", "value": "1k",
         "from": "20a", "to": "20e", "side": "bottom"}]))
    assert under.passive_cells == set()
    assert under.level_cells[-1]


def test_riser_claims_its_hole():
    """The socket is above the board but the pin is IN the hole, so B1
    has to see it as an occupant."""
    r = _occupancy(_lvl_island(risers=[{"at": "20a", "level": 1}]))
    assert (r.lat.x_of("a"), 20) in r.solder_cells
