"""Render/build-sheet contract tests: the committed fixture artefacts
must be regenerable byte-for-byte (determinism is what lets `bbnet check`
guard them), and the kitting table must carry real work instructions.

Exercised against the synthetic fixture corpus (tests/fixtures/), not
any host project's bench data — the tool's own coverage, portable with
no host project's data present. The magic counts below are measured
against the fixture, not guessed; see the comment at each one."""
from pathlib import Path

import pytest

from bbnet import cli as bbnet
from bbnet import model
from bbnet import render
from bbnet import router
from helpers import registry

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def _fixture_signals(monkeypatch):
    """demo-mcu declares pin_signals but is never allocated -- register
    an empty pinmap source so cli.load_data()/build() don't raise
    UnknownSignalSource (mirrors test_fixtures.py's `fx` fixture)."""
    monkeypatch.setattr(bbnet, "g_signals", registry())


def _islands():
    _p, _c, _r, islands, _pm, _pn = bbnet.load_data(FIXTURES)
    return islands


def _panels():
    _p, _c, _r, _i, _pm, panels = bbnet.load_data(FIXTURES)
    return panels


def _sections():
    """One section per panel plus one per island that stands alone."""
    panels = _panels()
    grouped = {n for p in panels for n in p.islands}
    return len(panels) + len(set(_islands()) - grouped)


def test_render_is_deterministic():
    assert bbnet.layout_html(FIXTURES) == bbnet.layout_html(FIXTURES)


def test_committed_layout_is_in_sync():
    """Guards fixtures/LAYOUT.html the same way `bbnet check` guards a
    host project's real bench copy -- the fixture corpus has no CI gate
    of its own, so this is the only thing that would catch a
    router/render change that forgot to regenerate the committed
    artefact."""
    committed = (FIXTURES / "LAYOUT.html").read_text(encoding="utf-8")
    assert bbnet.layout_html(FIXTURES) == committed, (
        "fixtures/LAYOUT.html out of sync — run: bbnet render "
        "--data-dir tests/fixtures")


def test_committed_report_is_in_sync():
    """Same guard, for REPORT.md -- `bbnet check` bundles this check with
    LAYOUT.html's for the real bench data (see cli.cmd_check); the
    fixture corpus gets no such bundling, so it needs its own test."""
    design, violations, todos = bbnet.build(FIXTURES)
    committed = (FIXTURES / "REPORT.md").read_text(encoding="utf-8")
    assert bbnet.render_report(design, violations, todos) == committed, (
        "fixtures/REPORT.md out of sync — run: bbnet report "
        "--data-dir tests/fixtures")


def test_kitting_has_route_instructions_and_checkboxes():
    islands = _islands()
    routed = router.route_design(islands)
    wires, _stats, lattice = routed["demo-left"]
    html = render.kitting_table(islands["demo-left"], wires, lattice)
    assert "class='cb'" in html
    # underside wires carry the solder-from-beneath instruction; fly
    # wires say where they go instead of pretending to run on-board
    assert "UNDERSIDE — solder from beneath" in html
    assert "off-board fly" in html and "flies up and away" in html
    for w in wires:
        if w.underside:
            assert "beneath" in render._route_text(w, lattice)


def test_wires_are_click_groups():
    html = bbnet.layout_html(FIXTURES)
    # measured: 6 (3 jumpers + 2 leads + 1 seam-stitched interlink) --
    # small margin below that so a harmless render tweak doesn't break
    # this, while losing a whole wire's group still does
    assert html.count('<g class="wire"') > 4
    assert 'data-w=' in html and "addEventListener" in html


def test_layer_toggles():
    # the layers toolbar filters by category: every filterable element
    # must carry its class/data-kind hook, and the checkboxes must exist
    html = bbnet.layout_html(FIXTURES)
    assert '<div class="layers">' in html
    for h in ("part", "res", "cap", "jumper", "lead", "interlink",
              "top", "bot", "label"):
        assert f'data-h="{h}"' in html, h
        assert f"body.hide-{h} " in html, h
    for kind in ("jumper", "lead", "interlink"):
        assert f'data-kind="{kind}"' in html, kind
    # measured: 3 parts (demo-left's U1+J1, demo-right's U2) -- every
    # drawn part, exactly, so no slack is meaningful here
    assert html.count('<g class="lyr-part">') >= 3
    # measured: 11 (3 static CSS selectors + 2 per passive x 4 passives)
    # -- small margin below that
    assert html.count('data-pk=') > 9
    assert 'class="seg-bot"' in html and 'class="seg-top"' in html


def test_passives_in_kitting_and_badge_layout():
    html = bbnet.layout_html(FIXTURES)
    # passives appear as kitting rows, keyed for the kind toggles
    for kind in ("resistor", "ceramic", "electrolytic"):
        assert f"data-kind='{kind}'" in html, kind
    # the underside cap renders dashed and says so in table + title
    assert "UNDERSIDE" in html
    # badge collision avoidance: no two passive badges of one island at
    # the same spot (C12/C13 crossing pair stacked before)
    import re
    badge_rx = re.compile(
        r'lyr-label" data-w="[^"]+" data-pk="[^"]+">'
        r'<rect x="([-\d.]+)" y="([-\d.]+)"')
    total = 0
    for isl_svg in html.split("<h2>")[1:]:
        pos = badge_rx.findall(isl_svg)
        total += len(pos)
        assert len(pos) == len(set(pos)), "stacked passive badges"
    # measured: 4 (one value badge per passive: R1, C1, C2, R2) -- small
    # margin below that; the floor mainly guards against total markup
    # drift (see the message)
    assert total > 2, "badge regex matched too little — markup drifted"


