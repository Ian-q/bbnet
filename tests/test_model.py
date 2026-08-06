"""Tests for the bbnet data model: footprints, island loading, derivation."""
import pytest

from bbnet import model
from bbnet.geometry import HoleAddr, RailAddr
from bbnet.model import (
    Island, ModelError, PinRef, XIsland,
    island_from, land, parse_endpoint, parts_lib_from,
)
from helpers import registry

DIP8 = {"dip8-adapter": {"kind": "dip",
                         "pins": ["1", "2", "3", "4", "5", "6", "7", "8"]}}


def test_parts_lib_dip():
    lib = parts_lib_from(DIP8)
    fp = lib["dip8-adapter"]
    assert fp.kind == "dip" and len(fp.pin_names) == 8


def test_parts_lib_rejects_odd_dip_and_dup_names():
    with pytest.raises(ModelError):
        parts_lib_from({"x": {"kind": "dip", "pins": ["1", "2", "3"]}})
    with pytest.raises(ModelError):
        parts_lib_from({"x": {"kind": "sil", "pins": ["A", "A"]}})


def test_dip_landing_walks_down_left_up_right():
    fp = parts_lib_from(DIP8)["dip8-adapter"]
    pins = land(fp, HoleAddr("main", 2, "L", None))
    assert pins["1"] == HoleAddr("main", 2, "L", None)
    assert pins["4"] == HoleAddr("main", 5, "L", None)
    assert pins["5"] == HoleAddr("main", 5, "R", None)   # wraps at the bottom
    assert pins["8"] == HoleAddr("main", 2, "R", None)


def test_dip_pin1_must_be_left():
    fp = parts_lib_from(DIP8)["dip8-adapter"]
    with pytest.raises(ModelError):
        land(fp, HoleAddr("main", 2, "R", None))


def test_sil_landing_consecutive_rows():
    lib = parts_lib_from({"ina": {"kind": "sil",
                                  "pins": ["VS", "GND", "SCL", "SDA"]}})
    pins = land(lib["ina"], HoleAddr("bb2", 4, "R", None))
    assert pins["VS"] == HoleAddr("bb2", 4, "R", None)
    assert pins["SDA"] == HoleAddr("bb2", 7, "R", None)


def test_parse_endpoint_variants():
    ep = parse_endpoint("U1.29", "main", model.BOARDS["full-830"], {})
    assert ep == PinRef("U1", "29")
    ep = parse_endpoint("U1.IN+", "main", model.BOARDS["full-830"], {})
    assert ep == PinRef("U1", "IN+")
    ep = parse_endpoint("gps-imu:4L", "main", model.BOARDS["full-830"], {})
    assert ep == XIsland("gps-imu", "4L")
    ep = parse_endpoint("43c", "main", model.BOARDS["full-830"], {})
    assert isinstance(ep, HoleAddr) and ep.hole == "c"
    ep = parse_endpoint("rail:top+", "main", model.BOARDS["full-830"],
                        {"top+": "5V"})
    assert isinstance(ep, RailAddr)


MINI_ISLAND = {
    "island": "bb1",
    "board": "mini-170",
    "parts": [
        {"ref": "U1", "value": "buck",
         "pins": {"IN+": "2R", "IN-": "3R", "OUT+": "5R", "OUT-": "7R"},
         "internal_ties": [["IN-", "OUT-"]],
         "seeds": {"OUT+": "5V"}},
    ],
    "passives": [
        {"ref": "C1", "kind": "electrolytic", "from": "11R", "to": "7R"},
    ],
    "jumpers": [{"from": "12R", "to": "11R", "colour": "BLU"}],
    "leads": [{"at": "2R", "colour": "YEL", "net": "12V", "label": "12V+ in"}],
}


def test_island_from_explicit_pins():
    isl = island_from(MINI_ISLAND, {})
    assert isinstance(isl, Island) and isl.name == "bb1"
    u1 = isl.parts[0]
    assert u1.pins["OUT-"] == HoleAddr("bb1", 7, "R", None)
    assert u1.internal_ties == [("IN-", "OUT-")]
    assert u1.seeds == {"OUT+": "5V"}
    assert isl.passives[0].kind == "electrolytic"
    assert isl.leads[0].net == "12V"


