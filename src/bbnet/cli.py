#!/usr/bin/env python3
"""bbnet — breadboard netlist tracker CLI.

A directory of island YAML records what is physically on each breadboard
(placements only); this tool derives the netlist, runs DRC, and renders
the committed report and build sheet.

Usage:
    bbnet check     # DRC + report/layout in sync (CI gate)
    bbnet report    # regenerate REPORT.md
    bbnet todo      # unmet requirements = placement instructions
    bbnet bom       # component rollup
    bbnet render    # autorouted board views + kitting tables (LAYOUT.html)

Add --pinmap PATH (a CSV, header exactly mcu,pin,signal) to any of the
above when a footprint declares pin_signals: pinmap:<mcu> -- omit it
entirely when nothing in the data dir does.

Requires Python >= 3.11 and PyYAML.
"""
from __future__ import annotations

import argparse
import csv
import difflib
import sys
from pathlib import Path

import yaml

from bbnet import drc, model, render
from bbnet.signals import SignalRegistry, SignalRow

# Set by a host application's own entry-point script before it calls
# main() below. None means "no host default" -- the CLI then falls back
# to the working directory, which is the right default for a standalone
# tool.
DATA_DIR = None
RESERVED = {"parts.yaml", "colours.yaml", "rules.yaml", "layout.yaml"}


# Host applications register their pin-allocation sources here before
# calling load_data(). An engine with no host registers nothing, and any
# footprint declaring pin_signals: will then raise -- which is correct:
# a declared source that nobody supplied is a broken configuration.
g_signals = SignalRegistry()


def _yaml(path):
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_pinmap(path):
    """--pinmap PATH -> list[SignalRow], the rows registered under the
    'pinmap' source name that pin_signals: pinmap:<mcu> already refers
    to.

    Before this flag, --data-dir/--out were the only CLI arguments, so
    the ONLY thing that could ever call SignalRegistry.register() was a
    host application's own entry-point script -- pin_signals: was real
    in the YAML but unreachable from this CLI, for anyone standalone.
    signals.py stays transport-agnostic on purpose (it doesn't know a
    file exists); this function is the one place a path and the
    three-column contract meet, kept out of that module deliberately.

    A missing file raises rather than seeding an empty source, for the
    same reason SignalRegistry.rows() distinguishes "nobody registered
    this name" from "registered, and it has nothing in it": a typo'd
    --pinmap path must not silently look like a design with zero real
    allocations -- that would mute DRC B3/B7 while the run still
    reports clean. A header that doesn't match raises too, naming both
    what the file actually had and what was required, because a
    reordered or misspelled column is a contract violation, not
    something to guess at positionally. Zero data rows past a valid
    header is accepted: "this table exists but allocates nothing yet"
    is a legitimate project state, not an error.
    """
    if not path.is_file():
        raise model.ModelError(
            f"--pinmap {path}: no such file — expected a CSV with "
            "header row 'mcu,pin,signal'")
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        want = ["mcu", "pin", "signal"]
        if reader.fieldnames != want:
            raise model.ModelError(
                f"--pinmap {path}: header is {reader.fieldnames!r}, "
                f"expected {want!r}")
        return [SignalRow(row["mcu"], row["pin"], row["signal"])
                for row in reader]


def load_data(data_dir):
    if not data_dir.is_dir():
        raise model.ModelError(
            f"--data-dir {data_dir}: no such directory — expected an "
            "island YAML directory")
    parts_lib = model.parts_lib_from(_yaml(data_dir / "parts.yaml"))
    colours = _yaml(data_dir / "colours.yaml")
    rules = _yaml(data_dir / "rules.yaml")
    islands = {}
    for p in sorted(data_dir.glob("*.yaml")):
        if p.name in RESERVED:
            continue
        isl = model.island_from(_yaml(p), parts_lib)
        if isl.name in islands:
            raise model.ModelError(f"duplicate island name {isl.name!r}")
        islands[isl.name] = isl
    if not islands:
        raise model.ModelError(
            f"--data-dir {data_dir}: no island YAML found (only "
            f"{sorted(RESERVED)} are reserved control filenames) — "
            "wrong directory?")
    panels = model.panels_from(_yaml(data_dir / "layout.yaml"), set(islands))
    return parts_lib, colours, rules, islands, g_signals, panels


def build(data_dir):
    _parts, colours, rules, islands, pm, _panels = load_data(data_dir)
    design = model.derive(islands, pm)
    violations, todos = drc.run_all(design, rules, colours)
    return design, violations, todos


def render_report(design, violations, todos):
    lines = [
        "# Breadboard netlist report",
        "",
        "<!-- GENERATED — DO NOT EDIT. Source of truth: the island YAML in",
        "     this directory; regenerate: bbnet report -->",
        "",
    ]
    for iname in sorted(design.islands):
        isl = design.islands[iname]
        lines += [f"## Island `{iname}` ({isl.board.name})", ""]
        lines += ["| Net | Members | Component edges |",
                  "| --- | --- | --- |"]
        for net in sorted(design.nets, key=lambda n: n.name):
            keys = [k for k in net.keys if k[1] == iname]
            if not keys:
                continue
            members = []
            for k in keys:
                loc = (f"{k[2]}{k[3]}" if k[0] == "row" else f"rail:{k[2]}")
                occ = ", ".join(design.node_members.get(k, [])) or "—"
                members.append(f"`{loc}` ({occ})")
            edges = [f"{e.ref} {e.kind} {e.value}".strip()
                     + f" ↔ `{design.net_by_id(e.b_nid if e.a_nid == net.nid else e.a_nid).name}`"
                     for e in design.edges
                     if net.nid in (e.a_nid, e.b_nid)]
            lines.append(f"| `{net.name}` | {'; '.join(members)} | "
                         f"{'; '.join(edges) or '—'} |")
        lines.append("")
    if todos:
        lines += ["## TODO — unmet requirements (place these)", ""]
        lines += [f"- **{t.pin}**: {t.instruction}" for t in todos]
        lines.append("")
    errs = [v for v in violations if v.severity == "error"]
    warns = [v for v in violations if v.severity == "warning"]
    lines += [
        "## Check summary", "",
        f"- {len(errs)} error(s), {len(warns)} warning(s), "
        f"{len(todos)} todo(s)", "",
    ]
    return "\n".join(lines)


