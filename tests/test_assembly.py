"""Assembly-view contract: layout.yaml `rails:` puts boards on a DIN rail,
each turned 90 degrees counter-clockwise the way its carrier holds it, and
the wires between two boards on the rail are drawn whole through a channel
above or below it. The upright sheets stay the focus view, one click away.

Exercised against the synthetic fixture corpus (tests/fixtures/)."""
import re
from pathlib import Path

import pytest

from bbnet import cli as bbnet
from bbnet import model
from bbnet import render
from helpers import registry

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TINTS = render.DEFAULT_RAIL_TINTS


@pytest.fixture(autouse=True)
def _fixture_signals(monkeypatch):
    monkeypatch.setattr(bbnet, "g_signals", registry())


def _loaded():
    _p, _c, rules, islands, sig, panels = bbnet.load_data(FIXTURES)
    design = model.derive(islands, sig)
    routed = render.route_design(islands, design, rules, panels)
    rails = model.rails_from(bbnet._yaml(FIXTURES / "layout.yaml"),
                             set(islands))
    return islands, rails, routed


def test_layout_declares_the_demo_rail():
    _i, rails, _r = _loaded()
    assert [(r.name, r.islands) for r in rails] == [
        ("Demo rail", ["demo-left", "demo-right"])]


@pytest.mark.parametrize("bad, msg", [
    ({"rails": [{"islands": ["demo-left"]}]}, "missing 'name:'"),
    ({"rails": [{"name": "x", "islands": []}]}, "no islands"),
    ({"rails": [{"name": "x", "islands": ["nope"]}]}, "unknown island"),
    ({"rails": [{"name": "x", "islands": ["demo-left", "demo-left"]}]},
     "listed twice"),
    ({"rails": [{"name": "x", "islands": ["demo-left"]},
                {"name": "y", "islands": ["demo-left"]}]}, "two rails"),
    ({"rails": [{"name": "x", "islands": ["demo-left"], "gap": -1}]},
     "gap must be"),
])
def test_bad_rail_is_rejected_with_a_useful_message(bad, msg):
    with pytest.raises(model.ModelError, match=msg):
        model.rails_from(bad, {"demo-left", "demo-right"})


def test_no_rails_means_no_assembly_view():
    assert model.rails_from({}, {"demo-left"}) == []


def test_every_board_is_turned_counter_clockwise():
    """matrix(0 -1 1 0 e f) maps upright (x, y) to (y + e, f - x): row 1
    lands on the left and column a (small x) at the bottom - the way the
    carrier holds the board."""
    islands, rails, routed = _loaded()
    svg, _l, _s = render.render_rail(rails[0], islands, routed, TINTS)
    turns = re.findall(r'class="asm-board" data-island="([^"]+)" '
                       r'transform="matrix\(0 -1 1 0 ', svg)
    assert turns == ["demo-left", "demo-right"]


def test_rail_mates_are_wired_whole_and_others_get_a_stub():
    islands, rails, routed = _loaded()
    svg, links, stubs = render.render_rail(rails[0], islands, routed, TINTS)
    expected = {k for k, _ in render.seam_links(
        model.Panel("p", rails[0].islands), routed).items()}
    assert set(links) == expected and links
    for k, ((_ia, wa), _b) in links.items():
        assert f'data-w="{wa.key}"' in svg


def test_board_names_link_to_their_upright_sheets():
    html = bbnet.layout_html(FIXTURES)
    for name in ("demo-left", "demo-right"):
        anchor = render.island_anchor(name)
        assert f'href="#{anchor}"' in html
        assert f'id="{anchor}"' in html


def test_assembly_section_comes_before_the_sheets():
    html = bbnet.layout_html(FIXTURES)
    assert html.index('class="island stacked assembly"') < \
        html.index('<section class="island">')
