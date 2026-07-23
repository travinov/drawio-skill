import copy
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import layout_builtin
import layout_contracts
import layout_geometry
from lifecycle_contracts import canonical_json_sha256


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "layout"
SHA = "a" * 64


def _node(node_id, *, x=0, y=0, width=100, height=60, locked=False, parent_id=None):
    value = {
        "node_id": node_id,
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "locked": locked,
    }
    if parent_id is not None:
        value["parent_id"] = parent_id
    return value


def _edge(edge_id, source, target, *, edge_class="main", locked=False, waypoints=None,
          source_port="east", target_port="west", label_size=None):
    value = {
        "edge_id": edge_id,
        "source": source,
        "target": target,
        "edge_class": edge_class,
        "source_port": source_port,
        "target_port": target_port,
        "locked": locked,
    }
    if waypoints is not None:
        value["waypoints"] = copy.deepcopy(waypoints)
    if label_size is not None:
        value["label_size"] = copy.deepcopy(label_size)
    return value


def layout_request(*, direction="TB", nodes=None, edges=None):
    nodes = nodes or [_node("start"), _node("left"), _node("right"), _node("end")]
    edges = edges or [
        _edge("start-left", "start", "left"),
        _edge("start-right", "start", "right"),
        _edge("left-end", "left", "end"),
        _edge("right-end", "right", "end"),
    ]
    return {
        "schema_version": 1,
        "request_id": "request-layout-builtin",
        "run_id": "run-layout-builtin",
        "semantic_plan_sha256": SHA,
        "diagram_type": "flowchart",
        "direction": direction,
        "mode": "create",
        "backend": "builtin-layered-v1",
        "strategy": "layered",
        "quality_profile_version": 2,
        "pages": [{"page_id": "page-a", "name": "A", "nodes": nodes, "edges": edges}],
        "scope": {
            "page_ids": ["page-a"],
            "node_refs": [{"page_id": "page-a", "cell_id": node["node_id"]} for node in nodes],
            "edge_refs": [{"page_id": "page-a", "cell_id": edge["edge_id"]} for edge in edges],
            "movable_node_refs": [{"page_id": "page-a", "cell_id": node["node_id"]} for node in nodes],
            "reroutable_edge_refs": [{"page_id": "page-a", "cell_id": edge["edge_id"]} for edge in edges],
        },
        "constraints": {"grid_size": 10, "node_separation": 40, "layer_separation": 80},
    }