def test_island_rejects_bad_kind_dup_ref_unknown_part():
    bad = dict(MINI_ISLAND,
               passives=[{"ref": "C1", "kind": "flux-capacitor",
                          "from": "1R", "to": "2R"}])
    with pytest.raises(ModelError):
        island_from(bad, {})
    bad = dict(MINI_ISLAND)
    bad["parts"] = MINI_ISLAND["parts"] * 2
    with pytest.raises(ModelError):
        island_from(bad, {})
    bad = dict(MINI_ISLAND,
               parts=[{"ref": "U9", "part": "nonexistent", "pin1": "1L"}])
    with pytest.raises(ModelError):
        island_from(bad, {})


def test_island_rejects_rails_on_railless_board():
    bad = dict(MINI_ISLAND, rails={"top+": "5V"})
    with pytest.raises(ModelError):
        island_from(bad, {})


def test_island_rejects_malformed_internal_ties_arity():
    bad = dict(MINI_ISLAND,
               parts=[{"ref": "U1", "pins": {"IN-": "3R", "OUT-": "7R"},
                       "internal_ties": [["IN-", "OUT-", "IN-"]]}])
    with pytest.raises(ModelError):
        island_from(bad, {})


# -------------------------------------------------------------- derivation tests

from collections import namedtuple

PmRow = namedtuple("PmRow", "mcu pin signal")

# A buck+LDO supply snake, minimally:
# LDO OUT at 12R, jumped to 11R. Caps C1 (electrolytic 11R->7R) and
# C2 (ceramic 11R->9R); jumper 9R->7R. 7R is buck OUT- (GND via lead).
SNAKE = {
    "island": "bb1",
    "board": "mini-170",
    "parts": [
        {"ref": "U1", "value": "buck",
         "pins": {"IN+": "2R", "IN-": "3R", "OUT+": "5R", "OUT-": "7R"},
         "internal_ties": [["IN-", "OUT-"]],
         "seeds": {"OUT+": "5V"}},
        {"ref": "U2", "value": "ldo",
         "pins": {"IN": "14R", "GND": "13R", "OUT": "12R"}},
    ],
    "jumpers": [
        {"from": "12R", "to": "11R", "colour": "BLU"},
        {"from": "9R", "to": "7R", "colour": "BLU"},
        {"from": "5R", "to": "14R", "colour": "RED"},
        {"from": "13R", "to": "9R", "colour": "BLU"},
    ],
    "passives": [
        {"ref": "C1", "kind": "electrolytic", "from": "11R", "to": "7R"},
        {"ref": "C2", "kind": "ceramic", "from": "11R", "to": "9R"},
    ],
    "leads": [
        {"at": "2R", "colour": "YEL", "net": "12V", "label": "12V+ in"},
        {"at": "3R", "colour": "BLU", "net": "GND", "label": "12V- in"},
        {"at": "12R", "colour": "RED", "net": "3V3", "label": "3.3V out"},
    ],
}


def _derive(*island_dicts, lib=None, pinmap=()):
    lib = lib or {}
    islands = {}
    for d in island_dicts:
        isl = island_from(d, lib)
        islands[isl.name] = isl
    return model.derive(islands, registry(pinmap))


def test_snake_collapses_to_named_nets_with_parallel_caps():
    design = _derive(SNAKE)
    out = design.net_of_pin("U2", "OUT")
    assert out.name == "3V3"
    assert ("row", "bb1", 12, "R") in out.keys
    assert ("row", "bb1", 11, "R") in out.keys
    gnd = design.net_of_pin("U1", "OUT-")
    assert gnd.name == "GND"
    # internal tie put IN- and OUT- on the same net; jumpers pulled in 9R/13R
    assert design.net_of_pin("U1", "IN-").nid == gnd.nid
    assert design.net_of_pin("U2", "GND").nid == gnd.nid
    # both caps are edges 3V3 <-> GND, whatever rows they physically sat on
    caps = [e for e in design.edges if e.ref in ("C1", "C2")]
    assert all({e.a_nid, e.b_nid} == {out.nid, gnd.nid} for e in caps)
    # 5V link: buck OUT+ seeded 5V, jumped to LDO IN
    assert design.net_of_pin("U2", "IN").name == "5V"


def test_unseeded_net_gets_synthetic_name():
    d = {"island": "bb1", "board": "mini-170",
         "jumpers": [{"from": "1L", "to": "2L", "colour": "GRN"}]}
    design = _derive(d)
    assert design.nets and design.nets[0].name.startswith("N$")


