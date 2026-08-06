"""Panel contract tests: boards that sit side by side on the bench
(layout.yaml) render as ONE board view, and the interlinks between them
are drawn whole across the seam — once, landing on the real hole, and
kitted once. Islands named by no panel must be untouched by all of it.

Exercised against the synthetic fixture corpus (tests/fixtures/), not
any host project's private bench data — this is the tool's own coverage
of seam stitching, stack ordering, label flipping, and cross-board
kitting, and must hold with no host project's data present at all."""
import re
from pathlib import Path

import pytest

from bbnet import cli as bbnet
from bbnet import model
from bbnet import render
from helpers import registry

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Any concrete palette works for these tests -- they assert on wire
# geometry and stitching, never on colour -- so the built-in defaults
# stand in for a project's own colours.yaml.
TINTS = render.DEFAULT_RAIL_TINTS


@pytest.fixture(autouse=True)
def _fixture_signals(monkeypatch):
    """demo-mcu declares pin_signals but is never allocated -- register
    an empty pinmap source so load_data()/derive() don't raise
    UnknownSignalSource (mirrors test_fixtures.py's `fx` fixture)."""
    monkeypatch.setattr(bbnet, "g_signals", registry())


def _loaded():
    _p, _c, rules, islands, sig, panels = bbnet.load_data(FIXTURES)
    design = model.derive(islands, sig)
    routed = render.route_design(islands, design, rules, panels)
    return islands, panels, routed


def _cross_panel_interlinks(islands, panel):
    """Interlinks the bench really runs between two boards of a panel."""
    return [(name, i)
            for name in panel.islands
            for i, j in enumerate(islands[name].interlinks)
            if not j.offgrid
            and getattr(j.b, "island", None) in panel.islands]


# ------------------------------------------------------- layout.yaml

def test_layout_declares_the_butted_demo_pair():
    _islands, panels, _routed = _loaded()
    assert panels, "layout.yaml declares no panel"
    pair = [p for p in panels
            if set(p.islands) == {"demo-left", "demo-right"}]
    assert len(pair) == 1
    # left-to-right order is the bench order, and it decides exit sides
    assert pair[0].islands == ["demo-left", "demo-right"]
    assert pair[0].side_of("demo-left", "demo-right") == "R"
    assert pair[0].side_of("demo-right", "demo-left") == "L"


@pytest.mark.parametrize("bad,msg", [
    ({"panels": [{"name": "p", "islands": ["demo-left", "nope"]}]},
     "unknown island"),
    ({"panels": [{"name": "p", "islands": ["demo-left"]}]},
     "at least two islands"),
    ({"panels": [{"name": "a", "islands": ["demo-left", "demo-right"]},
                 {"name": "b", "islands": ["demo-left", "mini"]}]},
     "two panels"),
    ({"panels": [{"islands": ["demo-left", "demo-right"]}]},
     "missing 'name:'"),
    ({"panels": [{"name": "p", "islands": ["demo-left", "demo-right"],
                  "seam": -5}]}, "non-negative"),
    ({"panels": [{"name": "p", "islands": ["demo-left", "demo-right"]},
                 {"name": "p", "islands": ["mini", "demo-left"]}]},
     "duplicate panel name"),
])
def test_bad_layout_is_rejected_with_a_useful_message(bad, msg):
    with pytest.raises(model.ModelError, match=msg):
        model.panels_from(bad, {"demo-left", "demo-right", "mini"})


def test_no_layout_file_means_every_island_stands_alone():
    assert model.panels_from({}, {"demo-left"}) == []


# ---------------------------------------------------------- stitching

def test_every_cross_panel_interlink_is_stitched():
    islands, panels, routed = _loaded()
    for panel in panels:
        links = render.seam_links(panel, routed)
        assert len(links) == len(_cross_panel_interlinks(islands, panel))
        for _link, (a, b) in links.items():
            assert a[0] != b[0], "a seam link joins two different boards"
            assert a[0] in panel.islands and b[0] in panel.islands


def _wire_group(svg, key):
    i = svg.index(f'data-w="{key}"')
    return svg[i:svg.index("</g>", i)]


