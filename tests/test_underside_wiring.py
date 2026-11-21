"""`wiring: underside` - the clean-top, messy-bottom build.

Parts only on the top face; every wire soldered underneath as a loose
point-to-point run that may cross any other, traced by hand with the
board turned over on its hinge. The sheet adds a bottom view drawn the
way the board then looks (mirrored as if rotated, not x-ray), straight
under the upright one, which keeps its x-ray of the same runs."""
import copy
import re
import shutil
from pathlib import Path

import pytest
import yaml

from bbnet import cli as bbnet
from bbnet import model, render, score
from bbnet.router import BOT
from helpers import registry

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def _fixture_signals(monkeypatch):
    monkeypatch.setattr(bbnet, "g_signals", registry())


def _raw():
    return {p.stem: bbnet._yaml(p) for p in FIXTURES.glob("demo-*.yaml")}


def _islands(raw):
    parts = model.parts_lib_from(bbnet._yaml(FIXTURES / "parts.yaml"))
    return {d["island"]: model.island_from(d, parts) for d in raw.values()}


def _loose(raw, name="demo-left", **extra):
    raw = copy.deepcopy(raw)
    raw[name]["wiring"] = "underside"
    raw[name].update(extra)
    return raw


def _route(raw):
    islands = _islands(raw)
    design = model.derive(islands, registry())
    return islands, design, render.route_design(islands, design)


def test_top_is_the_default_and_nothing_changes():
    islands = _islands(_raw())
    assert islands["demo-left"].wiring == "top"
    assert islands["demo-left"].flip == "cols"
    # the one declared underside run stays the only one
    assert [j.underside for j in islands["demo-left"].jumpers] == [
        False, False, True]


@pytest.mark.parametrize("key,bad", [("wiring", "bottom"),
                                     ("flip", "sideways")])
def test_a_misspelt_mode_is_refused(key, bad):
    raw = _raw()
    raw["demo-left"][key] = bad
    with pytest.raises(model.ModelError, match=f"{key} must be one of"):
        _islands(raw)


def test_every_wire_goes_underneath():
    islands = _islands(_loose(_raw()))
    isl = islands["demo-left"]
    assert all(j.underside for j in isl.jumpers + isl.interlinks)
    assert all(w.underside for w in isl.leads)


def test_a_wire_can_still_opt_back_onto_the_top():
    raw = _loose(_raw())
    raw["demo-left"]["jumpers"][0]["underside"] = False
    isl = _islands(raw)["demo-left"]
    assert [j.underside for j in isl.jumpers] == [False, True, True]


def test_no_top_routing_and_every_run_hangs_underneath():
    _islands_, _d, routed = _route(_loose(_raw()))
    wires, stats, lattice = routed["demo-left"]
    assert stats.failed == 0
    assert wires, "the island still has its wires"
    for w in wires:
        assert w.underside, w.label
        # a run is soldered at its two ends: top-hole, under, under,
        # top-hole - nothing in the hole grid between
        assert len(w.path) == 4 and w.path[1].layer == BOT
    assert stats.underside == len(wires)


def test_a_rail_end_lands_level_with_the_other_end():
    """A rail is one node the length of the board; the old airwire took
    its first cell (row 1) whatever the far end was. Loose runs take the
    rail hole nearest their other end."""
    _i, _d, routed = _route(_loose(_raw()))
    wires, _s, lattice = routed["demo-left"]
    w = next(w for w in wires if w.label.startswith("40e→rail:"))
    rail_end = [c for c in (w.path[0], w.path[-1])
                if lattice.is_rail(c.x)][0]
    assert abs(rail_end.y - 40) <= 1


def test_two_runs_into_one_node_take_different_holes():
    raw = _loose(_raw())
    raw["demo-left"]["jumpers"] += [
        {"from": "30L", "to": "33L", "colour": "BLU"},
        {"from": "30L", "to": "36L", "colour": "BLU"}]
    _i, _d, routed = _route(raw)
    wires, _s, lattice = routed["demo-left"]
    ends = [(w.path[0].x, w.path[0].y) for w in wires
            if w.path[0].y == 30 and lattice.half(w.path[0].x) == "L"]
    assert len(ends) == 2 and len(set(ends)) == 2


def test_score_does_not_count_by_design_underside_runs():
    islands, design, routed = _route(_loose(_raw()))
    s = score.score(design, islands, routed, [])
    left = next(i for i in s["islands"] if i["island"] == "demo-left")
    assert left["wiring"] == "underside"
    assert left["underside"] == 0
    assert "WIRED UNDERSIDE" in score.format_text(s)


