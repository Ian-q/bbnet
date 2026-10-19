"""The sheet's four views - pin names, pin numbers, holes, nets. Every
leg label carries all four as tspans and CSS shows one, so the switch
needs no re-render; the nets view adds a tint per node strip and rail,
keyed by net id so one net lights on every board at once."""
import re
from pathlib import Path

import pytest

from bbnet import cli as bbnet
from bbnet import model, render
from helpers import registry

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def _fixture_signals(monkeypatch):
    monkeypatch.setattr(bbnet, "g_signals", registry())


@pytest.fixture
def page():
    return bbnet.layout_html(FIXTURES)


@pytest.fixture
def design():
    _p, _c, _r, islands, sig, _pan = bbnet.load_data(FIXTURES)
    return model.derive(islands, registry())


def _spans(page, name):
    """{view: text} of the leg label whose names view reads `name`."""
    m = re.search(r'((?:<tspan class="vn vn-\w+">[^<]*</tspan>){4})',
                  page[page.index(f'<tspan class="vn vn-name">{name}<'):])
    return dict(re.findall(r'vn-(\w+)">([^<]*)<', m.group(1)))


def test_a_header_pin_carries_every_view(page, design):
    # demo-left J1 is a 4-pin sil header at 40a: TX is its third pin
    v = _spans(page, "TX")
    assert v["name"] == "TX"
    assert v["num"] == "3"
    assert v["hole"] == "42a"
    assert v["net"] == render._net_label(design.net_of_pin("J1", "TX"), 7)


def test_a_device_leg_is_numbered_and_placed(page):
    # demo-left Q1 has no legs: order, so its legs count in pin order
    v = _spans(page, "G")
    assert v["num"] == "1"
    assert v["hole"] == "36a"


def test_unnamed_nets_read_as_a_dash():
    net = model.Net(0, "N$demo:4R", [], [], [], [], [], [])
    assert render._net_label(net, 7) == "–"
    named = model.Net(1, "VERY_LONG_NAME", [], [], [], [], [], [])
    assert render._net_label(named, 7) == "VERY_L…"


def test_passive_ends_are_named_in_the_holes_view(page):
    assert re.search(r'class="vo vo-hole[^"]*"[^>]*>10a<', page)


def test_net_tints_cover_every_used_node(page, design):
    tints = re.findall(r'<rect class="net" data-net="(\d+)"', page)
    assert tints
    # every tint names a real net, and its hover title is that net
    for nid, title in re.findall(
            r'<rect class="net" data-net="(\d+)"[^>]*><title>([^<]*)<',
            page):
        assert design.nets[int(nid)].name == render.html.unescape(title)
    # the strip under J1.VCC (40a) is tinted as J1.VCC's net
    vcc = design.net_of_pin("J1", "VCC")
    assert str(vcc.nid) in tints


def test_names_is_the_default_and_the_print_view(page):
    assert ".vn-num,.vn-hole,.vn-net,.vo{display:none}" in page
    assert 'value="name" checked' in page
    for v in render.VIEWS:
        assert f'name="view" value="{v}"' in page
