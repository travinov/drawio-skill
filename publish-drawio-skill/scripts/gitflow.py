#!/usr/bin/env python3
"""Generate timeline-aware git-flow diagrams as draw.io XML.

Usage:
  python3 gitflow.py flow.json -o git-flow.drawio [--route auto|builtin|graphviz]
"""
import argparse
import importlib.util
import json
import math
import os
import shutil
import shlex
import subprocess
import sys
from collections import defaultdict
from xml.sax.saxutils import escape

HERE = os.path.dirname(os.path.abspath(__file__))
VALIDATOR_PATH = os.path.join(HERE, "gitflow_validate.py")
spec = importlib.util.spec_from_file_location("gitflow_validate", VALIDATOR_PATH)
gitflow_validate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gitflow_validate)

GRID = 10
LEFT = 150
TOP = 70
LANE_GAP = 130
SLOT_GAP = 240
LANE_H = 84
MARK = 18
PAGE_PAD = 80

LANE_STYLES = {
    "main": ("#1f2937", "#f3f4f6"),
    "master": ("#1f2937", "#f3f4f6"),
    "develop": ("#2f855a", "#f0fff4"),
    "feature": ("#2f5597", "#eaf2ff"),
    "release": ("#946200", "#fff7df"),
    "hotfix": ("#b85450", "#f8cecc"),
    "support": ("#6b46c1", "#f3efff"),
    "custom": ("#666666", "#f5f5f5"),
}
EVENT_FILL = {
    "commit": "#ffffff",
    "merge": "#d5e8d4",
    "branch": "#dae8fc",
    "tag": "#fff2cc",
    "release": "#ffe6cc",
    "hotfix": "#f8cecc",
    "cherry-pick": "#e1d5e7",
    "revert": "#f5f5f5",
    "note": "#fff2cc",
}


def attr(value):
    return escape(str(value), {'"': "&quot;", "\n": "&#xa;"})


def snap(value):
    return int(round(float(value) / GRID) * GRID)


def branch_kind(branch):
    return gitflow_validate.branch_kind(branch)


def label_width(text, minimum=80, maximum=160):
    return max(minimum, min(maximum, 8 * len(str(text)) + 24))


def sorted_branches(branches):
    order = {"main": 0, "master": 0, "hotfix": 1, "release": 2, "develop": 3,
             "feature": 4, "support": 5, "custom": 6}
    return sorted(branches, key=lambda b: (order.get(branch_kind(b), 6), str(b.get("label", b["id"]))))


def event_slot_key(event, index, mode):
    if mode == "date":
        return str(event.get("at"))
    return int(event.get("order", index))


def lane_gap_for_width(width, branch_count):
    """Keep wide multi-lane timelines inside the validator's 4:1 canvas bound."""
    if branch_count <= 1:
        return LANE_GAP
    lane_canvas_width = width + 100  # lane width - 60, plus 160px validator margin
    fixed_lane_canvas_height = LANE_H + 160
    required = (lane_canvas_width / 4 - fixed_lane_canvas_height) / (branch_count - 1)
    return max(LANE_GAP, int(math.ceil(required / GRID)) * GRID)


def layout(spec):
    branches = sorted_branches(spec["branches"])
    by_branch = {b["id"]: b for b in branches}

    mode = spec.get("timeMode", "date")
    events = gitflow_validate.normalize_events(spec)
    keys = [event_slot_key(e, i, mode) for i, e in enumerate(events)]
    unique = []
    for key in keys:
        if key not in unique:
            unique.append(key)
    slot_x = {key: LEFT + i * SLOT_GAP for i, key in enumerate(unique)}
    width = snap(max(slot_x.values() or [LEFT]) + PAGE_PAD + 260)
    lane_gap = lane_gap_for_width(width, len(branches))
    lane_y = {b["id"]: TOP + i * lane_gap for i, b in enumerate(branches)}
    per_slot = defaultdict(int)
    last_on_branch = {}
    event_pos = {}

    for i, event in enumerate(events):
        key = keys[i]
        x = slot_x[key] + per_slot[key] * 100
        per_slot[key] += 1
        etype = event.get("type")
        branch = event.get("branch")
        if etype == "branch":
            branch = event.get("to")
        elif etype == "merge":
            branch = event.get("to")
        elif not branch and event.get("from"):
            branch = event.get("from")
        y = lane_y[branch] + LANE_H / 2 if branch in lane_y else TOP
        event_pos[event["id"]] = (snap(x), snap(y), branch)
        if branch:
            last_on_branch[branch] = event["id"]

    height = TOP + max(len(branches), 1) * lane_gap + PAGE_PAD
    return by_branch, lane_y, event_pos, width, snap(height)


def route_builtin(src, dst):
    sx, sy = src
    tx, ty = dst
    if abs(sy - ty) < 2:
        return []
    mid = snap((sx + tx) / 2)
    if abs(mid - sx) < 30:
        mid = snap(sx + 50)
    return [(mid, sy), (mid, ty)]


