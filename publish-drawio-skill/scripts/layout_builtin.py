#!/usr/bin/env python3
"""Deterministic host-owned layered placement and orthogonal edge routing."""
from __future__ import annotations

import heapq
import itertools
from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from statistics import median
from typing import Any

import layout_contracts
import layout_geometry
from lifecycle_contracts import canonical_json_sha256

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


_SIDES = frozenset({"north", "east", "south", "west"})


def _port_point(bounds: Mapping[str, float], side: str, position: float) -> tuple[float, float]:
    if side == "north":
        return bounds["x"] + bounds["width"] * position, bounds["y"]
    if side == "south":
        return bounds["x"] + bounds["width"] * position, bounds["y"] + bounds["height"]
    if side == "west":
        return bounds["x"], bounds["y"] + bounds["height"] * position
    if side == "east":
        return bounds["x"] + bounds["width"], bounds["y"] + bounds["height"] * position
    raise ValueError(f"unsupported port side {side!r}")


def _pin_from_point(
    bounds: Mapping[str, float],
    side: str,
    point: Mapping[str, Any],
    *,
    role: str,
) -> float:
    px, py = float(point["x"]), float(point["y"])
    epsilon = 1e-6
    boundary = {
        "north": bounds["y"],
        "south": bounds["y"] + bounds["height"],
        "west": bounds["x"],
        "east": bounds["x"] + bounds["width"],
    }[side]
    boundary_value = py if side in {"north", "south"} else px
    if abs(boundary_value - boundary) > epsilon:
        raise ValueError(f"locked {role} endpoint must lie on the exact {side} boundary")
    if side in {"north", "south"}:
        coordinate, start, extent = px, bounds["x"], bounds["width"]
    else:
        coordinate, start, extent = py, bounds["y"], bounds["height"]
    if coordinate < start - epsilon or coordinate > start + extent + epsilon:
        raise ValueError(f"locked {role} endpoint lies outside the node span on the {side} boundary")
    pin = (coordinate - start) / extent
    if pin < 0.1 - epsilon or pin > 0.9 + epsilon:
        raise ValueError(f"locked {role} endpoint pin must be within [0.1, 0.9]")
    return pin


def _spread_pins(count: int) -> list[float]:
    if count <= 0:
        return []
    if count == 1:
        return [0.5]
    return [round(0.1 + (0.8 * index / (count - 1)), 9) for index in range(count)]


