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


Ref = tuple[str, str]


def _normalize_ref(value: Any) -> Ref:
    if not isinstance(value, Mapping):
        raise ValueError("layout scope references must include page_id and cell_id")
    page_id, cell_id = value.get("page_id"), value.get("cell_id")
    if not isinstance(page_id, str) or not page_id or not isinstance(cell_id, str) or not cell_id:
        raise ValueError("layout scope references must include non-empty page_id and cell_id")
    return page_id, cell_id


def _normalize_refs(value: Any) -> set[Ref]:
    if value is None:
        return set()
    if not isinstance(value, list):
        raise ValueError("layout scope references must be arrays")
    return {_normalize_ref(item) for item in value}


def _ref_value(value: Ref) -> dict[str, str]:
    return {"page_id": value[0], "cell_id": value[1]}


def _scope_shape(
    *, node_refs: set[Ref] | list[Ref] = (), edge_refs: set[Ref] | list[Ref] = (),
    movable_node_refs: set[Ref] | list[Ref] = (), reroutable_edge_refs: set[Ref] | list[Ref] = (),
    page_ids: set[str] | list[str] = (),
) -> dict[str, list[Any]]:
    nodes = set(node_refs)
    edges = set(edge_refs)
    movable = set(movable_node_refs)
    reroutable = set(reroutable_edge_refs)
    pages = set(page_ids) | {page_id for page_id, _ in nodes | edges | movable | reroutable}
    return {
        "page_ids": sorted(pages),
        "node_refs": [_ref_value(ref) for ref in sorted(nodes)],
        "edge_refs": [_ref_value(ref) for ref in sorted(edges)],
        "movable_node_refs": [_ref_value(ref) for ref in sorted(movable)],
        "reroutable_edge_refs": [_ref_value(ref) for ref in sorted(reroutable)],
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
) -> dict[str, list[Any]]:
    all_pages = {page["page_id"] for page in pages}
    all_nodes = {(page["page_id"], node_id) for page in pages for node_id in page["nodes"]}
    all_edges = {(page["page_id"], edge_id) for page in pages for edge_id in page["edges"]}
    requested = scope if isinstance(scope, Mapping) else {}
    if mode in {"create", "full_reflow"}:
        return _scope_shape(page_ids=all_pages, node_refs=all_nodes, edge_refs=all_edges, movable_node_refs=all_nodes, reroutable_edge_refs=all_edges)
    if mode == "preserve":
        return _scope_shape(page_ids=all_pages, node_refs=all_nodes, edge_refs=all_edges)
    edge_refs = _normalize_refs(requested.get("edge_refs"))
    if not edge_refs:
        raise ValueError("local_reflow requires an explicit edge-only scope")
    if edge_refs - all_edges:
        raise ValueError(f"local_reflow references unknown edges: {sorted(edge_refs - all_edges)!r}")
    node_refs = _normalize_refs(requested.get("node_refs"))
    movable = _normalize_refs(requested.get("movable_node_refs"))
    reroutable = _normalize_refs(requested.get("reroutable_edge_refs")) or edge_refs
    if node_refs - all_nodes or movable - all_nodes or reroutable - all_edges:
        raise ValueError("local_reflow scope references an unknown page-scoped element")
    return _scope_shape(node_refs=node_refs, edge_refs=edge_refs, movable_node_refs=movable, reroutable_edge_refs=reroutable)