def run_neato(nodes, edges):
    if not shutil.which("neato"):
        raise RuntimeError("Graphviz `neato` not found on PATH")
    lines = [
        "digraph G {",
        "graph [splines=ortho, outputorder=edgesfirst];",
        "node [shape=circle, width=0.25, height=0.25, fixedsize=true];",
    ]
    for nid, (x, y) in nodes.items():
        lines.append(f'{dot_quote(nid)} [pos="{x / 72:.4f},{-y / 72:.4f}!"];')
    for eid, source, target in edges:
        lines.append(f'{dot_quote(source)} -> {dot_quote(target)} [id={dot_quote(eid)}];')
    lines.append("}")
    proc = subprocess.run(["neato", "-n2", "-Tplain"], input="\n".join(lines),
                          text=True, capture_output=True, check=True)
    routes = {}
    edge_index = 0
    for line in proc.stdout.splitlines():
        tok = shlex.split(line)
        if tok and tok[0] == "edge":
            n = int(tok[3])
            pts = [(snap(float(tok[4 + 2 * i]) * 72), snap(-float(tok[5 + 2 * i]) * 72))
                   for i in range(n)]
            if edge_index < len(edges):
                routes[edges[edge_index][0]] = pts[1:-1]
            edge_index += 1
    return routes


