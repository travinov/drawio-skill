#!/usr/bin/env python3
"""Deterministic structural linter for .drawio files.

Catches the class of mistakes a vision self-check is slow and unreliable at:
dangling edge endpoints, duplicate or reserved ids, broken parent references,
and (as warnings) off-grid geometry, overlapping sibling nodes, and edge
routing defects. Runs without launching draw.io, so it is a fast pre-check
before the visual review step.

  python3 validate.py diagram.drawio

Edge routing checks (warnings): an edge segment crossing a non-incident leaf
vertex ("routes through vertex"), and two edges crossing each other ("edges X
and Y cross") — the two defects the SKILL.md step-5 self-check looks for
("Edge-shape overlap", "Stacked edges"), but caught here deterministically.

Routing is only knowable from the XML when an edge carries explicit waypoints
(``<Array as="points">``) — exactly the hand-routed case the SKILL.md tells
authors to use to route around shapes. Edges with no waypoints are auto-routed
by draw.io at render time (the path is not stored), so they are NOT geometry-
checked here, keeping these warnings free of false positives. Endpoints honour
``exitX/exitY``/``entryX/entryY`` when present, else the node centre, and
absolute positions are resolved through parent containers.

Exit status is non-zero when any error (or, with --strict, any warning) is
found, so it can gate a workflow. Compressed (non-XML) diagram pages are
skipped with a warning — this skill always writes uncompressed XML.

Usage: python3 validate.py <file.drawio> [--strict]
"""
import argparse
import html
import importlib.util
import json
import math
import os
import re
import sys
import xml.etree.ElementTree as ET

from validation_common import ValidationReport, print_report

RESERVED = {"0", "1"}


def rect(cell):
    """Return (x, y, w, h) floats for a cell's geometry, or None if absent/bad.

    x/y default to 0 when omitted: draw.io treats a missing position as the
    origin, and container-managed children (table rows, swimlane/UML-class
    lines under tableLayout) legitimately omit x/y while keeping width/height.
    Only width/height are required to be present and numeric.
    """
    g = cell.find("mxGeometry")
    if g is None:
        return None
    try:
        return (float(g.get("x", "0")), float(g.get("y", "0")),
                float(g.get("width", "nan")), float(g.get("height", "nan")))
    except ValueError:
        return None


def is_edge_label(cell):
    """True for a draw.io edge label / relative-positioned child vertex.

    These legitimately omit width/height: their position is given relative to a
    parent edge (style ``edgeLabel``) or via ``relative="1"`` geometry. Treating
    them as normal vertices wrongly flags them as missing/invalid geometry.
    """
    if "edgeLabel" in (cell.get("style") or ""):
        return True
    g = cell.find("mxGeometry")
    return g is not None and g.get("relative") == "1"


