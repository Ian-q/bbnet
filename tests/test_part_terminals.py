"""Off-grid terminals: named connection points on a part's BODY, not in
any hole - a Click's screw terminals, a module's solder pads. Each one is
its own node, addressed REF.NAME, so two terminals over one row are not
shorted through the strip beneath them and the hole under each stays
free. Leads (and interlinks) land on them like on any pin. Found on a
real build: a buck-boost Click whose VIN/VOUT screw terminals had no
place in the model at all, 2026-10-19."""
from pathlib import Path

import pytest

from bbnet import cli as bbnet
from bbnet import drc, model, render
from helpers import registry

FIXTURES = Path(__file__).resolve().parent / "fixtures"

CLICK = {
    "kind": "dip", "span": 9, "overhang": [0, 8],
    "pins": ["GND", "EN", "VCC", "SDA"],
    "terminals": {"VIN+": {"row": 6, "col": "c"},
                  "VIN-": {"row": 6, "col": "d"},
                  "VOUT-": {"row": 6, "col": "g"},
                  "VOUT+": {"row": 6, "col": "h"}},
}


@pytest.fixture(autouse=True)
def _fixture_signals(monkeypatch):
    monkeypatch.setattr(bbnet, "g_signals", registry())


def _islands(edit=None, fp=None):
    lib = bbnet._yaml(FIXTURES / "parts.yaml")
    lib["click-demo"] = fp or CLICK
    parts = model.parts_lib_from(lib)
    raw = {p.stem: bbnet._yaml(p) for p in FIXTURES.glob("demo-*.yaml")}
    left = raw["demo-left"]
    left["parts"].append({"ref": "CLK", "part": "click-demo", "pin1": "50b",
                          "seeds": {"VIN-": "GND", "VOUT-": "GND"}})
    left["leads"] = (left.get("leads") or []) + [
        {"at": "CLK.VIN+", "net": "CHG_VIN", "colour": "RED",
         "label": "12 V in"},
        {"at": "CLK.VOUT+", "net": "CHG_VOUT", "colour": "RED",
         "label": "to battery"},
    ]
    if edit:
        edit(raw)
    return {d["island"]: model.island_from(d, parts) for d in raw.values()}


def _design(edit=None):
    islands = _islands(edit)
    return islands, model.derive(islands, registry())


def test_terminals_land_relative_to_pin1():
    islands, _d = _design()
    clk = next(p for p in islands["demo-left"].parts if p.ref == "CLK")
    t = clk.terms["VIN+"]
    assert (t.row, t.hole) == (56, "c")
    assert clk.terms["VOUT+"].hole == "h"


def test_terminals_join_nets_by_seed_and_lead():
    _i, d = _design()
    assert d.net_of_pin("CLK", "VIN+").name == "CHG_VIN"
    assert d.net_of_pin("CLK", "VOUT+").name == "CHG_VOUT"
    assert d.net_of_pin("CLK", "VIN-").name == "GND"
    members = d.node_members[("term", "demo-left", "CLK", "VIN+")]
    assert "CLK.VIN+" in members


def test_two_terminals_over_one_row_are_not_shorted():
    # VIN+ and VIN- both sit over row 56, columns c and d - the same
    # half-row strip. A hole address would short them; a terminal is not
    # in the strip at all.
    _i, d = _design()
    assert (d.pin_nid[("CLK", "VIN+")] != d.pin_nid[("CLK", "VIN-")])
    assert ("row", "demo-left", 56, "L") not in d.nid_of_key


def test_no_occupancy_and_no_b19():
    # the lead ends on the body, over buried holes: that is what a
    # terminal is for, so neither B1 nor B19 may object
    _i, d = _design()
    assert [v for v in drc.rule_occupancy(d, {}, {})
            if "CLK" in v.message] == []
    assert drc.rule_offboard_under_body(d, {}, {}) == []


def test_a_hole_under_the_terminal_is_still_free():
    def edit(raw):
        raw["demo-left"]["leads"].append(
            {"at": "56a", "colour": "YEL", "label": "beside"})
    _i, d = _design(edit)
    assert [v for v in drc.rule_occupancy(d, {}, {})
            if "56" in v.message] == []


def test_unknown_terminal_is_an_error():
    def edit(raw):
        raw["demo-left"]["leads"].append(
            {"at": "CLK.VBAT", "colour": "RED", "label": "nope"})
    islands = _islands(edit)
    with pytest.raises(model.ModelError, match="no pin or terminal 'VBAT'"):
        model.derive(islands, registry())


@pytest.mark.parametrize("bad, msg", [
    ({"X": {"row": 1, "col": "z"}}, "col must be"),
    ({"X": {"row": "1", "col": "c"}}, "row must be"),
    ({"GND": {"row": 1, "col": "c"}}, "clashes with a pin"),
])
def test_bad_terminal_specs(bad, msg):
    with pytest.raises(model.ModelError, match=msg):
        model.parts_lib_from({"x": dict(CLICK, terminals=bad)})


def test_an_internal_tie_can_join_a_terminal_to_a_pin():
    # a Click's screw-terminal ground is the same plane as its header GND
    def edit(raw):
        clk = next(p for p in raw["demo-left"]["parts"]
                   if p["ref"] == "CLK")
        clk["internal_ties"] = [["GND", "VIN-"], ["GND", "VOUT-"]]
    _i, d = _design(edit)
    assert (d.pin_nid[("CLK", "GND")] == d.pin_nid[("CLK", "VIN-")]
            == d.pin_nid[("CLK", "VOUT-")])
    assert d.pin_nid[("CLK", "VIN+")] != d.pin_nid[("CLK", "GND")]


def test_an_interlink_can_land_on_a_terminal():
    def edit(raw):
        raw["demo-right"].setdefault("interlinks", []).append(
            {"from": "30a", "to": "demo-left:CLK.VOUT+", "colour": "RED"})
    _i, d = _design(edit)
    assert ("row", "demo-right", 30, "L") in d.nid_of_key
    assert (d.nid_of_key[("row", "demo-right", 30, "L")]
            == d.pin_nid[("CLK", "VOUT+")])


def test_render_draws_the_terminal_and_its_lead():
    islands, d = _design()
    page = render.render_html(islands, design=d)
    assert page.count('<g class="term">') == 4
    assert '<tspan class="vn vn-name">VOUT+</tspan>' in page
    assert "CLK.VIN+: terminal on the body" in page
    assert "12 V in" in page


def test_a_wire_landing_on_a_terminal_is_drawn_on_that_board_too():
    """The far half of an interlink that ends on a terminal used to be
    dropped from the far board's drawing: the router parsed `CLK.VOUT+`
    as a hole address, failed, and skipped it silently."""
    from bbnet import router

    def edit(raw):
        raw["demo-right"].setdefault("interlinks", []).append(
            {"from": "30a", "to": "demo-left:CLK.VOUT+", "colour": "RED"})
    islands, d = _design(edit)
    routed = router.route_design(islands, d)
    labels = [w.label for w in routed["demo-left"][0]
              if w.kind == "interlink"]
    assert "CLK.VOUT+ ⇐ demo-right:30a" in labels
