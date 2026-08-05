"""End-to-end CLI tests against a temp data dir."""
import pytest

from bbnet import cli as bbnet
from bbnet.model import ModelError

PARTS = "dip8-adapter:\n  kind: dip\n  pins: ['1','2','3','4','5','6','7','8']\n"
COLOURS = ("vocabulary: [RED, BLK, YEL, BLU, GRN]\n"
           "classes:\n  - {match: '^GND$', colours: [BLK, BLU]}\n")
RULES = ("ties: []\n"
         "pins:\n"
         "  U2.OUT:\n"
         "    - {decouple: {to: GND, kind: [electrolytic]}}\n"
         "    - {decouple: {to: GND, kind: [ceramic]}}\n")
SNAKE_YAML = """\
island: bb1
board: mini-170
parts:
  - ref: U1
    value: buck
    pins: {IN+: 2R, IN-: 3R, OUT+: 5R, OUT-: 7R}
    internal_ties: [[IN-, OUT-]]
    seeds: {OUT+: 5V}
  - ref: U2
    value: ldo
    pins: {IN: 14R, GND: 13R, OUT: 12R}
jumpers:
  - {from: 12R, to: 11R, colour: BLU}
  - {from: 9R, to: 7R, colour: BLU}
  - {from: 5R, to: 14R, colour: RED}
  - {from: 13R, to: 9R, colour: BLU}
passives:
  - {ref: C1, kind: electrolytic, from: 11R, to: 7R}
  - {ref: C2, kind: ceramic, from: 11R, to: 9R}
leads:
  - {at: 2R, colour: YEL, net: 12V, label: 12V+ in}
  - {at: 3R, colour: BLU, net: GND, label: 12V- in}
  - {at: 12R, colour: RED, net: 3V3, label: 3.3V out}
"""


def mkdata(tmp_path, island=SNAKE_YAML, rules=RULES):
    d = tmp_path / "breadboard"
    d.mkdir()
    (d / "parts.yaml").write_text(PARTS)
    (d / "colours.yaml").write_text(COLOURS)
    (d / "rules.yaml").write_text(rules)
    (d / "bb1.yaml").write_text(island)
    return d


def test_report_then_check_clean(tmp_path, capsys):
    d = mkdata(tmp_path)
    assert bbnet.main(["report", "--data-dir", str(d)]) == 0
    report = (d / "REPORT.md").read_text()
    assert "GENERATED" in report and "3V3" in report and "C2" in report
    # check guards LAYOUT.html in-sync too, so a clean pass needs both
    # generated artifacts present
    assert bbnet.main(["render", "--data-dir", str(d)]) == 0
    assert bbnet.main(["check", "--data-dir", str(d)]) == 0
    out = capsys.readouterr().out
    assert "0 error(s)" in out


def test_check_detects_report_drift(tmp_path, capsys):
    d = mkdata(tmp_path)
    bbnet.main(["report", "--data-dir", str(d)])
    # mutate topology the report renders: drop C2's edge
    (d / "bb1.yaml").write_text(SNAKE_YAML.replace(
        "  - {ref: C2, kind: ceramic, from: 11R, to: 9R}\n", ""))
    assert bbnet.main(["check", "--data-dir", str(d)]) == 1
    assert "DRIFT" in capsys.readouterr().out


def test_check_fails_on_error_violation(tmp_path, capsys):
    # drop the 9R->7R jumper: ceramic decouple lands on a floating net
    broken = SNAKE_YAML.replace("  - {from: 9R, to: 7R, colour: BLU}\n", "")
    d = mkdata(tmp_path, island=broken)
    bbnet.main(["report", "--data-dir", str(d)])
    assert bbnet.main(["check", "--data-dir", str(d)]) == 1
    assert "requirements" in capsys.readouterr().out


def test_todo_lists_unmet_placements(tmp_path, capsys):
    broken = SNAKE_YAML.replace("  - {ref: C2, kind: ceramic, from: 11R,"
                                " to: 9R}\n", "")
    d = mkdata(tmp_path, island=broken)
    bbnet.main(["todo", "--data-dir", str(d)])
    out = capsys.readouterr().out
    assert "U2.OUT" in out and "cap → GND" in out


