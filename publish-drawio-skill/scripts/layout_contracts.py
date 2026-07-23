#!/usr/bin/env python3
"""Host-owned validation for strict deterministic intake and layout contracts."""
from __future__ import annotations

import math
import numbers
from typing import Any

import lifecycle_contracts


def _diagnostic(code: str, pointer: str, message: str) -> dict[str, str]:
    return {"code": code, "pointer": pointer, "message": message}


def _layout_diagnostics(value: Any, *, request: bool) -> list[dict[str, str]]:
    diagnostics = _non_finite_number_diagnostics(value)
    if not isinstance(value, dict):
        return diagnostics

    pages = value.get("pages")
    if not isinstance(pages, list):
        return diagnostics

    locked_nodes: set[tuple[str, str]] = set()
    unlocked_nodes: set[tuple[str, str]] = set()
    locked_edges: set[tuple[str, str]] = set()
    unlocked_edges: set[tuple[str, str]] = set()
    known_nodes: set[tuple[str, str]] = set()
    known_edges: set[tuple[str, str]] = set()
    for page_index, page in enumerate(pages):
        if not isinstance(page, dict):
            continue
        page_id = page.get("page_id")
        if not isinstance(page_id, str):
            continue
        nodes = page.get("nodes")
        if isinstance(nodes, list):
            for node in nodes:
                if isinstance(node, dict) and isinstance(node.get("node_id"), str):
                    ref = (page_id, node["node_id"])
                    known_nodes.add(ref)
                    if node.get("locked") is True:
                        locked_nodes.add(ref)
                    elif node.get("locked") is False:
                        unlocked_nodes.add(ref)
        edges = page.get("edges")
        if not isinstance(edges, list):
            continue
        for edge_index, edge in enumerate(edges):
            if not isinstance(edge, dict):
                continue
            edge_id = edge.get("edge_id")
            if isinstance(edge_id, str):
                ref = (page_id, edge_id)
                known_edges.add(ref)
                if edge.get("locked") is True:
                    locked_edges.add(ref)
                elif edge.get("locked") is False:
                    unlocked_edges.add(ref)
            if not isinstance(edge.get("waypoints"), list):
                continue
            points = edge["waypoints"]
            previous: tuple[float, float] | None = None
            for point_index, point in enumerate(points):
                if not isinstance(point, dict):
                    continue
                x, y = point.get("x"), point.get("y")
                if not _finite_number(x) or not _finite_number(y):
                    previous = None
                    continue
                current = (float(x), float(y))
                if previous is not None and current[0] != previous[0] and current[1] != previous[1]:
                    diagnostics.append(_diagnostic(
                        "layout.route.diagonal_segment",
                        f"/pages/{page_index}/edges/{edge_index}/waypoints/{point_index}",
                        "consecutive route waypoints must share x or y",
                    ))
                previous = current

    if request and value.get("mode") == "local_reflow":
        scope = value.get("scope")
        if not isinstance(scope, dict):
            return diagnostics
        node_refs = _ref_set(scope.get("node_refs"))
        edge_refs = _ref_set(scope.get("edge_refs"))
        movable = _ref_set(scope.get("movable_node_refs"))
        reroutable = _ref_set(scope.get("reroutable_edge_refs"))
        for node_ref in sorted(movable - node_refs):
            diagnostics.append(_diagnostic("layout.scope.movable_node_outside", "/scope/movable_node_refs", f"movable node {node_ref!r} is outside declared node scope"))
        for edge_ref in sorted(reroutable - edge_refs):
            diagnostics.append(_diagnostic("layout.scope.reroutable_edge_outside", "/scope/reroutable_edge_refs", f"reroutable edge {edge_ref!r} is outside declared edge scope"))
        for node_ref in sorted(node_refs - known_nodes):
            diagnostics.append(_diagnostic("layout.scope.node_missing", "/scope/node_refs", f"scoped node {node_ref!r} does not exist"))
        for edge_ref in sorted(edge_refs - known_edges):
            diagnostics.append(_diagnostic("layout.scope.edge_missing", "/scope/edge_refs", f"scoped edge {edge_ref!r} does not exist"))
        for node_ref in sorted(locked_nodes & movable):
            diagnostics.append(_diagnostic("layout.scope.locked_movable_overlap", "/scope/movable_node_refs", f"locked node {node_ref!r} cannot be movable"))
        for node_ref in sorted(unlocked_nodes - movable):
            diagnostics.append(_diagnostic("layout.scope.unlocked_node_not_movable", "/scope/movable_node_refs", f"unlocked local-reflow node {node_ref!r} must be movable"))
        for edge_ref in sorted(unlocked_edges - reroutable):
            diagnostics.append(_diagnostic("layout.scope.unlocked_edge_not_reroutable", "/scope/reroutable_edge_refs", f"unlocked local-reflow edge {edge_ref!r} must be reroutable"))
    return diagnostics


