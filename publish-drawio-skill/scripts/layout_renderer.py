#!/usr/bin/env python3
"""Render draw.io XML from a validated semantic plan and layout result.

This module is intentionally a serialization boundary.  It binds page-scoped
semantic identities to the exact geometry returned by ``layout-result.v1``; it
does not place nodes, choose ports, or route edges.
"""
from __future__ import annotations

import os
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import layout_contracts
from diagram_model_v2 import validate_semantic_plan
from lifecycle_contracts import require_valid_contract


class LayoutRenderError(RuntimeError):
    """The renderer could not bind or safely serialize validated inputs."""


def _number(value: Any) -> str:
    numeric = float(value)
    if numeric == 0:
        return "0"
    if numeric.is_integer():
        return str(int(numeric))
    return repr(numeric)


def _style_with_hint(base: str, hint: Any, *, protected: Sequence[str] = ()) -> str:
    if not hint:
        return base
    protected_keys = {item.lower() for item in protected}
    safe: list[str] = []
    for raw in str(hint).split(";"):
        token = raw.strip()
        if not token:
            continue
        key = token.split("=", 1)[0].strip().lower()
        lowered = token.lower()
        if key in protected_keys or any(
            marker in lowered for marker in ("javascript:", "data:", "file:", "http:", "https:")
        ):
            continue
        safe.append(token)
    return base + (";".join(safe) + ";" if safe else "")


def _node_style(node: Mapping[str, Any], *, has_children: bool) -> str:
    semantic_type = str(node["semantic_type"]).strip().lower()
    if has_children or semantic_type in {"container", "group", "lane", "swimlane"}:
        base = (
            "swimlane;html=1;rounded=0;collapsible=0;container=1;"
            "recursiveResize=0;whiteSpace=wrap;fillColor=#f5f5f5;strokeColor=#666666;"
        )
    elif semantic_type in {"decision", "gateway", "condition"}:
        base = "rhombus;whiteSpace=wrap;html=1;fillColor=#fff2cc;strokeColor=#d6b656;"
    elif semantic_type in {"start", "end", "event", "terminal"}:
        base = "ellipse;whiteSpace=wrap;html=1;fillColor=#d5e8d4;strokeColor=#82b366;"
    else:
        base = "rounded=1;whiteSpace=wrap;html=1;fillColor=#dae8fc;strokeColor=#6c8ebf;"
    return _style_with_hint(base, node.get("style_hint"))


def _pin(side: str, position: Any) -> tuple[str, str]:
    rendered = _number(position)
    if side == "north":
        return rendered, "0"
    if side == "east":
        return "1", rendered
    if side == "south":
        return rendered, "1"
    if side == "west":
        return "0", rendered
    raise LayoutRenderError(f"unsupported endpoint side {side!r}")


def _canonical_points(points: Sequence[Mapping[str, Any]]) -> list[tuple[float, float]]:
    deduplicated: list[tuple[float, float]] = []
    for point in points:
        current = (float(point["x"]), float(point["y"]))
        if not deduplicated or current != deduplicated[-1]:
            deduplicated.append(current)
    if len(deduplicated) < 2:
        raise LayoutRenderError("a rendered route requires at least two distinct waypoints")

    canonical: list[tuple[float, float]] = []
    for point in deduplicated:
        canonical.append(point)
        while len(canonical) >= 3:
            first, middle, last = canonical[-3:]
            horizontal = first[1] == middle[1] == last[1]
            vertical = first[0] == middle[0] == last[0]
            middle_between = (
                min(first[0], last[0]) <= middle[0] <= max(first[0], last[0])
                and min(first[1], last[1]) <= middle[1] <= max(first[1], last[1])
            )
            if not ((horizontal or vertical) and middle_between):
                break
            canonical.pop(-2)
    for first, second in zip(canonical, canonical[1:]):
        if first[0] != second[0] and first[1] != second[1]:
            raise LayoutRenderError("layout result contains a diagonal route segment")
    return canonical


