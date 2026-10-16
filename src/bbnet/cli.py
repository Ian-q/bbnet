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
    bbnet serve     # the build sheet on 127.0.0.1, with label moves saved

Add --pinmap PATH (a CSV, header exactly mcu,pin,signal) to any of the
above when a footprint declares pin_signals: pinmap:<mcu> -- omit it
entirely when nothing in the data dir does.

Requires Python >= 3.11 and PyYAML.
"""
from __future__ import annotations

import argparse
import csv
import difflib
import json
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
RESERVED = {"parts.yaml", "colours.yaml", "rules.yaml", "layout.yaml",
            "labels.yaml"}


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


def build(data_dir, route=True):
    """Derive the netlist and run DRC. `route` also autoroutes, which the
    geometry-dependent rules (B12 in-node detour, B13 half-row landing)
    need — the router resolves half-row endpoints like `40R` to the hole
    they really use, which is exactly what those rules measure. Pass
    route=False for the connectivity-only commands, which pay nothing
    for geometry."""
    _parts, colours, rules, islands, pm, panels = load_data(data_dir)
    design = model.derive(islands, pm)
    routed = None
    if route:
        from bbnet import router
        rails = model.rails_from(_yaml(data_dir / "layout.yaml") or {},
                                 set(islands))
        routed = router.route_design(islands, design, rules, panels, rails)
    violations, todos = drc.run_all(design, rules, colours, routed)
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
    # a moved label whose id no longer renders is harmless but dead
    # weight - say so, never fail on it
    for lid in stale_label_offsets(load_label_offsets(data_dir), new_layout):
        print(f"WARNING: labels.yaml offset {lid!r} matches no rendered "
              "label (stale after an edit?) — drop it from labels.yaml")
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
    # connectivity-only: todos come from the requirements engine, so skip
    # the router (check/report still route — their violation counts must
    # agree or REPORT.md's summary drifts against the check every run)
    _design, _violations, todos = build(data_dir, route=False)
    if not todos:
        print("no unmet requirements — nothing to place")
        return 0
    for t in todos:
        print(f"{t.pin}: {t.instruction}")
    return 0


# ------------------------------------------------------------ labels.yaml
#
# Hand-dragged label offsets. User-authored data like the island YAML,
# and an input to LAYOUT.html like it, so `check` stays a pure function
# of the data dir. Written by `bbnet serve` (or pasted from a file://
# sheet's "Copy YAML"); ids are documented in render.py's movable-labels
# section.

LABELS_HEADER = "# dragged label offsets, svg px in the upright sheet\n"


def load_label_offsets(data_dir):
    """labels.yaml -> {label id: (dx, dy)}. Missing or empty is {}."""
    path = Path(data_dir) / "labels.yaml"
    raw = _yaml(path).get("offsets") or {}
    if not isinstance(raw, dict):
        raise model.ModelError(f"{path}: offsets: must be a mapping of "
                               "label id -> [dx, dy]")
    out = {}
    for lid, v in raw.items():
        if (not isinstance(v, (list, tuple)) or len(v) != 2
                or not all(isinstance(x, (int, float))
                           and not isinstance(x, bool) for x in v)):
            raise model.ModelError(f"{path}: offset {lid!r} must be "
                                   f"[dx, dy] numbers, got {v!r}")
        out[str(lid)] = (float(v[0]), float(v[1]))
    return out


def _fmt(v):
    return f"{round(float(v), 1):g}"


def dump_label_offsets(offsets):
    """{id: (dx, dy)} -> labels.yaml text, sorted so diffs stay small."""
    if not offsets:
        return LABELS_HEADER + "offsets: {}\n"
    lines = [LABELS_HEADER + "offsets:"]
    for lid in sorted(offsets):
        dx, dy = offsets[lid]
        lines.append(f"  {json.dumps(lid)}: [{_fmt(dx)}, {_fmt(dy)}]")
    return "\n".join(lines) + "\n"


def merge_label_offsets(stored, offsets=None, reset=()):
    """What a Save does to labels.yaml, as a pure function.

    The page shows each label at its already-offset position, so a drag
    is a DELTA: it adds to whatever is stored. `reset` drops a label's
    stored offset first (so reset-then-drag stores just the new drag).
    Offsets that come back to zero are dropped rather than kept as
    [0, 0] noise."""
    out = {k: (float(v[0]), float(v[1])) for k, v in (stored or {}).items()}
    for lid in reset or ():
        out.pop(str(lid), None)
    for lid, d in (offsets or {}).items():
        if (not isinstance(d, (list, tuple)) or len(d) != 2
                or not all(isinstance(x, (int, float))
                           and not isinstance(x, bool) for x in d)):
            raise ValueError(f"offset {lid!r} must be [dx, dy] numbers")
        bx, by = out.get(str(lid), (0.0, 0.0))
        nx, ny = round(bx + d[0], 1), round(by + d[1], 1)
        if nx or ny:
            out[str(lid)] = (nx, ny)
        else:
            out.pop(str(lid), None)
    return out


def stale_label_offsets(offsets, page):
    """Offset ids that match no rendered label - left behind when the
    label they moved was renamed or removed."""
    return sorted(set(offsets) - render.label_ids(page))


def layout_html(data_dir):
    """The one true LAYOUT.html invocation — used by render, the check
    sync-guard, serve, and the tests, so they can never disagree on
    inputs (the derived Design + rules make routing net/domain-aware;
    labels.yaml moves hand-placed labels)."""
    _parts, colours, rules, islands, pm, panels = load_data(data_dir)
    design = model.derive(islands, pm)
    layout = _yaml(data_dir / "layout.yaml") or {}
    rails = model.rails_from(layout, set(islands))
    return render.render_html(islands, design, rules, panels,
                              colours=colours, title=layout.get("title"),
                              rails=rails,
                              label_offsets=load_label_offsets(data_dir))


def cmd_render(data_dir, out=None):
    out = Path(out) if out else data_dir / "LAYOUT.html"
    out.write_text(layout_html(data_dir), encoding="utf-8")
    print(f"wrote {out}")
    return 0


SERVE_MARKER = '<meta name="bbnet-serve" content="1">'


def served_html(data_dir):
    """LAYOUT.html as `serve` sends it: the file output plus one marker
    the page script reads to offer Save. Only the served copy carries
    it, so the committed file stays exactly what `render` writes."""
    page = layout_html(data_dir)
    head = '<meta charset="utf-8">'
    return page.replace(head, head + "\n" + SERVE_MARKER, 1)


def save_labels(data_dir, payload):
    """POST /labels: merge a page's pending moves into labels.yaml, then
    rewrite LAYOUT.html from it so the committed sheet stays in sync and
    `check` passes. Returns the number of stored offsets.

    If the re-render fails (say an island YAML is mid-edit), labels.yaml
    is put back as it was rather than left ahead of LAYOUT.html."""
    if not isinstance(payload, dict):
        raise ValueError("body must be a JSON object")
    offsets = payload.get("offsets") or {}
    reset = payload.get("reset") or []
    if not isinstance(offsets, dict) or not isinstance(reset, list):
        raise ValueError("expected {offsets: {id: [dx, dy]}, reset: [ids]}")
    path = Path(data_dir) / "labels.yaml"
    merged = merge_label_offsets(load_label_offsets(data_dir), offsets,
                                 reset)
    old = path.read_text(encoding="utf-8") if path.exists() else None
    path.write_text(dump_label_offsets(merged), encoding="utf-8")
    try:
        page = layout_html(data_dir)
    except Exception:
        if old is None:
            path.unlink()
        else:
            path.write_text(old, encoding="utf-8")
        raise
    (Path(data_dir) / "LAYOUT.html").write_text(page, encoding="utf-8")
    return len(merged)


def make_server(data_dir, port=8765, host="127.0.0.1"):
    """The `serve` HTTP server, unstarted (tests run it on port 0).

    Loopback only: it writes files in the data dir on request, and has
    no business being reachable from anywhere else."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    lock = threading.Lock()     # one render/write at a time

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _reply(self, code, obj):
            self._send(code, json.dumps(obj), "application/json")

        def do_GET(self):   # noqa: N802 - http.server's spelling
            if self.path.split("?")[0] not in ("/", "/LAYOUT.html"):
                self._send(404, "not found", "text/plain; charset=utf-8")
                return
            try:
                with lock:
                    page = served_html(data_dir)
            except Exception as exc:   # show the YAML error, keep serving
                self._send(500, f"bbnet: {exc}", "text/plain; charset=utf-8")
                return
            self._send(200, page, "text/html; charset=utf-8")

        def do_POST(self):   # noqa: N802
            if self.path.split("?")[0] != "/labels":
                self._reply(404, {"ok": False, "error": "not found"})
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(n) or b"{}")
                with lock:
                    stored = save_labels(data_dir, payload)
            except (ValueError, model.ModelError) as exc:
                self._reply(400, {"ok": False, "error": str(exc)})
                return
            except Exception as exc:
                self._reply(500, {"ok": False, "error": str(exc)})
                return
            print(f"saved labels.yaml ({stored} offset(s)) + LAYOUT.html")
            self._reply(200, {"ok": True, "offsets": stored})

        def log_message(self, fmt, *args):   # keep the terminal quiet
            pass

    return ThreadingHTTPServer((host, port), Handler)


