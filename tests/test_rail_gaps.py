"""A half-size board's rails are not a continuous strip beside every
row: their holes come in groups of five with a blank between groups
(none beside rows 6, 12, 18, 24 or 30), and each sits ~0.4 of a pitch
below the row it is numbered with. A rail landing pinned to a gap row
has no hole to go in (B1), and the router never picks one. B8 also
measures a leg that lands on a rail, so a can squeezed across two
side-by-side rail holes is flagged. Both found on a real half-size
build, 2026-10-19."""
from pathlib import Path

import pytest

from bbnet import cli as bbnet
from bbnet import drc, model, render
from bbnet.geometry import BOARDS
from helpers import registry

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def _fixture_signals(monkeypatch):
    monkeypatch.setattr(bbnet, "g_signals", registry())


def _design(edit):
    parts = model.parts_lib_from(bbnet._yaml(FIXTURES / "parts.yaml"))
    raw = {p.stem: bbnet._yaml(p) for p in FIXTURES.glob("demo-*.yaml")}
    edit(raw)
    islands = {d["island"]: model.island_from(d, parts)
               for d in raw.values()}
    return islands, model.derive(islands, registry())


def _msgs(rule, edit, rules=None):
    _i, d = _design(edit)
    return [v.message for v in rule(d, rules or {}, {})]


def test_half_size_rail_rows():
    b = BOARDS["half-400"]
    assert [r for r in range(1, 31) if not b.has_rail_hole(r)] == [
        6, 12, 18, 24, 30]
    assert len(b.rail_rows) == 25
    assert BOARDS["full-830"].has_rail_hole(6)


def test_fixture_is_clean():
    assert _msgs(drc.rule_occupancy, lambda raw: None) == []


@pytest.mark.parametrize("row, bad", [(12, True), (13, False)])
def test_a_landing_in_a_gap_is_an_error(row, bad):
    def edit(raw):
        raw["demo-right"].setdefault("jumpers", []).append(
            {"from": f"rail:top+@{row}", "to": f"{row}a", "colour": "RED"})
    hit = any(f"rail:top+@{row}" in m and "gap in the rail" in m
              for m in _msgs(drc.rule_occupancy, edit))
    assert hit is bad


def test_a_passive_leg_in_a_gap_is_an_error():
    def edit(raw):
        raw["demo-right"]["passives"].append(
            {"ref": "C9", "kind": "ceramic", "value": "100n",
             "from": "rail:top+@18", "to": "rail:top-@19"})
    assert any("C9 lands in a gap" in m
               for m in _msgs(drc.rule_occupancy, edit))


def test_router_never_lands_an_unpinned_rail_end_in_a_gap():
    def edit(raw):
        raw["demo-right"].setdefault("jumpers", []).append(
            {"from": "rail:top+", "to": "12a", "colour": "RED"})
    islands, d = _design(edit)
    lat = render.route_design(islands, d)["demo-right"][2]
    x = lat.x_of("rail:top+")
    for w in render.route_design(islands, d)["demo-right"][0]:
        for c in (w.path[0], w.path[-1]):
            if c.x == x:
                assert BOARDS["half-400"].has_rail_hole(c.y), w.label


def test_a_can_across_a_rail_pair_is_too_short():
    def edit(raw):
        raw["demo-right"]["passives"].append(
            {"ref": "C9", "kind": "electrolytic", "value": "10u",
             "from": "rail:top+@13", "to": "rail:top-@13"})
    rules = {"passive_min_span": {"electrolytic": 2.0}}
    assert any("C9" in m and "span 1.00" in m
               for m in _msgs(drc.rule_passive_span, edit, rules))


def test_over_one_up_two_fits():
    def edit(raw):
        raw["demo-right"]["passives"].append(
            {"ref": "C9", "kind": "electrolytic", "value": "10u",
             "from": "rail:top+@13", "to": "rail:top-@11"})
    rules = {"passive_min_span": {"electrolytic": 2.0}}
    assert not any("C9" in m
                   for m in _msgs(drc.rule_passive_span, edit, rules))
