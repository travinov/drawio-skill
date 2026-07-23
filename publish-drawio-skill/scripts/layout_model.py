#!/usr/bin/env python3
"""Build deterministic, immutable layout requests from host-owned semantics."""
from __future__ import annotations

import copy
import math
from collections.abc import Mapping
from typing import Any

from diagram_model_v2 import page_scoped_element_key
from lifecycle_contracts import canonical_json_sha256
import layout_contracts


GRID = 10
NODE_SIZE_BY_TYPE = {
    "decision": (140, 90),
    "start": (100, 50),
    "end": (100, 50),
    "database": (140, 80),
    "default": (160, 70),
}


def _grid_ceil(value: float) -> int:
    return int(math.ceil(value / GRID) * GRID)


def _label_size(label: Any) -> dict[str, int]:
    text = str(label or "")
    lines = text.splitlines() or [""]
    return {
        "width": _grid_ceil(max(1, max(len(line) for line in lines)) * 8 + 20),
        "height": _grid_ceil(max(1, len(lines)) * 20),
    }


def _node_size(node: Mapping[str, Any]) -> tuple[int, int]:
    semantic_type = str(node.get("semantic_type") or "default").lower()
    default_width, default_height = NODE_SIZE_BY_TYPE.get(semantic_type, NODE_SIZE_BY_TYPE["default"])
    label = _label_size(node.get("label"))
    return max(default_width, label["width"] + 20), max(default_height, label["height"] + 20)


def _semantic_result(semantic_plan: Mapping[str, Any]) -> Mapping[str, Any]:
    result = semantic_plan.get("result")
    return result if isinstance(result, Mapping) else semantic_plan


def _element_id(element: Mapping[str, Any]) -> str:
    identity = element.get("stable_identity")
    if isinstance(identity, Mapping) and isinstance(identity.get("cell_id"), str):
        return identity["cell_id"]
    value = element.get("id") or element.get("node_id") or element.get("edge_id")
    if not isinstance(value, str) or not value:
        raise ValueError("layout element requires a stable cell id")
    return value


def _reference_cell_id(reference: Mapping[str, Any]) -> str:
    value = reference.get("cell_id")
    if isinstance(value, str) and value:
        return value
    return _element_id(reference)


def _page_id(page: Mapping[str, Any]) -> str:
    value = page.get("page_id") or page.get("id")
    if not isinstance(value, str) or not value:
        raise ValueError("layout page requires a stable page id")
    return value


def _is_baseline_edge(cell: Mapping[str, Any]) -> bool:
    return cell.get("kind") == "edge" or (cell.get("source_id") is not None and cell.get("target_id") is not None)


def _baseline_indexes(baseline: Mapping[str, Any] | None) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, str]]:
    cells: dict[tuple[str, str], dict[str, Any]] = {}
    page_names: dict[str, str] = {}
    if not isinstance(baseline, Mapping):
        return cells, page_names
    for page in baseline.get("pages", []):
        if not isinstance(page, Mapping):
            continue
        page_id = _page_id(page)
        page_names[page_id] = str(page.get("name") or "")
        for cell in page.get("cells", []):
            if not isinstance(cell, Mapping):
                continue
            cell_id = cell.get("id") or cell.get("node_id") or cell.get("edge_id")
            if not isinstance(cell_id, str) or not cell_id or cell_id in {"0", "1"}:
                continue
            if cell.get("kind") in {"root", "layer", "wrapper"} or cell.get("technical") is True:
                continue
            cells[(page_id, cell_id)] = copy.deepcopy(dict(cell))
    return cells, page_names


def _baseline_hash(cell: Mapping[str, Any]) -> str:
    for key in ("element_sha256", "hash", "sha256"):
        value = cell.get(key)
        if isinstance(value, str) and len(value) == 64:
            return value
    return canonical_json_sha256(cell)


def _baseline_bounds(cell: Mapping[str, Any]) -> Mapping[str, Any] | None:
    geometry = cell.get("geometry")
    if not isinstance(geometry, Mapping):
        return None
    bounds = geometry.get("bounds")
    return bounds if isinstance(bounds, Mapping) else None