PINMAP_PARTS = PARTS + (
    "sil4-pm:\n"
    "  kind: sil\n"
    "  pins: ['1', '2', '3', '4']\n"
    "  pin_signals: pinmap:test-mcu\n"
)
PINMAP_ISLAND = """\
island: pm1
board: mini-170
parts:
  - ref: U1
    part: sil4-pm
    pin1: 2R
    seeds: {'1': GND}
"""


def mkdata_pinmap(tmp_path):
    """A one-part island whose footprint declares pin_signals:
    pinmap:test-mcu, for exercising --pinmap end to end. Pin '1' is
    seed-overridden (so it never touches the pin table); pins '2'-'4'
    are unallocated on the bench, so whatever a registered 'pinmap'
    source says about them is the only thing that can seed their nets.
    rules.yaml declares no per-pin requirements, so an unconnected pin
    is neither a floating-pin error nor a todo -- this fixture is only
    about signal seeding, not requirements DRC.
    """
    d = tmp_path / "breadboard"
    d.mkdir()
    (d / "parts.yaml").write_text(PINMAP_PARTS)
    (d / "colours.yaml").write_text(COLOURS)
    (d / "rules.yaml").write_text("ties: []\npins: {}\n")
    (d / "pm1.yaml").write_text(PINMAP_ISLAND)
    return d


def test_pinmap_flag_registers_rows_that_reach_derivation(
        tmp_path, monkeypatch):
    """The whole point of --pinmap: a row for (test-mcu, pin 2) in the
    CSV must actually become a net name, not just make the run not
    crash. Start from a fresh registry so this test can't pass on
    leftover registration from another test in the same process."""
    from bbnet import signals
    monkeypatch.setattr(bbnet, "g_signals", signals.SignalRegistry())
    d = mkdata_pinmap(tmp_path)
    csv_path = tmp_path / "signals.csv"
    csv_path.write_text("mcu,pin,signal\ntest-mcu,2,FOOSIG\n")

    assert bbnet.main(["report", "--data-dir", str(d),
                       "--pinmap", str(csv_path)]) == 0
    design, _violations, _todos = bbnet.build(d)
    assert "FOOSIG" in {n.name for n in design.nets}


def test_pinmap_bad_header_raises_naming_found_and_expected(tmp_path):
    csv_path = tmp_path / "signals.csv"
    csv_path.write_text("device,pin,signal\ntest-mcu,2,FOOSIG\n")
    with pytest.raises(ModelError) as exc:
        bbnet.load_pinmap(csv_path)
    msg = str(exc.value)
    assert "device" in msg    # the offending header, found
    assert "mcu" in msg       # what was expected


def test_pinmap_missing_file_raises(tmp_path):
    missing = tmp_path / "no-such-signals.csv"
    with pytest.raises(ModelError, match=r"no such file"):
        bbnet.load_pinmap(missing)


def test_pinmap_header_only_file_yields_empty_source(tmp_path):
    csv_path = tmp_path / "signals.csv"
    csv_path.write_text("mcu,pin,signal\n")
    assert bbnet.load_pinmap(csv_path) == []


def test_bom_rolls_up_components(tmp_path, capsys):
    d = mkdata(tmp_path)
    bbnet.main(["bom", "--data-dir", str(d)])
    out = capsys.readouterr().out
    assert "C1" in out and "electrolytic" in out and "U2" in out


def test_load_data_rejects_a_nonexistent_data_dir(tmp_path):
    """A typo'd --data-dir must not look like a clean, empty bench: glob
    on a missing directory silently returns [], so 'todo'/'check' used to
    print success (0 requirements, 0 errors) instead of failing loudly."""
    missing = tmp_path / "typo-no-such-dir"
    with pytest.raises(ModelError, match=r"no such directory"):
        bbnet.load_data(missing)