def test_the_circuit_is_the_same_circuit():
    """Moving every wire underneath re-lays the board; it must not
    rewire it."""
    base = _raw()
    i0, d0, _r0 = _route(base)
    i1, d1, _r1 = _route(_loose(base))
    assert score.fingerprint(d0, i0) == score.fingerprint(d1, i1)


# ---------------------------------------------------------------- render

@pytest.fixture
def loose_dir(tmp_path):
    out = tmp_path / "fx"
    shutil.copytree(FIXTURES, out)
    p = out / "demo-right.yaml"
    d = yaml.safe_load(p.read_text())
    d["wiring"] = "underside"
    p.write_text(yaml.safe_dump(d, sort_keys=False))
    return out


def _uv(page):
    m = re.search(r'<svg class="uv-sheet".*?</svg>', page, re.S)
    assert m, "no underside view"
    return m.group(0)


def test_the_bottom_view_follows_the_top_view(loose_dir):
    page = bbnet.layout_html(loose_dir)
    assert page.count('class="uv-sheet"') == 1
    # straight under the panel holding demo-right, before anything else
    panel = page.index("Demo pair")
    under = page.index("demo-right <span class=\"sub\">— underside")
    assert panel < under
    assert "board turned over" in _uv(page)


def test_only_underside_boards_get_one():
    page = bbnet.layout_html(FIXTURES)
    assert 'class="uv-sheet"' not in page


def test_the_x_ray_stays_in_the_upright_view(loose_dir):
    page = bbnet.layout_html(loose_dir)
    upright = page[:page.index('class="uv-sheet"')]
    assert 'class="seg-bot"' in upright
    assert 'name="actlyr" value="bot"' in page


def test_bottom_view_mirrors_columns_by_default(loose_dir):
    """Turned over like a page, column j is now the leftmost letter."""
    uv = _uv(bbnet.layout_html(loose_dir))
    letters = re.findall(r'<text x="([\d.]+)" y="[\d.]+" class="cn" '
                         r'text-anchor="middle">([a-j])</text>', uv)
    xs = {}
    for x, letter in letters:
        xs.setdefault(letter, float(x))
    assert xs["j"] < xs["a"]


def test_flip_rows_mirrors_rows_instead(loose_dir):
    p = loose_dir / "demo-right.yaml"
    d = yaml.safe_load(p.read_text())
    d["flip"] = "rows"
    p.write_text(yaml.safe_dump(d, sort_keys=False))
    uv = _uv(bbnet.layout_html(loose_dir))
    letters = dict((b, float(a)) for a, b in re.findall(
        r'<text x="([\d.]+)" y="[\d.]+" class="cn" '
        r'text-anchor="middle">([a-j])</text>', uv))
    assert letters["a"] < letters["j"]
    rows = {}
    for y, r in re.findall(r'<text x="[\d.]+" y="([\d.]+)" class="rn[^"]*" '
                           r'text-anchor="middle">(\d+)</text>', uv):
        rows.setdefault(int(r), float(y))
    assert rows[1] > rows[30]
    assert "row 1 is at the BOTTOM" in uv


def test_text_is_never_mirrored(loose_dir):
    uv = _uv(bbnet.layout_html(loose_dir))
    assert "scale(-1" not in uv


def test_bottom_wires_share_keys_with_the_upright_view(loose_dir):
    """One key per wire across both views and the kitting table, so a
    click isolates it everywhere at once."""
    page = bbnet.layout_html(loose_dir)
    uv = _uv(page)
    keys = set(re.findall(r'<g class="wire" data-w="([^"]+)"', uv))
    assert keys
    upright = page[:page.index('class="uv-sheet"')]
    for k in keys:
        assert f'data-w="{k}"' in upright


def test_legs_show_as_joints_and_parts_as_ghosts(loose_dir):
    uv = _uv(bbnet.layout_html(loose_dir))
    assert "U2.1" in uv                   # a leg's solder joint
    assert "(on the top face)" in uv      # its body, ghosted
    assert 'class="uvref halo"' in uv


@pytest.mark.parametrize("name", ["demo-left", "demo-right"])
def test_loose_board_passes_check(tmp_path, name):
    """demo-left carries risers, a link bar and devices; demo-right a
    dip and a panel stub: both must render and check clean."""
    loose_dir = tmp_path / "fx"
    shutil.copytree(FIXTURES, loose_dir)
    p = loose_dir / f"{name}.yaml"
    d = yaml.safe_load(p.read_text())
    d["wiring"] = "underside"
    p.write_text(yaml.safe_dump(d, sort_keys=False))
    assert bbnet.main(["render", "--data-dir", str(loose_dir)]) == 0
    assert bbnet.main(["report", "--data-dir", str(loose_dir)]) == 0
    assert bbnet.main(["check", "--data-dir", str(loose_dir)]) == 0