def test_electrolytic_polarity_markers():
    html = bbnet.layout_html(FIXTURES)
    assert 'class="pol"' in html            # +/- glyphs on the drawing
    assert "polarized: + at" in html        # hover title
    assert "(+) → " in html                 # kitting route


def test_dual_pane_shell():
    # KiCad-style page: per-island board|kitting panes, controls in a
    # fixed dock on the far right (not a bar above the boards)
    html = bbnet.layout_html(FIXTURES)
    n = _sections()
    assert html.count('<section class="island">') == n
    assert html.count('<div class="board">') == n
    assert html.count('<div class="kit">') == n
    assert ".island{display:grid" in html
    assert ".layers{position:fixed" in html
    assert 'class="help"' in html                  # legend text collapses
    # panes are resizable (drag divider) and boards zoom independently
    # (pinch/ctrl+scroll is captured per board, never zooms the page)
    assert html.count('<div class="split"') == n
    assert "--kitw" in html
    assert "zreset" in html and "{passive: false}" in html
    # Safari delivers pinch as gesture* events (not ctrl+wheel) — both
    # must be captured or the PAGE zooms; a zoomed board cancels EVERY
    # wheel event (Chromium latches the gesture to the page after one
    # uncancelled event, so scroll-chaining at the clamp broke Brave)
    assert "gesturechange" in html
    assert "pointermove" in html
    assert "e.cancelable" in html


def test_layer_and_offboard_symbology():
    """No vias exist (a wire changes layers only at its own solder
    joints): underside runs get hollow terminal pucks, interlinks land
    on a ghost bus past the board edge, and leads carry an up-and-away
    arrow instead of pretending to run on the surface."""
    # panels=() here, NOT the fixture's committed layout.yaml -- with
    # the "Demo pair" panel applied, the one interlink is seam-stitched
    # and never reaches the ghost-bus code path at all (that stitching
    # behaviour is test_panels.py's job). Rendering with no panels makes
    # both islands' interlink halves fly off as ordinary ghost stubs,
    # which is what this test is actually checking.
    _p, _c, rules, islands, sig, _pn = bbnet.load_data(FIXTURES)
    design = model.derive(islands, sig)
    html = render.render_html(islands, design, rules)
    assert "DIVE" not in html                      # via glyphs are gone
    assert "departs on the UNDERSIDE here" in html
    assert "off-board bus" in html                 # interlink ghost bus
    assert 'class="ghost"' in html
    assert "flies off-board" in html
    # measured: 2 -- exactly one lead-fly arrow per lead (demo-left's
    # "12V input", demo-right's "bench GND"); the fixture has only two
    # leads, so there is no meaningful margin to give up here
    assert html.count("lead-fly") >= 2


# ------------------------------------------------------- 2.5D level layer

def _left_svg():
    _p, _c, rules, islands, sig, panels = bbnet.load_data(FIXTURES)
    design = model.derive(islands, sig)
    routed = router.route_design(islands, design, rules, panels)
    isl = islands["demo-left"]
    wires, stats, lat = routed["demo-left"]
    return render.render_island(isl, wires, stats, lat, {})


def test_level_layer_draws_the_corpus_stack():
    """The fixture carries a worked 2.5D example — a 3V3 fan-out bar at
    level 1 over a MOSFET still lying on the surface. All three new
    object kinds have to reach the build sheet, or the sheet quietly
    stops describing the board."""
    svg = _left_svg()
    assert 'class="lyr-level"' in svg
    assert 'data-level="1"' in svg
    assert "riser 30a" in svg or "riser demo-left:30a" in svg
    assert "LK1 1x5 @ level 1" in svg
    assert "Q1 mosfet 2N7000" in svg


def test_bar_pins_are_drawn_by_their_role():
    """Bonded, clipped and floating pins are three different physical
    facts, so they must not render as the same dot — the sheet is what
    someone builds from."""
    svg = _left_svg()
    # LK1 bonds 30a/34a and clips 31a-33a: two filled pucks, three dashed
    assert svg.count('fill="#b3452b"') >= 2
    assert 'stroke-dasharray="2 2"' in svg


def test_lifted_objects_are_drawn_off_their_holes():
    """A bar drawn flat on its holes answers the opposite of the one
    question this view exists for. Level 1 must be visibly offset."""
    assert render._lift((100.0, 100.0), 0) == (100.0, 100.0)
    up = render._lift((100.0, 100.0), 1)
    assert up != (100.0, 100.0)
    assert render._lift((100.0, 100.0), 2) != up


def test_level_layer_is_absent_when_nothing_is_lifted():
    """demo-right has no risers, bars or devices — it must not gain an
    empty group, or every flat board's sheet grows noise."""
    _p, _c, rules, islands, sig, panels = bbnet.load_data(FIXTURES)
    design = model.derive(islands, sig)
    routed = router.route_design(islands, design, rules, panels)
    isl = islands["demo-right"]
    wires, stats, lat = routed["demo-right"]
    svg = render.render_island(isl, wires, stats, lat, {})
    assert 'class="lyr-level"' not in svg