def _finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, numbers.Number):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, TypeError, ValueError):
        return False


def _non_finite_number_diagnostics(value: Any, path: tuple[Any, ...] = ()) -> list[dict[str, str]]:
    """Report non-finite numbers at every contract-owned value location."""
    if isinstance(value, dict):
        diagnostics: list[dict[str, str]] = []
        for key in sorted(value, key=lambda item: (type(item).__name__, repr(item))):
            diagnostics.extend(_non_finite_number_diagnostics(value[key], path + (key,)))
        return diagnostics
    if isinstance(value, list):
        diagnostics = []
        for index, item in enumerate(value):
            diagnostics.extend(_non_finite_number_diagnostics(item, path + (index,)))
        return diagnostics
    if isinstance(value, numbers.Number) and not isinstance(value, bool) and not _finite_number(value):
        return [_diagnostic(
            "layout.number.non_finite",
            lifecycle_contracts.json_pointer(path),
            "numeric values must be finite",
        )]
    return []


def _ref_set(value: Any) -> set[tuple[str, str]]:
    if not isinstance(value, list):
        return set()
    return {
        (item["page_id"], item["cell_id"])
        for item in value
        if isinstance(item, dict)
        and isinstance(item.get("page_id"), str)
        and isinstance(item.get("cell_id"), str)
    }


def validate_diagram_intake(value: Any) -> list[dict[str, str]]:
    """Validate diagram intake shape and finite contract numbers."""
    return lifecycle_contracts.validate_contract(value, "diagram-intake", 1) + _non_finite_number_diagnostics(value)


def validate_diagram_intake_analysis(value: Any) -> list[dict[str, str]]:
    """Validate diagram intake analysis shape and finite contract numbers."""
    return lifecycle_contracts.validate_contract(value, "diagram-intake-analysis", 1) + _non_finite_number_diagnostics(value)


def validate_layout_request(value: Any) -> list[dict[str, str]]:
    """Validate the schema and host-only request invariants."""
    return lifecycle_contracts.validate_contract(value, "layout-request", 1) + _layout_diagnostics(value, request=True)


def validate_layout_result(
    value: Any, *, expected_request_sha256: str | None = None
) -> list[dict[str, str]]:
    """Validate result shape, routes, and optional immutable request binding."""
    diagnostics = lifecycle_contracts.validate_contract(value, "layout-result", 1)
    diagnostics.extend(_layout_diagnostics(value, request=False))
    if expected_request_sha256 is not None and isinstance(value, dict) and value.get("request_sha256") != expected_request_sha256:
        diagnostics.append(_diagnostic(
            "layout.result.request_sha256_mismatch", "/request_sha256",
            "result request_sha256 does not match the immutable request digest",
        ))
    return diagnostics


def _require(diagnostics: list[dict[str, str]]) -> None:
    if diagnostics:
        first = diagnostics[0]
        error = lifecycle_contracts.ContractError(first["code"], first["message"], pointer=first["pointer"])
        error.diagnostics = diagnostics
        raise error


def require_layout_request(value: Any) -> None:
    _require(validate_layout_request(value))


def require_diagram_intake(value: Any) -> None:
    _require(validate_diagram_intake(value))


def require_diagram_intake_analysis(value: Any) -> None:
    _require(validate_diagram_intake_analysis(value))


def require_layout_result(value: Any, *, expected_request_sha256: str | None = None) -> None:
    _require(validate_layout_result(value, expected_request_sha256=expected_request_sha256))
