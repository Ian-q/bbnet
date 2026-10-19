"""B19: an interlink or lead may not end in a hole under a module's body.
A wire that leaves the board has to rise out of its hole, and a module
sitting over the hole pins it underneath. Found on a real build where an
interrupt line was drawn landing between a socketed Click's two headers.
The fix is a short jumper under the body to an open row, and the
off-board wire from there - jumpers under a body stay legal."""
from pathlib import Path

import pytest

from bbnet import cli as bbnet
from bbnet import drc, model
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
    return model.derive(islands, registry())


def _b19(edit):
    return [v.message for v in
            drc.rule_offboard_under_body(_design(edit), {}, {})]


def test_fixture_is_clean():
    assert _b19(lambda raw: None) == []


def test_interlink_under_a_dip_is_an_error():
    # demo-left U1 is a 0.6" dip from 10d, rows 10-15: 12f is between
    # its two pin rows
    def edit(raw):
        raw["demo-left"]["interlinks"].append(
            {"from": "12f", "to": "demo-right:30a", "colour": "GRN"})
    msgs = _b19(edit)
    assert any("demo-left:12f" in m and "U1's body" in m for m in msgs)


def test_the_far_end_is_checked_too():
    # demo-right U2 is a dip8 at 8c, rows 8-11: 9f lies under it
    def edit(raw):
        raw["demo-left"]["interlinks"].append(
            {"from": "30a", "to": "demo-right:9f", "colour": "GRN"})
    assert any("demo-right:9f" in m and "U2's body" in m
               for m in _b19(edit))


def test_lead_under_a_dip_is_an_error():
    def edit(raw):
        raw["demo-right"]["leads"].append(
            {"at": "9e", "colour": "YEL", "net": "X", "label": "probe"})
    assert any("demo-right:9e" in m and "lead probe" in m
               for m in _b19(edit))


def test_beside_the_body_is_fine():
    def edit(raw):
        raw["demo-left"]["interlinks"].append(
            {"from": "12c", "to": "demo-right:30a", "colour": "GRN"})
    assert _b19(edit) == []


def test_a_jumper_under_the_body_is_fine():
    def edit(raw):
        raw["demo-left"]["jumpers"].append(
            {"from": "12f", "to": "13f", "colour": "GRN"})
    assert _b19(edit) == []


def test_an_underside_interlink_is_exempt():
    def edit(raw):
        raw["demo-left"]["interlinks"].append(
            {"from": "12f", "to": "demo-right:30a", "colour": "GRN",
             "underside": True})
    assert _b19(edit) == []


def test_body_holes_leave_out_the_legs():
    d = _design(lambda raw: None)
    u2 = next(p for p in d.islands["demo-right"].parts if p.ref == "U2")
    holes = model.body_holes(u2)
    assert (9, "e") in holes
    assert (8, "c") not in holes          # pin 1's own hole
    assert (8, model.dip_right_col(u2)) not in holes


def test_a_frozen_island_only_warns():
    def edit(raw):
        raw["demo-left"]["frozen"] = "built"
        raw["demo-left"]["interlinks"].append(
            {"from": "12f", "to": "demo-right:30a", "colour": "GRN"})
    vs = drc.rule_offboard_under_body(_design(edit), {}, {})
    assert vs and all(v.severity == "warning" for v in vs)
