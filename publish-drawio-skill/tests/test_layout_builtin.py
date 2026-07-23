import copy
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import layout_builtin


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


def _edge(edge_id, source, target):
    return {
        "edge_id": edge_id,
        "source": source,
        "target": target,
        "edge_class": "main",
        "source_port": "east",
        "target_port": "west",
        "locked": False,
    }


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
        xs = {node["bounds"]["x"] for node in first["pages"][0]["nodes"]}
        self.assertGreater(len(xs), 1)


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


if __name__ == "__main__":
    unittest.main()
