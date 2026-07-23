#!/usr/bin/env python3
"""Deterministic, host-owned layered placement without edge routing.

The module deliberately stops after placement.  Ports, waypoints, labels and a
contract-valid ``layout-result`` are introduced by the routing task that
follows; keeping them out here prevents a direct line from being mistaken for a
safe orthogonal route.
"""
from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from statistics import median
from typing import Any


def _node_id(node: Mapping[str, Any]) -> str:
    value = node.get("node_id")
    if not isinstance(value, str) or not value:
        raise ValueError("layout node requires a non-empty node_id")
    return value


def _edge_id(edge: Mapping[str, Any]) -> str:
    value = edge.get("edge_id")
    if not isinstance(value, str) or not value:
        raise ValueError("layout edge requires a non-empty edge_id")
    return value


def _ordered_nodes(nodes: Sequence[Mapping[str, Any]]) -> list[str]:
    return sorted({_node_id(node) for node in nodes})


def _ordered_edges(edges: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return sorted(edges, key=lambda edge: (_edge_id(edge), str(edge.get("source", "")), str(edge.get("target", ""))))


def strongly_connected_components(
    nodes: Sequence[Mapping[str, Any]], edges: Sequence[Mapping[str, Any]]
) -> list[Sequence[str]]:
    """Return Tarjan strongly connected components in stable cell-id order."""
    node_ids = _ordered_nodes(nodes)
    known = set(node_ids)
    adjacency: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    for edge in _ordered_edges(edges):
        source, target = edge.get("source"), edge.get("target")
        if isinstance(source, str) and isinstance(target, str) and source in known and target in known:
            adjacency[source].append(target)
    for source in adjacency:
        adjacency[source].sort()

    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indexes: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    components: list[tuple[str, ...]] = []

    def visit(node_id: str) -> None:
        nonlocal index
        indexes[node_id] = index
        lowlinks[node_id] = index
        index += 1
        stack.append(node_id)
        on_stack.add(node_id)
        for target in adjacency[node_id]:
            if target not in indexes:
                visit(target)
                lowlinks[node_id] = min(lowlinks[node_id], lowlinks[target])
            elif target in on_stack:
                lowlinks[node_id] = min(lowlinks[node_id], indexes[target])
        if lowlinks[node_id] == indexes[node_id]:
            component: list[str] = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == node_id:
                    break
            components.append(tuple(sorted(component)))

    for node_id in node_ids:
        if node_id not in indexes:
            visit(node_id)
    return sorted(components, key=lambda component: component[0])


def choose_feedback_edges(
    nodes: Sequence[Mapping[str, Any]], edges: Sequence[Mapping[str, Any]]
) -> set[str]:
    """Choose the DFS back-edges that make the layered graph acyclic.

    DFS roots, incident edges and each tie are sorted by stable cell id / edge
    id.  A directed DFS back-edge is sufficient to break every encountered
    cycle and is deterministic across Python versions.
    """
    node_ids = _ordered_nodes(nodes)
    known = set(node_ids)
    outgoing: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for edge in _ordered_edges(edges):
        source, target = edge.get("source"), edge.get("target")
        if isinstance(source, str) and isinstance(target, str) and source in known and target in known:
            outgoing[source].append(edge)
    for source in outgoing:
        outgoing[source].sort(key=lambda edge: (str(edge["target"]), _edge_id(edge)))

    state: dict[str, int] = {node_id: 0 for node_id in node_ids}
    feedback: set[str] = set()

    def visit(node_id: str) -> None:
        state[node_id] = 1
        for edge in outgoing[node_id]:
            target = str(edge["target"])
            if state[target] == 0:
                visit(target)
            elif state[target] == 1:
                feedback.add(_edge_id(edge))
        state[node_id] = 2

    for node_id in node_ids:
        if state[node_id] == 0:
            visit(node_id)
    return feedback


def assign_layers(
    nodes: Sequence[Mapping[str, Any]], edges: Sequence[Mapping[str, Any]], feedback_edges: set[str]
) -> dict[str, int]:
    """Assign longest-path ranks after removing deterministic feedback edges."""
    node_ids = _ordered_nodes(nodes)
    known = set(node_ids)
    indegree = {node_id: 0 for node_id in node_ids}
    outgoing: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for edge in _ordered_edges(edges):
        edge_id = _edge_id(edge)
        source, target = edge.get("source"), edge.get("target")
        if edge_id in feedback_edges or not isinstance(source, str) or not isinstance(target, str):
            continue
        if source not in known or target not in known:
            continue
        outgoing[source].append(edge)
        indegree[target] += 1
    for source in outgoing:
        outgoing[source].sort(key=lambda edge: (str(edge["target"]), _edge_id(edge)))

    ready = deque(sorted(node_id for node_id, degree in indegree.items() if degree == 0))
    layers = {node_id: 0 for node_id in node_ids}
    visited: list[str] = []
    while ready:
        node_id = ready.popleft()
        visited.append(node_id)
        for edge in outgoing[node_id]:
            target = str(edge["target"])
            layers[target] = max(layers[target], layers[node_id] + 1)
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
        if len(ready) > 1:
            ready = deque(sorted(ready))
    if len(visited) != len(node_ids):
        # Defensive deterministic fallback for malformed input where callers
        # supplied an incomplete feedback set.  Valid callers use the chooser.
        next_layer = max(layers.values(), default=-1) + 1
        for node_id in node_ids:
            if node_id not in visited:
                layers[node_id] = next_layer
                next_layer += 1
    return {node_id: layers[node_id] for node_id in node_ids}


def minimize_crossings(
    layers: Mapping[int, Sequence[str]], edges: Sequence[Mapping[str, Any]], *, sweeps: int = 4
) -> list[list[str]]:
    """Apply exactly four deterministic barycenter/median ordering sweeps."""
    if sweeps != 4:
        raise ValueError("deterministic layered placement requires exactly four sweeps")
    ordered_levels = sorted(layers)
    result = {level: sorted(str(node_id) for node_id in layers[level]) for level in ordered_levels}
    if not ordered_levels:
        return []
    node_layer = {node_id: level for level in ordered_levels for node_id in result[level]}
    predecessors: dict[str, list[str]] = defaultdict(list)
    successors: dict[str, list[str]] = defaultdict(list)
    for edge in _ordered_edges(edges):
        source, target = edge.get("source"), edge.get("target")
        if not isinstance(source, str) or not isinstance(target, str):
            continue
        if source not in node_layer or target not in node_layer:
            continue
        if node_layer[source] < node_layer[target]:
            predecessors[target].append(source)
            successors[source].append(target)
    for values in predecessors.values():
        values.sort()
    for values in successors.values():
        values.sort()

    def reorder(level: int, neighbours: Mapping[str, list[str]], reference_level: int) -> None:
        positions = {node_id: index for index, node_id in enumerate(result[reference_level])}
        original = {node_id: index for index, node_id in enumerate(result[level])}

        def key(node_id: str) -> tuple[float, int, str]:
            values = [positions[other] for other in neighbours.get(node_id, []) if other in positions]
            anchor = float(median(values)) if values else float(original[node_id])
            return anchor, original[node_id], node_id

        result[level] = sorted(result[level], key=key)

    for _ in range(4):
        for index in range(1, len(ordered_levels)):
            reorder(ordered_levels[index], predecessors, ordered_levels[index - 1])
        for index in range(len(ordered_levels) - 2, -1, -1):
            reorder(ordered_levels[index], successors, ordered_levels[index + 1])
    return [result[level] for level in ordered_levels]


def _grid(value: float, grid_size: float) -> float:
    return round(value / grid_size) * grid_size


def _bounds(node: Mapping[str, Any], x: float, y: float) -> dict[str, float]:
    return {
        "x": x,
        "y": y,
        "width": float(node["width"]),
        "height": float(node["height"]),
    }


def _page_layers(nodes: Sequence[Mapping[str, Any]], layers: Mapping[Any, Any], page_id: str) -> dict[str, int]:
    nested = layers.get(page_id) if isinstance(layers, Mapping) else None
    source = nested if isinstance(nested, Mapping) else layers
    return {_node_id(node): int(source.get(_node_id(node), 0)) for node in nodes}


def _primary_extent(bounds: Mapping[str, float], direction: str) -> tuple[float, float]:
    if direction == "TB":
        return bounds["y"], bounds["height"]
    return bounds["x"], bounds["width"]


def _cross_extent(bounds: Mapping[str, float], direction: str) -> tuple[float, float]:
    if direction == "TB":
        return bounds["x"], bounds["width"]
    return bounds["y"], bounds["height"]


def _set_extents(node: Mapping[str, Any], direction: str, primary: float, cross: float) -> dict[str, float]:
    if direction == "TB":
        return _bounds(node, cross, primary)
    return _bounds(node, primary, cross)


def _children_by_parent(nodes: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    children: dict[str, list[str]] = defaultdict(list)
    for node in nodes:
        parent_id = node.get("parent_id")
        if isinstance(parent_id, str):
            children[parent_id].append(_node_id(node))
    for values in children.values():
        values.sort()
    return children


def _expand_containers(
    nodes: Sequence[Mapping[str, Any]], bounds: dict[str, dict[str, float]], grid_size: float
) -> None:
    """Expand unlocked structural parents around descendants, inside-out."""
    by_id = {_node_id(node): node for node in nodes}
    children = _children_by_parent(nodes)
    remaining = set(children)
    while remaining:
        progressed = False
        for parent_id in sorted(remaining):
            direct_children = children[parent_id]
            if any(child in remaining for child in direct_children):
                continue
            parent = by_id[parent_id]
            if not parent.get("locked"):
                child_bounds = [bounds[child] for child in direct_children]
                pad = grid_size * 2
                left = min(item["x"] for item in child_bounds) - pad
                top = min(item["y"] for item in child_bounds) - pad
                right = max(item["x"] + item["width"] for item in child_bounds) + pad
                bottom = max(item["y"] + item["height"] for item in child_bounds) + pad
                bounds[parent_id] = {
                    "x": _grid(left, grid_size), "y": _grid(top, grid_size),
                    "width": _grid(right - left, grid_size), "height": _grid(bottom - top, grid_size),
                }
            remaining.remove(parent_id)
            progressed = True
        if not progressed:
            raise ValueError("layout parent hierarchy contains a cycle")


def _subtree_ids(root_id: str, children: Mapping[str, Sequence[str]]) -> list[str]:
    output: list[str] = []

    def visit(node_id: str) -> None:
        output.append(node_id)
        for child_id in children.get(node_id, ()):
            visit(child_id)

    visit(root_id)
    return output


def _translate_subtree(
    root_id: str, children: Mapping[str, Sequence[str]], bounds: dict[str, dict[str, float]], *, x: float, y: float
) -> None:
    original = bounds[root_id]
    dx, dy = x - original["x"], y - original["y"]
    for node_id in _subtree_ids(root_id, children):
        bounds[node_id] = {
            **bounds[node_id],
            "x": bounds[node_id]["x"] + dx,
            "y": bounds[node_id]["y"] + dy,
        }


def _rectangles_overlap(first: Mapping[str, float], second: Mapping[str, float]) -> bool:
    return not (
        first["x"] + first["width"] <= second["x"]
        or second["x"] + second["width"] <= first["x"]
        or first["y"] + first["height"] <= second["y"]
        or second["y"] + second["height"] <= first["y"]
    )


def _contains_bounds(parent: Mapping[str, float], child: Mapping[str, float]) -> bool:
    return (
        parent["x"] <= child["x"]
        and parent["y"] <= child["y"]
        and parent["x"] + parent["width"] >= child["x"] + child["width"]
        and parent["y"] + parent["height"] >= child["y"] + child["height"]
    )


def _parent_candidates(
    parent: Mapping[str, float], child: Mapping[str, float], *, grid_size: float, padding: float, direction: str
) -> list[tuple[float, float]]:
    left, top = parent["x"] + padding, parent["y"] + padding
    right, bottom = parent["x"] + parent["width"] - padding, parent["y"] + parent["height"] - padding
    max_x, max_y = right - child["width"], bottom - child["height"]
    if max_x < left or max_y < top:
        return []
    xs = [left + index * grid_size for index in range(int((max_x - left) // grid_size) + 1)]
    ys = [top + index * grid_size for index in range(int((max_y - top) // grid_size) + 1)]
    pairs = ((x, y) for x in xs for y in ys) if direction == "TB" else ((x, y) for y in ys for x in xs)
    return list(pairs)


def _reserve_with_separation(bounds: Mapping[str, float], separation: float) -> dict[str, float]:
    return {
        "x": bounds["x"] - separation,
        "y": bounds["y"] - separation,
        "width": bounds["width"] + separation * 2,
        "height": bounds["height"] + separation * 2,
    }


def _contain_locked_parents(
    nodes: Sequence[Mapping[str, Any]], bounds: dict[str, dict[str, float]], *, grid_size: float,
    separation: float, direction: str,
) -> None:
    """Pack unlocked child subtrees inside immutable structural parents.

    An immutable parent is a user lock, so its bounds are never changed.  Its
    direct unlocked children are placed in a deterministic padded content area;
    descendants move together with an unlocked container.  A child that cannot
    fit is a placement error rather than a reason to expand or move the lock.
    """
    by_id = {_node_id(node): node for node in nodes}
    children = _children_by_parent(nodes)
    parent_of = {
        _node_id(node): node["parent_id"]
        for node in nodes
        if isinstance(node.get("parent_id"), str)
    }
    locked_parents = [node_id for node_id in children if by_id[node_id].get("locked")]

    def depth(node_id: str) -> int:
        value, result = node_id, 0
        seen: set[str] = set()
        while value in parent_of:
            if value in seen:
                raise ValueError("layout parent hierarchy contains a cycle")
            seen.add(value)
            result += 1
            value = parent_of[value]
        return result

    for parent_id in sorted(locked_parents, key=lambda item: (-depth(item), item)):
        parent_bounds = bounds[parent_id]
        direct_children = list(children[parent_id])
        occupied: list[dict[str, float]] = []
        for child_id in direct_children:
            child = by_id[child_id]
            if child.get("locked"):
                if not _contains_bounds(parent_bounds, bounds[child_id]):
                    raise ValueError(
                        f"locked parent {parent_id!r} capacity does not contain locked child {child_id!r}"
                    )
                occupied.append(_reserve_with_separation(bounds[child_id], separation))
        for child_id in direct_children:
            child = by_id[child_id]
            if child.get("locked"):
                continue
            current = bounds[child_id]
            selected: tuple[float, float] | None = None
            for x, y in _parent_candidates(
                parent_bounds, current, grid_size=grid_size, padding=grid_size * 2, direction=direction
            ):
                candidate = {**current, "x": x, "y": y}
                if all(not _rectangles_overlap(candidate, reservation) for reservation in occupied):
                    selected = (x, y)
                    occupied.append(_reserve_with_separation(candidate, separation))
                    break
            if selected is None:
                raise ValueError(
                    f"locked parent {parent_id!r} capacity cannot fit child {child_id!r}"
                )
            _translate_subtree(child_id, children, bounds, x=selected[0], y=selected[1])
        for descendant_id in _subtree_ids(parent_id, children)[1:]:
            if not _contains_bounds(parent_bounds, bounds[descendant_id]):
                raise ValueError(
                    f"locked parent {parent_id!r} capacity cannot contain descendant {descendant_id!r}"
                )


def _next_free_cross_coordinate(
    start: float, size: float, reserved: list[tuple[float, float]], separation: float
) -> float:
    """Return the first deterministic cross-axis slot that avoids reservations."""
    candidate = start
    for left, right in sorted(reserved):
        if candidate + size + separation <= left:
            break
        if candidate < right + separation:
            candidate = right + separation
    return candidate


def assign_coordinates(request: Mapping[str, Any], layers: Mapping[Any, Any]) -> dict[str, dict]:
    """Place page-scoped nodes deterministically while preserving locked bounds."""
    direction = request.get("direction")
    if direction not in {"TB", "LR"}:
        raise ValueError("layout request direction must be TB or LR")
    constraints = request.get("constraints")
    if not isinstance(constraints, Mapping):
        raise ValueError("layout request requires constraints")
    grid_size = float(constraints["grid_size"])
    node_separation = float(constraints["node_separation"])
    layer_separation = float(constraints["layer_separation"])
    output: dict[str, dict] = {}
    pages = request.get("pages")
    if not isinstance(pages, list):
        raise ValueError("layout request requires pages")

    for page in sorted((page for page in pages if isinstance(page, Mapping)), key=lambda page: str(page.get("page_id", ""))):
        page_id = page.get("page_id")
        nodes = page.get("nodes")
        if not isinstance(page_id, str) or not isinstance(nodes, list):
            raise ValueError("layout page requires page_id and nodes")
        ordered = sorted((node for node in nodes if isinstance(node, Mapping)), key=_node_id)
        by_id = {_node_id(node): node for node in ordered}
        layer_map = _page_layers(ordered, layers, page_id)
        grouped: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
        for node in ordered:
            grouped[layer_map[_node_id(node)]].append(node)
        page_edges = page.get("edges")
        ordered_layer_ids = minimize_crossings(
            {layer: [_node_id(node) for node in members] for layer, members in grouped.items()},
            page_edges if isinstance(page_edges, list) else [],
        )
        ordered_layers = [
            [by_id[node_id] for node_id in node_ids]
            for node_ids in ordered_layer_ids
        ]
        page_bounds: dict[str, dict[str, float]] = {}
        primary_cursor = 0.0
        for members in ordered_layers:
            locked_primary = [
                _primary_extent(_bounds(node, float(node["x"]), float(node["y"])), direction)[0]
                for node in members if node.get("locked")
            ]
            layer_primary = max([primary_cursor, *locked_primary])
            max_primary_size = 0.0
            locked_members = [node for node in members if node.get("locked")]
            unlocked_members = [node for node in members if not node.get("locked")]
            reserved = []
            for node in locked_members:
                node_id = _node_id(node)
                bounds = _bounds(node, float(node["x"]), float(node["y"]))
                locked_cross, cross_size = _cross_extent(bounds, direction)
                reserved.append((locked_cross, locked_cross + cross_size))
                page_bounds[node_id] = {
                    key: _grid(value, grid_size) for key, value in bounds.items()
                }
                _, primary_size = _primary_extent(page_bounds[node_id], direction)
                max_primary_size = max(max_primary_size, primary_size)
            cross_cursor = 0.0
            for node in unlocked_members:
                node_id = _node_id(node)
                probe = _bounds(node, 0, 0)
                _, cross_size = _cross_extent(probe, direction)
                cross_cursor = _next_free_cross_coordinate(
                    cross_cursor, cross_size, reserved, node_separation
                )
                bounds = _set_extents(node, direction, layer_primary, cross_cursor)
                cross_cursor += cross_size + node_separation
                page_bounds[node_id] = {
                    key: _grid(value, grid_size) for key, value in bounds.items()
                }
                _, primary_size = _primary_extent(page_bounds[node_id], direction)
                max_primary_size = max(max_primary_size, primary_size)
            primary_cursor = max(primary_cursor, layer_primary + max_primary_size + layer_separation)
        _expand_containers(ordered, page_bounds, grid_size)
        _contain_locked_parents(
            ordered, page_bounds, grid_size=grid_size, separation=node_separation, direction=direction
        )
        output.update({f"{page_id}/{node_id}": page_bounds[node_id] for node_id in sorted(page_bounds)})
    return output


def layout(request: Mapping[str, Any]) -> dict:
    """Return a deterministic placement snapshot; routing remains intentionally absent."""
    pages = request.get("pages")
    if not isinstance(pages, list):
        raise ValueError("layout request requires pages")
    page_layers: dict[str, dict[str, int]] = {}
    page_orders: dict[str, list[list[str]]] = {}
    for page in sorted((page for page in pages if isinstance(page, Mapping)), key=lambda page: str(page.get("page_id", ""))):
        page_id, nodes, edges = page.get("page_id"), page.get("nodes"), page.get("edges")
        if not isinstance(page_id, str) or not isinstance(nodes, list) or not isinstance(edges, list):
            raise ValueError("layout page requires page_id, nodes and edges")
        feedback = choose_feedback_edges(nodes, edges)
        raw_layers = assign_layers(nodes, edges, feedback)
        grouped: dict[int, list[str]] = defaultdict(list)
        for node_id, layer in raw_layers.items():
            grouped[layer].append(node_id)
        ordered_layers = minimize_crossings(grouped, edges)
        page_layers[page_id] = raw_layers
        page_orders[page_id] = ordered_layers
    bounds = assign_coordinates(request, page_layers)
    result_pages = []
    for page in sorted((page for page in pages if isinstance(page, Mapping)), key=lambda page: str(page.get("page_id", ""))):
        page_id = str(page["page_id"])
        nodes = [
            {"node_id": _node_id(node), "locked": bool(node.get("locked")), "bounds": bounds[f"{page_id}/{_node_id(node)}"]}
            for node in sorted(page["nodes"], key=_node_id)
        ]
        result_pages.append({"page_id": page_id, "name": str(page.get("name", "")), "nodes": nodes, "layers": page_orders[page_id]})
    return {"backend": "python-layered-placement-v1", "pages": result_pages}
