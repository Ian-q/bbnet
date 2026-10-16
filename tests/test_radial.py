"""Radial parts (`dia:` in mm) bury the holes under their body. A
6.3 mm 100 uF can on legs 3b/4b covers 3a, 4a, 3c and 4c - nothing can
then go in those holes (B1) or lie flat across them (B9), and the router
steers around them. Found on a real build where a jumper had been laid under the can."""
from pathlib import Path

import pytest

from bbnet import cli as bbnet
from bbnet import drc, model, render
from helpers import registry

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CAN = {"ref": "C9", "kind": "electrolytic", "value": "100u 25V",
       "rating": "25V", "from": "23b", "to": "22b", "dia": 6.3}


@pytest.fixture(autouse=True)
def _fixture_signals(monkeypatch):
    monkeypatch.setattr(bbnet, "g_signals", registry())


def _design(edit):
    parts = model.parts_lib_from(bbnet._yaml(FIXTURES / "parts.yaml"))
    raw = {p.stem: bbnet._yaml(p) for p in FIXTURES.glob("demo-*.yaml")}
    raw["demo-right"]["passives"].append(dict(CAN))
    edit(raw["demo-right"])
    islands = {d["island"]: model.island_from(d, parts)
               for d in raw.values()}
    return islands, model.derive(islands, registry())


def _msgs(design, rule):
    return [v.message for v in rule(design, {}, {})]


def test_cover_is_the_holes_inside_the_circle():
    q = model.Passive("C", "electrolytic", "", model.HoleAddr("x", 4, "L", "b"),
                      model.HoleAddr("x", 3, "L", "b"), "x", dia=6.3)
    assert {(r, h) for r, _half, h in model.radial_cover(q)} == {
        (3, "a"), (4, "a"), (3, "c"), (4, "c")}


def test_axial_part_covers_nothing():
    q = model.Passive("R", "resistor", "", model.HoleAddr("x", 4, "L", "b"),
                      model.HoleAddr("x", 3, "L", "b"), "x")
    assert model.radial_cover(q) == []


def test_clean_can_has_no_violations():
    _i, d = _design(lambda isl: None)
    assert _msgs(d, drc.rule_occupancy) == []
    assert _msgs(d, drc.rule_passive_overlap) == []


def test_a_wire_end_under_the_can_is_an_error():
    def edit(isl):
        isl.setdefault("jumpers", []).append(
            {"from": "22c", "to": "28c", "colour": "GRN"})
    _i, d = _design(edit)
    assert any("demo-right:22c:" in m and "C9 body" in m
               for m in _msgs(d, drc.rule_occupancy))


def test_a_resistor_lying_under_the_can_is_an_error():
    def edit(isl):
        isl["passives"].append({"ref": "R9", "kind": "resistor",
                                "value": "4k7", "from": "25c", "to": "21c"})
    _i, d = _design(edit)
    assert any("R9 lies under C9" in m
               for m in _msgs(d, drc.rule_passive_overlap))


def test_the_next_column_over_is_free():
    def edit(isl):
        isl.setdefault("jumpers", []).append(
            {"from": "22d", "to": "28d", "colour": "GRN"})
    _i, d = _design(edit)
    assert _msgs(d, drc.rule_occupancy) == []


def test_router_treats_the_cover_as_a_body():
    islands, d = _design(lambda isl: None)
    routed = render.route_design(islands, d)
    for w in routed["demo-right"][0]:
        lat = routed["demo-right"][2]
        assert not any((c.x, c.y) in {(lat.x_of("a"), 22), (lat.x_of("c"),
                                                              23)}
                       for c in w.path[1:-1]), w.label


@pytest.mark.parametrize("bad", [-1, "big", True])
def test_bad_dia_is_rejected(bad):
    with pytest.raises(model.ModelError, match="dia must be"):
        _design(lambda isl: isl["passives"][-1].update(dia=bad))


def test_dia_needs_named_holes():
    with pytest.raises(model.ModelError, match="dia needs both legs"):
        _design(lambda isl: isl["passives"][-1].update(to="rail:top-"))