def _edge_label_geometry(
    label_bounds: Mapping[str, Any],
    points: Sequence[tuple[float, float]],
) -> tuple[float, float, tuple[float, float]]:
    """Convert absolute label bounds to draw.io relative edge geometry.

    ``mxGeometry.x`` is normalized arclength (-1 at source, +1 at target);
    ``mxGeometry.y`` is signed perpendicular distance from the selected route
    segment. The offset point carries only the residual world-space delta that
    the relative representation cannot encode (normally exactly zero).
    """
    if len(points) < 2:
        raise LayoutRenderError("edge label projection requires a non-empty route")
    center = (
        float(label_bounds["x"]) + float(label_bounds["width"]) / 2.0,
        float(label_bounds["y"]) + float(label_bounds["height"]) / 2.0,
    )
    segments: list[dict[str, float | int]] = []
    prefix = 0.0
    for index, (start, end) in enumerate(zip(points, points[1:])):
        dx, dy = end[0] - start[0], end[1] - start[1]
        length = abs(dx) + abs(dy)
        if length <= 0:
            raise LayoutRenderError("edge label projection cannot use a zero-length segment")
        if dx != 0 and dy != 0:
            raise LayoutRenderError("edge label projection requires a Manhattan route")
        length_squared = dx * dx + dy * dy
        parameter = (
            (center[0] - start[0]) * dx + (center[1] - start[1]) * dy
        ) / length_squared
        parameter = max(0.0, min(1.0, parameter))
        projected_x = start[0] + parameter * dx
        projected_y = start[1] + parameter * dy
        distance_squared = (
            (center[0] - projected_x) ** 2 + (center[1] - projected_y) ** 2
        )
        segments.append(
            {
                "index": index,
                "distance_squared": distance_squared,
                "prefix": prefix,
                "length": length,
                "parameter": parameter,
                "projected_x": projected_x,
                "projected_y": projected_y,
                "dx": dx,
                "dy": dy,
            }
        )
        prefix += length
    if prefix <= 0 or not segments:
        raise LayoutRenderError("edge label projection requires positive route length")

    # Segment index is the stable tie-break for bends, loops and crossings.
    selected = min(
        segments,
        key=lambda segment: (
            float(segment["distance_squared"]),
            int(segment["index"]),
        ),
    )
    length = float(selected["length"])
    unit_x = float(selected["dx"]) / length
    unit_y = float(selected["dy"]) / length
    normal_x, normal_y = -unit_y, unit_x
    delta_x = center[0] - float(selected["projected_x"])
    delta_y = center[1] - float(selected["projected_y"])
    perpendicular = delta_x * normal_x + delta_y * normal_y
    represented_x = float(selected["projected_x"]) + normal_x * perpendicular
    represented_y = float(selected["projected_y"]) + normal_y * perpendicular
    residual_x = center[0] - represented_x
    residual_y = center[1] - represented_y
    if abs(residual_x) < 1e-12:
        residual_x = 0.0
    if abs(residual_y) < 1e-12:
        residual_y = 0.0
    arclength = float(selected["prefix"]) + float(selected["parameter"]) * length
    relative = 2.0 * arclength / prefix - 1.0
    if relative < -1.0 - 1e-12 or relative > 1.0 + 1e-12:
        raise LayoutRenderError("edge label projection is outside the representable route")
    relative = max(-1.0, min(1.0, relative))
    return relative, perpendicular, (residual_x, residual_y)


def _unique_index(
    values: Sequence[Mapping[str, Any]], key: str, *, description: str
) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for value in values:
        identity = value.get(key)
        if not isinstance(identity, str) or not identity:
            raise LayoutRenderError(f"{description} requires a stable identity")
        if identity in result:
            raise LayoutRenderError(f"duplicate {description} identity {identity!r}")
        result[identity] = value
    return result


def _bind_pages(
    semantic_plan: Mapping[str, Any], layout_result: Mapping[str, Any]
) -> list[tuple[Mapping[str, Any], Mapping[str, Any]]]:
    semantic_pages = _unique_index(
        semantic_plan["result"]["pages"], "page_id", description="semantic page"
    )
    layout_pages = _unique_index(layout_result["pages"], "page_id", description="layout page")
    if set(semantic_pages) != set(layout_pages):
        raise LayoutRenderError("semantic and layout page identities differ")

    bound = []
    for page_id in sorted(semantic_pages):
        semantic_page, layout_page = semantic_pages[page_id], layout_pages[page_id]
        if semantic_page["name"] != layout_page["name"]:
            raise LayoutRenderError(f"page name binding differs for {page_id!r}")
        semantic_nodes = _unique_index(
            [
                {**node, "_cell_id": node["stable_identity"]["cell_id"]}
                for node in semantic_page["nodes"]
            ],
            "_cell_id",
            description=f"semantic node on page {page_id}",
        )
        layout_nodes = _unique_index(
            layout_page["nodes"], "node_id", description=f"layout node on page {page_id}"
        )
        semantic_edges = _unique_index(
            [
                {**edge, "_cell_id": edge["stable_identity"]["cell_id"]}
                for edge in semantic_page["edges"]
            ],
            "_cell_id",
            description=f"semantic edge on page {page_id}",
        )
        layout_edges = _unique_index(
            layout_page["edges"], "edge_id", description=f"layout edge on page {page_id}"
        )
        if set(semantic_nodes) != set(layout_nodes):
            raise LayoutRenderError(f"semantic and layout node identities differ on page {page_id!r}")
        if set(semantic_edges) != set(layout_edges):
            raise LayoutRenderError(f"semantic and layout edge identities differ on page {page_id!r}")
        if set(semantic_nodes) & set(semantic_edges):
            raise LayoutRenderError(f"node and edge identities overlap on page {page_id!r}")
        for edge_id, edge in semantic_edges.items():
            layout_edge = layout_edges[edge_id]
            source = edge["source"]
            target = edge["target"]
            if source["page_id"] != page_id or target["page_id"] != page_id:
                raise LayoutRenderError(f"edge {page_id}/{edge_id} crosses a page boundary")
            if source["cell_id"] not in semantic_nodes or target["cell_id"] not in semantic_nodes:
                raise LayoutRenderError(f"edge {page_id}/{edge_id} references a missing node")
            if (
                layout_edge["source"] != source["cell_id"]
                or layout_edge["target"] != target["cell_id"]
            ):
                raise LayoutRenderError(f"edge endpoint binding differs for {page_id}/{edge_id}")
        bound.append((semantic_page, layout_page))
    return bound