def cmd_serve(data_dir, port=8765):
    layout_html(data_dir)        # fail fast on a broken data dir
    srv = make_server(data_dir, port)
    print(f"bbnet serve: http://127.0.0.1:{srv.server_address[1]}/ "
          f"({data_dir}) — double-click a label to move it, Save writes "
          "labels.yaml + LAYOUT.html; Ctrl-C to stop")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


def cmd_score(data_dir, as_json=False):
    """How tidy the layout is, and a fingerprint of the circuit that
    survives any re-layout - the yardstick for skills/bbnet-layout-review.
    Exit status follows DRC errors, like check."""
    from bbnet import router, score
    _parts, colours, rules, islands, pm, panels = load_data(data_dir)
    design = model.derive(islands, pm)
    rails = model.rails_from(_yaml(data_dir / "layout.yaml") or {},
                             set(islands))
    routed = router.route_design(islands, design, rules, panels, rails)
    violations, _todos = drc.run_all(design, rules, colours, routed)
    s = score.score(design, islands, routed, violations, rails)
    if as_json:
        print(json.dumps(s, indent=1))
    else:
        print(score.format_text(s))
    return 1 if s["totals"]["errors"] else 0


def cmd_bom(data_dir):
    design, _violations, todos = build(data_dir, route=False)  # no geometry
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
                        choices=["check", "report", "todo", "bom", "render",
                                 "score", "serve"])
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
    parser.add_argument("--json", action="store_true",
                        help="score only: machine-readable output")
    parser.add_argument("--port", type=int, default=8765,
                        help="serve only: port on 127.0.0.1 "
                             "(default: %(default)s)")
    args = parser.parse_args(argv)
    # Registered before dispatch so it's live for every subcommand --
    # check/report/todo/bom/render all call load_data(), which hands
    # g_signals to derive() regardless of which command asked for it.
    if args.pinmap is not None:
        g_signals.register("pinmap", load_pinmap(args.pinmap))
    if args.command == "render":
        return cmd_render(args.data_dir, args.out)
    if args.command == "score":
        return cmd_score(args.data_dir, args.json)
    if args.command == "serve":
        return cmd_serve(args.data_dir, args.port)
    fn = {"check": cmd_check, "report": cmd_report,
          "todo": cmd_todo, "bom": cmd_bom}[args.command]
    return fn(args.data_dir)


if __name__ == "__main__":
    raise SystemExit(main())
