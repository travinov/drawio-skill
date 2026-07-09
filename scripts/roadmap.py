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
LANE_H = 180
HEADER_H = 54
LEFT_W = 180
COL_W = 170
TOP = 80

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


def build_drawio(model):
    errors, warnings, deltas = validate_model(model)
    if errors:
        raise SystemExit("error: invalid roadmap:\n" + "\n".join(f"- {e}" for e in errors))

    lanes, lane_index = lane_lookup(model)
    start, end = collect_dates(model)
    months = timeline_months(start, end)
    width = LEFT_W + len(months) * COL_W + 120
    height = TOP + HEADER_H + len(lanes) * LANE_H + 80

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
        idx = lane_index[lane["id"]]
        lane_cell = cell("1", f"lane_{lane['id']}", lane["title"],
                         style(swimlane=1, horizontal=0, startSize=32, html=1,
                               whiteSpace="wrap", fillColor="#f5f5f5", strokeColor="#666666"),
                         vertex=True)
        geometry(lane_cell, 20, TOP + HEADER_H + idx * LANE_H, width - 60, LANE_H)
        root.append(lane_cell)

    id_to_cell = {}
    task_y_offset = 28
    for task in model.get("tasks", []) or []:
        lane_id = item_lane(task, lane_index)
        y = TOP + HEADER_H + lane_index[lane_id] * LANE_H + task_y_offset
        start_x = x_for_date(task.get("start") or task.get("end") or start.isoformat(), months)
        end_x = x_for_date(task.get("end") or task.get("start") or task.get("date") or end.isoformat(), months)
        if end_x < start_x:
            start_x, end_x = end_x, start_x
        fill, stroke = STATUS_COLORS.get(task.get("status", "planned"), STATUS_COLORS["planned"])
        label = task["title"]
        if task.get("owner"):
            label += f"\\n{task['owner']}"
        task_cell = cell(lane_parent(lane_id), f"task_{task['id']}", label,
                         style(rounded=1, arcSize=8, whiteSpace="wrap", html=1,
                               fillColor=fill, strokeColor=stroke), vertex=True)
        geometry(task_cell, start_x - 20, task_y_offset, max(70, end_x - start_x), 42)
        root.append(task_cell)
        id_to_cell[task["id"]] = f"task_{task['id']}"

    baseline_milestones = {m["id"]: m for m in model.get("baseline", {}).get("milestones", []) or []}
    current_milestones = {m["id"]: m for m in model.get("milestones", []) or []}
    delta_by_id = {d["id"]: d for d in deltas}

    for mid, old in baseline_milestones.items():
        if mid not in current_milestones or delta_by_id.get(mid, {}).get("state") in ("delayed", "accelerated", "removed"):
            lane_id = item_lane(old, lane_index)
            x = x_for_date(old["date"], months)
            old_cell = cell(lane_parent(lane_id), f"baseline_{mid}", old["title"],
                            style(rhombus=1, whiteSpace="wrap", html=1, fillColor="#ffffff",
                                  strokeColor="#999999", dashed=1, opacity=45), vertex=True)
            geometry(old_cell, x - 20 - 14, 90 - 14, 28, 28)
            root.append(old_cell)

    for milestone in model.get("milestones", []) or []:
        lane_id = item_lane(milestone, lane_index)
        x = x_for_date(milestone["date"], months)
        fill, stroke = STATUS_COLORS.get(milestone.get("status", "planned"), ("#e1d5e7", "#9673a6"))
        ms = cell(lane_parent(lane_id), f"milestone_{milestone['id']}", milestone["title"],
                  style(rhombus=1, whiteSpace="wrap", html=1, fillColor=fill, strokeColor=stroke),
                  vertex=True)
        geometry(ms, x - 20 - 18, 90 - 18, 36, 36)
        root.append(ms)
        id_to_cell[milestone["id"]] = f"milestone_{milestone['id']}"

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

    for task in model.get("tasks", []) or []:
        for oid in task.get("outcomes", []) or []:
            outcome = next((o for o in model.get("outcomes", []) or [] if o["id"] == oid), None)
            source = id_to_cell.get(task["id"])
            if not outcome or not source:
                continue
            lane_id = item_lane(task, lane_index)
            y = TOP + HEADER_H + lane_index[lane_id] * LANE_H + 84
            out = cell("1", f"outcome_{oid}_{task['id']}", outcome["title"],
                       style(rounded=1, whiteSpace="wrap", html=1, fillColor="#fff2cc", strokeColor="#d6b656"),
                       vertex=True)
            geometry(out, width - 250, y, 190, 40)
            root.append(out)
            edge = cell("1", f"outcome_edge_{oid}_{task['id']}", "",
                        style(endArrow="open", dashed=1, strokeColor="#d6b656", html=1),
                        edge=True, source=source, target=f"outcome_{oid}_{task['id']}")
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
