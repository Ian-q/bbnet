"""A footprint pin is landed as a half-row node, but its leg still sits
in one hole - and a wire end pushed into that hole is a collision (B1).
Before this, only wire ends and passive ends registered their holes, so a
wire dropped onto a dip's pin passed DRC with 0 errors (found by the
first bbnet-layout-review pass, 2026-10-15)."""
from pathlib import Path

import pytest

from bbnet import cli as bbnet
from bbnet import drc, model
from helpers import registry

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def _fixture_signals(monkeypatch):
    monkeypatch.setattr(bbnet, "g_signals", registry())


def _occupancy(edit):
    parts = model.parts_lib_from(bbnet._yaml(FIXTURES / "parts.yaml"))
    raw = {p.stem: bbnet._yaml(p) for p in FIXTURES.glob("demo-*.yaml")}
    edit(raw)
    islands = {d["island"]: model.island_from(d, parts)
               for d in raw.values()}
    design = model.derive(islands, registry())
    return [v.message for v in drc.rule_occupancy(design, {}, {})]


def _jumper(raw, island, a, b):
    raw[island].setdefault("jumpers", []).append(
        {"from": a, "to": b, "colour": "GRN"})


def test_fixture_has_no_collisions():
    assert _occupancy(lambda raw: None) == []


@pytest.mark.parametrize("hole, pin", [
    ("10d", "U1.1"),    # demo-left U1: pin 1, left row, its anchor
    ("12d", "U1.3"),    # same row of legs, further down
])
def test_wire_end_in_a_left_row_leg_hole_is_an_error(hole, pin):
    msgs = _occupancy(lambda raw: _jumper(raw, "demo-left", hole, "60a"))
    assert any(f"demo-left:{hole}:" in m and pin in m for m in msgs), msgs


def test_wire_end_in_a_right_row_leg_hole_is_an_error():
    # demo-right U2 is a 0.3" dip at 8c with no span: right row mirrors
    # to column h; its last pin (8) sits opposite pin 1
    msgs = _occupancy(lambda raw: _jumper(raw, "demo-right", "8h", "30a"))
    assert any("demo-right:8h:" in m and "U2.8" in m for m in msgs), msgs


def test_another_hole_in_the_same_node_is_fine():
    assert _occupancy(lambda raw: _jumper(raw, "demo-left", "10b",
                                          "60a")) == []


def test_pin_hole_matches_the_router_and_renderer_column():
    parts = model.parts_lib_from(bbnet._yaml(FIXTURES / "parts.yaml"))
    isl = model.island_from(bbnet._yaml(FIXTURES / "demo-right.yaml"),
                            parts)
    u2 = next(p for p in isl.parts if p.ref == "U2")
    assert model.pin_hole(u2, u2.pins["1"]) == "c"
    assert model.pin_hole(u2, u2.pins["8"]) == model.dip_right_col(u2)