def test_rail_seeds_and_pinmap_seeds():
    lib = parts_lib_from({
        "mcu2": {"kind": "dip", "pins": ["1", "2", "GND", "X"],
                 "seeds": {"GND": "GND"}, "pin_signals": "pinmap:mcu2"}})
    d = {"island": "big", "board": "full-830", "rails_bridged": True,
         "rails": {"top+": "5V", "top-": "GND"},
         "parts": [{"ref": "M1", "part": "mcu2", "pin1": "10L"}],
         "jumpers": [{"from": "M1.GND", "to": "rail:GND", "colour": "BLK"}]}
    design = _derive(d, lib=lib,
                     pinmap=[PmRow("mcu2", "1", "SIG_A"),
                             PmRow("mcu2", "2", "SIG_B")])
    assert design.net_of_pin("M1", "1").name == "SIG_A"
    assert design.net_of_pin("M1", "GND").name == "GND"
    assert design.net_of_pin("M1", "GND").rail_seeds == ["GND"]
    assert design.net_of_pin("M1", "1").signal_seeds == ["SIG_A"]


def test_interlink_merges_across_islands():
    a = {"island": "bb1", "board": "mini-170",
         "leads": [{"at": "4L", "net": "SDA", "colour": "GRN"}],
         "interlinks": [{"from": "4L", "to": "bb2:9L", "colour": "GRN"}]}
    b = {"island": "bb2", "board": "mini-170"}
    design = _derive(a, b)
    nets = design.nets_named("SDA")
    assert len(nets) == 1
    assert ("row", "bb2", 9, "L") in nets[0].keys


def test_duplicate_ref_across_islands_rejected():
    a = {"island": "bb1", "board": "mini-170",
         "parts": [{"ref": "U1", "pins": {"A": "1L"}}]}
    b = {"island": "bb2", "board": "mini-170",
         "parts": [{"ref": "U1", "pins": {"A": "1L"}}]}
    with pytest.raises(ModelError):
        _derive(a, b)


def test_conflicting_seeds_kept_for_drc():
    # A jumper shorting a 5V-seeded node to a GND lead: net keeps both seeds
    d = {"island": "bb1", "board": "mini-170",
         "parts": [{"ref": "U1", "pins": {"OUT+": "5R"},
                    "seeds": {"OUT+": "5V"}}],
         "leads": [{"at": "7R", "net": "GND", "colour": "BLK"}],
         "jumpers": [{"from": "5R", "to": "7R", "colour": "RED"}]}
    design = _derive(d)
    net = design.net_of_pin("U1", "OUT+")
    assert sorted(n for n, _src in net.seeds) == ["5V", "GND"]
    assert net.name == "5V+GND"


def test_passive_side():
    d = {"island": "bb1", "board": "mini-170", "passives": [
        {"ref": "C1", "kind": "ceramic", "value": "10p",
         "from": "1a", "to": "2b"},
        {"ref": "C2", "kind": "ceramic", "value": "100p",
         "from": "2a", "to": "1b", "side": "bottom"}]}
    isl = island_from(d, {})
    assert isl.passives[0].side == "top"      # default
    assert isl.passives[1].side == "bottom"   # crossing pair fits
    with pytest.raises(ModelError):
        island_from({**d, "passives": [
            {"ref": "C1", "kind": "ceramic", "value": "10p",
             "from": "1a", "to": "2b", "side": "under"}]}, {})


def test_rail_row_pin():
    """rail:top+@5 pins a rail endpoint at a physical height — pure
    geometry: the net key ignores the row, so @5 and bare top+ merge."""
    d = {"island": "bb1", "board": "full-830",
         "rails": {"top+": "3V3", "top-": "GND",
                   "bot+": "5V", "bot-": "GND"},
         "rails_bridged": True,
         "passives": [{"ref": "C1", "kind": "ceramic", "value": "100n",
                       "from": "rail:top+@7", "to": "rail:top-@7"}],
         "jumpers": [{"from": "rail:top+@3", "to": "10a",
                      "colour": "RED"}]}
    isl = island_from(d, {})
    assert isl.passives[0].a.row == 7 and isl.passives[0].b.row == 7
    assert isl.jumpers[0].a.row == 3
    assert isl.passives[0].a.node_key() == ("rail", "bb1", "top+")
    with pytest.raises(Exception):
        island_from({**d, "jumpers": [
            {"from": "rail:top+@99", "to": "10a", "colour": "RED"}]}, {})