class LayoutBuiltinTests(unittest.TestCase):
    def test_tarjan_sccs_and_feedback_edge_selection_are_stable(self):
        fixture = json.loads((FIXTURES / "cycle.json").read_text(encoding="utf-8"))
        self.assertEqual(
            layout_builtin.strongly_connected_components(fixture["nodes"], fixture["edges"]),
            [("a", "b", "c"), ("d",)],
        )
        self.assertEqual(
            layout_builtin.choose_feedback_edges(fixture["nodes"], fixture["edges"]),
            {"cycle-ca"},
        )

    def test_longest_path_layers_ignore_the_deterministic_feedback_edge(self):
        fixture = json.loads((FIXTURES / "cycle.json").read_text(encoding="utf-8"))
        feedback = layout_builtin.choose_feedback_edges(fixture["nodes"], fixture["edges"])
        self.assertEqual(
            layout_builtin.assign_layers(fixture["nodes"], fixture["edges"], feedback),
            {"a": 0, "b": 1, "c": 2, "d": 3},
        )

    def test_crossing_minimization_has_stable_four_sweep_order(self):
        layers = {0: ["a", "b"], 1: ["right", "left"], 2: ["end"]}
        edges = [
            _edge("a-left", "a", "left"),
            _edge("b-right", "b", "right"),
            _edge("left-end", "left", "end"),
            _edge("right-end", "right", "end"),
        ]
        self.assertEqual(
            layout_builtin.minimize_crossings(layers, edges),
            [["a", "b"], ["left", "right"], ["end"]],
        )

    def test_coordinate_assignment_supports_tb_and_lr_without_overlap(self):
        request = layout_request()
        layers = {"start": 0, "left": 1, "right": 1, "end": 2}
        tb = layout_builtin.assign_coordinates(request, layers)
        lr = layout_builtin.assign_coordinates(layout_request(direction="LR"), layers)
        self.assertLess(tb["page-a/start"]["y"], tb["page-a/left"]["y"])
        self.assertLess(lr["page-a/start"]["x"], lr["page-a/left"]["x"])
        self.assertFalse(_overlap(tb["page-a/left"], tb["page-a/right"]))
        self.assertFalse(_overlap(lr["page-a/left"], lr["page-a/right"]))

    def test_locked_coordinates_are_never_moved_and_expand_spacing(self):
        request = layout_request(nodes=[
            _node("start", x=0, y=0, locked=True),
            _node("left", x=0, y=0),
            _node("right", x=0, y=0),
            _node("end", x=900, y=0, locked=True),
        ])
        layers = {"start": 0, "left": 1, "right": 1, "end": 2}
        bounds = layout_builtin.assign_coordinates(request, layers)
        self.assertEqual(bounds["page-a/start"]["x"], 0)
        self.assertEqual(bounds["page-a/start"]["y"], 0)
        self.assertEqual(bounds["page-a/end"]["x"], 900)
        self.assertEqual(bounds["page-a/end"]["y"], 0)
        self.assertFalse(_overlap(bounds["page-a/left"], bounds["page-a/right"]))

    def test_locked_parent_contains_multiple_movable_children_without_moving(self):
        parent = _node("parent", x=100, y=100, width=500, height=350, locked=True)
        request = layout_request(nodes=[
            parent,
            _node("child-a", width=120, height=70, parent_id="parent"),
            _node("child-b", width=120, height=70, parent_id="parent"),
        ], edges=[_edge("a-b", "child-a", "child-b")])
        before = {"x": 100, "y": 100, "width": 500, "height": 350}
        bounds = layout_builtin.assign_coordinates(request, {"parent": 0, "child-a": 1, "child-b": 2})
        self.assertEqual(bounds["page-a/parent"], before)
        self.assertTrue(_contains(bounds["page-a/parent"], bounds["page-a/child-a"]))
        self.assertTrue(_contains(bounds["page-a/parent"], bounds["page-a/child-b"]))
        self.assertFalse(_overlap(bounds["page-a/child-a"], bounds["page-a/child-b"]))

    def test_locked_outer_parent_contains_nested_unlocked_subtree(self):
        request = layout_request(nodes=[
            _node("outer", x=100, y=100, width=600, height=500, locked=True),
            _node("inner", width=200, height=120, parent_id="outer"),
            _node("first", width=110, height=70, parent_id="inner"),
            _node("second", width=110, height=70, parent_id="inner"),
        ], edges=[_edge("first-second", "first", "second")])
        bounds = layout_builtin.assign_coordinates(request, {"outer": 0, "inner": 1, "first": 2, "second": 3})
        outer = bounds["page-a/outer"]
        for node_id in ("inner", "first", "second"):
            self.assertTrue(_contains(outer, bounds[f"page-a/{node_id}"]))

    def test_insufficient_locked_parent_capacity_fails_closed(self):
        request = layout_request(nodes=[
            _node("parent", x=0, y=0, width=180, height=120, locked=True),
            _node("child-a", width=100, height=60, parent_id="parent"),
            _node("child-b", width=100, height=60, parent_id="parent"),
        ], edges=[_edge("a-b", "child-a", "child-b")])
        with self.assertRaisesRegex(ValueError, "locked parent.*capacity"):
            layout_builtin.assign_coordinates(request, {"parent": 0, "child-a": 1, "child-b": 2})

    def test_locked_descendant_is_unchanged_or_rejected_when_outside_locked_parent(self):
        valid = layout_request(nodes=[
            _node("parent", x=0, y=0, width=400, height=300, locked=True),
            _node("child", x=100, y=100, width=100, height=60, locked=True, parent_id="parent"),
        ], edges=[])
        bounds = layout_builtin.assign_coordinates(valid, {"parent": 0, "child": 1})
        self.assertEqual(bounds["page-a/child"], {"x": 100, "y": 100, "width": 100, "height": 60})
        invalid = layout_request(nodes=[
            _node("parent", x=0, y=0, width=200, height=120, locked=True),
            _node("child", x=150, y=80, width=100, height=60, locked=True, parent_id="parent"),
        ], edges=[])
        with self.assertRaisesRegex(ValueError, "locked parent.*locked child"):
            layout_builtin.assign_coordinates(invalid, {"parent": 0, "child": 1})

    def test_nested_containers_and_lanes_keep_children_inside_parents(self):
        fixture = json.loads((FIXTURES / "nested-containers.json").read_text(encoding="utf-8"))
        request = layout_request(nodes=fixture["nodes"], edges=fixture["edges"])
        layers = {"lane": 0, "group": 0, "task-a": 1, "task-b": 2, "outside": 3}
        bounds = layout_builtin.assign_coordinates(request, layers)
        self.assertTrue(_contains(bounds["page-a/lane"], bounds["page-a/group"]))
        self.assertTrue(_contains(bounds["page-a/group"], bounds["page-a/task-a"]))
        self.assertTrue(_contains(bounds["page-a/group"], bounds["page-a/task-b"]))

    def test_layout_is_deterministic_and_branching_nodes_do_not_collapse(self):
        request = layout_request()
        first = layout_builtin.layout(request)
        second = layout_builtin.layout(copy.deepcopy(request))
        self.assertEqual(first, second)
        xs = {node["x"] for node in first["pages"][0]["nodes"]}
        self.assertGreater(len(xs), 1)

    def test_degree_aware_ports_are_distinct_normalized_and_page_scoped(self):
        fixture = json.loads((FIXTURES / "fan-in-out.json").read_text(encoding="utf-8"))
        request = layout_request(nodes=fixture["nodes"], edges=fixture["edges"])
        request["pages"].append({
            "page_id": "page-b",
            "name": "B",
            "nodes": copy.deepcopy(fixture["nodes"]),
            "edges": copy.deepcopy(fixture["edges"]),
        })
        request["scope"] = _scope_for_pages(request["pages"])
        bounds = _fixture_bounds(request)
        ports = layout_builtin.allocate_ports(request, bounds)
        page_a_pins = [
            ports[f"page-a/{edge_id}"]["source"]["position"]
            for edge_id in ("fan-out-1", "fan-out-2", "fan-out-3")
        ]
        self.assertEqual(page_a_pins, sorted(page_a_pins))
        self.assertEqual(len(set(page_a_pins)), 3)
        self.assertTrue(all(0.1 <= pin <= 0.9 for pin in page_a_pins))
        self.assertIn("page-b/fan-out-1", ports)
        self.assertNotEqual(
            ports["page-a/fan-out-1"]["source"]["point"],
            ports["page-a/fan-out-3"]["source"]["point"],
        )

    def test_visibility_route_avoids_expanded_obstacle_and_is_manhattan(self):
        fixture = json.loads((FIXTURES / "routing-obstacles.json").read_text(encoding="utf-8"))
        request = layout_request(direction="LR", nodes=fixture["nodes"], edges=fixture["edges"])
        bounds = _fixture_bounds(request)
        ports = layout_builtin.allocate_ports(request, bounds)
        routes = layout_builtin.route_edges(request, bounds, ports)
        points = _points(routes["page-a/across"]["waypoints"])
        self.assertTrue(layout_geometry.is_manhattan(points))
        obstacle = bounds["page-a/obstacle"]
        obstacle_rect = (obstacle["x"], obstacle["y"], obstacle["width"], obstacle["height"])
        self.assertTrue(all(
            not layout_geometry.segment_hits_rect(segment, obstacle_rect, clearance=10)
            for segment in layout_geometry.route_segments(points)
        ))

    def test_self_loop_and_feedback_use_external_channels(self):
        fixture = json.loads((FIXTURES / "return-loop.json").read_text(encoding="utf-8"))
        request = layout_request(direction="TB", nodes=fixture["nodes"], edges=fixture["edges"])
        bounds = _fixture_bounds(request)
        ports = layout_builtin.allocate_ports(request, bounds)
        routes = layout_builtin.route_edges(request, bounds, ports)
        loop = _points(routes["page-a/self-loop"]["waypoints"])
        node = bounds["page-a/retry"]
        self.assertTrue(any(
            x < node["x"] or x > node["x"] + node["width"]
            or y < node["y"] or y > node["y"] + node["height"]
            for x, y in loop[1:-1]
        ))
        feedback = _points(routes["page-a/feedback"]["waypoints"])
        leftmost = min(item["x"] for item in bounds.values())
        self.assertLess(min(x for x, _ in feedback), leftmost)
        self.assertTrue(layout_geometry.is_manhattan(loop))
        self.assertTrue(layout_geometry.is_manhattan(feedback))

    def test_reserved_segments_nudge_parallel_routes_apart(self):
        nodes = [
            _node("source", x=0, y=100, locked=True),
            _node("target", x=400, y=100, locked=True),
        ]
        edges = [
            _edge("parallel-a", "source", "target"),
            _edge("parallel-b", "source", "target"),
        ]
        request = layout_request(direction="LR", nodes=nodes, edges=edges)
        bounds = _fixture_bounds(request)
        ports = layout_builtin.allocate_ports(request, bounds)
        routes = layout_builtin.route_edges(request, bounds, ports)
        first = _points(routes["page-a/parallel-a"]["waypoints"])
        second = _points(routes["page-a/parallel-b"]["waypoints"])
        self.assertNotEqual(first, second)
        self.assertEqual(layout_geometry.shared_route_length(first, second), 0)

    def test_label_bounds_avoid_nodes_and_other_labels_or_report_collision(self):
        nodes = [
            _node("source", x=0, y=0, locked=True),
            _node("target", x=400, y=0, locked=True),
            _node("blocker", x=190, y=-10, width=120, height=80, locked=True),
        ]
        edges = [
            _edge("labeled-a", "source", "target", label_size={"width": 80, "height": 20}),
            _edge("labeled-b", "source", "target", label_size={"width": 80, "height": 20}),
        ]
        request = layout_request(direction="LR", nodes=nodes, edges=edges)
        bounds = _fixture_bounds(request)
        ports = layout_builtin.allocate_ports(request, bounds)
        routes = layout_builtin.route_edges(request, bounds, ports)
        labels, collisions = layout_builtin.place_edge_labels(request, bounds, routes)
        rectangles = []
        for edge_id in ("labeled-a", "labeled-b"):
            label = labels[f"page-a/{edge_id}"]
            rect = (label["x"], label["y"], label["width"], label["height"])
            for node_bounds in bounds.values():
                node_rect = (
                    node_bounds["x"], node_bounds["y"],
                    node_bounds["width"], node_bounds["height"],
                )
                self.assertFalse(layout_geometry.rects_overlap(rect, node_rect))
            self.assertTrue(all(not layout_geometry.rects_overlap(rect, prior) for prior in rectangles))
            rectangles.append(rect)
        self.assertEqual(collisions, 0)

    def test_label_reservations_are_independent_between_pages(self):
        edge = _edge("label", "source", "target", label_size={"width": 80, "height": 20})
        page = {
            "page_id": "page-a",
            "name": "A",
            "nodes": [
                _node("source", x=0, y=0, locked=True),
                _node("target", x=300, y=0, locked=True),
            ],
            "edges": [edge],
        }
        request = layout_request(nodes=page["nodes"], edges=page["edges"], direction="LR")
        second = copy.deepcopy(page)
        second["page_id"] = "page-b"
        request["pages"].append(second)
        request["scope"] = _scope_for_pages(request["pages"])
        bounds = _fixture_bounds(request)
        ports = layout_builtin.allocate_ports(request, bounds)
        routes = layout_builtin.route_edges(request, bounds, ports)
        labels, collisions = layout_builtin.place_edge_labels(request, bounds, routes)
        self.assertEqual(labels["page-a/label"], labels["page-b/label"])
        self.assertEqual(collisions, 0)

    def test_locked_manual_route_and_port_sides_are_preserved(self):
        waypoints = [
            {"x": 100, "y": 30},
            {"x": 150, "y": 30},
            {"x": 150, "y": 230},
            {"x": 300, "y": 230},
        ]
        nodes = [
            _node("source", x=0, y=0, locked=True),
            _node("target", x=300, y=200, locked=True),
        ]
        edge = _edge(
            "manual", "source", "target", locked=True, waypoints=waypoints,
            source_port="east", target_port="west",
        )
        request = layout_request(direction="LR", nodes=nodes, edges=[edge])
        bounds = _fixture_bounds(request)
        ports = layout_builtin.allocate_ports(request, bounds)
        routes = layout_builtin.route_edges(request, bounds, ports)
        result = routes["page-a/manual"]
        self.assertEqual(result["waypoints"], waypoints)
        self.assertEqual(result["source_port"], "east")
        self.assertEqual(result["target_port"], "west")

    def test_locked_route_crossing_third_node_fails_closed(self):
        waypoints = [{"x": 100, "y": 30}, {"x": 300, "y": 30}]
        request = layout_request(
            direction="LR",
            nodes=[
                _node("source", x=0, y=0, locked=True),
                _node("obstacle", x=140, y=0, width=100, height=60, locked=True),
                _node("target", x=300, y=0, locked=True),
            ],
            edges=[_edge("manual", "source", "target", locked=True, waypoints=waypoints)],
        )
        bounds = _fixture_bounds(request)
        ports = layout_builtin.allocate_ports(request, bounds)
        with self.assertRaisesRegex(ValueError, "locked edge.*obstacle"):
            layout_builtin.route_edges(request, bounds, ports)

    def test_locked_route_crossing_unrelated_container_fails_closed(self):
        waypoints = [{"x": 100, "y": 30}, {"x": 400, "y": 30}]
        request = layout_request(
            direction="LR",
            nodes=[
                _node("source", x=0, y=0, locked=True),
                _node("unrelated-container", x=160, y=-20, width=140, height=120, locked=True),
                _node("container-child", x=180, y=70, width=80, height=20,
                      locked=True, parent_id="unrelated-container"),
                _node("target", x=400, y=0, locked=True),
            ],
            edges=[_edge("manual", "source", "target", locked=True, waypoints=waypoints)],
        )
        bounds = _fixture_bounds(request)
        ports = layout_builtin.allocate_ports(request, bounds)
        with self.assertRaisesRegex(ValueError, "locked edge.*unrelated-container"):
            layout_builtin.route_edges(request, bounds, ports)

    def test_locked_route_inside_legitimate_shared_ancestor_is_preserved(self):
        waypoints = [{"x": 150, "y": 110}, {"x": 350, "y": 110}]
        request = layout_request(
            direction="LR",
            nodes=[
                _node("shared", x=0, y=0, width=500, height=240, locked=True),
                _node("source", x=50, y=80, locked=True, parent_id="shared"),
                _node("target", x=350, y=80, locked=True, parent_id="shared"),
            ],
            edges=[_edge("manual", "source", "target", locked=True, waypoints=waypoints)],
        )
        bounds = _fixture_bounds(request)
        ports = layout_builtin.allocate_ports(request, bounds)
        result = layout_builtin.route_edges(request, bounds, ports)["page-a/manual"]
        self.assertEqual(result["waypoints"], waypoints)

    def test_locked_route_rejects_endpoint_on_wrong_declared_side(self):
        waypoints = [
            {"x": 50, "y": 0},
            {"x": 50, "y": 30},
            {"x": 300, "y": 30},
        ]
        request = layout_request(
            direction="LR",
            nodes=[
                _node("source", x=0, y=0, locked=True),
                _node("target", x=300, y=0, locked=True),
            ],
            edges=[_edge(
                "manual", "source", "target", locked=True, waypoints=waypoints,
                source_port="east", target_port="west",
            )],
        )
        with self.assertRaisesRegex(ValueError, "source endpoint.*east"):
            layout_builtin.allocate_ports(request, _fixture_bounds(request))

    def test_locked_route_rejects_endpoint_not_on_node_boundary(self):
        waypoints = [
            {"x": 90, "y": 30},
            {"x": 300, "y": 30},
        ]
        request = layout_request(
            direction="LR",
            nodes=[
                _node("source", x=0, y=0, locked=True),
                _node("target", x=300, y=0, locked=True),
            ],
            edges=[_edge(
                "manual", "source", "target", locked=True, waypoints=waypoints,
                source_port="east", target_port="west",
            )],
        )
        with self.assertRaisesRegex(ValueError, "source endpoint.*boundary"):
            layout_builtin.allocate_ports(request, _fixture_bounds(request))

    def test_unlocked_port_does_not_reuse_a_locked_pin_on_the_same_side(self):
        manual = [
            {"x": 100, "y": 30},
            {"x": 200, "y": 30},
        ]
        nodes = [
            _node("source", x=0, y=0, locked=True),
            _node("locked-target", x=200, y=0, locked=True),
            _node("free-target", x=200, y=120, locked=True),
        ]
        edges = [
            _edge("locked", "source", "locked-target", locked=True, waypoints=manual),
            _edge("free", "source", "free-target"),
        ]
        request = layout_request(direction="LR", nodes=nodes, edges=edges)
        ports = layout_builtin.allocate_ports(request, _fixture_bounds(request))
        self.assertEqual(ports["page-a/locked"]["source"]["position"], 0.5)
        self.assertNotEqual(
            ports["page-a/free"]["source"]["position"],
            ports["page-a/locked"]["source"]["position"],
        )

    def test_metrics_do_not_count_intentional_parent_containment_as_node_overlap(self):
        fixture = json.loads((FIXTURES / "nested-containers.json").read_text(encoding="utf-8"))
        request = layout_request(nodes=fixture["nodes"], edges=fixture["edges"])
        result = layout_builtin.layout(request)
        self.assertEqual(result["metrics"]["overlaps"], 0)

    def test_layout_returns_contract_valid_hash_bound_multi_page_result(self):
        request = layout_request()
        second = copy.deepcopy(request["pages"][0])
        second["page_id"] = "page-b"
        second["name"] = "B"
        request["pages"].append(second)
        request["scope"] = _scope_for_pages(request["pages"])
        first = layout_builtin.layout(request)
        second_result = layout_builtin.layout(copy.deepcopy(request))
        self.assertEqual(first, second_result)
        self.assertEqual(first["request_sha256"], canonical_json_sha256(request))
        self.assertEqual(first["backend"], "python-layered")
        self.assertEqual([page["page_id"] for page in first["pages"]], ["page-a", "page-b"])
        for page in first["pages"]:
            self.assertEqual(
                [edge["edge_id"] for edge in page["edges"]],
                sorted(edge["edge_id"] for edge in page["edges"]),
            )
            self.assertTrue(all(
                layout_geometry.is_manhattan(_points(edge["waypoints"]))
                for edge in page["edges"]
            ))
        self.assertEqual(
            layout_contracts.validate_layout_result(
                first, expected_request_sha256=canonical_json_sha256(request)
            ),
            [],
        )
        self.assertEqual(
            set(first["metrics"]),
            {
                "crossings", "overlaps", "route_length", "bend_count",
                "shared_route_length", "label_collisions",
            },
        )
        self.assertEqual(first["metrics"]["overlaps"], 0)
        self.assertEqual(first["metrics"]["shared_route_length"], 0)