def overlap(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


# --- Edge routing geometry -------------------------------------------------
#
# These helpers reason about edge paths. They only apply to edges with explicit
# waypoints (the route is otherwise computed by draw.io at render time and not
# stored in the XML), so the checks never guess an auto-routed path.

def style_num(style, key):
    """Return float value of ``key=`` in a draw.io style string, or None."""
    for part in (style or "").split(";"):
        if part.startswith(key + "="):
            try:
                return float(part.split("=", 1)[1])
            except ValueError:
                return None
    return None


def style_value(style, key):
    """Return raw draw.io style value for key, or None."""
    for part in (style or "").split(";"):
        if part.startswith(key + "="):
            return part.split("=", 1)[1]
    return None


def cell_text(cell):
    """Return readable text for a cell value, stripping simple HTML markup."""
    value = html.unescape(cell.get("value") or "")
    value = value.replace("&#xa;", "\n").replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    value = re.sub(r"<[^>]+>", "", value)
    return value.strip()


def estimated_text_size(text, font_size):
    """Approximate rendered text size for draw.io labels.

    This is intentionally conservative and dependency-free. It is not a text
    renderer; it catches obvious cases such as long labels inside 36px
    milestone diamonds while avoiding pixel-perfect claims.
    """
    lines = [line for line in text.splitlines() if line.strip()] or [text]
    widths = []
    for line in lines:
        width = 0.0
        for ch in line:
            if ch.isspace():
                width += 0.35 * font_size
            elif ord(ch) > 127:
                width += 0.72 * font_size
            elif ch.isupper():
                width += 0.62 * font_size
            else:
                width += 0.55 * font_size
        widths.append(width)
    return max(widths, default=0.0), len(lines) * font_size * 1.25


def text_box_capacity(cell, box):
    """Return approximate (width, height) available for a cell label."""
    _, _, w, h = box
    style = cell.get("style") or ""
    # Diamonds/rhombi have much less horizontal room near their vertical center
    # than their bounding box suggests, and labels inside them are the common
    # source of visually overflowing roadmap milestones.
    shape = style_value(style, "shape") or ""
    if "rhombus" in style or shape == "rhombus":
        w *= 0.58
        h *= 0.72
    elif "ellipse" in style or shape == "ellipse":
        w *= 0.78
        h *= 0.78
    else:
        w -= 12
        h -= 8
    return max(1.0, w), max(1.0, h)


def is_constrained_text_shape(cell, box):
    """True when labels have little room and overflow is likely visible."""
    _, _, w, h = box
    style = cell.get("style") or ""
    shape = style_value(style, "shape") or ""
    return "rhombus" in style or shape in {"rhombus", "ellipse"} or w < 90


def text_fit_warnings(cells):
    """Warnings for labels that clearly cannot fit inside their vertex."""
    warns = []
    for c in cells:
        if c.get("vertex") != "1" or is_edge_label(c):
            continue
        text = cell_text(c)
        if not text:
            continue
        box = rect(c)
        if box is None or any(v != v for v in box):
            continue
        style = c.get("style") or ""
        font_size = style_num(style, "fontSize") or 12.0
        required_w, required_h = estimated_text_size(text, font_size)
        capacity_w, capacity_h = text_box_capacity(c, box)
        cid = c.get("id")
        if required_h > capacity_h * 1.15:
            warns.append(
                f"vertex {cid!r} label height likely overflows "
                f"({required_h:.0f}px text > {capacity_h:.0f}px box)"
            )
        # If wrapping is enabled, long text can still fit by wrapping, but only
        # when the available height can hold the resulting line count. Estimate
        # the wrapped line count instead of warning on width alone.
        constrained = is_constrained_text_shape(c, box)
        if style_value(style, "whiteSpace") == "wrap" or "whiteSpace=wrap" in style:
            wrapped_lines = max(1, int((required_w + capacity_w - 1) // capacity_w))
            wrapped_h = wrapped_lines * font_size * 1.25
            severe = required_w > capacity_w * 2.2 and wrapped_h > capacity_h * 1.5
            if (constrained or severe) and wrapped_h > capacity_h * 1.15:
                warns.append(
                    f"vertex {cid!r} label likely overflows after wrapping "
                    f"({wrapped_lines} line(s) need {wrapped_h:.0f}px > {capacity_h:.0f}px)"
                )
        elif constrained and required_w > capacity_w * 1.10:
            warns.append(
                f"vertex {cid!r} label width likely overflows "
                f"({required_w:.0f}px text > {capacity_w:.0f}px box)"
            )
    return warns


def abs_rect(cell, by_id):
    """Absolute (x, y, w, h) of a vertex, summing parent-container offsets.

    Children of a container use coordinates relative to the container origin, so
    an edge spanning containers needs absolute positions to be compared.
    """
    r = rect(cell)
    if r is None or any(v != v for v in r):
        return None
    x, y, w, h = r
    parent, seen = cell.get("parent"), set()
    while parent and parent in by_id and parent not in seen:
        seen.add(parent)
        p = by_id[parent]
        if p.get("vertex") == "1":
            pr = rect(p)
            if pr and not any(v != v for v in pr):
                x += pr[0]
                y += pr[1]
        parent = p.get("parent")
    return (x, y, w, h)


def endpoint(edge, end, by_id):
    """Absolute (x, y) where ``edge`` meets its source/target vertex.

    Honours exitX/exitY (source) and entryX/entryY (target) if the style pins
    them; otherwise the vertex centre. Returns None if the vertex is unresolved.
    """
    vid = edge.get(end)
    if not vid or vid not in by_id:
        return None
    box = abs_rect(by_id[vid], by_id)
    if box is None:
        return None
    x, y, w, h = box
    style = edge.get("style") or ""
    fx = style_num(style, "exitX" if end == "source" else "entryX")
    fy = style_num(style, "exitY" if end == "source" else "entryY")
    return (x + (fx if fx is not None else 0.5) * w,
            y + (fy if fy is not None else 0.5) * h)


def edge_waypoints(edge):
    """Explicit <Array as="points"> waypoints of an edge as [(x, y), ...]."""
    g = edge.find("mxGeometry")
    if g is None:
        return []
    arr = g.find("Array")
    if arr is None:
        return []
    pts = []
    for pt in arr.findall("mxPoint"):
        px, py = pt.get("x"), pt.get("y")
        if px is not None and py is not None:
            try:
                pts.append((float(px), float(py)))
            except ValueError:
                pass
    return pts


def edge_route(edge, by_id):
    """Absolute polyline [(x, y), ...] for a waypointed edge, or None.

    Returns None when the edge has no explicit waypoints (auto-routed; path
    unknown) or an endpoint cannot be resolved.
    """
    waypoints = edge_waypoints(edge)
    if not waypoints:
        return None
    s, t = endpoint(edge, "source", by_id), endpoint(edge, "target", by_id)
    if s is None or t is None:
        return None
    return [s] + waypoints + [t]


def _orient(a, b, c):
    v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    return 0 if abs(v) < 1e-9 else (1 if v > 0 else -1)


def segments_cross(p1, p2, p3, p4):
    """True if segments p1p2 and p3p4 properly cross (interior intersection).

    Proper crossing only: collinear overlap and shared-endpoint touches return
    False, so edges meeting at a common node or grazing a corner are not flagged.
    """
    o1, o2 = _orient(p1, p2, p3), _orient(p1, p2, p4)
    o3, o4 = _orient(p3, p4, p1), _orient(p3, p4, p2)
    return o1 != o2 and o3 != o4 and 0 not in (o1, o2, o3, o4)


def _point_in_rect(p, box, eps=1e-6):
    x, y, w, h = box
    return x + eps < p[0] < x + w - eps and y + eps < p[1] < y + h - eps


def route_hits_rect(points, box):
    """True if a polyline enters a rectangle's interior or crosses a border."""
    x, y, w, h = box
    corners = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    borders = list(zip(corners, corners[1:] + corners[:1]))
    for a, b in zip(points, points[1:]):
        if _point_in_rect(a, box) or _point_in_rect(b, box):
            return True
        if any(segments_cross(a, b, c, d) for c, d in borders):
            return True
    return False


def routes_cross(pa, pb):
    """True if any segment of polyline pa properly crosses any of pb."""
    for a1, a2 in zip(pa, pa[1:]):
        for b1, b2 in zip(pb, pb[1:]):
            if segments_cross(a1, a2, b1, b2):
                return True
    return False


def geometry_warnings(cells, ids, parents):
    """Edge-through-vertex and edge-crossing warnings for waypointed edges."""
    warns = []
    routed = []          # (edge_id, polyline, {source, target})
    for c in cells:
        if c.get("edge") == "1":
            pts = edge_route(c, ids)
            if pts:
                routed.append((c.get("id"), pts,
                               {c.get("source"), c.get("target")}))
    # Edge routes through an unrelated leaf vertex (containers wrap children, so
    # an edge legitimately traverses them — restrict to leaves, as overlap does).
    leaves = [(c.get("id"), abs_rect(c, ids)) for c in cells
              if c.get("vertex") == "1" and c.get("id") not in parents
              and not is_edge_label(c)]
    leaves = [(vid, box) for vid, box in leaves if box]
    for eid, pts, ends in routed:
        for vid, box in leaves:
            if vid not in ends and route_hits_rect(pts, box):
                warns.append(f"edge {eid!r} routes through vertex {vid!r}")
    # Edge-edge crossings (both routes known).
    for i in range(len(routed)):
        for j in range(i + 1, len(routed)):
            (ia, pa, _), (ib, pb, _) = routed[i], routed[j]
            if routes_cross(pa, pb):
                warns.append(f"edges {ia!r} and {ib!r} cross")
    return warns


def check_page(diagram):
    """Return (errors, warnings) for one <diagram> page."""
    name = diagram.get("name", "?")
    model = diagram.find("mxGraphModel")
    if model is None:
        if (diagram.text or "").strip():
            return [], [f"page {name!r}: compressed, skipped (cannot lint)"]
        return [f"page {name!r}: no <mxGraphModel>"], []
    root = model.find("root")
    if root is None:
        return [f"page {name!r}: no <root>"], []
    # Normalize UserObject/object wrappers (used for links & metadata): the id
    # lives on the wrapper, geometry/style on the inner mxCell — fold the two
    # into one cell so edges referencing the wrapper id resolve.
    cells = []
    for child in (root if root is not None else []):
        if child.tag == "mxCell":
            cells.append(child)
        elif child.tag in ("UserObject", "object"):
            inner = child.find("mxCell")
            if inner is not None:
                inner.set("id", child.get("id", ""))
                cells.append(inner)
    errors, warns = [], []
    ids = {}
    for c in cells:
        cid = c.get("id")
        if not cid:
            errors.append("cell is missing required id")
            continue
        if cid in ids:
            errors.append(f"duplicate id {cid!r}")
        ids[cid] = c
    parents = {c.get("parent") for c in cells}            # ids that have children
    for c in cells:
        cid, parent = c.get("id"), c.get("parent")
        is_v, is_e = c.get("vertex") == "1", c.get("edge") == "1"
        if is_v and is_e:
            errors.append(f"cell {cid!r} cannot be both vertex and edge")
        if is_e and c.find("mxGeometry") is None:
            errors.append(f"edge {cid!r} has missing geometry")
        if parent is not None and parent not in ids:
            errors.append(f"cell {cid!r} parent {parent!r} does not exist")
        for end in ("source", "target"):
            ref = c.get(end)
            if ref and ref not in ids:
                errors.append(f"edge {cid!r} {end} {ref!r} does not exist")
        if (is_v or is_e) and cid in RESERVED:
            errors.append(f"cell {cid!r} reuses reserved id 0/1")
        if is_v and not is_edge_label(c):
            r = rect(c)
            if r is None or any(not math.isfinite(v) for v in r):
                errors.append(f"vertex {cid!r} has missing/invalid geometry")
            else:
                x, y, w, h = r
                if w <= 0 or h <= 0:
                    warns.append(f"vertex {cid!r} non-positive size {w:g}x{h:g}")
                if x < 0 or y < 0:
                    warns.append(f"vertex {cid!r} negative position ({x:g},{y:g})")
    # Sibling overlap: only leaf vertices (containers legitimately wrap children).
    boxes = [(c.get("id"), c.get("parent"), rect(c)) for c in cells
             if c.get("vertex") == "1" and c.get("id") not in parents and rect(c)
             and not any(not math.isfinite(v) for v in rect(c))]
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            (ia, pa, ra), (ib, pb, rb) = boxes[i], boxes[j]
            if pa == pb and overlap(ra, rb):
                warns.append(f"vertices {ia!r} and {ib!r} overlap")
    warns += text_fit_warnings(cells)
    warns += geometry_warnings(cells, ids, parents)
    return errors, warns


def _cells(tree):
    result = []
    for page in tree.getroot().findall("diagram") or [tree.getroot()]:
        root = page.find("mxGraphModel/root")
        if root is None:
            continue
        for child in root:
            if child.tag == "mxCell":
                result.append(child)
            elif child.tag in ("UserObject", "object"):
                inner = child.find("mxCell")
                if inner is not None:
                    inner.set("id", child.get("id", ""))
                    result.append(inner)
    return result


def _module(name):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name + ".py")
    module_spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


def _profile_roadmap(tree, source, report):
    roadmap_validate = _module("roadmap_validate")
    timeline = _module("roadmap_timeline")
    model, source_report = roadmap_validate.validate_document(roadmap_validate.load_yaml(source))
    if source_report["summary"]["errors"]:
        report.add("artifact-parse", "error", "artifact.source.invalid", "", "roadmap source does not pass validation")
        return
    cells = _cells(tree)
    by_id = {cell.get("id"): cell for cell in cells if cell.get("id")}
    axis = timeline.TimelineAxis(model)
    lanes = model.get("lanes") or [{"id": "roadmap", "title": "Roadmap"}]
    lane_ids = {lane["id"] for lane in lanes}
    default_lane = lanes[0]["id"]

    diagrams = tree.getroot().findall("diagram") or [tree.getroot()]
    if diagrams and diagrams[0].get("data-schema-version") != str(model.get("schema_version", 1)):
        report.add("round-trip", "error", "artifact.contract.schema_version", "/schema_version", "diagram schema version does not match source")

    title = by_id.get("title")
    if title is None or title.get("value") != model["title"]:
        report.add("round-trip", "error", "artifact.text.title", "/title", "diagram title does not exactly match roadmap source", "title")
    for lane in lanes:
        element = by_id.get(f"lane_{lane['id']}")
        if element is None:
            report.add("round-trip", "error", "artifact.coverage.lane", "/lanes", f"missing lane cell for {lane['id']!r}", lane["id"])
        elif element.get("value") != lane["title"]:
            report.add("round-trip", "error", "artifact.text.lane", "/lanes", f"lane label for {lane['id']!r} is not lossless", lane["id"])

    for kind, items in (("task", model.get("tasks", []) or []), ("milestone", model.get("milestones", []) or [])):
        for index, item in enumerate(items):
            cid = f"{kind}_{item['id']}"
            element = by_id.get(cid)
            path = f"/{kind}s/{index}"
            if element is None:
                report.add("round-trip", "error", f"artifact.coverage.{kind}", path, f"missing {kind} cell {cid!r}", item["id"])
                continue
            lane = item.get("lane") if item.get("lane") in lane_ids else default_lane
            if element.get("parent") != f"lane_{lane}":
                report.add("round-trip", "error", "artifact.coordinate.lane", path + "/lane", f"{kind} {item['id']!r} is not in lane {lane!r}", item["id"])
            if element.get("data-title") != item["title"]:
                report.add("round-trip", "error", "artifact.text.entity", path + "/title", f"{kind} title is not preserved exactly", item["id"])
            for field in ("status", "risk"):
                if element.get(f"data-{field}", "") != str(item.get(field, "")):
                    report.add("round-trip", "error", f"artifact.coverage.{field}", path + f"/{field}", f"{field} is not preserved", item["id"])
            if model.get("schema_version") == 2 and kind == "milestone":
                for field in ("revision_id", "revision_order", "plan_version", "recorded_at", "reason"):
                    attribute = f"data-{field.replace('_', '-')}"
                    if element.get(attribute, "") != str(item.get(field, "")):
                        report.add("round-trip", "error", f"artifact.coverage.current_{field}", path + f"/{field}", f"current milestone {field} is not preserved", item["id"])
            box = rect(element)
            lane_box = rect(by_id.get(f"lane_{lane}")) if by_id.get(f"lane_{lane}") is not None else None
            if box and lane_box and (box[1] < 0 or box[1] + box[3] > lane_box[3] + 0.1):
                report.add("round-trip", "error", "artifact.coordinate.lane_bounds", path, f"{kind} lies outside lane Y bounds", item["id"])
            if box:
                expected = axis.task_span(item)[0] - 20 if kind == "task" else axis.milestone_x(item) - 20 - 18
                if abs(box[0] - expected) > 1.1:
                    report.add("round-trip", "error", "artifact.coordinate.timeline", path, f"{kind} X coordinate does not match {axis.scale} source coordinate", item["id"])
            for oid in item.get("outcomes", []) or []:
                if f"outcome_edge_{oid}_{item['id']}" not in by_id:
                    report.add("round-trip", "error", "artifact.coverage.outcome", path + "/outcomes", f"missing outcome link {oid!r}", item["id"])
    if model.get("schema_version") == 2:
        history_deltas = source_report.get("history_deltas", [])
        for milestone_index, milestone in enumerate(model.get("milestones", []) or []):
            lane = milestone.get("lane") if milestone.get("lane") in lane_ids else default_lane
            revisions = roadmap_validate.milestone_revisions(milestone, model.get("time_scale", "month"))
            for revision_index, revision in enumerate(revisions[:-1]):
                rid = revision["revision_id"]
                path = f"/milestones/{milestone_index}/history/{revision_index}"
                marker_id = f"history_{milestone['id']}_{rid}"
                element = by_id.get(marker_id)
                if element is None:
                    report.add("round-trip", "error", "artifact.coverage.milestone_history", path, f"missing history marker {marker_id!r}", milestone["id"])
                    continue
                if element.get("parent") != f"lane_{lane}":
                    report.add("round-trip", "error", "artifact.coordinate.history_lane", path + "/lane", "history marker is in the wrong lane", milestone["id"])
                box = rect(element)
                expected = axis.x(revision["order"] if axis.scale == "order" else revision["date"]) - 20 - 14
                if box and abs(box[0] - expected) > 1.1:
                    report.add("round-trip", "error", "artifact.coordinate.history_timeline", path, "history marker X coordinate does not match revision coordinate", milestone["id"])
                for field in ("revision_id", "revision_order", "plan_version", "recorded_at", "reason"):
                    if element.get(f"data-{field.replace('_', '-')}", "") != str(revision.get(field, "")):
                        report.add("round-trip", "error", f"artifact.coverage.history_{field}", path + f"/{field}", f"history {field} is not preserved", milestone["id"])
                label = by_id.get(f"history_label_{milestone['id']}_{rid}")
                expected_label = f"{milestone['title']}\n{revision['plan_version']}"
                if label is None or label.get("value") != expected_label:
                    report.add("round-trip", "error", "artifact.text.milestone_history", path, "history label is not lossless", milestone["id"])
        for delta in history_deltas:
            edge_id = f"history_shift_{delta['id']}_{delta['from_revision_id']}_{delta['to_revision_id']}"
            edge = by_id.get(edge_id)
            suffix = "" if model.get("time_scale") == "order" else "d"
            source_id = f"history_{delta['id']}_{delta['from_revision_id']}"
            current_milestone = next(item for item in model["milestones"] if item["id"] == delta["id"])
            target_id = (
                f"milestone_{delta['id']}" if current_milestone["revision_id"] == delta["to_revision_id"]
                else f"history_{delta['id']}_{delta['to_revision_id']}"
            )
            if edge is None:
                report.add("round-trip", "error", "artifact.coverage.history_shift", "/milestones", f"missing history shift edge {edge_id!r}", delta["id"])
            elif edge.get("source") != source_id or edge.get("target") != target_id:
                report.add("round-trip", "error", "artifact.reference.history_shift", "/milestones", "history shift endpoints do not match source", delta["id"])
            elif edge.get("value") != f"{delta['delta']:+d}{suffix}":
                report.add("round-trip", "error", "artifact.text.history_shift", "/milestones", "history shift label does not match sequential delta", delta["id"])
    for index, dep in enumerate(model.get("dependencies", []) or []):
        edge = by_id.get(f"dep_{dep['id']}")
        if edge is None:
            report.add("round-trip", "error", "artifact.coverage.dependency", f"/dependencies/{index}", f"missing dependency edge {dep['id']!r}", dep["id"])
            continue
        expected_source = ("task_" if dep["from"] in {t["id"] for t in model.get("tasks", []) or []} else "milestone_") + dep["from"]
        expected_target = ("task_" if dep["to"] in {t["id"] for t in model.get("tasks", []) or []} else "milestone_") + dep["to"]
        if edge.get("source") != expected_source or edge.get("target") != expected_target:
            report.add("round-trip", "error", "artifact.reference.dependency", f"/dependencies/{index}", "dependency endpoints do not match source", dep["id"])


def _profile_gitflow(tree, source, report):
    validator = _module("gitflow_validate")
    generator = _module("gitflow")
    model, source_report = validator.validate_document(validator.load_spec(source))
    if source_report["summary"]["errors"]:
        report.add("artifact-parse", "error", "artifact.source.invalid", "", "git-flow source does not pass validation")
        return
    cells = _cells(tree)
    by_id = {cell.get("id"): cell for cell in cells if cell.get("id")}
    for index, branch in enumerate(model.get("branches", [])):
        lane = by_id.get(f"lane_{branch['id']}")
        if lane is None:
            report.add("round-trip", "error", "artifact.coverage.branch", f"/branches/{index}", f"missing branch lane {branch['id']!r}", branch["id"])
        elif lane.get("value") != branch.get("label", branch["id"]):
            report.add("round-trip", "error", "artifact.text.branch", f"/branches/{index}/label", "branch label is not lossless", branch["id"])
    previous_center = None
    for index, event in enumerate(validator.normalize_events(model)):
        cid = f"event_{event['id']}"
        element = by_id.get(cid)
        if element is None:
            report.add("round-trip", "error", "artifact.coverage.event", f"/events/{index}", f"missing event cell {cid!r}", event["id"])
            continue
        branch = validator.event_branch(event)
        if element.get("parent") != f"lane_{branch}":
            report.add("round-trip", "error", "artifact.coordinate.branch", f"/events/{index}", f"event is not in branch lane {branch!r}", event["id"])
        box = rect(element)
        if box:
            center = box[0] + box[2] / 2
            if previous_center is not None and center < previous_center - 0.1:
                report.add("round-trip", "error", "artifact.coordinate.chronology", f"/events/{index}", "event X coordinate reverses normalized chronology", event["id"])
            previous_center = center
        label = event.get("label")
        if label:
            label_cell = element if event.get("type") in ("tag", "note") else by_id.get(f"label_{event['id']}")
            if label_cell is None or label_cell.get("value") != label:
                report.add("round-trip", "error", "artifact.text.event", f"/events/{index}/label", "event label is not lossless", event["id"])
    for edge_id, source_id, target_id, _ in generator.edge_specs(model):
        edge = by_id.get(edge_id)
        if edge is None or edge.get("source") != source_id or edge.get("target") != target_id:
            report.add("round-trip", "error", "artifact.coverage.gitflow_edge", "/events", f"missing or incorrect relationship edge {edge_id!r}", edge_id)


def _code(message, severity):
    if "missing required id" in message:
        return "artifact.id.missing"
    if "duplicate id" in message:
        return "artifact.id.duplicate"
    if "both vertex and edge" in message:
        return "artifact.cell.invalid_kind"
    if " parent " in message or " source " in message or " target " in message:
        return "artifact.reference.unresolved"
    if "geometry" in message or "size" in message or "position" in message:
        return "artifact.geometry.invalid"
    if "overlap" in message:
        return "artifact.readability.overlap"
    if "label" in message and "overflow" in message:
        return "artifact.readability.text_overflow"
    if "cross" in message:
        return "artifact.readability.crossing"
    if "routes through" in message:
        return "artifact.readability.route_through"
    if "compressed" in message:
        return "artifact.page.compressed"
    return f"artifact.{'structure' if severity == 'error' else 'readability'}.generic"


def validate_tree(tree, strict=False, profile=None, source=None):
    report = ValidationReport()
    pages = tree.getroot().findall("diagram") or [tree.getroot()]
    if pages:
        version = pages[0].get("data-schema-version")
        report.schema_version = int(version) if version and version.isdigit() else None
    for page_index, page in enumerate(pages):
        errors, warnings = check_page(page)
        for message in errors:
            report.add("artifact-parse", "error", _code(message, "error"), f"/pages/{page_index}", message)
        for message in warnings:
            report.add("layout", "warning", _code(message, "warning"), f"/pages/{page_index}", message)
    if profile and not source:
        report.add("artifact-parse", "error", "artifact.source.required", "", "--source is required with --profile")
    elif profile == "roadmap":
        _profile_roadmap(tree, source, report)
    elif profile == "gitflow":
        _profile_gitflow(tree, source, report)
    return report.finish(strict=strict)


def main():
    ap = argparse.ArgumentParser(description="Lint a .drawio file for structural errors.")
    ap.add_argument("file")
    ap.add_argument("--strict", action="store_true", help="treat warnings as failure too")
    ap.add_argument("--score", action="store_true",
                    help="also print a readability score (lower is better) — "
                         "useful for comparing layout variants of the same graph")
    ap.add_argument("--json", action="store_true", help="print stable machine-readable findings")
    ap.add_argument("--profile", choices=("roadmap", "gitflow"), help="run source-aware generator checks")
    ap.add_argument("--source", help="source roadmap YAML or git-flow JSON for profile checks")
    args = ap.parse_args()
    try:
        tree = ET.parse(args.file)
    except (ET.ParseError, OSError) as exc:
        report = ValidationReport()
        report.add("artifact-parse", "error", "artifact.xml.parse", "", f"cannot parse {args.file}: {exc}")
        print_report(report.finish(strict=args.strict), as_json=args.json)
        sys.exit(1)
    report = validate_tree(tree, strict=args.strict, profile=args.profile, source=args.source)
    errors = [f["message"] for f in report["findings"] if f["severity"] == "error"]
    warns = [f["message"] for f in report["findings"] if f["severity"] == "warning"]
    print_report(report, as_json=args.json)
    if args.score:
        # Weighted by how badly each defect hurts readability. Comparable only
        # across variants of the SAME graph (same nodes/edges).
        through = sum(1 for w in warns if "routes through" in w)
        cross = sum(1 for w in warns if " cross" in w)
        olap = sum(1 for w in warns if " overlap" in w)
        text = sum(1 for w in warns if "label" in w and "overflow" in w)
        print(f"score: {20 * through + 10 * cross + 5 * olap + 3 * text} "
              f"({through} through-vertex, {cross} crossings, {olap} overlaps, {text} text)")
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