def test_seam_wire_lands_on_the_far_board_real_hole():
    """The whole point: the far end is the destination hole itself, not
    a ghost bus stub — so the stitched wire's own puck must sit on the
    far island's hole pixel, shifted by that board's offset in the
    panel. Checked inside the wire's <g>, so a puck belonging to the
    far board's own art cannot satisfy it."""
    islands, panels, routed = _loaded()
    panel = panels[0]
    svg, links = render.render_panel(panel, islands, routed, TINTS)
    assert links
    for _link, ((an, aw), (bn, bw)) in links.items():
        g = _wire_group(svg, aw.key)
        for name, w in ((an, aw), (bn, bw)):
            _body, _labs, px = render.island_body(islands[name],
                                                  *routed[name][:3],
                                                  rail_tints=TINTS)
            dx = _offset(panel, islands, routed, name)
            hole = w.path[0]     # path[0] is the real terminal on-board
            cx = round(px.x(hole.x) + dx, 1)
            cy = px.y(hole.y)
            assert f'cx="{cx}" cy="{cy}"' in g, (
                f"{aw.label}: no puck on {name} at ({cx},{cy})")


def _offset(panel, islands, routed, name):
    x = 0.0
    for n in panel.islands:
        if n == name:
            return x
        _b, _l, px = render.island_body(islands[n], *routed[n][:3],
                                        rail_tints=TINTS)
        x += px.width - (2 * render.MARGIN - panel.seam)
    raise AssertionError(name)


def test_stitched_wire_is_drawn_once_not_twice():
    islands, panels, routed = _loaded()
    panel = panels[0]
    svg, links = render.render_panel(panel, islands, routed, TINTS)
    for _link, ((_an, aw), (_bn, bw)) in links.items():
        assert svg.count(f'data-w="{aw.key}"') == 1, "declaring half"
        assert f'data-w="{bw.key}"' not in svg, "far board redrew its stub"


def test_interlink_leaving_the_panel_keeps_its_ghost_bus():
    """demo-left's interlink targets demo-right; render it inside a
    panel that has demo-left alone (demo-right is a panel-external
    island for this render) — that wire must still stub out to a ghost
    bus rather than silently vanish."""
    islands, _panels, routed = _loaded()
    solo = model.Panel(name="solo", islands=["demo-left"])
    svg, links = render.render_panel(solo, islands, routed, TINTS)
    outbound = [w for w in routed["demo-left"][0]
                if w.kind == "interlink" and w.link not in links
                and "demo-right" in w.label]
    assert outbound, "expected an interlink out of the panel"
    assert 'class="ghost"' in svg


# ------------------------------------------------------------ kitting

def test_a_seam_wire_is_kitted_once_on_the_board_that_declares_it():
    islands, panels, routed = _loaded()
    panel = panels[0]
    links = render.seam_links(panel, routed)
    tables = {n: render.kitting_table(islands[n], routed[n][0],
                                      routed[n][2], links)
              for n in panel.islands}
    for _link, ((an, aw), (bn, bw)) in links.items():
        assert f"data-w='{aw.key}'" in tables[an]
        assert f"data-w='{bw.key}'" not in tables[bn]
    assert sum(t.count("over the seam") for t in tables.values()) == len(links)


def test_seam_stack_is_shortest_first_and_covers_every_link():
    """The stack ordinal is a solder instruction: lay the shortest wire
    flat first, then arch each longer one over the bundle already down.
    That only works if the order really is monotonic in cut length."""
    islands, panels, routed = _loaded()
    links = render.seam_links(panels[0], routed)
    stack = render.seam_stack(links)
    assert set(stack) == set(links), "every seam wire gets a stack slot"
    assert sorted(stack.values()) == list(range(1, len(links) + 1))
    lengths = [render.seam_length_mm(*(h[1] for h in links[k]))
               for k in sorted(links, key=lambda k: stack[k])]
    assert lengths == sorted(lengths), f"stack not shortest-first: {lengths}"


