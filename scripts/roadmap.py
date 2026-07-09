#!/usr/bin/env python3
"""Generate deterministic roadmap .drawio XML from roadmap.yaml.

Usage:
  python3 roadmap.py roadmap.yaml -o roadmap.drawio
"""
import argparse
import datetime as dt
import html
import os
import sys
import xml.etree.ElementTree as ET

from roadmap_validate import calculate_milestone_deltas, load_yaml, validate_model


NS = "https://www.drawio.com"
BASE_LANE_H = 250
HEADER_H = 54
LEFT_W = 180
COL_W = 170
TOP = 80
TASK_H = 44
TASK_GAP = 12
OUTCOME_H = 40
OUTCOME_GAP = 10

STATUS_COLORS = {
    "planned": ("#dae8fc", "#6c8ebf"),
    "in_progress": ("#dae8fc", "#6c8ebf"),
    "on_track": ("#d5e8d4", "#82b366"),
    "at_risk": ("#fff2cc", "#d6b656"),
    "blocked": ("#f8cecc", "#b85450"),
    "done": ("#d5e8d4", "#82b366"),
    "cancelled": ("#f5f5f5", "#999999"),
}


def parse_date(value):
    return dt.date.fromisoformat(str(value))


def month_start(value):
    d = parse_date(value)
    return dt.date(d.year, d.month, 1)