def allocate_ports(request: Mapping[str, Any], bounds: Mapping[str, Mapping[str, float]]) -> dict[str, dict]:
    """Allocate stable page-scoped sides, normalized pins, and absolute points."""
    direction = str(request.get("direction"))
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    fixed_pins: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    output: dict[str, dict] = {}
    pages = request.get("pages")
    if not isinstance(pages, list):
        raise ValueError("layout request requires pages")
    for page in sorted((item for item in pages if isinstance(item, Mapping)), key=lambda item: str(item.get("page_id", ""))):
        page_id = str(page.get("page_id"))
        nodes = {
            _node_id(node): node
            for node in page.get("nodes", [])
            if isinstance(node, Mapping)
        }
        ordered_edges = _ordered_edges(page.get("edges", []))
        feedback_ids = choose_feedback_edges(list(nodes.values()), ordered_edges)
        for edge in ordered_edges:
            edge_id = _edge_id(edge)
            edge_key = f"{page_id}/{edge_id}"
            source, target = str(edge.get("source")), str(edge.get("target"))
            if source not in nodes or target not in nodes:
                raise ValueError(f"edge {edge_key} references an unknown endpoint")
            if edge.get("locked"):
                source_side, target_side = str(edge.get("source_port")), str(edge.get("target_port"))
            elif source == target or edge.get("edge_class") == "self_loop":
                source_side, target_side = ("east", "south") if direction == "TB" else ("south", "east")
            elif edge.get("edge_class") == "feedback" or edge_id in feedback_ids:
                source_side, target_side = ("west", "west") if direction == "TB" else ("north", "north")
            else:
                source_side, target_side = ("south", "north") if direction == "TB" else ("east", "west")
            if source_side not in _SIDES or target_side not in _SIDES:
                raise ValueError(f"edge {edge_key} requires valid port sides")
            output[edge_key] = {
                "source": {"side": source_side},
                "target": {"side": target_side},
            }
            route = edge.get("waypoints")
            if edge.get("locked") and isinstance(route, list) and len(route) >= 2:
                for role, node_id, side, point in (
                    ("source", source, source_side, route[0]),
                    ("target", target, target_side, route[-1]),
                ):
                    node_bounds = bounds[f"{page_id}/{node_id}"]
                    pin = _pin_from_point(node_bounds, side, point, role=role)
                    output[edge_key][role] = {
                        "side": side,
                        "position": pin,
                        "point": (float(point["x"]), float(point["y"])),
                    }
                    fixed_pins[(page_id, node_id, side)].append(pin)
                continue
            for role, node_id, side, opposite_id in (
                ("source", source, source_side, target),
                ("target", target, target_side, source),
            ):
                opposite = bounds[f"{page_id}/{opposite_id}"]
                primary = opposite["y"] if direction == "TB" else opposite["x"]
                cross = opposite["x"] if direction == "TB" else opposite["y"]
                groups[(page_id, node_id, side)].append({
                    "edge_key": edge_key,
                    "edge_id": edge_id,
                    "role": role,
                    "sort": (primary, cross, edge_id, role),
                })
    for (page_id, node_id, side), incidents in sorted(groups.items()):
        ordered = sorted(incidents, key=lambda item: item["sort"])
        fixed = fixed_pins.get((page_id, node_id, side), [])
        candidates = [
            pin
            for pin in _spread_pins(len(ordered) + len(fixed))
            if all(abs(pin - occupied) > 1e-9 for occupied in fixed)
        ]
        if len(candidates) < len(ordered):
            candidates.extend(
                pin for pin in (round(index / 10, 9) for index in range(1, 10))
                if all(abs(pin - occupied) > 1e-9 for occupied in [*fixed, *candidates])
            )
        if len(candidates) < len(ordered):
            raise ValueError(f"port side {page_id}/{node_id}/{side} has no distinct normalized pins")
        for incident, pin in zip(ordered, candidates):
            node_bounds = bounds[f"{page_id}/{node_id}"]
            output[incident["edge_key"]][incident["role"]] = {
                "side": side,
                "position": pin,
                "point": _port_point(node_bounds, side, pin),
            }
    return {edge_key: output[edge_key] for edge_key in sorted(output)}


def _side_vector(side: str) -> tuple[float, float]:
    return {
        "north": (0.0, -1.0),
        "east": (1.0, 0.0),
        "south": (0.0, 1.0),
        "west": (-1.0, 0.0),
    }[side]


def _rect(bounds: Mapping[str, float]) -> layout_geometry.Rect:
    return bounds["x"], bounds["y"], bounds["width"], bounds["height"]


def _expanded_contains(point: layout_geometry.Point, rect: layout_geometry.Rect, clearance: float) -> bool:
    x, y, width, height = rect
    return (
        x - clearance <= point[0] <= x + width + clearance
        and y - clearance <= point[1] <= y + height + clearance
    )