def _baseline_waypoints(cell: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    geometry = cell.get("geometry")
    if not isinstance(geometry, Mapping):
        return None
    value = geometry.get("waypoints") or geometry.get("points")
    if not isinstance(value, list):
        return None
    return [copy.deepcopy(point) for point in value if isinstance(point, Mapping)]


def _edge_class(edge: Mapping[str, Any], source: str, target: str) -> str:
    if source == target:
        return "self_loop"
    relationship = " ".join(
        str(edge.get(key) or "").lower() for key in ("relationship", "semantic_type", "label")
    )
    if any(marker in relationship for marker in ("feedback", "return", "retry", "loop")):
        return "feedback"
    if any(marker in relationship for marker in ("branch", "yes", "no", "condition")):
        return "branch"
    return "main"


def _ports(direction: str) -> tuple[str, str]:
    return ("south", "north") if direction == "TB" else ("east", "west")


def _normalize_mode(mode: str) -> str:
    aliases = {"improve": "local_reflow"}
    normalized = aliases.get(mode, mode)
    if normalized not in {"create", "preserve", "local_reflow", "full_reflow"}:
        raise ValueError(f"unsupported layout mode {mode!r}")
    return normalized


def _scope_shape(
    *, page_ids: list[str], node_ids: list[str], edge_ids: list[str], movable_nodes: list[str], reroutable_edges: list[str]
) -> dict[str, list[str]]:
    return {
        "page_ids": sorted(set(page_ids)),
        "node_ids": sorted(set(node_ids)),
        "edge_ids": sorted(set(edge_ids)),
        "movable_nodes": sorted(set(movable_nodes)),
        "reroutable_edges": sorted(set(reroutable_edges)),
    }


def _model_pages(semantic_plan: Mapping[str, Any], baseline: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    result = _semantic_result(semantic_plan)
    baseline_cells, baseline_page_names = _baseline_indexes(baseline)
    pages: dict[str, dict[str, Any]] = {}
    for raw_page in result.get("pages", []):
        if not isinstance(raw_page, Mapping):
            continue
        page_id = _page_id(raw_page)
        entry = pages.setdefault(page_id, {"page_id": page_id, "name": str(raw_page.get("name") or ""), "nodes": {}, "edges": {}})
        for kind, output in (("nodes", "nodes"), ("edges", "edges")):
            for element in raw_page.get(kind, []):
                if isinstance(element, Mapping):
                    entry[output][_element_id(element)] = copy.deepcopy(dict(element))
    for (page_id, cell_id), cell in baseline_cells.items():
        entry = pages.setdefault(page_id, {"page_id": page_id, "name": baseline_page_names.get(page_id, ""), "nodes": {}, "edges": {}})
        target = "edges" if _is_baseline_edge(cell) else "nodes"
        entry[target].setdefault(cell_id, copy.deepcopy(cell))
    return [pages[page_id] for page_id in sorted(pages)]


def _scope_for_request(
    pages: list[dict[str, Any]], mode: str, scope: Mapping[str, Any] | None
) -> dict[str, list[str]]:
    all_pages = [page["page_id"] for page in pages]
    all_nodes = [node_id for page in pages for node_id in page["nodes"]]
    all_edges = [edge_id for page in pages for edge_id in page["edges"]]
    requested = scope if isinstance(scope, Mapping) else {}
    if mode in {"create", "full_reflow"}:
        return _scope_shape(page_ids=all_pages, node_ids=all_nodes, edge_ids=all_edges, movable_nodes=all_nodes, reroutable_edges=all_edges)
    if mode == "preserve":
        return _scope_shape(page_ids=all_pages, node_ids=all_nodes, edge_ids=all_edges, movable_nodes=[], reroutable_edges=[])
    edge_ids = [value for value in requested.get("edge_ids", []) if isinstance(value, str)]
    if not edge_ids:
        raise ValueError("local_reflow requires an explicit edge-only scope")
    page_ids = [value for value in requested.get("page_ids", []) if isinstance(value, str)]
    node_ids = [value for value in requested.get("node_ids", []) if isinstance(value, str)]
    movable = [value for value in requested.get("movable_nodes", []) if isinstance(value, str)]
    reroutable = [value for value in requested.get("reroutable_edges", edge_ids) if isinstance(value, str)]
    return _scope_shape(page_ids=page_ids, node_ids=node_ids, edge_ids=edge_ids, movable_nodes=movable, reroutable_edges=reroutable)


def build_layout_request(
    semantic_plan: Mapping[str, Any], *, run_id: str, semantic_plan_sha256: str,
    mode: str, backend: str, strategy_id: str, quality_profile_version: int,
    baseline: Mapping[str, Any] | None = None, scope: Mapping[str, Any] | None = None,
) -> dict:
    """Create the sole immutable host input accepted by layout backends."""
    if quality_profile_version not in {1, 2} or isinstance(quality_profile_version, bool):
        raise ValueError("quality_profile_version must be 1 or 2")
    result = _semantic_result(semantic_plan)
    normalized_mode = _normalize_mode(mode)
    direction = str(result.get("direction") or "LR")
    if direction not in {"TB", "LR"}:
        raise ValueError(f"unsupported layout direction {direction!r}")
    diagram_type = str(result.get("diagram_type") or "generic")
    pages = _model_pages(semantic_plan, baseline)
    request_scope = _scope_for_request(pages, normalized_mode, scope)
    baseline_cells, _ = _baseline_indexes(baseline)
    source_port, target_port = _ports(direction)
    output_pages: list[dict[str, Any]] = []
    for page in pages:
        page_id = page["page_id"]
        output_nodes = []
        for node_id, node in sorted(page["nodes"].items(), key=lambda item: page_scoped_element_key(page_id, item[0])):
            baseline_cell = baseline_cells.get((page_id, node_id))
            bounds = _baseline_bounds(baseline_cell) if baseline_cell else None
            width, height = _node_size(node)
            if bounds is not None:
                width = _grid_ceil(float(bounds.get("width", width)))
                height = _grid_ceil(float(bounds.get("height", height)))
            locked = normalized_mode in {"preserve", "local_reflow"} and node_id not in request_scope["movable_nodes"]
            value = {
                "node_id": node_id,
                "x": _grid_ceil(float(bounds.get("x", 0))) if bounds is not None else 0,
                "y": _grid_ceil(float(bounds.get("y", 0))) if bounds is not None else 0,
                "width": width,
                "height": height,
                "locked": locked,
            }
            if baseline_cell is not None:
                value["element_sha256"] = _baseline_hash(baseline_cell)
            output_nodes.append(value)
        output_edges = []
        for edge_id, edge in sorted(page["edges"].items(), key=lambda item: page_scoped_element_key(page_id, item[0])):
            source = _reference_cell_id(edge.get("source", {})) if isinstance(edge.get("source"), Mapping) else str(edge.get("source_id") or "")
            target = _reference_cell_id(edge.get("target", {})) if isinstance(edge.get("target"), Mapping) else str(edge.get("target_id") or "")
            if not source or not target:
                raise ValueError(f"edge {page_id}/{edge_id} requires source and target")
            baseline_cell = baseline_cells.get((page_id, edge_id))
            semantic_route = edge.get("route") if isinstance(edge.get("route"), Mapping) else None
            route = copy.deepcopy(semantic_route.get("waypoints")) if semantic_route else _baseline_waypoints(baseline_cell) if baseline_cell else None
            locked = bool(semantic_route) or (normalized_mode in {"preserve", "local_reflow"} and edge_id not in request_scope["reroutable_edges"])
            if locked and (not isinstance(route, list) or len(route) < 2):
                raise ValueError(f"locked edge {page_id}/{edge_id} requires an explicit baseline route")
            value = {
                "edge_id": edge_id,
                "source": source,
                "target": target,
                "edge_class": _edge_class(edge, source, target),
                "source_port": source_port,
                "target_port": target_port,
                "locked": locked,
                "label_size": _label_size(edge.get("label")),
            }
            if route is not None:
                value["waypoints"] = route
            if baseline_cell is not None:
                value["element_sha256"] = _baseline_hash(baseline_cell)
            output_edges.append(value)
        output_pages.append({"page_id": page_id, "name": page["name"], "nodes": output_nodes, "edges": output_edges})
    request_seed = {
        "run_id": run_id, "semantic_plan_sha256": semantic_plan_sha256, "diagram_type": diagram_type,
        "direction": direction, "mode": normalized_mode, "backend": backend, "strategy": strategy_id,
        "quality_profile_version": quality_profile_version, "pages": output_pages, "scope": request_scope,
    }
    request = {
        "schema_version": 1,
        "request_id": "layout-" + canonical_json_sha256(request_seed)[:16],
        **request_seed,
        "constraints": {"grid_size": GRID, "node_separation": 40, "layer_separation": 80},
    }
    layout_contracts.require_layout_request(request)
    return request


def _edge_records(diagram_spec: Mapping[str, Any]) -> list[tuple[str, str, str, str]]:
    records = []
    for page in diagram_spec.get("pages", []):
        if not isinstance(page, Mapping):
            continue
        page_id = _page_id(page)
        for cell in page.get("cells", []):
            if not isinstance(cell, Mapping) or not _is_baseline_edge(cell):
                continue
            edge_id = str(cell.get("id") or cell.get("edge_id") or "")
            source_ref = cell.get("source")
            target_ref = cell.get("target")
            source = _reference_cell_id(source_ref) if isinstance(source_ref, Mapping) else str(cell.get("source_id") or source_ref or "")
            target = _reference_cell_id(target_ref) if isinstance(target_ref, Mapping) else str(cell.get("target_id") or target_ref or "")
            if edge_id and source and target:
                records.append((page_id, edge_id, source, target))
    return sorted(records)


def _node_ids_by_page(diagram_spec: Mapping[str, Any]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for page in diagram_spec.get("pages", []):
        if not isinstance(page, Mapping):
            continue
        page_id = _page_id(page)
        result[page_id] = {
            str(cell.get("id") or cell.get("node_id"))
            for cell in page.get("cells", [])
            if isinstance(cell, Mapping) and not _is_baseline_edge(cell) and str(cell.get("id") or cell.get("node_id") or "") not in {"", "0", "1"}
        }
    return result


def infer_scope_from_findings(diagram_spec: Mapping[str, Any], findings: list[Mapping[str, Any]]) -> dict:
    """Start local repair with only explicitly found edges reroutable."""
    known = {edge_id for _, edge_id, _, _ in _edge_records(diagram_spec)}
    edge_ids = sorted({
        str(finding.get("edge_id") or finding.get("element_id") or finding.get("cell_id") or "")
        for finding in findings if isinstance(finding, Mapping)
    } - {""})
    unknown = sorted(set(edge_ids) - known)
    if unknown:
        raise ValueError(f"findings reference unknown edges: {', '.join(unknown)}")
    return _scope_shape(page_ids=[], node_ids=[], edge_ids=edge_ids, movable_nodes=[], reroutable_edges=edge_ids)


def expand_scope(diagram_spec: Mapping[str, Any], scope: Mapping[str, Any], level: str) -> dict:
    """Expand a repair scope one safe, explicit level at a time."""
    current = _scope_shape(
        page_ids=list(scope.get("page_ids", [])), node_ids=list(scope.get("node_ids", [])),
        edge_ids=list(scope.get("edge_ids", [])), movable_nodes=list(scope.get("movable_nodes", [])),
        reroutable_edges=list(scope.get("reroutable_edges", [])),
    )
    records = _edge_records(diagram_spec)
    nodes_by_page = _node_ids_by_page(diagram_spec)
    if level == "adjacent_nodes":
        if current["movable_nodes"] or not current["edge_ids"]:
            raise ValueError("adjacent_nodes expansion requires edge-only scope")
        selected = [record for record in records if record[1] in set(current["edge_ids"])]
        nodes = sorted({node for _, _, source, target in selected for node in (source, target)})
        return _scope_shape(page_ids=[], node_ids=nodes, edge_ids=current["edge_ids"], movable_nodes=nodes, reroutable_edges=current["reroutable_edges"])
    if level == "layer":
        if not current["movable_nodes"] or current["page_ids"]:
            raise ValueError("layer expansion requires the adjacent-nodes scope")
        pages = sorted({page_id for page_id, _, source, target in records if source in current["movable_nodes"] or target in current["movable_nodes"]})
        nodes = sorted({node for page_id in pages for node in nodes_by_page.get(page_id, set())})
        return _scope_shape(page_ids=pages, node_ids=nodes, edge_ids=current["edge_ids"], movable_nodes=nodes, reroutable_edges=current["reroutable_edges"])
    if level == "component":
        if not current["page_ids"]:
            raise ValueError("component expansion requires the layer scope")
        component_nodes = set(current["movable_nodes"])
        component_edges = set(current["edge_ids"])
        changed = True
        while changed:
            changed = False
            for page_id, edge_id, source, target in records:
                if page_id not in current["page_ids"]:
                    continue
                if source in component_nodes or target in component_nodes:
                    if edge_id not in component_edges:
                        component_edges.add(edge_id)
                        changed = True
                    for node in (source, target):
                        if node not in component_nodes:
                            component_nodes.add(node)
                            changed = True
        return _scope_shape(page_ids=current["page_ids"], node_ids=sorted(component_nodes), edge_ids=sorted(component_edges), movable_nodes=sorted(component_nodes), reroutable_edges=sorted(component_edges))
    raise ValueError(f"unsupported scope expansion {level!r}")
