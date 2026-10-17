"""`legs:` on a device is its datasheet leg order with the flat face
toward you; bbnet turns it into "flat face toward column a/j". A builder
holding a TO-92 needs that, not just three letters on three holes."""
from pathlib import Path

import pytest

from bbnet import cli as bbnet
from bbnet import model, render
from helpers import registry

FIXTURES = Path(__file__).resolve().parent / "fixtures"
H = model.HoleAddr


@pytest.fixture(autouse=True)
def _fixture_signals(monkeypatch):
    monkeypatch.setattr(bbnet, "g_signals", registry())


def _dev(legs, **holes):
    terms = [model.Terminal(n, a, i) for i, (n, a) in enumerate(holes.items())]
    return model.Device("Q", "mosfet", "2N7000", terms, "x", legs=legs)


def test_datasheet_order_down_the_rows_faces_column_a():
    q = _dev(("S", "G", "D"), S=H("x", 15, "L", "c"), G=H("x", 16, "L", "c"),
             D=H("x", 17, "L", "c"))
    assert model.face_toward(q) == "a"


def test_reversed_order_faces_column_j():
    # a lever FET placed D 5b, G 6b, S 7b
    q = _dev(("S", "G", "D"), D=H("x", 5, "L", "b"), G=H("x", 6, "L", "b"),
             S=H("x", 7, "L", "b"))
    assert model.face_toward(q) == "j"


def test_no_legs_or_scrambled_legs_say_nothing():
    q = _dev((), D=H("x", 5, "L", "b"), G=H("x", 6, "L", "b"),
             S=H("x", 7, "L", "b"))
    assert model.face_toward(q) is None
    q = _dev(("S", "G", "D"), G=H("x", 5, "L", "b"), S=H("x", 6, "L", "b"),
             D=H("x", 7, "L", "b"))
    assert model.face_toward(q) is None


def _island(legs):
    parts = model.parts_lib_from(bbnet._yaml(FIXTURES / "parts.yaml"))
    raw = {p.stem: bbnet._yaml(p) for p in FIXTURES.glob("demo-*.yaml")}
    raw["demo-right"].setdefault("devices", []).append(
        {"ref": "Q7", "kind": "mosfet", "value": "2N7000", "legs": legs,
         "pins": {"D": "22b", "G": "23b", "S": "24b"}})
    return {d["island"]: model.island_from(d, parts) for d in raw.values()}


def test_legs_accepts_a_dashed_string():
    isl = _island("S-G-D")["demo-right"]
    q = next(d for d in isl.devices if d.ref == "Q7")
    assert q.legs == ("S", "G", "D")


def test_bad_legs_are_rejected():
    with pytest.raises(model.ModelError, match="must name each of its pins"):
        _island("S-G-X")


def test_sheet_and_kitting_table_say_which_way():
    islands = _island(["S", "G", "D"])
    html = render.render_html(islands, model.derive(islands, registry()))
    assert "S 24b · G 23b · D 22b" not in html       # listed in board order
    assert "D 22b · G 23b · S 24b — flat face toward column j" in html
    assert 'class="flatface"' in html