def _node_depth(node_id: str, semantic_nodes: Mapping[str, Mapping[str, Any]]) -> int:
    depth = 0
    current = semantic_nodes[node_id]
    seen = {node_id}
    while current.get("parent") is not None:
        parent_id = current["parent"]["cell_id"]
        if parent_id in seen:
            raise LayoutRenderError(f"semantic parent cycle includes {parent_id!r}")
        if parent_id not in semantic_nodes:
            raise LayoutRenderError(f"semantic parent {parent_id!r} is missing")
        seen.add(parent_id)
        depth += 1
        current = semantic_nodes[parent_id]
    return depth


def _render_page(
    mxfile: ET.Element,
    semantic_page: Mapping[str, Any],
    layout_page: Mapping[str, Any],
) -> None:
    page_id = semantic_page["page_id"]
    diagram = ET.SubElement(
        mxfile,
        "diagram",
        {"id": page_id, "name": semantic_page["name"], "data-schema-version": "2"},
    )
    model = ET.SubElement(
        diagram,
        "mxGraphModel",
        {
            "dx": "1200",
            "dy": "800",
            "grid": "1",
            "gridSize": "10",
            "guides": "1",
            "tooltips": "1",
            "connect": "1",
            "arrows": "1",
            "fold": "1",
            "page": "1",
            "pageScale": "1",
            "pageWidth": "1169",
            "pageHeight": "827",
            "math": "0",
            "shadow": "0",
        },
    )
    root = ET.SubElement(model, "root")
    ET.SubElement(root, "mxCell", {"id": "0"})
    ET.SubElement(root, "mxCell", {"id": "1", "parent": "0"})

    semantic_nodes = {
        node["stable_identity"]["cell_id"]: node for node in semantic_page["nodes"]
    }
    layout_nodes = {node["node_id"]: node for node in layout_page["nodes"]}
    children = {node_id: [] for node_id in semantic_nodes}
    for node_id, node in semantic_nodes.items():
        parent = node.get("parent")
        if parent is not None:
            if parent["page_id"] != page_id or parent["cell_id"] not in semantic_nodes:
                raise LayoutRenderError(f"invalid parent binding for {page_id}/{node_id}")
            children[parent["cell_id"]].append(node_id)

    for node_id in sorted(semantic_nodes, key=lambda item: (_node_depth(item, semantic_nodes), item)):
        node = semantic_nodes[node_id]
        bounds = layout_nodes[node_id]
        parent = node.get("parent")
        parent_id = parent["cell_id"] if parent is not None else "1"
        x, y = float(bounds["x"]), float(bounds["y"])
        if parent is not None:
            parent_bounds = layout_nodes[parent_id]
            x -= float(parent_bounds["x"])
            y -= float(parent_bounds["y"])
        attributes = {
            "id": node_id,
            "value": node["label"],
            "style": _node_style(node, has_children=bool(children[node_id])),
            "vertex": "1",
            "parent": parent_id,
            "data-semantic-type": node["semantic_type"],
            "data-page-id": page_id,
            "data-layout-x": _number(bounds["x"]),
            "data-layout-y": _number(bounds["y"]),
            "data-layout-width": _number(bounds["width"]),
            "data-layout-height": _number(bounds["height"]),
        }
        if node.get("style_hint") is not None:
            attributes["data-style-hint"] = node["style_hint"]
        cell = ET.SubElement(root, "mxCell", attributes)
        ET.SubElement(
            cell,
            "mxGeometry",
            {
                "x": _number(x),
                "y": _number(y),
                "width": _number(bounds["width"]),
                "height": _number(bounds["height"]),
                "as": "geometry",
            },
        )

    semantic_edges = {
        edge["stable_identity"]["cell_id"]: edge for edge in semantic_page["edges"]
    }
    layout_edges = {edge["edge_id"]: edge for edge in layout_page["edges"]}
    for edge_id in sorted(semantic_edges):
        edge, route = semantic_edges[edge_id], layout_edges[edge_id]
        exit_x, exit_y = _pin(route["source_port"], route["source_pin"])
        entry_x, entry_y = _pin(route["target_port"], route["target_pin"])
        base_style = (
            "edgeStyle=orthogonalEdgeStyle;orthogonalLoop=1;jettySize=auto;"
            "html=1;rounded=0;"
            f"exitX={exit_x};exitY={exit_y};entryX={entry_x};entryY={entry_y};"
        )
        attributes = {
            "id": edge_id,
            "value": edge["label"],
            "style": _style_with_hint(
                base_style,
                edge.get("style_hint"),
                protected=(
                    "edgeStyle",
                    "orthogonalLoop",
                    "rounded",
                    "exitX",
                    "exitY",
                    "entryX",
                    "entryY",
                ),
            ),
            "edge": "1",
            "parent": edge["parent"]["cell_id"] if edge.get("parent") is not None else "1",
            "source": route["source"],
            "target": route["target"],
            "data-semantic-type": "edge",
            "data-relationship": edge["relationship"],
            "data-page-id": page_id,
            "data-edge-class": route["edge_class"],
        }
        if route.get("route_group") is not None:
            # The XML marker is evidence only. Validator exemptions remain
            # host-owned and are never inferred from raw draw.io attributes.
            attributes["data-route-group"] = route["route_group"]
        if edge.get("style_hint") is not None:
            attributes["data-style-hint"] = edge["style_hint"]
        label_bounds = route.get("label_bounds")
        if label_bounds is not None:
            for key in ("x", "y", "width", "height"):
                attributes[f"data-label-{key}"] = _number(label_bounds[key])
        cell = ET.SubElement(root, "mxCell", attributes)
        canonical_points = _canonical_points(route["waypoints"])
        geometry_attributes = {"relative": "1", "as": "geometry"}
        label_offset = None
        if label_bounds is not None:
            relative_x, perpendicular_y, label_offset = _edge_label_geometry(
                label_bounds, canonical_points
            )
            geometry_attributes.update(
                {
                    "x": _number(relative_x),
                    "y": _number(perpendicular_y),
                    "width": _number(label_bounds["width"]),
                    "height": _number(label_bounds["height"]),
                }
            )
        geometry = ET.SubElement(cell, "mxGeometry", geometry_attributes)
        points = ET.SubElement(geometry, "Array", {"as": "points"})
        for x, y in canonical_points:
            ET.SubElement(points, "mxPoint", {"x": _number(x), "y": _number(y)})
        if label_offset is not None:
            ET.SubElement(
                geometry,
                "mxPoint",
                {
                    "as": "offset",
                    "x": _number(label_offset[0]),
                    "y": _number(label_offset[1]),
                },
            )