def _visibility_route(
    start: layout_geometry.Point,
    target: layout_geometry.Point,
    obstacles: Sequence[layout_geometry.Rect],
    *,
    clearance: float,
    grid_size: float,
    reserved: Sequence[layout_geometry.Segment],
    extra_x: Sequence[float] = (),
    extra_y: Sequence[float] = (),
) -> list[layout_geometry.Point]:
    """Route between outside stubs on a rectilinear visibility graph."""
    xs = {start[0], target[0], *extra_x}
    ys = {start[1], target[1], *extra_y}
    for x, y, width, height in obstacles:
        xs.update((x - clearance - grid_size, x + width + clearance + grid_size))
        ys.update((y - clearance - grid_size, y + height + clearance + grid_size))
    points = {
        (float(x), float(y))
        for x in xs for y in ys
        if not any(_expanded_contains((float(x), float(y)), obstacle, clearance) for obstacle in obstacles)
    }
    points.update((start, target))
    adjacency: dict[layout_geometry.Point, list[layout_geometry.Point]] = defaultdict(list)
    by_x: dict[float, list[layout_geometry.Point]] = defaultdict(list)
    by_y: dict[float, list[layout_geometry.Point]] = defaultdict(list)
    for point in points:
        by_x[point[0]].append(point)
        by_y[point[1]].append(point)

    def connect(first: layout_geometry.Point, second: layout_geometry.Point) -> None:
        segment = (first, second)
        if any(layout_geometry.segment_hits_rect(segment, obstacle, clearance=clearance) for obstacle in obstacles):
            return
        adjacency[first].append(second)
        adjacency[second].append(first)

    for values in by_x.values():
        ordered = sorted(values, key=lambda point: (point[1], point[0]))
        for first, second in zip(ordered, ordered[1:]):
            connect(first, second)
    for values in by_y.values():
        ordered = sorted(values, key=lambda point: (point[0], point[1]))
        for first, second in zip(ordered, ordered[1:]):
            connect(first, second)
    for point in adjacency:
        adjacency[point].sort()

    counter = itertools.count()
    queue: list[tuple[float, int, tuple[layout_geometry.Point, ...], int, layout_geometry.Point, str | None]] = []
    heapq.heappush(queue, (0.0, 0, (start,), next(counter), start, None))
    best: dict[tuple[layout_geometry.Point, str | None], float] = {(start, None): 0.0}
    while queue:
        cost, bends, path, _, point, orientation = heapq.heappop(queue)
        if point == target:
            result = layout_geometry.canonicalize_route(list(path))
            if not layout_geometry.is_manhattan(result):
                raise ValueError("visibility route is not Manhattan")
            return result
        if cost > best.get((point, orientation), float("inf")) + 1e-9:
            continue
        for neighbour in adjacency.get(point, []):
            next_orientation = "H" if abs(point[1] - neighbour[1]) <= 1e-9 else "V"
            segment = (point, neighbour)
            length = abs(point[0] - neighbour[0]) + abs(point[1] - neighbour[1])
            shared = sum(layout_geometry.collinear_overlap(segment, occupied) for occupied in reserved)
            bend = orientation is not None and orientation != next_orientation
            next_cost = cost + length + shared * 50.0 + (grid_size * 2 if bend else 0.0)
            state = (neighbour, next_orientation)
            if next_cost + 1e-9 >= best.get(state, float("inf")):
                continue
            best[state] = next_cost
            next_path = path + (neighbour,)
            heapq.heappush(
                queue,
                (next_cost, bends + int(bend), next_path, next(counter), neighbour, next_orientation),
            )
    raise ValueError(f"no orthogonal visibility route between {start!r} and {target!r}")


def _ancestor_ids(nodes: Mapping[str, Mapping[str, Any]], node_id: str) -> set[str]:
    result: set[str] = set()
    current = nodes.get(node_id)
    while isinstance(current, Mapping) and isinstance(current.get("parent_id"), str):
        parent_id = str(current["parent_id"])
        if parent_id in result:
            raise ValueError("layout parent hierarchy contains a cycle")
        result.add(parent_id)
        current = nodes.get(parent_id)
    return result


def _serialize_points(points: Sequence[layout_geometry.Point]) -> list[dict[str, float]]:
    return [{"x": float(point[0]), "y": float(point[1])} for point in points]


