"""The synthetic corpus: the engine's own test bed and worked example.

These assertions are the contract that later generality fixes are proved
against -- a host project's own board data cannot prove them, because it
is exactly the special case that hid the bugs.
"""
from pathlib import Path

import pytest

from bbnet import cli, model, signals

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def fx(monkeypatch):
    """The fixture corpus loaded with its own signal source registered."""
    reg = signals.SignalRegistry()
    reg.register("pinmap", [])          # demo-mcu has no allocations
    monkeypatch.setattr(cli, "g_signals", reg)
    return FIXTURES


def test_fixture_corpus_has_no_drc_errors(fx):
    _design, violations, _todos = cli.build(fx)
    errors = [v for v in violations if v.severity == "error"]
    assert errors == [], f"fixture corpus must stay DRC-clean: {errors}"


def test_fixture_corpus_warnings_are_the_expected_set(fx):
    """Warnings are deliberate -- a sterile fixture would not exercise
    the warning paths. Assert the exact set so drift is visible.

    cli.build() routes, so this also pins the geometry-dependent rules
    (B12 in-node detour, B13 half-row landing) at zero for the corpus:
    a change that starts drawing wires into occupied or body-covered
    holes shows up here as a new rule name, not as a silent redraw."""
    _design, violations, _todos = cli.build(fx)
    rules = sorted({v.rule for v in violations})
    assert rules == ["pinmap-xcheck", "rail-split"]


def test_fixture_panel_stitches_the_cross_island_interlink(fx):
    html = cli.layout_html(fx)
    assert "Demo pair" in html
    assert "seam" in html


def test_fixture_render_is_deterministic(fx):
    assert cli.layout_html(fx) == cli.layout_html(fx)


def test_ravine_keepout_blocks_a_straddling_part():
    """full-830 mounting screws sit in the ravine; a DIP body spanning
    them must be refused. The fixture board carries the keep-outs, so
    this proves the mechanism, not just the data."""
    lib = model.parts_lib_from({
        "mcu-demo": {"kind": "dip", "span": 6,
                     "pins": [str(n) for n in range(1, 13)]}})
    with pytest.raises(model.ModelError, match="mounting-hole row"):
        model.island_from({
            "island": "t", "board": "full-830", "rails": {},
            "parts": [{"ref": "UX", "part": "mcu-demo", "pin1": "29d"}]}, lib)


def _bridged_rails_html(monkeypatch, tmp_path):
    """The fixture corpus with demo-left's two + rails on ONE net."""
    import shutil
    import yaml
    shutil.copytree(FIXTURES, tmp_path / "fx")
    p = tmp_path / "fx" / "demo-left.yaml"
    d = yaml.safe_load(p.read_text())
    d["rails"]["bot+"] = "3V3"          # same net as top+ now
    p.write_text(yaml.safe_dump(d))
    reg = signals.SignalRegistry()
    reg.register("pinmap", [])
    monkeypatch.setattr(cli, "g_signals", reg)
    return cli.layout_html(tmp_path / "fx")


def test_pwr_pad_warning_only_when_plus_rails_differ(fx):
    """Split rails -> warn. This is a real host board's case and must
    keep working."""
    assert "NEVER bridge" in cli.layout_html(fx)


def test_no_pwr_pad_warning_when_plus_rails_are_one_net(
        monkeypatch, tmp_path):
    """A board whose + rails ARE one net must not be told never to bridge
    them. The label was previously unconditional on every railed board."""
    assert "NEVER bridge" not in _bridged_rails_html(monkeypatch, tmp_path)


def test_pwr_pad_says_nothing_about_sameness_when_a_strip_is_undeclared(fx):
    """demo-right (half-400) declares only top+ -- bot+ never appears in
    its rails: mapping at all. The model has verified nothing about
    bot+ (it may carry no net, or one this design just never mentions),
    so the label must neither warn (nothing is known to conflict) nor
    claim "one net" (that claim was never established). A formula that
    reaches "one net" via `len({declared nets}) == 1` gets this wrong,
    because that is also true when only one strip was ever declared."""
    html = cli.layout_html(fx)
    demo_right = html[html.find(">demo-right"):]
    assert "one net" not in demo_right
    assert "NEVER bridge" not in demo_right


def test_page_title_comes_from_layout_yaml(fx):
    """The heading is data, not a constant. Asserting the generic
    fallback is ABSENT is the load-bearing half: it proves the title was
    read from layout.yaml rather than defaulted to."""
    html = cli.layout_html(fx)
    assert "Demo pair" in html
    assert "Breadboard layout — routed build sheet" not in html


def test_rail_tints_come_from_colours_yaml(monkeypatch, tmp_path):
    """A project's own rail names must be tintable; previously anything
    outside one particular host project's net names fell through to
    grey."""
    import shutil
    import yaml
    shutil.copytree(FIXTURES, tmp_path / "fx")
    p = tmp_path / "fx" / "colours.yaml"
    d = yaml.safe_load(p.read_text())
    d["rail_tints"] = {"3V3": "#123456"}
    p.write_text(yaml.safe_dump(d))
    reg = signals.SignalRegistry()
    reg.register("pinmap", [])
    monkeypatch.setattr(cli, "g_signals", reg)
    assert "#123456" in cli.layout_html(tmp_path / "fx")