def test_load_data_rejects_a_dir_with_no_island_yaml(tmp_path):
    """A directory that exists but holds only the reserved control files
    (or nothing at all) has no island to load -- must fail loudly rather
    than silently deriving an empty design."""
    d = tmp_path / "breadboard"
    d.mkdir()
    (d / "parts.yaml").write_text(PARTS)
    (d / "colours.yaml").write_text(COLOURS)
    (d / "rules.yaml").write_text(RULES)
    with pytest.raises(ModelError, match=r"no island YAML"):
        bbnet.load_data(d)


def test_engine_does_not_derive_paths_from_its_own_location():
    """A standalone engine has no repo to be relative to. Nothing in the
    package may resolve a data path from __file__."""
    import inspect

    from bbnet import cli
    src = inspect.getsource(cli)
    assert "parents[2]" not in src
    assert "docs/hardware" not in src.replace("\\", "/")


def test_cli_reads_the_data_dir_it_is_given(tmp_path, monkeypatch):
    """--data-dir is the only thing that decides where islands come from.

    Calling cli.build(tmp_path) directly would pass even before this
    task's change -- build() has always taken an explicit path and never
    consulted the module-level DATA_DIR. The behaviour actually at risk
    here is main()'s *default*: with no host-set cli.DATA_DIR and no
    --data-dir flag, it must fall back to the working directory rather
    than a stale value baked in at import time (or crash on None).
    """
    from bbnet import cli, signals

    (tmp_path / "solo.yaml").write_text(
        "island: solo\nboard: mini-170\nrails: {}\n"
        "parts:\n  - {ref: U1, pins: {'1': 5c, '2': 6c}}\n",
        encoding="utf-8")
    reg = signals.SignalRegistry()
    reg.register("pinmap", [])
    monkeypatch.setattr(cli, "g_signals", reg)
    monkeypatch.setattr(cli, "DATA_DIR", None)
    monkeypatch.chdir(tmp_path)

    assert cli.main(["report"]) == 0
    report = (tmp_path / "REPORT.md").read_text(encoding="utf-8")
    assert "solo" in report


def _prose(path):
    """Comment and docstring text only.

    Deliberately NOT the whole file: render.py embeds CSS and SVG with
    hex colours (fill:#888, color:#222), and a naive '#\\d{3}' sweep
    matches 11 of them. Issue references live in prose, never in a
    colour literal, so scope the search to prose and the ambiguity
    disappears.
    """
    import ast
    import io
    import tokenize

    src = path.read_text(encoding="utf-8")
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            out.append(tok.string)
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                out.append(doc)
    return out


def test_no_user_facing_string_names_the_host_repo_layout():
    """A standalone tool must not instruct users to run a path that only
    exists inside a private host repo, nor cite issues they cannot open."""
    import re
    from pathlib import Path

    src_dir = Path(__file__).resolve().parents[1] / "src" / "bbnet"
    tests_dir = Path(__file__).resolve().parent
    offenders = []
    for path in sorted(src_dir.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "bbnet.py" in text:
            offenders.append(f"{path.name}: stale CLI invocation")
        if "pinmap.csv" in text:
            offenders.append(f"{path.name}: names a host data file")
        if re.search(r"docs/(superpowers|hardware)/", text):
            offenders.append(f"{path.name}: host repo path")
        for chunk in _prose(path):
            if re.search(r"#\d{2,4}\b", chunk):
                offenders.append(f"{path.name}: private issue reference")
                break

    # tests/*.py legitimately talks about fixtures paths and the "bbnet"
    # package name, so the src-only checks above don't apply here --
    # only the host-repo identity strings matter. Prose only (comments +
    # docstrings): test_fixtures.py has a live regression assertion that
    # checks an old hardcoded page title is NOT rendered, and that
    # string literal (naming the old host board) must stay put -- it
    # lives in an assert argument, not a comment or docstring, so it is
    # outside the scope of this sweep.
    for path in sorted(tests_dir.glob("*.py")):
        for chunk in _prose(path):
            if "ET-embed" in chunk:
                offenders.append(f"{path.name}: names the host repo")
            if "VCU" in chunk:
                offenders.append(f"{path.name}: names the host board")
            if "tools/breadboard/" in chunk:
                offenders.append(f"{path.name}: host repo path")

    assert offenders == [], offenders