def route_edges(
    request: Mapping[str, Any],
    bounds: Mapping[str, Mapping[str, float]],
    ports: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict]:
    """Route every page independently, reserving stable channels as edges finish."""
    constraints = request.get("constraints")
    if not isinstance(constraints, Mapping):
        raise ValueError("layout request requires constraints")
    grid_size = float(constraints["grid_size"])
    clearance = max(grid_size, min(float(constraints["node_separation"]) / 2.0, grid_size * 2))
    output: dict[str, dict] = {}
    pages = request.get("pages")
    if not isinstance(pages, list):
        raise ValueError("layout request requires pages")
    for page in sorted((item for item in pages if isinstance(item, Mapping)), key=lambda item: str(item.get("page_id", ""))):
        page_id = str(page["page_id"])
        nodes = {_node_id(node): node for node in page.get("nodes", []) if isinstance(node, Mapping)}
        edges = _ordered_edges(page.get("edges", []))
        feedback_ids = choose_feedback_edges(list(nodes.values()), edges)
        reserved: list[layout_geometry.Segment] = []
        outer_index = 0

        def route_priority(edge: Mapping[str, Any]) -> tuple[int, str]:
            edge_id = _edge_id(edge)
            edge_class = str(edge.get("edge_class"))
            external = edge_class in {"feedback", "self_loop"} or edge_id in feedback_ids
            return (0 if external else 1, edge_id)

        for edge in sorted(edges, key=route_priority):
            edge_id = _edge_id(edge)
            edge_key = f"{page_id}/{edge_id}"
            source, target = str(edge["source"]), str(edge["target"])
            effective_class = str(edge.get("edge_class"))
            if source == target:
                effective_class = "self_loop"
            elif edge_id in feedback_ids:
                effective_class = "feedback"
            port = ports[edge_key]
            record = {
                "edge_id": edge_id,
                "source": source,
                "target": target,
                "edge_class": effective_class,
                "source_port": str(port["source"]["side"]),
                "target_port": str(port["target"]["side"]),
                "source_pin": float(port["source"]["position"]),
                "target_pin": float(port["target"]["position"]),
            }
            if edge.get("locked"):
                route = edge.get("waypoints")
                if not isinstance(route, list) or len(route) < 2:
                    raise ValueError(f"locked edge {edge_key} requires an explicit route")
                route_points = [
                    (float(point["x"]), float(point["y"]))
                    for point in route
                ]
                common_ancestors = _ancestor_ids(nodes, source) & _ancestor_ids(nodes, target)
                obstacle_ids = [
                    node_id for node_id in sorted(nodes)
                    if node_id not in {source, target} and node_id not in common_ancestors
                ]
                for obstacle_id in obstacle_ids:
                    obstacle = _rect(bounds[f"{page_id}/{obstacle_id}"])
                    if any(
                        layout_geometry.segment_hits_rect(segment, obstacle, clearance=clearance)
                        for segment in layout_geometry.route_segments(route_points)
                    ):
                        raise ValueError(
                            f"locked edge {edge_key} crosses expanded obstacle {obstacle_id!r}"
                        )
                record["waypoints"] = [dict(point) for point in route]
                output[edge_key] = record
                reserved.extend(layout_geometry.route_segments(route_points))
                continue

            start = tuple(port["source"]["point"])
            end = tuple(port["target"]["point"])
            source_vector = _side_vector(record["source_port"])
            target_vector = _side_vector(record["target_port"])
            stub_distance = clearance + grid_size
            source_stub = (
                start[0] + source_vector[0] * stub_distance,
                start[1] + source_vector[1] * stub_distance,
            )
            target_stub = (
                end[0] + target_vector[0] * stub_distance,
                end[1] + target_vector[1] * stub_distance,
            )
            ancestor_exemptions = _ancestor_ids(nodes, source) | _ancestor_ids(nodes, target)
            obstacles = [
                _rect(bounds[f"{page_id}/{node_id}"])
                for node_id in sorted(nodes)
                if node_id not in ancestor_exemptions
            ]
            if effective_class in {"feedback", "self_loop"}:
                all_rects = [_rect(bounds[f"{page_id}/{node_id}"]) for node_id in sorted(nodes)]
                if str(request.get("direction")) == "TB":
                    extreme = min(rect[0] for rect in all_rects) - clearance - grid_size * (2 + outer_index)
                    anchor_start = (extreme, source_stub[1])
                    anchor_end = (extreme, target_stub[1])
                    extra_x, extra_y = (extreme,), ()
                else:
                    extreme = min(rect[1] for rect in all_rects) - clearance - grid_size * (2 + outer_index)
                    anchor_start = (source_stub[0], extreme)
                    anchor_end = (target_stub[0], extreme)
                    extra_x, extra_y = (), (extreme,)
                outer_index += 1
                first = _visibility_route(
                    source_stub, anchor_start, obstacles, clearance=clearance,
                    grid_size=grid_size, reserved=reserved, extra_x=extra_x, extra_y=extra_y,
                )
                last = _visibility_route(
                    anchor_end, target_stub, obstacles, clearance=clearance,
                    grid_size=grid_size, reserved=reserved, extra_x=extra_x, extra_y=extra_y,
                )
                middle = [anchor_start, anchor_end]
                routed = layout_geometry.canonicalize_route([start, *first, *middle, *last, end])
            else:
                middle = _visibility_route(
                    source_stub, target_stub, obstacles, clearance=clearance,
                    grid_size=grid_size, reserved=reserved,
                )
                routed = layout_geometry.canonicalize_route([start, *middle, end])
            if not layout_geometry.is_manhattan(routed):
                raise ValueError(f"edge {edge_key} route is not orthogonal")
            record["waypoints"] = _serialize_points(routed)
            output[edge_key] = record
            reserved.extend(layout_geometry.route_segments(routed))
    return {edge_key: output[edge_key] for edge_key in sorted(output)}


def _label_candidate(
    segment: layout_geometry.Segment,
    width: float,
    height: float,
    offset: float,
) -> layout_geometry.Rect:
    (ax, ay), (bx, by) = segment
    center_x, center_y = (ax + bx) / 2.0, (ay + by) / 2.0
    if abs(ay - by) <= 1e-9:
        center_y += offset
    else:
        center_x += offset
    return center_x - width / 2.0, center_y - height / 2.0, width, height