def _print_violations(violations):
    for v in violations:
        print(f"DRC {v.rule} [{v.severity}]: {v.message}")
    errs = sum(1 for v in violations if v.severity == "error")
    warns = len(violations) - errs
    print(f"DRC: {errs} error(s), {warns} warning(s)")
    return 1 if errs else 0


def cmd_check(data_dir):
    design, violations, todos = build(data_dir)
    status = _print_violations(violations)
    report = data_dir / "REPORT.md"
    new = render_report(design, violations, todos)
    old = report.read_text(encoding="utf-8") if report.exists() else ""
    if old != new:
        status = 1
        print(f"DRIFT: {report} out of sync — run: bbnet report")
        sys.stdout.writelines(difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True),
            fromfile="REPORT.md (committed)", tofile="REPORT.md (rendered)"))
    # LAYOUT.html rides the same guard: the router is deterministic, so
    # any island edit that changes routing (including occupying a
    # half-row an existing tunnel dives through) must regen the layout
    # in the same commit. No diff dump — it's generated HTML.
    layout = data_dir / "LAYOUT.html"
    new_layout = layout_html(data_dir)
    old_layout = layout.read_text(encoding="utf-8") if layout.exists() else ""
    if old_layout != new_layout:
        status = 1
        print(f"DRIFT: {layout} out of sync — run: bbnet render")
    if status == 0:
        print("bbnet check OK: DRC has no errors, REPORT.md + "
              "LAYOUT.html in sync")
    return status


def cmd_report(data_dir):
    design, violations, todos = build(data_dir)
    report = data_dir / "REPORT.md"
    report.write_text(render_report(design, violations, todos),
                      encoding="utf-8")
    print(f"wrote {report}")
    return _print_violations(violations)


def cmd_todo(data_dir):
    _design, _violations, todos = build(data_dir)
    if not todos:
        print("no unmet requirements — nothing to place")
        return 0
    for t in todos:
        print(f"{t.pin}: {t.instruction}")
    return 0


def layout_html(data_dir):
    """The one true LAYOUT.html invocation — used by render, the check
    sync-guard, and the tests, so they can never disagree on inputs
    (the derived Design + rules make routing net/domain-aware)."""
    _parts, colours, rules, islands, pm, panels = load_data(data_dir)
    design = model.derive(islands, pm)
    title = (_yaml(data_dir / "layout.yaml") or {}).get("title")
    return render.render_html(islands, design, rules, panels,
                              colours=colours, title=title)


def cmd_render(data_dir, out=None):
    out = Path(out) if out else data_dir / "LAYOUT.html"
    out.write_text(layout_html(data_dir), encoding="utf-8")
    print(f"wrote {out}")
    return 0


def cmd_bom(data_dir):
    design, _violations, todos = build(data_dir)
    for iname in sorted(design.islands):
        isl = design.islands[iname]
        print(f"# {iname}")
        for part in isl.parts:
            first = sorted(part.pins.values(), key=str)[0]
            print(f"  {part.ref}: {part.value or part.part_id} @ {first}")
        for q in isl.passives:
            print(f"  {q.ref}: {q.kind} {q.value} {q.a} ↔ {q.b}".rstrip())
    if todos:
        print("# MISSING (unmet requirements)")
        for t in todos:
            print(f"  {t.pin}: {t.instruction}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",
                        choices=["check", "report", "todo", "bom", "render"])
    # DATA_DIR is a module global, read here -- inside main(), not at
    # import time -- so a host that sets cli.DATA_DIR before calling
    # main() is honoured.
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR or Path.cwd(),
                        help="island YAML directory (default: %(default)s)")
    parser.add_argument("--out", type=Path, default=None,
                        help="render only: output path "
                             "(default: <data-dir>/LAYOUT.html)")
    parser.add_argument("--pinmap", type=Path, default=None,
                        help="CSV pin-allocation table (header: "
                             "mcu,pin,signal), registered as the "
                             "'pinmap' signal source. Required only if "
                             "a footprint declares pin_signals: "
                             "pinmap:<mcu>")
    args = parser.parse_args(argv)
    # Registered before dispatch so it's live for every subcommand --
    # check/report/todo/bom/render all call load_data(), which hands
    # g_signals to derive() regardless of which command asked for it.
    if args.pinmap is not None:
        g_signals.register("pinmap", load_pinmap(args.pinmap))
    if args.command == "render":
        return cmd_render(args.data_dir, args.out)
    fn = {"check": cmd_check, "report": cmd_report,
          "todo": cmd_todo, "bom": cmd_bom}[args.command]
    return fn(args.data_dir)


if __name__ == "__main__":
    raise SystemExit(main())
