#!/usr/bin/env python3
"""Generate deterministic roadmap .drawio XML from versioned roadmap YAML."""
from __future__ import annotations

import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET

from roadmap_timeline import TimelineAxis
from roadmap_validate import load_yaml, milestone_revisions, validate_document
from validation_common import messages


BASE_LANE_H = 270
HEADER_H = 54
LEFT_W = 180
COL_W = 170
TOP = 80
TASK_H = 56
TASK_GAP = 12
OUTCOME_H = 40
OUTCOME_GAP = 10

STATUS_COLORS = {
    "planned": ("#dae8fc", "#6c8ebf"), "in_progress": ("#dae8fc", "#6c8ebf"),
    "on_track": ("#d5e8d4", "#82b366"), "at_risk": ("#fff2cc", "#d6b656"),
    "blocked": ("#f8cecc", "#b85450"), "done": ("#d5e8d4", "#82b366"),
    "cancelled": ("#f5f5f5", "#999999"),
}


def style(**parts):
    return ";".join(f"{key}={value}" for key, value in parts.items() if value is not None) + ";"


def cell(parent, cid, value="", style_value="", vertex=False, edge=False, source=None, target=None):
    attrs = {"id": cid, "value": str(value), "style": style_value, "parent": parent}
    if vertex:
        attrs["vertex"] = "1"
    if edge:
        attrs["edge"] = "1"
        if source:
            attrs["source"] = source
        if target:
            attrs["target"] = target
    return ET.Element("mxCell", attrs)


def geometry(element, x=None, y=None, w=None, h=None, relative=None):
    attrs = {"as": "geometry"}
    for key, value in (("x", x), ("y", y), ("width", w), ("height", h)):
        if value is not None:
            attrs[key] = f"{float(value):.1f}"
    if relative is not None:
        attrs["relative"] = str(relative)
    return ET.SubElement(element, "mxGeometry", attrs)


def label_width(text, minimum=120, maximum=260):
    return max(minimum, min(maximum, 8 * max((len(line) for line in str(text).splitlines()), default=0) + 24))


def clamp(value, low, high):
    return max(low, min(high, value))


def lanes_for(model):
    lanes = model.get("lanes") or [{"id": "roadmap", "title": "Roadmap"}]
    return lanes, {lane["id"]: index for index, lane in enumerate(lanes)}


def item_lane(item, lane_index):
    return item.get("lane") if item.get("lane") in lane_index else next(iter(lane_index))


def assign_task_levels(tasks, lane_index, axis):
    result, occupied = {}, {lane: [] for lane in lane_index}
    for task in tasks:
        lane = item_lane(task, lane_index)
        start_x, end_x = axis.task_span(task)
        if end_x < start_x:
            start_x, end_x = end_x, start_x
        local_x = start_x - 20
        width = max(140, end_x - start_x, label_width(task["title"], 140, 280))
        interval = (local_x, local_x + width)
        level = next((i for i, spans in enumerate(occupied[lane]) if all(interval[1] + 8 <= a or interval[0] >= b + 8 for a, b in spans)), None)
        if level is None:
            level = len(occupied[lane])
            occupied[lane].append([])
        occupied[lane][level].append(interval)
        result[task["id"]] = (level, local_x, width)
    return result, {lane: max(1, len(levels)) for lane, levels in occupied.items()}


def lane_layout(lanes, level_counts, model):
    heights, offsets, y = {}, {}, TOP + HEADER_H
    lane_ids, default = {lane["id"] for lane in lanes}, lanes[0]["id"]
    outcomes = {lane["id"]: set() for lane in lanes}
    milestones = {lane["id"]: 0 for lane in lanes}
    for item in (model.get("tasks", []) or []) + (model.get("milestones", []) or []):
        lane = item.get("lane") if item.get("lane") in lane_ids else default
        outcomes[lane].update(item.get("outcomes", []) or [])
        if "date" in item or "order" in item:
            milestones[lane] += 1 + len(item.get("history", []) or [])
    for lane in lanes:
        lid = lane["id"]
        task_band = 30 + level_counts[lid] * (TASK_H + TASK_GAP)
        milestone_band = max(1, milestones[lid]) * 42 + 80
        outcome_band = len(outcomes[lid]) * (OUTCOME_H + OUTCOME_GAP) + 20
        heights[lid] = max(BASE_LANE_H, task_band + milestone_band + outcome_band)
        offsets[lid] = y
        y += heights[lid]
    return offsets, heights, y


