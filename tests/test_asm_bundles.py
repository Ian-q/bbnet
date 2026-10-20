"""Assembly-view bundles: the wires between one pair of boards share a
block of adjacent lanes in the channel, with a wider gap between blocks,
so each pair's wires can be cut, laid and traced as one harness. Wires
that leave a board at the same row jog apart instead of dropping down
one line. Found on a real build (2026-10-20): six jetson-ctl ->
jetson-grp wires were interleaved with longer runs, and two pairs of
drops sat exactly on top of each other."""

import pytest

from bbnet import cli as bbnet
from bbnet import render
from bbnet.render import ASM_BUNDLE_GAP, ASM_LANE, _asm_bundle_pack

from helpers import registry
from test_assembly import TINTS, _loaded


@pytest.fixture(autouse=True)
def _fixture_signals(monkeypatch):
    monkeypatch.setattr(bbnet, "g_signals", registry())


def test_a_bundle_takes_adjacent_lanes():
    # A short pair bundle and a long run that overlaps it: the long run
    # must not land between the bundle's wires.
    off, depth = _asm_bundle_pack([
        ("long", "AC", 0, 300),
        ("ab1", "AB", 10, 100), ("ab2", "AB", 20, 90),
        ("ab3", "AB", 5, 110)])
    ab = sorted(off[k] for k in ("ab1", "ab2", "ab3"))
    assert ab == [0, ASM_LANE, 2 * ASM_LANE]
    assert off["long"] == 3 * ASM_LANE + ASM_BUNDLE_GAP
    assert depth == off["long"] + ASM_LANE


def test_shortest_run_in_a_bundle_hugs_the_boards():
    off, _d = _asm_bundle_pack([("wide", "AB", 0, 100),
                                ("narrow", "AB", 40, 60)])
    assert off["narrow"] < off["wide"]


def test_bundles_that_do_not_overlap_share_the_first_lane():
    off, depth = _asm_bundle_pack([("a", "AB", 0, 50), ("b", "CD", 100, 150)])
    assert off == {"a": 0, "b": 0} and depth == ASM_LANE


def test_a_bundle_fits_under_a_gap_left_above_it():
    # CD sits deep (pushed out by AB+EF); a short block that overlaps only
    # CD's span may not slot between bundles without the gap either side
    off, _d = _asm_bundle_pack([
        ("ab", "AB", 0, 40), ("cd1", "CD", 0, 200), ("cd2", "CD", 0, 200)])
    assert off["cd1"] >= ASM_LANE + ASM_BUNDLE_GAP


def test_the_demo_rail_still_draws_every_link():
    islands, rails, routed = _loaded()
    svg, links, _s = render.render_rail(rails[0], islands, routed, TINTS)
    for _k, ((_ia, wa), _b) in links.items():
        assert f'data-w="{wa.key}"' in svg


class _W:
    def __init__(self, key):
        self.key = key


def test_a_bundle_tag_selects_both_halves_of_every_wire():
    links = {"x#0": ((0, _W("a0")), (1, _W("b0"))),
             "x#1": ((0, _W("a1")), (1, _W("b1"))),
             "y#0": ((0, _W("lone")), (2, _W("far")))}
    spans = {"bot": [("x#0", (0, 1), 10, 60), ("x#1", (0, 1), 20, 50),
                     ("y#0", (0, 2), 10, 200)], "top": []}
    lanes = {"x#0": 7, "x#1": 0, "y#0": 23}
    out = render._asm_bundle_handles(
        links, spans, {}, lanes, lambda side, off: 100 + off,
        ["ctl", "grp", "mon"])
    assert len(out) == 1          # a single wire is no bundle
    assert "ctl ↔ grp · 2" in out[0]
    for k in ("a0", "b0", "a1", "b1"):
        assert f"&quot;{k}&quot;" in out[0]
    assert "lone" not in out[0]