# --------------------------------------------------------- schema_version


def test_island_without_schema_version_is_version_1():
    """Absence means 1. Every island file written before the key existed
    must keep loading untouched -- that is the whole point of optional."""
    isl = model.island_from({
        "island": "t", "board": "mini-170", "rails": {},
        "parts": [{"ref": "U1", "pins": {"1": "5c", "2": "6c"}}]}, {})
    assert isl.schema_version == 1


def test_island_may_declare_the_current_schema_version():
    isl = model.island_from({
        "island": "t", "board": "mini-170", "rails": {},
        "schema_version": 1,
        "parts": [{"ref": "U1", "pins": {"1": "5c", "2": "6c"}}]}, {})
    assert isl.schema_version == 1


def test_future_schema_version_is_refused_with_a_useful_message():
    """A file from a newer bbnet must fail loudly, not be half-read."""
    with pytest.raises(model.UnsupportedSchemaVersion) as e:
        model.island_from({
            "island": "t", "board": "mini-170", "rails": {},
            "schema_version": 99,
            "parts": [{"ref": "U1", "pins": {"1": "5c"}}]}, {})
    assert "99" in str(e.value)
    assert "1" in str(e.value)


@pytest.mark.parametrize("sv", [
    1.5,     # float: int(1.5) == 1 would silently truncate into range
    2.9,     # float: int(2.9) == 2 would silently truncate OUT of range --
             # whether a bad float got caught used to depend on luck
    True,    # bool: isinstance(True, int) is True in Python
    False,
    "1.0",   # str: quoting must not change what a value means
    "1",     # str: no str fallback at all, quoted or not
    0,       # below the valid floor
    -3,
    None,
])
def test_malformed_schema_version_is_rejected(sv):
    """A version field exists to be trusted -- a value that merely
    LOOKS numeric (a float, a bool, a quoted string) must not be
    silently coerced into a verdict. Only a genuine int >= 1 passes."""
    with pytest.raises(model.ModelError) as e:
        model.island_from({
            "island": "t", "board": "mini-170", "rails": {},
            "schema_version": sv,
            "parts": [{"ref": "U1", "pins": {"1": "5c"}}]}, {})
    assert repr(sv) in str(e.value)


# ------------------------------------------------- multi-terminal devices

def _dev_island(**over):
    d = {"island": "bb1", "board": "full-830",
         "rails": {"top+": "3V3", "top-": "GND"}}
    d.update(over)
    return d


FET = {"ref": "Q1", "kind": "mosfet", "value": "2N7000",
       "pins": {"G": "20a", "D": "21a", "S": "22a"}}


def test_device_legs_land_on_separate_nets():
    """The point of the whole refactor: a three-legged part keeps its
    legs on three nets. `Passive` could only ever express two, so a
    2N7000 was unrepresentable before this."""
    design = _derive(_dev_island(devices=[dict(FET)]))
    nids = design.device_nids["Q1"]
    assert sorted(nids) == ["D", "G", "S"]
    assert len({nids["G"], nids["D"], nids["S"]}) == 3


def test_device_leg_merges_with_what_it_is_wired_to():
    """A device leg is an ordinary net member — a jumper from the gate
    to the 3V3 rail must put the gate ON 3V3, not beside it."""
    design = _derive(_dev_island(
        devices=[dict(FET)],
        jumpers=[{"from": "20b", "to": "rail:top+", "colour": "RED"}]))
    gate = design.nets[design.device_nids["Q1"]["G"]]
    assert gate.name == "3V3"


def test_device_pins_are_hole_members_like_any_other_leg():
    """B1 occupancy counts legs per hole out of design.hole_members; a
    device leg has to be in there or two parts could share a hole and
    the DRC would never notice."""
    design = _derive(_dev_island(devices=[dict(FET)]))
    assert design.hole_members[("bb1", 20, "L", "a")] == ["Q1.G"]
    assert design.hole_members[("bb1", 22, "L", "a")] == ["Q1.S"]


def test_device_rejects_unknown_pin_name():
    """A leg name the pinout does not know is a wiring mistake, not a
    free-form label — on a part whose legs are not interchangeable the
    netlist would otherwise absorb it silently."""
    bad = dict(FET, pins={"G": "20a", "D": "21a", "SOURCE": "22a"})
    with pytest.raises(ModelError, match="no pin"):
        _derive(_dev_island(devices=[bad]))