def _source_metadata(element, item, fields):
    for field in fields:
        value = item.get(field, "")
        if isinstance(value, (list, dict)):
            value = json.dumps(value, ensure_ascii=False, sort_keys=True)
        element.set(f"data-{field.replace('_', '-')}", str(value))


def build_drawio(model):
    normalized, report = validate_document(model)
    errors, _ = messages(report)
    if errors:
        raise SystemExit("error: invalid roadmap:\n" + "\n".join(f"- {error}" for error in errors))
    model = normalized
    deltas = report.get("deltas", [])
    milestone_deltas = {d["id"]: d for d in deltas if d.get("entity") == "milestone"}
    history_deltas = report.get("history_deltas", [])
    lanes, lane_index = lanes_for(model)
    axis = TimelineAxis(model, LEFT_W, COL_W)
    task_levels, level_counts = assign_task_levels(model.get("tasks", []) or [], lane_index, axis)
    lane_y, lane_heights, bottom = lane_layout(lanes, level_counts, model)
    width, height = axis.width, bottom + 80

    mxfile = ET.Element("mxfile", {"host": "app.diagrams.net", "type": "device"})
    diagram = ET.SubElement(mxfile, "diagram", {
        "id": "roadmap", "name": "Roadmap", "data-schema-version": str(model.get("schema_version", 1)),
        "data-time-scale": model.get("time_scale", "month"),
        "data-lane-dimension": model.get("lane_dimension", ""),
        "data-assumptions": json.dumps(model.get("assumptions", []), ensure_ascii=False, sort_keys=True),
    })
    graph = ET.SubElement(diagram, "mxGraphModel", {
        "dx": "1200", "dy": "800", "grid": "1", "gridSize": "10", "guides": "1",
        "tooltips": "1", "connect": "1", "arrows": "1", "fold": "1", "page": "1",
        "pageScale": "1", "pageWidth": str(int(width)), "pageHeight": str(int(height)), "math": "0", "shadow": "0",
    })
    root = ET.SubElement(graph, "root")
    ET.SubElement(root, "mxCell", {"id": "0"})
    ET.SubElement(root, "mxCell", {"id": "1", "parent": "0"})

    title = cell("1", "title", model["title"], style(fontSize=22, fontStyle=1, html=1, whiteSpace="wrap"), vertex=True)
    title.set("data-source-title", model["title"])
    geometry(title, 20, 20, width - 40, 40)
    root.append(title)
    for index, label in enumerate(axis.labels):
        header = cell("1", f"period_{index}", label, style(rounded=0, whiteSpace="wrap", html=1, fillColor="#e6e6e6", strokeColor="#999999", fontStyle=1), vertex=True)
        geometry(header, LEFT_W + index * COL_W, TOP, COL_W, HEADER_H)
        root.append(header)
    for lane in lanes:
        lane_cell = cell("1", f"lane_{lane['id']}", lane["title"], style(swimlane=1, horizontal=0, startSize=32, html=1, whiteSpace="wrap", fillColor="#f5f5f5", strokeColor="#666666"), vertex=True)
        _source_metadata(lane_cell, lane, ("title", "description"))
        geometry(lane_cell, 20, lane_y[lane["id"]], width - 60, lane_heights[lane["id"]])
        root.append(lane_cell)

    id_to_cell = {}
    for task in model.get("tasks", []) or []:
        lane = item_lane(task, lane_index)
        level, x, task_width = task_levels[task["id"]]
        fill, stroke = STATUS_COLORS.get(task.get("status", "planned"), STATUS_COLORS["planned"])
        label = task["title"]
        if task.get("owner"):
            label += f"\n{task['owner']}"
        if task.get("risk"):
            label += f"\n⚠ {task['risk']}"
        task_cell = cell(f"lane_{lane}", f"task_{task['id']}", label, style(rounded=1, arcSize=8, whiteSpace="wrap", html=1, fillColor=fill, strokeColor=stroke, strokeWidth=3 if task.get("risk") else 1), vertex=True)
        _source_metadata(task_cell, task, ("title", "owner", "status", "risk", "notes", "outcomes", "milestones"))
        geometry(task_cell, x, 28 + level * (TASK_H + TASK_GAP), task_width, TASK_H)
        root.append(task_cell)
        id_to_cell[task["id"]] = task_cell.get("id")

    baseline = {m["id"]: m for m in (model.get("baseline") or {}).get("milestones", []) or []}
    current = {m["id"]: m for m in model.get("milestones", []) or []}
    label_counts = {lane["id"]: 0 for lane in lanes}
    marker_y = {lane["id"]: 48 + level_counts[lane["id"]] * (TASK_H + TASK_GAP) for lane in lanes}
    for mid, old in sorted(baseline.items()):
        delta = milestone_deltas.get(mid, {})
        if mid in current and delta.get("state") not in ("delayed", "accelerated"):
            continue
        lane, x = item_lane(old, lane_index), axis.milestone_x(old) - 20
        marker = cell(f"lane_{lane}", f"baseline_{mid}", "", style(rhombus=1, html=1, fillColor="#ffffff", strokeColor="#999999", dashed=1, opacity=45), vertex=True)
        geometry(marker, x - 14, marker_y[lane] - 14, 28, 28)
        root.append(marker)
        text = old["title"] + ("\nremoved" if delta.get("state") == "removed" else "")
        lw, row = label_width(text), label_counts[lane]
        label_counts[lane] += 1
        label_cell = cell(f"lane_{lane}", f"baseline_label_{mid}", text, style(whiteSpace="wrap", html=1, fillColor="#ffffff", strokeColor="none", fontColor="#666666", fontSize=10), vertex=True)
        geometry(label_cell, clamp(x - lw / 2, 8, width - 80 - lw), marker_y[lane] + 22 + row * 42, lw, 38)
        root.append(label_cell)

    if model.get("schema_version") == 2:
        suffix = "" if model.get("time_scale") == "order" else "d"
        history_delta_by_edge = {
            (item["id"], item["from_revision_id"], item["to_revision_id"]): item
            for item in history_deltas
        }
        for milestone in model.get("milestones", []) or []:
            lane = item_lane(milestone, lane_index)
            revisions = milestone_revisions(milestone, model.get("time_scale", "month"))
            for revision in revisions[:-1]:
                rid = revision["revision_id"]
                x = axis.x(revision["order"] if axis.scale == "order" else revision["date"]) - 20
                marker = cell(
                    f"lane_{lane}", f"history_{milestone['id']}_{rid}", "",
                    style(rhombus=1, html=1, fillColor="#ffffff", strokeColor="#999999", dashed=1, opacity=45),
                    vertex=True,
                )
                _source_metadata(marker, revision, ("revision_id", "revision_order", "plan_version", "recorded_at", "reason"))
                marker.set("data-milestone-id", milestone["id"])
                geometry(marker, x - 14, marker_y[lane] - 14, 28, 28)
                root.append(marker)
                text = f"{milestone['title']}\n{revision['plan_version']}"
                lw, row = label_width(text), label_counts[lane]
                label_counts[lane] += 1
                label_cell = cell(
                    f"lane_{lane}", f"history_label_{milestone['id']}_{rid}", text,
                    style(whiteSpace="wrap", html=1, fillColor="#ffffff", strokeColor="none", fontColor="#666666", fontSize=10, opacity=65),
                    vertex=True,
                )
                geometry(label_cell, clamp(x - lw / 2, 8, width - 80 - lw), marker_y[lane] + 22 + row * 42, lw, 38)
                root.append(label_cell)

            for previous, current_revision in zip(revisions, revisions[1:]):
                delta = history_delta_by_edge[(milestone["id"], previous["revision_id"], current_revision["revision_id"])]
                color = "#b85450" if delta["state"] == "delayed" else "#82b366" if delta["state"] == "accelerated" else "#999999"
                source_id = f"history_{milestone['id']}_{previous['revision_id']}"
                target_id = (
                    f"milestone_{milestone['id']}" if current_revision.get("is_current")
                    else f"history_{milestone['id']}_{current_revision['revision_id']}"
                )
                edge = cell(
                    "1", f"history_shift_{milestone['id']}_{previous['revision_id']}_{current_revision['revision_id']}",
                    f"{delta['delta']:+d}{suffix}",
                    style(endArrow="block", strokeColor=color, dashed=1, html=1, labelBackgroundColor="#ffffff"),
                    edge=True, source=source_id, target=target_id,
                )
                edge.set("data-cumulative-delta", str(delta["cumulative_delta"]))
                geometry(edge, relative=1)
                root.append(edge)

    for milestone in model.get("milestones", []) or []:
        lane, x = item_lane(milestone, lane_index), axis.milestone_x(milestone) - 20
        fill, stroke = STATUS_COLORS.get(milestone.get("status", "planned"), STATUS_COLORS["planned"])
        marker = cell(f"lane_{lane}", f"milestone_{milestone['id']}", "", style(rhombus=1, html=1, fillColor=fill, strokeColor=stroke, strokeWidth=3 if milestone.get("risk") else 1), vertex=True)
        metadata_fields = ("title", "owner", "status", "risk", "notes", "outcomes")
        if model.get("schema_version") == 2:
            metadata_fields += ("revision_id", "revision_order", "plan_version", "recorded_at", "reason")
        _source_metadata(marker, milestone, metadata_fields)
        geometry(marker, x - 18, marker_y[lane] - 18, 36, 36)
        root.append(marker)
        id_to_cell[milestone["id"]] = marker.get("id")
        text = milestone["title"] + (f"\n⚠ {milestone['risk']}" if milestone.get("risk") else "")
        lw, row = label_width(text), label_counts[lane]
        label_counts[lane] += 1
        label_cell = cell(f"lane_{lane}", f"milestone_label_{milestone['id']}", text, style(whiteSpace="wrap", html=1, fillColor="#ffffff", strokeColor="none", fontSize=10), vertex=True)
        geometry(label_cell, clamp(x - lw / 2, 8, width - 80 - lw), marker_y[lane] + 22 + row * 42, lw, 38)
        root.append(label_cell)

    for delta in sorted(milestone_deltas.values(), key=lambda item: item["id"]):
        if delta["state"] not in ("delayed", "accelerated"):
            continue
        color = "#b85450" if delta["state"] == "delayed" else "#82b366"
        suffix = "" if model.get("time_scale") == "order" else "d"
        edge = cell("1", f"shift_{delta['id']}", f"{delta['delta']:+d}{suffix}", style(endArrow="block", strokeColor=color, dashed=1, html=1, labelBackgroundColor="#ffffff"), edge=True, source=f"baseline_{delta['id']}", target=f"milestone_{delta['id']}")
        geometry(edge, relative=1)
        root.append(edge)

    for dep in model.get("dependencies", []) or []:
        dep_type = dep.get("type", "relates_to")
        influence = dep_type == "influences"
        label = dep_type + (f": {dep['impact']}" if dep.get("impact") else "")
        edge = cell("1", f"dep_{dep['id']}", label, style(endArrow="open" if influence else "block", dashed=1 if influence else 0, strokeColor="#9673a6" if influence else "#666666", html=1, labelBackgroundColor="#ffffff"), edge=True, source=id_to_cell[dep["from"]], target=id_to_cell[dep["to"]])
        _source_metadata(edge, dep, ("type", "impact", "rationale"))
        geometry(edge, relative=1)
        root.append(edge)

    outcomes_by_id = {outcome["id"]: outcome for outcome in model.get("outcomes", []) or []}
    outcome_cells, outcome_counts = {}, {lane["id"]: 0 for lane in lanes}
    for item in (model.get("tasks", []) or []) + (model.get("milestones", []) or []):
        lane = item_lane(item, lane_index)
        for oid in item.get("outcomes", []) or []:
            key = (lane, oid)
            if key not in outcome_cells:
                outcome = outcomes_by_id[oid]
                index = outcome_counts[lane]
                outcome_counts[lane] += 1
                out = cell("1", f"outcome_{oid}_{lane}", outcome["title"], style(rounded=1, whiteSpace="wrap", html=1, fillColor="#fff2cc", strokeColor="#d6b656"), vertex=True)
                _source_metadata(out, outcome, ("title", "description", "metric"))
                geometry(out, width - 250, lane_y[lane] + lane_heights[lane] - 58 - index * (OUTCOME_H + OUTCOME_GAP), 190, OUTCOME_H)
                root.append(out)
                outcome_cells[key] = out.get("id")
            edge = cell("1", f"outcome_edge_{oid}_{item['id']}", "", style(endArrow="open", dashed=1, strokeColor="#d6b656", html=1), edge=True, source=id_to_cell[item["id"]], target=outcome_cells[key])
            geometry(edge, relative=1)
            root.append(edge)
    return ET.ElementTree(mxfile), report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Generate roadmap .drawio from roadmap.yaml.")
    parser.add_argument("input", help="roadmap YAML")
    parser.add_argument("-o", "--output", required=True, help="output .drawio path")
    parser.add_argument("--report", help="report path (default: <output>.validation-report.json)")
    args = parser.parse_args(argv)
    try:
        tree, report = build_drawio(load_yaml(args.input))
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        ET.indent(tree, space="  ")
        tree.write(args.output, encoding="utf-8", xml_declaration=True)
        with open(args.report or args.output + ".validation-report.json", "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2, sort_keys=True)
    except (OSError, ValueError) as exc:
        sys.exit(f"error: {exc}")


if __name__ == "__main__":
    main()
