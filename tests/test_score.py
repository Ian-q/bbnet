"""Score contract: the fingerprint is the circuit with every position
stripped out - a re-layout keeps it, a rewire changes it - and the
metrics count what makes a sheet hard to build. Together they are what
lets a layout reviewer prove a tidy-up is only a tidy-up."""
import copy
from pathlib import Path

import pytest

from bbnet import cli as bbnet
from bbnet import model
from bbnet import render
from bbnet import score
from helpers import registry

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def _fixture_signals(monkeypatch):
    monkeypatch.setattr(bbnet, "g_signals", registry())


def _raw():
    return {p.stem: bbnet._yaml(p) for p in FIXTURES.glob("demo-*.yaml")}


def _score(raw):
    parts = model.parts_lib_from(bbnet._yaml(FIXTURES / "parts.yaml"))
    islands = {d["island"]: model.island_from(d, parts) for d in raw.values()}
    design = model.derive(islands, registry())
    routed = render.route_design(islands, design)
    return score.score(design, islands, routed, [])


def _passive(raw, island, ref):
    return next(q for q in raw[island]["passives"] if q["ref"] == ref)


def test_fingerprint_is_stable():
    assert _score(_raw())["fingerprint"] == _score(_raw())["fingerprint"]


def test_turning_a_resistor_end_for_end_keeps_the_circuit():
    raw = _raw()
    base = _score(raw)["fingerprint"]
    r = _passive(raw, "demo-right", "R2")
    r["from"], r["to"] = r["to"], r["from"]
    assert _score(raw)["fingerprint"] == base


def test_turning_an_electrolytic_end_for_end_is_a_different_circuit():
    raw = _raw()
    base = _score(raw)["fingerprint"]
    c = _passive(raw, "demo-left", "C2")
    c["from"], c["to"] = c["to"], c["from"]
    assert _score(raw)["fingerprint"] != base


def test_moving_a_part_and_its_wiring_keeps_the_circuit():
    raw = _raw()
    base = _score(raw)
    moved = copy.deepcopy(raw)
    # R2 from 20a to 22a: same node relationship (its own row + the rail)
    _passive(moved, "demo-right", "R2")["from"] = "22a"
    after = _score(moved)
    assert after["fingerprint"] == base["fingerprint"]


def test_rewiring_changes_the_fingerprint():
    raw = _raw()
    base = _score(raw)["fingerprint"]
    _passive(raw, "demo-right", "R2")["to"] = "rail:top-"   # 3V3 -> GND
    assert _score(raw)["fingerprint"] != base


def test_totals_follow_rank_order():
    s = _score(_raw())
    assert list(s["totals"]) == ["errors", *score.RANK]
    assert s["rank"] == [s["totals"][k] for k in s["totals"]]


def test_frozen_is_reported():
    raw = _raw()
    raw["demo-left"]["frozen"] = "built 2026-10-15"
    s = _score(raw)
    assert {i["island"]: i["frozen"] for i in s["islands"]} == {
        "demo-left": "built 2026-10-15", "demo-right": ""}


@pytest.mark.parametrize("bad", [3, "", " "])
def test_bad_frozen_is_rejected(bad):
    raw = _raw()
    raw["demo-left"]["frozen"] = bad
    with pytest.raises(model.ModelError, match="frozen must be"):
        _score(raw)


def test_score_command_runs(capsys):
    assert bbnet.main(["score", "--data-dir", str(FIXTURES)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("fingerprint ")
    assert "rail 'Demo rail'" in out