def test_device_rejects_unplaced_pin():
    bad = dict(FET, pins={"G": "20a", "D": "21a"})
    with pytest.raises(ModelError, match="unplaced"):
        _derive(_dev_island(devices=[bad]))


def test_two_terminal_kind_is_rejected_as_a_device():
    """Resistors keep their from/to form; `devices:` is for parts the
    two-terminal schema cannot express."""
    with pytest.raises(ModelError, match="passives"):
        _derive(_dev_island(devices=[{"ref": "R1", "kind": "resistor",
                                      "pins": {"a": "20a", "b": "21a"}}]))


def test_pot_wiper_is_its_own_net():
    design = _derive(_dev_island(devices=[
        {"ref": "RV1", "kind": "pot", "value": "10k",
         "pins": {"A": "20a", "W": "21a", "B": "22a"}}]))
    nids = design.device_nids["RV1"]
    assert len({nids["A"], nids["W"], nids["B"]}) == 3


def test_new_two_terminal_kinds_derive_as_edges():
    """inductor/ferrite/fuse take the passive path, so they arrive in
    design.edges and stay visible to the geometry rules that walk it."""
    design = _derive(_dev_island(passives=[
        {"ref": "L1", "kind": "inductor", "value": "10u",
         "from": "20a", "to": "21a"},
        {"ref": "F1", "kind": "fuse", "value": "2A",
         "from": "22a", "to": "23a"}]))
    kinds = {e.ref: e.kind for e in design.edges}
    assert kinds == {"L1": "inductor", "F1": "fuse"}
    assert all(e.a_nid != e.b_nid for e in design.edges)


def test_terminal_groups_yields_passives_and_devices_uniformly():
    """One derivation path is the whole design premise — a caller must
    not need isinstance to walk both kinds of inline part."""
    isl = island_from(_dev_island(
        devices=[dict(FET)],
        passives=[{"ref": "R1", "kind": "resistor", "value": "10k",
                   "from": "30a", "to": "31a"}]), {})
    groups = {p.ref: len(ts) for p, ts in isl.terminal_groups()}
    assert groups == {"R1": 2, "Q1": 3}


# ------------------------------------------------------- levels + risers

def test_side_bottom_is_level_minus_one():
    """`side` and `level` are the same axis at different resolutions, so
    every island YAML written before levels existed keeps its meaning."""
    isl = island_from(_dev_island(passives=[
        {"ref": "C1", "kind": "ceramic", "value": "100n",
         "from": "20a", "to": "21a", "side": "bottom"}]), {})
    assert isl.passives[0].level == -1
    assert isl.passives[0].side == "bottom"


def test_level_defaults_to_the_board_surface():
    isl = island_from(_dev_island(passives=[
        {"ref": "R1", "kind": "resistor", "value": "1k",
         "from": "20a", "to": "21a"}]), {})
    assert isl.passives[0].level == 0 and isl.passives[0].side == "top"


def test_explicit_level_sets_side_for_free():
    isl = island_from(_dev_island(devices=[
        dict(FET, level=2)]), {})
    assert isl.devices[0].level == 2 and isl.devices[0].side == "top"


def test_side_and_level_must_agree():
    """`side: bottom` at `level: 2` is not a thing that can be built.
    Silently preferring one would bury the contradiction in whichever
    field the reader did not happen to look at."""
    with pytest.raises(ModelError, match="disagree"):
        island_from(_dev_island(passives=[
            {"ref": "R1", "kind": "resistor", "value": "1k",
             "from": "20a", "to": "21a",
             "side": "bottom", "level": 2}]), {})


def test_level_must_be_an_integer():
    with pytest.raises(ModelError, match="must be an integer"):
        island_from(_dev_island(devices=[dict(FET, level="high")]), {})


def test_riser_adds_no_net():
    """The load-bearing invariant of the whole levels design. A riser is
    electrically the same node as its hole, so putting one down must not
    change the netlist by even one net."""
    base = _dev_island(jumpers=[
        {"from": "20a", "to": "21a", "colour": "GRN"}])
    before = _derive(base)
    after = _derive(dict(base, risers=[{"at": "20a", "level": 1}]))
    assert len(after.nets) == len(before.nets)
    assert ({n.name for n in after.nets} == {n.name for n in before.nets})