def _write_no_clobber(output: Path, payload: bytes) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise LayoutRenderError(f"refusing to overwrite existing output: {output}")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=output.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, output)
        except FileExistsError as exc:
            raise LayoutRenderError(f"refusing to overwrite existing output: {output}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def render_layout(
    semantic_plan: Mapping[str, Any],
    layout_result: Mapping[str, Any],
    output: Path,
) -> Path:
    """Serialize only validated, identity-bound layout geometry to draw.io."""
    try:
        require_valid_contract(semantic_plan, "semantic-plan", 2)
        semantic_diagnostics = validate_semantic_plan(semantic_plan)
        if semantic_diagnostics:
            first = semantic_diagnostics[0]
            raise LayoutRenderError(
                f"semantic plan is not executable: {first['code']}: {first['message']}"
            )
        layout_contracts.require_layout_result(layout_result)
        bound_pages = _bind_pages(semantic_plan, layout_result)
    except LayoutRenderError:
        raise
    except Exception as exc:
        raise LayoutRenderError(f"renderer input validation failed: {exc}") from exc

    mxfile = ET.Element(
        "mxfile",
        {
            "host": "GigaCode",
            "agent": "drawio-agent-extension",
            "version": "layout-result.v1",
            "data-layout-result-id": layout_result["result_id"],
            "data-layout-request-sha256": layout_result["request_sha256"],
            "data-layout-backend": layout_result["backend"],
        },
    )
    for semantic_page, layout_page in bound_pages:
        _render_page(mxfile, semantic_page, layout_page)
    ET.indent(mxfile, space="  ")
    payload = ET.tostring(mxfile, encoding="utf-8", xml_declaration=True) + b"\n"
    target = Path(output).expanduser().resolve()
    if target.suffix.lower() != ".drawio":
        raise LayoutRenderError("renderer output must use the .drawio suffix")
    _write_no_clobber(target, payload)
    return target


__all__ = ["LayoutRenderError", "render_layout"]