def test_kitting_lists_seam_wires_first_in_stack_order():
    """A scattered ordinal is not an instruction — the seam rows lead the
    table in the order the bench solders them."""
    islands, panels, routed = _loaded()
    panel = panels[0]
    links = render.seam_links(panel, routed)
    table = render.kitting_table(islands["demo-left"], routed["demo-left"][0],
                                 routed["demo-left"][2], links)
    seen = [int(m) for m in re.findall(r"stack (\d+)/", table)]
    assert seen, "expected seam rows in the declaring board's table"
    assert seen == sorted(seen), f"seam rows out of stack order: {seen}"
    # and they lead: no non-seam row may precede the last seam row
    body = table[:table.rindex("stack ")]
    assert body.count("<tr class='krow'") == len(seen)


def test_seam_signals_are_routed_on_the_top_face():
    """Rule: interlinks stitched across a seam are routed on the top
    face stack, length-ordered — an underside run is blind to solder and
    forces the board off the plate to change even one wire, so a seam
    stitch never uses one."""
    islands, panels, routed = _loaded()
    links = render.seam_links(panels[0], routed)
    under = [links[k][0][1].label for k in links
             if links[k][0][1].underside or links[k][1][1].underside]
    assert under == [], f"unexpected underside seam wire(s): {under}"


def test_seam_cut_length_beats_either_half_alone():
    islands, panels, routed = _loaded()
    links = render.seam_links(panels[0], routed)
    (_an, aw), (_bn, bw) = next(iter(links.values()))
    assert render.seam_length_mm(aw, bw) >= max(render.wire_length_mm(aw),
                                                render.wire_length_mm(bw))


# ------------------------------------------------------------- labels

def test_tight_seam_flips_edge_labels_to_the_outer_margin():
    """A seam-facing side has only the gutter; rather than print a stub
    nobody can read, the label moves to this board's own outer margin
    with an arrow showing which way the wire actually leaves."""
    islands, panels, routed = _loaded()
    panel = panels[0]
    assert panel.seam == 90, "this test assumes the fixture's butted seam"
    svg, _links = render.render_panel(panel, islands, routed, TINTS)
    # demo-right's lead naturally prints on edgeL, which faces the seam
    # here -- it flips to edgeR and picks up a "this way out" arrow
    assert "← " in svg or " →</text>" in svg or " →<title" in svg
    # and the leads that exit toward the seam are still named somewhere
    assert "bench GND" in svg


def test_label_that_does_not_fit_keeps_its_full_text_on_hover():
    shown, full = render._fit_label("a very long lead label indeed", 60)
    assert shown.endswith("…") and full == "a very long lead label indeed"
    assert len(shown) < len(full)


def test_standalone_island_labels_are_not_flipped():
    islands, _panels, routed = _loaded()
    body, _labs, _px = render.island_body(islands["demo-left"],
                                          *routed["demo-left"][:3],
                                          rail_tints=TINTS)
    assert " →</text>" not in body and "← " not in body


# -------------------------------------------------------------- shell

def test_page_has_one_section_per_panel_plus_each_lone_island():
    islands, panels, _routed = _loaded()
    html = bbnet.layout_html(FIXTURES)
    grouped = {n for p in panels for n in p.islands}
    assert html.count('<section class="island">') == \
        len(panels) + len(set(islands) - grouped)
    # the panel's pane holds a kitting table per member board
    for name in panels[0].islands:
        assert f"kitting table — {name}" in html


def test_panel_labels_are_painted_after_the_seam_wires():
    """A stitched interlink is drawn by the PANEL, after every island
    body — so a label deferred only within its own body still ends up
    underneath it. The row numbers on seam-facing rows were exactly
    that: reserved a strip, painted last within the board, and buried
    anyway by the wires crossing to the next board."""
    islands, panels, routed = _loaded()
    svg, _links = render.render_panel(panels[0], islands, routed, TINTS)
    last_seam = svg.rfind('data-kind="interlink"')
    assert last_seam > 0, "no stitched seam wire in this panel"
    assert svg.find('class="rn halo"', last_seam) > 0, (
        "row numbers are painted before the last seam wire")
