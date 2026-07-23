#!/usr/bin/env python3
"""Host-owned validation for strict deterministic layout contracts."""
from __future__ import annotations

import math
from typing import Any

import lifecycle_contracts


def _diagnostic(code: str, pointer: str, message: str) -> dict[str, str]:
    return {"code": code, "pointer": pointer, "message": message}


def _layout_diagnostics(value: Any, *, request: bool) -> list[dict[str, str]]:
    diagnostics: list[dict[str, str]] = []
    if not isinstance(value, dict):
        return diagnostics

    pages = value.get("pages")
    if not isinstance(pages, list):
        return diagnostics

    locked_nodes: set[str] = set()
    unlocked_nodes: set[str] = set()
    for page_index, page in enumerate(pages):
        if not isinstance(page, dict):
            continue
        nodes = page.get("nodes")
        if isinstance(nodes, list):
            for node in nodes:
                if isinstance(node, dict) and isinstance(node.get("node_id"), str):
                    if node.get("locked") is True:
                        locked_nodes.add(node["node_id"])
                    elif node.get("locked") is False:
                        unlocked_nodes.add(node["node_id"])
        edges = page.get("edges")
        if not isinstance(edges, list):
            continue
        for edge_index, edge in enumerate(edges):
            if not isinstance(edge, dict) or not isinstance(edge.get("waypoints"), list):
                continue
            points = edge["waypoints"]
            previous: tuple[float, float] | None = None
            for point_index, point in enumerate(points):
                if not isinstance(point, dict):
                    continue
                x, y = point.get("x"), point.get("y")
                if not _finite_number(x) or not _finite_number(y):
                    diagnostics.append(_diagnostic(
                        "layout.route.point_non_finite",
                        f"/pages/{page_index}/edges/{edge_index}/waypoints/{point_index}",
                        "route waypoint coordinates must be finite",
                    ))
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
        node_ids = _string_set(scope.get("node_ids"))
        edge_ids = _string_set(scope.get("edge_ids"))
        movable = _string_set(scope.get("movable_nodes"))
        reroutable = _string_set(scope.get("reroutable_edges"))
        for node_id in sorted(movable - node_ids):
            diagnostics.append(_diagnostic("layout.scope.movable_node_outside", "/scope/movable_nodes", f"movable node {node_id!r} is outside declared node scope"))
        for edge_id in sorted(reroutable - edge_ids):
            diagnostics.append(_diagnostic("layout.scope.reroutable_edge_outside", "/scope/reroutable_edges", f"reroutable edge {edge_id!r} is outside declared edge scope"))
        for node_id in sorted(locked_nodes & movable):
            diagnostics.append(_diagnostic("layout.scope.locked_movable_overlap", "/scope/movable_nodes", f"locked node {node_id!r} cannot be movable"))
        for node_id in sorted(unlocked_nodes - movable):
            diagnostics.append(_diagnostic("layout.scope.unlocked_node_not_movable", "/scope/movable_nodes", f"unlocked local-reflow node {node_id!r} must be movable"))
    return diagnostics


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _string_set(value: Any) -> set[str]:
    return {item for item in value if isinstance(item, str)} if isinstance(value, list) else set()


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


def require_layout_result(value: Any, *, expected_request_sha256: str | None = None) -> None:
    _require(validate_layout_result(value, expected_request_sha256=expected_request_sha256))
