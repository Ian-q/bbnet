"""Diodes and LEDs follow the electrolytic convention: `from` is the +
end, i.e. the ANODE, and `to` is the cathode. The build sheet marks the
cathode with a band and a K, and the kitting table says which end is
which - a builder holding a zener needs to know where the band goes."""
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


def _sheet(kind):
    parts = model.parts_lib_from(bbnet._yaml(FIXTURES / "parts.yaml"))
    raw = {p.stem: bbnet._yaml(p) for p in FIXTURES.glob("demo-*.yaml")}
    raw["demo-right"]["passives"].append(
        {"ref": "D9", "kind": kind, "value": "1N4148",
         "from": "22b", "to": "26b"})
    islands = {d["island"]: model.island_from(d, parts)
               for d in raw.values()}
    return render.render_html(islands, model.derive(islands, registry()))


@pytest.mark.parametrize("kind", ["diode", "led"])
def test_band_is_drawn_at_the_cathode_end(kind):
    html = _sheet(kind)
    m = re.search(r'<line class="seg-top band" x1="([\d.]+)" '
                  r'y1="([\d.]+)"[^>]*><title>D9[^<]*cathode \(band\) '
                  r'at 26b', html)
    assert m, "no band drawn for D9"
    # rows run down the sheet: the band sits nearer row 26 than row 22
    band_y = float(m.group(2))
    rows = [float(y) for y in re.findall(
        r'<line class="seg-top" x1="[\d.]+" y1="([\d.]+)" x2="[\d.]+" '
        r'y2="[\d.]+" [^>]*><title>D9', html)]
    assert rows, "no body line for D9"
    assert band_y > rows[0]


def test_kitting_table_names_the_ends():
    assert "22b(A) → 26b(K, band)" in _sheet("diode")


def test_resistors_get_no_band():
    html = _sheet("diode")
    assert not re.search(r'band"[^>]*><title>R2', html)


def test_kitting_table_lists_parts_and_devices_with_their_holes():
    html = _sheet("diode")
    # demo-right's U2 is a 0.3" dip at 8c: pin 1 in 8c, pin 8 opposite
    assert "<td>U2 Demo octal</td>" in html
    assert re.search(r"<td>U2 Demo octal</td><td>1 8c · [^<]*8 8h", html)
    # a device's legs are listed in board order
    assert re.search(r"<td>Q1 2N7000</td><td>G 36a · D 37a · S 38a</td>",
                     html)