def test_riser_sockets_report_reachable_levels():
    isl = island_from(_dev_island(risers=[
        {"at": "20a", "level": 1},
        {"at": "20a", "level": 2},
        {"at": "24c", "level": 1}]), {})
    assert isl.sockets() == {(20, "L", "a"): {1, 2}, (24, "L", "c"): {1}}


def test_riser_must_name_a_hole_not_a_half_row():
    """A riser is soldered into ONE hole; `20L` names a node, not a
    hole, so it cannot say where the pin actually goes."""
    with pytest.raises(ModelError, match="must name the hole"):
        island_from(_dev_island(risers=[{"at": "20L", "level": 1}]), {})


def test_riser_level_must_be_above_the_board():
    with pytest.raises(ModelError, match="reach ABOVE"):
        island_from(_dev_island(risers=[{"at": "20a", "level": 0}]), {})


# ----------------------------------------------------------- link bars

def _lk_island(**over):
    d = {"island": "bb1", "board": "full-830",
         "rails": {"top+": "3V3", "top-": "GND"}}
    d.update(over)
    return d


def test_link_positions_fill_the_span_between_bonded_holes():
    """The bar covers every slot it physically spans, not just the ones
    named — the unnamed ones are exactly what B15 has to police."""
    isl = island_from(_lk_island(links=[
        {"ref": "LK1", "level": 1, "connects": ["20a", "24a"]}]), {})
    lk = isl.links[0]
    assert [a.row for a in lk.positions] == [20, 21, 22, 23, 24]
    assert lk.length == 5
    assert [a.row for a in lk.floats()] == [21, 22, 23]


def test_link_runs_along_a_row_too():
    isl = island_from(_lk_island(links=[
        {"ref": "LK1", "level": 1, "connects": ["20a", "20e"]}]), {})
    assert [a.hole for a in isl.links[0].positions] == list("abcde")


def test_link_cannot_cross_the_ravine():
    """A rigid bar spanning a->f would have to bridge the gutter."""
    with pytest.raises(ModelError, match="ravine"):
        island_from(_lk_island(links=[
            {"ref": "LK1", "level": 1, "connects": ["20a", "20f"]}]), {})


def test_link_cannot_run_diagonally():
    with pytest.raises(ModelError, match="not in one line"):
        island_from(_lk_island(links=[
            {"ref": "LK1", "level": 1, "connects": ["20a", "24c"]}]), {})


def test_link_at_level_zero_is_a_jumper():
    with pytest.raises(ModelError, match="jumpers"):
        island_from(_lk_island(links=[
            {"ref": "LK1", "level": 0, "connects": ["20a", "22a"]}]), {})


def test_link_needs_two_bonded_positions():
    with pytest.raises(ModelError, match="at least two"):
        island_from(_lk_island(links=[
            {"ref": "LK1", "level": 1, "connects": ["20a"]}]), {})


def test_position_cannot_be_both_connected_and_clipped():
    with pytest.raises(ModelError, match="BOTH connected"):
        island_from(_lk_island(links=[
            {"ref": "LK1", "level": 1, "connects": ["20a", "22a"],
             "clipped": ["22a"]}]), {})


def test_a_clipped_end_position_still_sets_the_bar_length():
    """Snipping a pin does not shorten the PCB it was on, so a clipped
    end position still says how long the bar is — which is what the
    stock check (B16) has to measure against."""
    isl = island_from(_lk_island(links=[
        {"ref": "LK1", "level": 1, "connects": ["20a", "22a"],
         "clipped": ["24a"]}]), {})
    assert isl.links[0].length == 5


def test_link_fab_must_be_known():
    """pcb-rail or bent-wire — the model stays agnostic between ordering
    1xN rails and bending solid core on a jig, but not silently so."""
    with pytest.raises(ModelError, match="fab"):
        island_from(_lk_island(links=[
            {"ref": "LK1", "level": 1, "connects": ["20a", "22a"],
             "fab": "wishful-thinking"}]), {})


def test_link_terminals_are_one_conductor():
    """All bonded positions share net_index 0 — that single fact is what
    makes derivation need no special case for bars."""
    isl = island_from(_lk_island(links=[
        {"ref": "LK1", "level": 1,
         "connects": ["20a", "22a", "24a"], "clipped": ["21a", "23a"]}]), {})
    ts = isl.links[0].terminals()
    assert len(ts) == 3
    assert {t.net_index for t in ts} == {0}