def add_month(d):
    year = d.year + (d.month // 12)
    month = d.month % 12 + 1
    return dt.date(year, month, 1)


def month_label(d):
    return d.strftime("%Y-%m")


def collect_dates(model):
    dates = []
    for task in model.get("tasks", []) or []:
        if task.get("start"):
            dates.append(parse_date(task["start"]))
        if task.get("end"):
            dates.append(parse_date(task["end"]))
    for milestone in model.get("milestones", []) or []:
        dates.append(parse_date(milestone["date"]))
    for milestone in model.get("baseline", {}).get("milestones", []) or []:
        dates.append(parse_date(milestone["date"]))
    if not dates:
        today = dt.date.today()
        dates = [today]
    return min(dates), max(dates)


def timeline_months(start, end):
    cur = dt.date(start.year, start.month, 1)
    last = dt.date(end.year, end.month, 1)
    months = []
    while cur <= last:
        months.append(cur)
        cur = add_month(cur)
    if len(months) == 1:
        months.append(add_month(months[0]))
    return months


def x_for_date(value, months):
    d = parse_date(value)
    idx = (d.year - months[0].year) * 12 + (d.month - months[0].month)
    days = 31 if d.month == 12 else (add_month(dt.date(d.year, d.month, 1)) - dt.date(d.year, d.month, 1)).days
    return LEFT_W + idx * COL_W + ((d.day - 1) / max(days, 1)) * COL_W


def esc(value):
    return html.escape(str(value), quote=True)


def style(**parts):
    return ";".join(f"{k}={v}" for k, v in parts.items() if v is not None) + ";"


def cell(parent, cid, value="", style_value="", vertex=False, edge=False, source=None, target=None):
    attrs = {"id": cid, "value": esc(value), "style": style_value, "parent": parent}
    if vertex:
        attrs["vertex"] = "1"
    if edge:
        attrs["edge"] = "1"
        if source:
            attrs["source"] = source
        if target:
            attrs["target"] = target
    return ET.Element("mxCell", attrs)


def geometry(cell_el, x=None, y=None, w=None, h=None, relative=None, points=None):
    attrs = {"as": "geometry"}
    if x is not None:
        attrs["x"] = f"{x:.1f}"
    if y is not None:
        attrs["y"] = f"{y:.1f}"
    if w is not None:
        attrs["width"] = f"{w:.1f}"
    if h is not None:
        attrs["height"] = f"{h:.1f}"
    if relative is not None:
        attrs["relative"] = str(relative)
    g = ET.SubElement(cell_el, "mxGeometry", attrs)
    if points:
        arr = ET.SubElement(g, "Array", {"as": "points"})
        for px, py in points:
            ET.SubElement(arr, "mxPoint", {"x": f"{px:.1f}", "y": f"{py:.1f}"})
    return g


def lane_lookup(model):
    lanes = model.get("lanes") or [{"id": "roadmap", "title": "Roadmap"}]
    return lanes, {lane["id"]: idx for idx, lane in enumerate(lanes)}


def item_lane(item, lane_index):
    lane_id = item.get("lane")
    if lane_id in lane_index:
        return lane_id
    return next(iter(lane_index))


def lane_parent(lane_id):
    return f"lane_{lane_id}"


def label_width(text, min_w=120, max_w=210):
    # Approximate draw.io text width without rendering. This only sizes labels,
    # not layout-critical geometry.
    return max(min_w, min(max_w, 8 * len(str(text)) + 24))


def clamp(value, low, high):
    return max(low, min(high, value))


def task_span(task, months, start, end):
    start_x = x_for_date(task.get("start") or task.get("end") or start.isoformat(), months)
    end_x = x_for_date(task.get("end") or task.get("start") or task.get("date") or end.isoformat(), months)
    if end_x < start_x:
        start_x, end_x = end_x, start_x
    return start_x, end_x


def assign_task_levels(tasks, lane_index, months, start, end):
    levels_by_task = {}
    lane_levels = {lane_id: [] for lane_id in lane_index}
    for task in tasks:
        lane_id = item_lane(task, lane_index)
        start_x, end_x = task_span(task, months, start, end)
        width = max(140, end_x - start_x, label_width(task["title"], 140, 260))
        span = (start_x - 20, start_x - 20 + width)
        levels = lane_levels[lane_id]
        chosen = None
        for idx, occupied in enumerate(levels):
            if all(span[1] + 8 <= other[0] or span[0] >= other[1] + 8 for other in occupied):
                chosen = idx
                break
        if chosen is None:
            chosen = len(levels)
            levels.append([])
        levels[chosen].append(span)
        levels_by_task[task["id"]] = (chosen, span[0], span[1] - span[0])
    return levels_by_task, {lane_id: max(1, len(levels)) for lane_id, levels in lane_levels.items()}


def lane_layout(lanes, lane_task_counts, model):
    outcome_counts = {lane["id"]: set() for lane in lanes}
    label_counts = {lane["id"]: 0 for lane in lanes}
    lane_ids = {lane["id"] for lane in lanes}
    default_lane = lanes[0]["id"]
    for task in model.get("tasks", []) or []:
        lane_id = task.get("lane") if task.get("lane") in lane_ids else default_lane
        for oid in task.get("outcomes", []) or []:
            outcome_counts.setdefault(lane_id, set()).add(oid)
    for milestone in model.get("milestones", []) or []:
        lane_id = milestone.get("lane") if milestone.get("lane") in lane_ids else default_lane
        label_counts[lane_id] = label_counts.get(lane_id, 0) + 1
    current_ids = {m["id"] for m in model.get("milestones", []) or []}
    for milestone in model.get("baseline", {}).get("milestones", []) or []:
        lane_id = milestone.get("lane") if milestone.get("lane") in lane_ids else default_lane
        # Baseline labels are separate only when the old marker must be visible.
        if milestone.get("id") not in current_ids or milestone.get("date") != next(
            (m.get("date") for m in model.get("milestones", []) or [] if m.get("id") == milestone.get("id")),
            None,
        ):
            label_counts[lane_id] = label_counts.get(lane_id, 0) + 1

    heights = {}
    y_offsets = {}
    y = TOP + HEADER_H
    for lane in lanes:
        lid = lane["id"]
        task_band = 28 + lane_task_counts.get(lid, 1) * (TASK_H + TASK_GAP)
        outcome_band = len(outcome_counts.get(lid, set())) * (OUTCOME_H + OUTCOME_GAP)
        label_band = max(1, label_counts.get(lid, 0)) * 36
        heights[lid] = max(BASE_LANE_H, task_band + 95 + label_band + outcome_band)
        y_offsets[lid] = y
        y += heights[lid]
    return y_offsets, heights, y


def milestone_y(lane_id, lane_task_counts):
    return 28 + lane_task_counts.get(lane_id, 1) * (TASK_H + TASK_GAP) + 46


def build_drawio(model):
    errors, warnings, deltas = validate_model(model)
    if errors:
        raise SystemExit("error: invalid roadmap:\n" + "\n".join(f"- {e}" for e in errors))

    lanes, lane_index = lane_lookup(model)
    start, end = collect_dates(model)
    months = timeline_months(start, end)
    task_levels, lane_task_counts = assign_task_levels(model.get("tasks", []) or [], lane_index, months, start, end)
    lane_y, lane_heights, lane_bottom = lane_layout(lanes, lane_task_counts, model)
    width = LEFT_W + len(months) * COL_W + 120
    height = lane_bottom + 80

    mxfile = ET.Element("mxfile", {"host": "app.diagrams.net", "type": "device"})
    diagram = ET.SubElement(mxfile, "diagram", {"id": "roadmap", "name": "Roadmap"})
    model_el = ET.SubElement(diagram, "mxGraphModel", {
        "dx": "1200", "dy": "800", "grid": "1", "gridSize": "10", "guides": "1",
        "tooltips": "1", "connect": "1", "arrows": "1", "fold": "1", "page": "1",
        "pageScale": "1", "pageWidth": str(int(width)), "pageHeight": str(int(height)),
        "math": "0", "shadow": "0"
    })
    root = ET.SubElement(model_el, "root")
    ET.SubElement(root, "mxCell", {"id": "0"})
    ET.SubElement(root, "mxCell", {"id": "1", "parent": "0"})

    title = cell("1", "title", model["title"], style(fontSize=22, fontStyle=1, html=1, whiteSpace="wrap"))
    geometry(title, 20, 20, width - 40, 40)
    title.set("vertex", "1")
    root.append(title)

    for idx, month in enumerate(months):
        header = cell("1", f"period_{idx}", month_label(month),
                      style(rounded=0, whiteSpace="wrap", html=1, fillColor="#e6e6e6",
                            strokeColor="#999999", fontStyle=1), vertex=True)
        geometry(header, LEFT_W + idx * COL_W, TOP, COL_W, HEADER_H)
        root.append(header)

    for lane in lanes:
        lane_cell = cell("1", f"lane_{lane['id']}", lane["title"],
                         style(swimlane=1, horizontal=0, startSize=32, html=1,
                               whiteSpace="wrap", fillColor="#f5f5f5", strokeColor="#666666"),
                         vertex=True)
        geometry(lane_cell, 20, lane_y[lane["id"]], width - 60, lane_heights[lane["id"]])
        root.append(lane_cell)

    id_to_cell = {}
    for task in model.get("tasks", []) or []:
        lane_id = item_lane(task, lane_index)
        level, local_x, task_w = task_levels[task["id"]]
        local_y = 28 + level * (TASK_H + TASK_GAP)
        fill, stroke = STATUS_COLORS.get(task.get("status", "planned"), STATUS_COLORS["planned"])
        label = task["title"]
        if task.get("owner"):
            label += f"\\n{task['owner']}"
        task_cell = cell(lane_parent(lane_id), f"task_{task['id']}", label,
                         style(rounded=1, arcSize=8, whiteSpace="wrap", html=1,
                               fillColor=fill, strokeColor=stroke), vertex=True)
        geometry(task_cell, local_x, local_y, task_w, TASK_H)
        root.append(task_cell)
        id_to_cell[task["id"]] = f"task_{task['id']}"

    baseline_milestones = {m["id"]: m for m in model.get("baseline", {}).get("milestones", []) or []}
    current_milestones = {m["id"]: m for m in model.get("milestones", []) or []}
    delta_by_id = {d["id"]: d for d in deltas}
    milestone_label_counts = {}

    for mid, old in baseline_milestones.items():
        if mid not in current_milestones or delta_by_id.get(mid, {}).get("state") in ("delayed", "accelerated", "removed"):
            lane_id = item_lane(old, lane_index)
            x = x_for_date(old["date"], months)
            my = milestone_y(lane_id, lane_task_counts)
            old_cell = cell(lane_parent(lane_id), f"baseline_{mid}", "",
                            style(rhombus=1, whiteSpace="wrap", html=1, fillColor="#ffffff",
                                  strokeColor="#999999", dashed=1, opacity=45), vertex=True)
            marker_x = x - 20 - 14
            geometry(old_cell, marker_x, my - 14, 28, 28)
            root.append(old_cell)
            label_text = old["title"]
            if delta_by_id.get(mid, {}).get("state") == "removed":
                label_text += "\\nremoved"
            lw = label_width(label_text)
            label_x = clamp(marker_x + 14 - lw / 2, 8, width - 80 - lw)
            label_idx = milestone_label_counts.get(lane_id, 0)
            milestone_label_counts[lane_id] = label_idx + 1
            label_cell = cell(lane_parent(lane_id), f"baseline_label_{mid}", label_text,
                              style(rounded=0, whiteSpace="wrap", html=1, fillColor="#ffffff",
                                    strokeColor="none", fontColor="#666666", fontSize=10),
                              vertex=True)
            geometry(label_cell, label_x, my + 22 + label_idx * 36, lw, 32)
            root.append(label_cell)

    for milestone in model.get("milestones", []) or []:
        lane_id = item_lane(milestone, lane_index)
        x = x_for_date(milestone["date"], months)
        my = milestone_y(lane_id, lane_task_counts)
        fill, stroke = STATUS_COLORS.get(milestone.get("status", "planned"), ("#e1d5e7", "#9673a6"))
        ms = cell(lane_parent(lane_id), f"milestone_{milestone['id']}", "",
                  style(rhombus=1, whiteSpace="wrap", html=1, fillColor=fill, strokeColor=stroke),
                  vertex=True)
        marker_x = x - 20 - 18
        geometry(ms, marker_x, my - 18, 36, 36)
        root.append(ms)
        id_to_cell[milestone["id"]] = f"milestone_{milestone['id']}"
        lw = label_width(milestone["title"])
        label_x = clamp(marker_x + 18 - lw / 2, 8, width - 80 - lw)
        label_idx = milestone_label_counts.get(lane_id, 0)
        milestone_label_counts[lane_id] = label_idx + 1
        label_cell = cell(lane_parent(lane_id), f"milestone_label_{milestone['id']}", milestone["title"],
                          style(rounded=0, whiteSpace="wrap", html=1, fillColor="#ffffff",
                                strokeColor="none", fontSize=10),
                          vertex=True)
        geometry(label_cell, label_x, my + 22 + label_idx * 36, lw, 34)
        root.append(label_cell)

    for delta in deltas:
        if delta["state"] not in ("delayed", "accelerated"):
            continue
        source = f"baseline_{delta['id']}"
        target = f"milestone_{delta['id']}"
        color = "#b85450" if delta["state"] == "delayed" else "#82b366"
        label = f"{delta['days']:+d}d"
        edge = cell("1", f"shift_{delta['id']}", label,
                    style(endArrow="block", strokeColor=color, dashed=1, html=1,
                          labelBackgroundColor="#ffffff"), edge=True, source=source, target=target)
        geometry(edge, relative=1)
        root.append(edge)

    for dep in model.get("dependencies", []) or []:
        source = id_to_cell.get(dep["from"])
        target = id_to_cell.get(dep["to"])
        if not source or not target:
            continue
        is_influence = dep.get("type") == "influences"
        edge = cell("1", f"dep_{dep['id']}", dep.get("impact", dep.get("type", "")),
                    style(endArrow="open" if is_influence else "block",
                          dashed=1 if is_influence else 0,
                          strokeColor="#9673a6" if is_influence else "#666666",
                          html=1, labelBackgroundColor="#ffffff"),
                    edge=True, source=source, target=target)
        geometry(edge, relative=1)
        root.append(edge)

    outcome_cells = {}
    outcome_counts = {}
    for task in model.get("tasks", []) or []:
        for oid in task.get("outcomes", []) or []:
            outcome = next((o for o in model.get("outcomes", []) or [] if o["id"] == oid), None)
            source = id_to_cell.get(task["id"])
            if not outcome or not source:
                continue
            lane_id = item_lane(task, lane_index)
            key = (lane_id, oid)
            if key not in outcome_cells:
                ordinal = outcome_counts.get(lane_id, 0)
                outcome_counts[lane_id] = ordinal + 1
                out_id = f"outcome_{oid}_{lane_id}"
                out = cell("1", out_id, outcome["title"],
                           style(rounded=1, whiteSpace="wrap", html=1, fillColor="#fff2cc", strokeColor="#d6b656"),
                           vertex=True)
                y = lane_y[lane_id] + lane_heights[lane_id] - 58 - ordinal * (OUTCOME_H + OUTCOME_GAP)
                geometry(out, width - 250, y, 190, OUTCOME_H)
                root.append(out)
                outcome_cells[key] = out_id
            edge = cell("1", f"outcome_edge_{oid}_{task['id']}", "",
                        style(endArrow="open", dashed=1, strokeColor="#d6b656", html=1),
                        edge=True, source=source, target=outcome_cells[key])
            geometry(edge, relative=1)
            root.append(edge)

    return ET.ElementTree(mxfile)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Generate roadmap .drawio from roadmap.yaml.")
    ap.add_argument("input", help="roadmap YAML")
    ap.add_argument("-o", "--output", required=True, help="output .drawio path")
    args = ap.parse_args(argv)
    try:
        model = load_yaml(args.input)
        tree = build_drawio(model)
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        ET.indent(tree, space="  ")
        tree.write(args.output, encoding="utf-8", xml_declaration=True)
    except (OSError, ValueError) as exc:
        sys.exit(f"error: {exc}")


if __name__ == "__main__":
    main()