def build_layout_request(
    semantic_plan: Mapping[str, Any], *, run_id: str, semantic_plan_sha256: str,
    mode: str, backend: str, strategy_id: str, quality_profile_version: int,
    baseline: Mapping[str, Any] | None = None, scope: Mapping[str, Any] | None = None,
    strategy_options: Mapping[str, Any] | None = None,
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
            locked = normalized_mode in {"preserve", "local_reflow"} and (page_id, node_id) not in _normalize_refs(request_scope["movable_node_refs"])
            value = {
                "node_id": node_id,
                "x": _grid_ceil(float(bounds.get("x", 0))) if bounds is not None else 0,
                "y": _grid_ceil(float(bounds.get("y", 0))) if bounds is not None else 0,
                "width": width,
                "height": height,
                "locked": locked,
            }
            parent = node.get("parent")
            if isinstance(parent, Mapping):
                parent_page = parent.get("page_id")
                parent_id = parent.get("cell_id")
                if parent_page != page_id:
                    raise ValueError(
                        f"layout parent for {page_id}/{node_id} must be on the same page"
                    )
                if not isinstance(parent_id, str) or not parent_id:
                    raise ValueError(f"layout parent for {page_id}/{node_id} requires a cell id")
                value["parent_id"] = parent_id
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
            locked = bool(semantic_route) or (normalized_mode in {"preserve", "local_reflow"} and (page_id, edge_id) not in _normalize_refs(request_scope["reroutable_edge_refs"]))
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
    normalized_strategy_options = copy.deepcopy(dict(strategy_options or {}))
    spacing = float(normalized_strategy_options.get("spacing", 1.0))
    port_separation = float(
        normalized_strategy_options.get("port_separation", 1.0)
    )
    request_seed = {
        "run_id": run_id, "semantic_plan_sha256": semantic_plan_sha256, "diagram_type": diagram_type,
        "direction": direction, "mode": normalized_mode, "backend": backend, "strategy": strategy_id,
        "quality_profile_version": quality_profile_version, "pages": output_pages, "scope": request_scope,
    }
    if strategy_options is not None:
        request_seed["strategy_options"] = normalized_strategy_options
    request = {
        "schema_version": 1,
        "request_id": "layout-" + canonical_json_sha256(request_seed)[:16],
        **request_seed,
        "constraints": {
            "grid_size": GRID,
            "node_separation": round(40 * spacing * port_separation, 6),
            "layer_separation": round(80 * spacing, 6),
        },
    }
    layout_contracts.require_layout_request(request)
    return request


def _edge_records(diagram_spec: Mapping[str, Any]) -> list[tuple[Ref, Ref, Ref]]:
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
                records.append(((page_id, edge_id), (page_id, source), (page_id, target)))
    return sorted(records)


def infer_scope_from_findings(diagram_spec: Mapping[str, Any], findings: list[Mapping[str, Any]]) -> dict:
    """Start local repair with only explicitly found edges reroutable."""
    records = _edge_records(diagram_spec)
    known = {edge_ref for edge_ref, _, _ in records}
    by_cell_id: dict[str, list[Ref]] = {}
    for edge_ref in known:
        by_cell_id.setdefault(edge_ref[1], []).append(edge_ref)
    selected: set[Ref] = set()
    for finding in findings:
        if not isinstance(finding, Mapping):
            continue
        explicit = finding.get("edge_ref")
        if explicit is None and isinstance(finding.get("page_id"), str):
            explicit = {"page_id": finding["page_id"], "cell_id": finding.get("edge_id") or finding.get("cell_id")}
        if isinstance(explicit, Mapping):
            edge_ref = _normalize_ref(explicit)
            if edge_ref not in known:
                raise ValueError(f"finding references unknown edge {edge_ref!r}")
            selected.add(edge_ref)
            continue
        edge_id = finding.get("edge_id") or finding.get("element_id") or finding.get("cell_id")
        if not isinstance(edge_id, str) or not edge_id:
            raise ValueError("finding must identify an edge")
        matches = by_cell_id.get(edge_id, [])
        if len(matches) != 1:
            raise ValueError(f"finding edge {edge_id!r} is ambiguous or unknown; provide page_id")
        selected.add(matches[0])
    return _scope_shape(edge_refs=selected, reroutable_edge_refs=selected)


def expand_scope(diagram_spec: Mapping[str, Any], scope: Mapping[str, Any], level: str) -> dict:
    """Expand a repair scope one safe, explicit level at a time."""
    current = _scope_shape(
        page_ids={value for value in scope.get("page_ids", []) if isinstance(value, str)},
        node_refs=_normalize_refs(scope.get("node_refs")), edge_refs=_normalize_refs(scope.get("edge_refs")),
        movable_node_refs=_normalize_refs(scope.get("movable_node_refs")),
        reroutable_edge_refs=_normalize_refs(scope.get("reroutable_edge_refs")),
    )
    records = _edge_records(diagram_spec)
    current_edges = _normalize_refs(current["edge_refs"])
    current_movable = _normalize_refs(current["movable_node_refs"])
    current_reroutable = _normalize_refs(current["reroutable_edge_refs"])
    if level == "adjacent_nodes":
        if current_movable or not current_edges:
            raise ValueError("adjacent_nodes expansion requires edge-only scope")
        selected = [record for record in records if record[0] in current_edges]
        nodes = {node for _, source, target in selected for node in (source, target)}
        return _scope_shape(node_refs=nodes, edge_refs=current_edges, movable_node_refs=nodes, reroutable_edge_refs=current_reroutable)
    if level == "layer":
        if not current_movable or not current_edges:
            raise ValueError("layer expansion requires the adjacent-nodes scope")
        one_hop = [record for record in records if record[1] in current_movable or record[2] in current_movable]
        nodes = current_movable | {node for _, source, target in one_hop for node in (source, target)}
        edges = current_edges | {edge_ref for edge_ref, _, _ in one_hop}
        return _scope_shape(node_refs=nodes, edge_refs=edges, movable_node_refs=nodes, reroutable_edge_refs=edges)
    if level == "component":
        if not current["page_ids"] or not current_movable:
            raise ValueError("component expansion requires the layer scope")
        allowed_pages = set(current["page_ids"])
        component_nodes = set(current_movable)
        component_edges = set(current_edges)
        changed = True
        while changed:
            changed = False
            for edge_ref, source, target in records:
                if edge_ref[0] not in allowed_pages:
                    continue
                if source in component_nodes or target in component_nodes:
                    if edge_ref not in component_edges:
                        component_edges.add(edge_ref)
                        changed = True
                    for node in (source, target):
                        if node not in component_nodes:
                            component_nodes.add(node)
                            changed = True
        return _scope_shape(page_ids=allowed_pages, node_refs=component_nodes, edge_refs=component_edges, movable_node_refs=component_nodes, reroutable_edge_refs=component_edges)
    raise ValueError(f"unsupported scope expansion {level!r}")