def _rect_value(rect: layout_geometry.Rect) -> dict[str, float]:
    return {"x": float(rect[0]), "y": float(rect[1]), "width": float(rect[2]), "height": float(rect[3])}


def place_edge_labels(
    request: Mapping[str, Any],
    bounds: Mapping[str, Mapping[str, float]],
    routes: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, dict[str, float]], int]:
    """Place labels on the longest free route segment with bounded offsets."""
    labels: dict[str, dict[str, float]] = {}
    fallback_count = 0
    pages = request.get("pages")
    if not isinstance(pages, list):
        raise ValueError("layout request requires pages")
    for page in sorted((item for item in pages if isinstance(item, Mapping)), key=lambda item: str(item.get("page_id", ""))):
        page_id = str(page["page_id"])
        occupied_labels: list[layout_geometry.Rect] = []
        page_fallback_count = 0
        node_rects = [
            _rect(bounds[f"{page_id}/{_node_id(node)}"])
            for node in page.get("nodes", [])
            if isinstance(node, Mapping)
        ]
        for edge in _ordered_edges(page.get("edges", [])):
            size = edge.get("label_size")
            if not isinstance(size, Mapping):
                continue
            edge_key = f"{page_id}/{_edge_id(edge)}"
            width, height = float(size["width"]), float(size["height"])
            points = [
                (float(point["x"]), float(point["y"]))
                for point in routes[edge_key]["waypoints"]
            ]
            segments = list(enumerate(layout_geometry.route_segments(points)))
            segments.sort(
                key=lambda item: (
                    -(abs(item[1][1][0] - item[1][0][0]) + abs(item[1][1][1] - item[1][0][1])),
                    item[0],
                    item[1],
                )
            )
            selected: layout_geometry.Rect | None = None
            for _, segment in segments:
                for offset in (0.0, 20.0, -20.0, 40.0, -40.0):
                    candidate = _label_candidate(segment, width, height, offset)
                    if any(layout_geometry.rects_overlap(candidate, rect) for rect in node_rects):
                        continue
                    if any(layout_geometry.rects_overlap(candidate, rect) for rect in occupied_labels):
                        continue
                    selected = candidate
                    break
                if selected is not None:
                    break
            if selected is None:
                fallback_count += 1
                page_fallback_count += 1
                left = min((rect[0] for rect in [*node_rects, *occupied_labels]), default=0.0)
                top = min((rect[1] for rect in [*node_rects, *occupied_labels]), default=0.0)
                selected = (
                    left - width - 40.0,
                    top + page_fallback_count * (height + 20.0),
                    width,
                    height,
                )
            labels[edge_key] = _rect_value(selected)
            occupied_labels.append(selected)
    return labels, fallback_count


def _proper_cross(first: layout_geometry.Segment, second: layout_geometry.Segment) -> bool:
    if layout_geometry.collinear_overlap(first, second) > 0:
        return False
    (a, b), (c, d) = first, second
    first_horizontal = abs(a[1] - b[1]) <= 1e-9
    second_horizontal = abs(c[1] - d[1]) <= 1e-9
    if first_horizontal == second_horizontal:
        return False
    horizontal, vertical = (first, second) if first_horizontal else (second, first)
    (hx1, hy), (hx2, _) = horizontal
    (vx, vy1), (_, vy2) = vertical
    return (
        min(hx1, hx2) < vx < max(hx1, hx2)
        and min(vy1, vy2) < hy < max(vy1, vy2)
    )