def _overlap(first, second):
    return not (
        first["x"] + first["width"] <= second["x"]
        or second["x"] + second["width"] <= first["x"]
        or first["y"] + first["height"] <= second["y"]
        or second["y"] + second["height"] <= first["y"]
    )


def _contains(parent, child):
    return (
        parent["x"] <= child["x"]
        and parent["y"] <= child["y"]
        and parent["x"] + parent["width"] >= child["x"] + child["width"]
        and parent["y"] + parent["height"] >= child["y"] + child["height"]
    )


def _fixture_bounds(request):
    return {
        f"{page['page_id']}/{node['node_id']}": {
            "x": float(node["x"]),
            "y": float(node["y"]),
            "width": float(node["width"]),
            "height": float(node["height"]),
        }
        for page in request["pages"]
        for node in page["nodes"]
    }


def _points(waypoints):
    return [(float(point["x"]), float(point["y"])) for point in waypoints]


def _scope_for_pages(pages):
    nodes = [
        {"page_id": page["page_id"], "cell_id": node["node_id"]}
        for page in pages for node in page["nodes"]
    ]
    edges = [
        {"page_id": page["page_id"], "cell_id": edge["edge_id"]}
        for page in pages for edge in page["edges"]
    ]
    return {
        "page_ids": sorted(page["page_id"] for page in pages),
        "node_refs": nodes,
        "edge_refs": edges,
        "movable_node_refs": nodes,
        "reroutable_edge_refs": edges,
    }


if __name__ == "__main__":
    unittest.main()
