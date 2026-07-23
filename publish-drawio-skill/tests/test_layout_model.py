import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import layout_model
from lifecycle_contracts import canonical_json_sha256


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "layout"
SHA = "a" * 64


def semantic_plan():
    return json.loads((FIXTURES / "order-process-plan.json").read_text(encoding="utf-8"))


def baseline():
    return {
        "pages": [
            {
                "id": "page-a",
                "name": "A page",
                "cells": [
                    {
                        "id": "a-node", "kind": "vertex", "semantic_type": "process",
                        "label": "A", "geometry": {"bounds": {"x": 10, "y": 20, "width": 160, "height": 70}},
                        "hash": "1" * 64,
                    },
                    {
                        "id": "z-node", "kind": "vertex", "semantic_type": "decision",
                        "label": "Z", "geometry": {"bounds": {"x": 260, "y": 10, "width": 140, "height": 90}},
                        "hash": "3" * 64,
                    },
                    {
                        "id": "a-edge", "kind": "edge", "source_id": "a-node", "target_id": "z-node",
                        "geometry": {"waypoints": [{"x": 170, "y": 55}, {"x": 260, "y": 55}]},
                        "hash": "2" * 64,
                    },
                    {
                        "id": "z-edge", "kind": "edge", "source_id": "z-node", "target_id": "z-node",
                        "geometry": {"waypoints": [{"x": 400, "y": 55}, {"x": 430, "y": 55}, {"x": 430, "y": 100}]},
                        "hash": "4" * 64,
                    },
                ],
            },
            {"id": "page-z", "name": "Z page", "cells": []},
        ]
    }


class LayoutModelTests(unittest.TestCase):
    def build_create(self):
        return layout_model.build_layout_request(
            semantic_plan(),
            run_id="run-1",
            semantic_plan_sha256=SHA,
            mode="create",
            backend="builtin-layered-v1",
            strategy_id="layered",
            quality_profile_version=2,
        )

    def test_create_is_page_and_cell_stably_ordered_without_generated_routes(self):
        request = self.build_create()
        self.assertEqual([page["page_id"] for page in request["pages"]], ["page-a", "page-z"])
        self.assertEqual([node["node_id"] for node in request["pages"][0]["nodes"]], ["a-node", "z-node"])
        self.assertEqual([edge["edge_id"] for edge in request["pages"][0]["edges"]], ["a-edge", "z-edge"])
        self.assertNotIn("waypoints", request["pages"][0]["edges"][0])
        self.assertFalse(request["pages"][0]["edges"][0]["locked"])

    def test_node_and_edge_label_measurements_are_grid_normalized_and_deterministic(self):
        first = self.build_create()
        second = self.build_create()
        node = next(node for page in first["pages"] for node in page["nodes"] if node["node_id"] == "z-node")
        edge = next(edge for page in first["pages"] for edge in page["edges"] if edge["edge_id"] == "a-edge")
        self.assertEqual(node["width"] % layout_model.GRID, 0)
        self.assertEqual(node["height"] % layout_model.GRID, 0)
        self.assertEqual(edge["label_size"]["width"] % layout_model.GRID, 0)
        self.assertEqual(edge["label_size"]["height"] % layout_model.GRID, 0)
        self.assertEqual(first, second)

    def test_edge_classification_is_stable_for_feedback_and_self_loop(self):
        request = self.build_create()
        edges = {edge["edge_id"]: edge for page in request["pages"] for edge in page["edges"]}
        self.assertEqual(edges["a-edge"]["edge_class"], "feedback")
        self.assertEqual(edges["z-edge"]["edge_class"], "self_loop")

    def test_improve_starts_edge_only_and_locks_every_other_baseline_cell_with_hashes_and_routes(self):
        request = layout_model.build_layout_request(
            semantic_plan(),
            run_id="run-1",
            semantic_plan_sha256=SHA,
            mode="local_reflow",
            backend="builtin-layered-v1",
            strategy_id="layered",
            quality_profile_version=2,
            baseline=baseline(),
            scope={"edge_ids": ["a-edge"]},
        )
        self.assertEqual(request["scope"]["edge_ids"], ["a-edge"])
        self.assertEqual(request["scope"]["movable_nodes"], [])
        self.assertEqual(request["scope"]["reroutable_edges"], ["a-edge"])
        nodes = {node["node_id"]: node for page in request["pages"] for node in page["nodes"]}
        edges = {edge["edge_id"]: edge for page in request["pages"] for edge in page["edges"]}
        self.assertTrue(nodes["a-node"]["locked"])
        self.assertTrue(nodes["z-node"]["locked"])
        self.assertEqual(nodes["a-node"]["element_sha256"], "1" * 64)
        self.assertTrue(edges["z-edge"]["locked"])
        self.assertEqual(edges["z-edge"]["element_sha256"], "4" * 64)
        self.assertIn("waypoints", edges["z-edge"])
        self.assertFalse(edges["a-edge"]["locked"])
        self.assertEqual(edges["a-edge"]["element_sha256"], "2" * 64)

    def test_expansion_progresses_only_from_edge_to_adjacent_nodes_then_layer_then_component(self):
        spec = baseline()
        edge = {"edge_ids": ["a-edge"]}
        adjacent = layout_model.expand_scope(spec, edge, "adjacent_nodes")
        self.assertEqual(adjacent["movable_nodes"], ["a-node", "z-node"])
        layer = layout_model.expand_scope(spec, adjacent, "layer")
        self.assertEqual(layer["page_ids"], ["page-a"])
        self.assertEqual(layer["node_ids"], ["a-node", "z-node"])
        component = layout_model.expand_scope(spec, layer, "component")
        self.assertEqual(component["edge_ids"], ["a-edge", "z-edge"])
        with self.assertRaises(ValueError):
            layout_model.expand_scope(spec, edge, "component")

    def test_findings_infer_edge_only_scope(self):
        scope = layout_model.infer_scope_from_findings(
            baseline(),
            [{"edge_id": "a-edge", "code": "route_through"}],
        )
        self.assertEqual(scope["edge_ids"], ["a-edge"])
        self.assertEqual(scope["movable_nodes"], [])
        self.assertEqual(scope["reroutable_edges"], ["a-edge"])

    def test_findings_accept_v2_page_scoped_edge_references(self):
        spec = {
            "pages": [{
                "id": "page-1",
                "cells": [
                    {"id": "left", "kind": "vertex"},
                    {"id": "right", "kind": "vertex"},
                    {
                        "id": "edge-1", "kind": "edge",
                        "source": {"page_id": "page-1", "cell_id": "left"},
                        "target": {"page_id": "page-1", "cell_id": "right"},
                    },
                ],
            }]
        }
        scope = layout_model.infer_scope_from_findings(spec, [{"edge_id": "edge-1"}])
        self.assertEqual(scope["edge_ids"], ["edge-1"])
        self.assertEqual(layout_model.expand_scope(spec, scope, "adjacent_nodes")["movable_nodes"], ["left", "right"])

    def test_identical_semantic_plan_has_identical_canonical_json_and_sha256(self):
        first = self.build_create()
        second = layout_model.build_layout_request(
            copy.deepcopy(semantic_plan()),
            run_id="run-1",
            semantic_plan_sha256=SHA,
            mode="create",
            backend="builtin-layered-v1",
            strategy_id="layered",
            quality_profile_version=2,
        )
        self.assertEqual(canonical_json_sha256(first), canonical_json_sha256(second))
        self.assertEqual(
            hashlib.sha256(json.dumps(first, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            hashlib.sha256(json.dumps(second, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        )


if __name__ == "__main__":
    unittest.main()