def dot_quote(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def route_edges(spec, event_pos, mode):
    point_nodes = {f"event_{eid}": (x, y) for eid, (x, y, _) in event_pos.items()}
    edges = edge_specs(spec)
    builtin = {}
    for edge in edges:
        _, source, target, _ = edge
        sx, sy = point_nodes[source]
        tx, ty = point_nodes[target]
        builtin[edge[0]] = route_builtin((sx, sy), (tx, ty))
    if mode == "builtin":
        return builtin, "builtin"
    if mode in ("auto", "graphviz"):
        try:
            graphviz = run_neato(point_nodes, [(e[0], e[1], e[2]) for e in edges])
            for edge in edges:
                pts = graphviz.get(edge[0])
                if not pts:
                    continue
                # Keep Graphviz only for bends. Reject non-finite or off-canvas-looking routes.
                if all(-100000 < x < 100000 and -100000 < y < 100000 for x, y in pts):
                    builtin[edge[0]] = pts
            return builtin, "graphviz"
        except (RuntimeError, subprocess.CalledProcessError) as exc:
            if mode == "graphviz":
                raise RuntimeError(str(exc))
            return builtin, "builtin"
    raise RuntimeError(f"unknown route mode {mode!r}")


def previous_event_on_branch(events, event_pos, event_id, branch):
    prev = None
    for event in events:
        if event.get("id") == event_id:
            break
        _, _, ebranch = event_pos.get(event["id"], (None, None, None))
        if ebranch == branch:
            prev = event["id"]
    return prev


def edge_specs(spec):
    events = gitflow_validate.normalize_events(spec)
    # event_pos is not needed for finding ids; use chronological event order.
    by_id = {e["id"]: e for e in events}
    pseudo_pos = {}
    result = []
    branch_last = {}
    for event in events:
        eid = event["id"]
        etype = event.get("type")
        if etype == "branch":
            src = branch_last.get(event.get("from"))
            if src:
                result.append((f"edge_{eid}", f"event_{src}", f"event_{eid}", "branch"))
            branch_last[event.get("to")] = eid
        elif etype == "merge":
            src = branch_last.get(event.get("from"))
            if src:
                result.append((f"edge_{eid}", f"event_{src}", f"event_{eid}", "merge"))
            branch_last[event.get("to")] = eid
        else:
            branch = event.get("branch")
            prev = branch_last.get(branch)
            if prev:
                result.append((f"edge_{eid}_seq", f"event_{prev}", f"event_{eid}", "sequence"))
            if branch:
                branch_last[branch] = eid
    return result


def cell(id_, value, style, x, y, w, h, parent="1"):
    return (
        f'        <mxCell id="{attr(id_)}" value="{attr(value)}" style="{attr(style)}" '
        f'vertex="1" parent="{attr(parent)}">\n'
        f'          <mxGeometry x="{snap(x)}" y="{snap(y)}" width="{snap(w)}" height="{snap(h)}" as="geometry"/>\n'
        "        </mxCell>"
    )


def edge_cell(id_, value, style, source, target, points):
    if points:
        pts = "".join(f'<mxPoint x="{snap(x)}" y="{snap(y)}"/>' for x, y in points)
        geom = f'<mxGeometry relative="1" as="geometry"><Array as="points">{pts}</Array></mxGeometry>'
    else:
        geom = '<mxGeometry relative="1" as="geometry"/>'
    return (
        f'        <mxCell id="{attr(id_)}" value="{attr(value)}" style="{attr(style)}" '
        f'edge="1" parent="1" source="{attr(source)}" target="{attr(target)}">\n'
        f"          {geom}\n"
        "        </mxCell>"
    )


def to_drawio(spec, route_mode):
    spec = dict(spec)
    spec["events"] = gitflow_validate.normalize_events(spec)
    by_branch, lane_y, event_pos, width, height = layout(spec)
    routes, used_router = route_edges(spec, event_pos, route_mode)
    cells = []
    title = spec.get("title", "Git-flow")
    cells.append(cell("title", title, "text;html=1;strokeColor=none;fillColor=none;fontSize=18;fontStyle=1;",
                      20, 20, label_width(title, 180, 420), 30))
    for bid, branch in by_branch.items():
        kind = branch_kind(branch)
        stroke, fill = LANE_STYLES.get(kind, LANE_STYLES["custom"])
        label = branch.get("label", bid)
        style = (f"rounded=1;whiteSpace=wrap;html=1;container=1;recursiveResize=0;"
                 f"fillColor={fill};strokeColor={stroke};fontColor={stroke};align=left;"
                 f"spacingLeft=10;verticalAlign=middle;")
        cells.append(cell(f"lane_{bid}", label, style, 20, lane_y[bid], width - 60, LANE_H))

    for event in spec.get("events", []):
        eid = event["id"]
        x, y, branch = event_pos[eid]
        etype = event.get("type")
        kind = branch_kind(by_branch[branch]) if branch in by_branch else "custom"
        stroke, _ = LANE_STYLES.get(kind, LANE_STYLES["custom"])
        fill = EVENT_FILL.get(etype, "#ffffff")
        if etype == "tag":
            style = f"rounded=1;whiteSpace=wrap;html=1;fillColor={fill};strokeColor=#d6b656;fontSize=10;"
            cells.append(cell(f"event_{eid}", event.get("label", eid), style,
                              x - 34, 4, 68, 24, f"lane_{branch}"))
            continue
        if etype == "note":
            style = "shape=note;whiteSpace=wrap;html=1;fillColor=#fff2cc;strokeColor=#d6b656;fontSize=10;"
            cells.append(cell(f"event_{eid}", event.get("label", event.get("note", eid)), style,
                              x - 45, 4, 90, 36, f"lane_{branch}"))
            continue
        thick = "strokeWidth=2;" if etype == "merge" else ""
        style = f"ellipse;whiteSpace=wrap;html=1;aspect=fixed;fillColor={fill};strokeColor={stroke};{thick}"
        cells.append(cell(f"event_{eid}", "", style, x - MARK / 2, y - lane_y[branch] - MARK / 2, MARK, MARK,
                          f"lane_{branch}"))
        label = event.get("label")
        if label:
            cells.append(cell(f"label_{eid}", label,
                              "text;html=1;strokeColor=none;fillColor=none;fontSize=10;align=center;",
                              x - label_width(label) / 2, y - lane_y[branch] + 16, label_width(label), 28,
                              f"lane_{branch}"))

    edge_styles = {
        "sequence": "html=1;rounded=0;endArrow=none;strokeColor=#999999;",
        "branch": "html=1;rounded=1;endArrow=open;strokeColor=#2f5597;dashed=1;",
        "merge": "html=1;rounded=1;endArrow=block;strokeColor=#666666;strokeWidth=2;",
    }
    for eid, source, target, kind in edge_specs(spec):
        label = ""
        event_id = eid.replace("edge_", "").replace("_seq", "")
        for event in spec.get("events", []):
            if event.get("id") == event_id and kind in ("branch", "merge"):
                label = event.get("label", "")
                break
        cells.append(edge_cell(eid, label, edge_styles[kind], source, target, routes.get(eid, [])))

    page = (
        f'  <diagram id="gitflow" name="{attr(title)}" data-schema-version="1" '
        f'data-time-mode="{attr(spec.get("timeMode", "date"))}">\n'
        f'    <mxGraphModel dx="1200" dy="800" grid="1" gridSize="10" guides="1" tooltips="1" '
        f'connect="1" arrows="1" fold="1" page="1" pageScale="1" pageWidth="{width}" '
        f'pageHeight="{height}" math="0" shadow="0">\n'
        "      <root>\n"
        '        <mxCell id="0"/>\n'
        '        <mxCell id="1" parent="0"/>\n'
        + "\n".join(cells)
        + "\n      </root>\n    </mxGraphModel>\n  </diagram>\n"
    )
    return "<mxfile host=\"drawio\">\n" + page + "</mxfile>\n", used_router


def main(argv=None):
    ap = argparse.ArgumentParser(description="Git-flow JSON -> timeline-aware draw.io XML.")
    ap.add_argument("input", help="flow JSON file")
    ap.add_argument("-o", "--output", help="output .drawio path (default: stdout)")
    ap.add_argument("--route", choices=("auto", "builtin", "graphviz"), default="auto",
                    help="edge routing backend (default: auto)")
    args = ap.parse_args(argv)
    try:
        with open(args.input, encoding="utf-8") as fh:
            spec = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        sys.exit(f"error: cannot read {args.input}: {exc}")
    normalized, report = gitflow_validate.validate_document(spec)
    errors, warnings = gitflow_validate.messages(report)
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
    try:
        xml, router = to_drawio(normalized, args.route)
    except RuntimeError as exc:
        sys.exit(f"error: {exc}")
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(xml)
        print(f"wrote {args.output} using {router} routing", file=sys.stderr)
    else:
        sys.stdout.write(xml)
        print(f"used {router} routing", file=sys.stderr)


if __name__ == "__main__":
    main()