def _layout_metrics(
    request: Mapping[str, Any],
    bounds: Mapping[str, Mapping[str, float]],
    routes: Mapping[str, Mapping[str, Any]],
    *,
    label_collisions: int,
) -> dict[str, int | float]:
    containment_pairs: set[frozenset[str]] = set()
    for page in request.get("pages", []):
        if not isinstance(page, Mapping):
            continue
        page_id = str(page.get("page_id"))
        nodes = {
            _node_id(node): node
            for node in page.get("nodes", [])
            if isinstance(node, Mapping)
        }
        for node_id in sorted(nodes):
            for ancestor_id in _ancestor_ids(nodes, node_id):
                containment_pairs.add(frozenset((
                    f"{page_id}/{node_id}",
                    f"{page_id}/{ancestor_id}",
                )))
    overlaps = 0
    page_edge_keys: dict[str, list[str]] = {}
    for page in request.get("pages", []):
        if not isinstance(page, Mapping):
            continue
        page_id = str(page.get("page_id"))
        node_keys = sorted(
            f"{page_id}/{_node_id(node)}"
            for node in page.get("nodes", [])
            if isinstance(node, Mapping)
        )
        overlaps += sum(
            int(layout_geometry.rects_overlap(_rect(bounds[first]), _rect(bounds[second])))
            for first, second in itertools.combinations(node_keys, 2)
            if frozenset((first, second)) not in containment_pairs
        )
        page_edge_keys[page_id] = sorted(
            f"{page_id}/{_edge_id(edge)}"
            for edge in page.get("edges", [])
            if isinstance(edge, Mapping)
        )
    route_points = {
        edge_key: [
            (float(point["x"]), float(point["y"]))
            for point in route["waypoints"]
        ]
        for edge_key, route in sorted(routes.items())
    }
    crossings = 0
    shared = 0.0
    for page_id in sorted(page_edge_keys):
        for first_key, second_key in itertools.combinations(page_edge_keys[page_id], 2):
            first, second = route_points[first_key], route_points[second_key]
            shared += layout_geometry.shared_route_length(first, second)
            crossings += sum(
                int(_proper_cross(left, right))
                for left in layout_geometry.route_segments(first)
                for right in layout_geometry.route_segments(second)
            )
    return {
        "crossings": crossings,
        "overlaps": overlaps,
        "route_length": float(sum(layout_geometry.manhattan_length(points) for points in route_points.values())),
        "bend_count": sum(layout_geometry.bend_count(points) for points in route_points.values()),
        "shared_route_length": float(shared),
        "label_collisions": int(label_collisions),
    }


def layout(request: Mapping[str, Any]) -> dict:
    """Return a strict, request-bound deterministic layout result."""
    layout_contracts.require_layout_request(request)
    pages = request.get("pages")
    if not isinstance(pages, list):
        raise ValueError("layout request requires pages")
    page_layers: dict[str, dict[str, int]] = {}
    for page in sorted((page for page in pages if isinstance(page, Mapping)), key=lambda page: str(page.get("page_id", ""))):
        page_id, nodes, edges = page.get("page_id"), page.get("nodes"), page.get("edges")
        if not isinstance(page_id, str) or not isinstance(nodes, list) or not isinstance(edges, list):
            raise ValueError("layout page requires page_id, nodes and edges")
        feedback = choose_feedback_edges(nodes, edges)
        raw_layers = assign_layers(nodes, edges, feedback)
        page_layers[page_id] = raw_layers
    bounds = assign_coordinates(request, page_layers)
    ports = allocate_ports(request, bounds)
    routes = route_edges(request, bounds, ports)
    labels, label_collisions = place_edge_labels(request, bounds, routes)
    result_pages = []
    for page in sorted((page for page in pages if isinstance(page, Mapping)), key=lambda page: str(page.get("page_id", ""))):
        page_id = str(page["page_id"])
        nodes = [
            {
                "node_id": _node_id(node),
                **bounds[f"{page_id}/{_node_id(node)}"],
                "locked": bool(node.get("locked")),
            }
            for node in sorted(page["nodes"], key=_node_id)
        ]
        edges = []
        reservations = []
        for edge in _ordered_edges(page["edges"]):
            edge_key = f"{page_id}/{_edge_id(edge)}"
            result_edge = dict(routes[edge_key])
            if edge_key in labels:
                result_edge["label_bounds"] = labels[edge_key]
            edges.append(result_edge)
            route_points = [
                (float(point["x"]), float(point["y"]))
                for point in result_edge["waypoints"]
            ]
            reservations.extend({
                "edge_id": result_edge["edge_id"],
                "start": {"x": float(start[0]), "y": float(start[1])},
                "end": {"x": float(end[0]), "y": float(end[1])},
            } for start, end in layout_geometry.route_segments(route_points))
        result_pages.append({
            "page_id": page_id,
            "name": str(page.get("name", "")),
            "nodes": nodes,
            "edges": edges,
            "channel_reservations": reservations,
        })
    request_sha256 = canonical_json_sha256(request)
    result = {
        "schema_version": 1,
        "result_id": "result-" + request_sha256[:16],
        "request_sha256": request_sha256,
        "backend": "python-layered",
        "pages": result_pages,
        "metrics": _layout_metrics(request, bounds, routes, label_collisions=label_collisions),
    }
    layout_contracts.require_layout_result(result, expected_request_sha256=request_sha256)
    return result
