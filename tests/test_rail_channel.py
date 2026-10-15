"""Rail channel contract: a rail's `channel:` (below / above) drives
routing - every interlink half on a board of that rail leaves by the
channel's long edge, whatever panel the board is drawn in, so no wire
crosses through the gap between two carriers. A panel then stitches only
the wires whose halves still face each other; the rest keep their
labelled stubs.

Exercised against the fixture corpus: demo-left 15j -> demo-right 15a is
the one interlink, and the panel puts demo-left on the left."""
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


def _route(channel):
    _p, _c, rules, islands, sig, panels = bbnet.load_data(FIXTURES)
    design = model.derive(islands, sig)
    layout = bbnet._yaml(FIXTURES / "layout.yaml")
    for r in layout["rails"]:
        if channel:
            r["channel"] = channel
    rails = model.rails_from(layout, set(islands))
    routed = render.route_design(islands, design, rules, panels, rails)
    return islands, panels, rails, routed


def _halves(routed):
    out = {}
    for name, (wires, _s, lat) in routed.items():
        for w in wires:
            if w.kind == "interlink" and w.link:
                side = "L" if w.path[-1].x <= lat.x_of("edgeL") else "R"
                out[(name, w.link_end)] = side
    return out


def test_without_a_channel_the_panel_halves_face_each_other():
    _i, panels, _r, routed = _route(None)
    assert _halves(routed) == {("demo-left", "a"): "R",
                               ("demo-right", "b"): "L"}
    assert len(render.seam_links(panels[0], routed)) == 1


@pytest.mark.parametrize("channel, side", [("below", "L"), ("above", "R")])
def test_a_channel_puts_every_half_on_its_edge(channel, side):
    _i, panels, rails, routed = _route(channel)
    assert set(_halves(routed).values()) == {side}
    # no gap crossing on the rail ...
    assert score.rail_metrics(rails[0], routed)["gap_crossings"] == 0
    # ... and the panel no longer draws it across a board
    assert render.seam_links(panels[0], routed) == {}


def test_without_a_channel_the_same_wire_crosses_a_gap():
    _i, _p, rails, routed = _route(None)
    assert score.rail_metrics(rails[0], routed)["gap_crossings"] == 1


@pytest.mark.parametrize("channel", ["below", "above"])
def test_assembly_view_runs_the_wire_in_that_channel(channel):
    islands, _p, rails, routed = _route(channel)
    _svg, links, stubs = render.render_rail(rails[0], islands, routed,
                                            render.DEFAULT_RAIL_TINTS)
    table = render.assembly_table(rails[0], links, stubs, routed)
    assert len(links) == 1 and not stubs
    assert f"<td>{channel}</td>" in table


def test_bad_channel_is_rejected():
    with pytest.raises(model.ModelError, match="channel must be"):
        model.rails_from({"rails": [{"name": "x", "islands": ["demo-left"],
                                     "channel": "left"}]}, {"demo-left"})


def test_channel_maps_to_an_upright_edge():
    assert model.Rail("x", ["a"], channel="below").exit_side == "L"
    assert model.Rail("x", ["a"], channel="above").exit_side == "R"
    assert model.Rail("x", ["a"]).exit_side is None
