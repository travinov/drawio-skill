import copy
import json
import sys
import unittest
from pathlib import Path

import jsonschema


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import layout_contracts
import lifecycle_contracts


SHA = "a" * 64


def valid_intake():
    return {
        "schema_version": 1, "intake_id": "intake-1", "mode": "create",
        "request_sha256": SHA, "status": "complete", "classification": "flowchart",
        "questions": [], "answers": [], "assumptions": [], "completeness": 1,
    }


def valid_intake_analysis():
    return {"schema_version": 1, "role": "semantic_analyst", "status": "ok", "result": valid_intake()}


def valid_layout_request(mode="create"):
    return {
        "schema_version": 1, "request_id": "request-1", "run_id": "run-1",
        "semantic_plan_sha256": SHA, "diagram_type": "flowchart", "direction": "LR",
        "mode": mode, "backend": "builtin", "strategy": "layered",
        "quality_profile_version": "v1",
        "pages": [{
            "page_id": "page-1", "name": "Page 1",
            "nodes": [
                {"node_id": "node-1", "x": 0, "y": 0, "width": 100, "height": 60, "locked": True},
                {"node_id": "node-2", "x": 200, "y": 0, "width": 100, "height": 60, "locked": False},
            ],
            "edges": [{
                "edge_id": "edge-1", "source": "node-1", "target": "node-2", "edge_class": "main",
                "source_port": "east", "target_port": "west",
                "waypoints": [{"x": 100, "y": 30}, {"x": 200, "y": 30}],
            }],
        }],
        "scope": {"page_ids": ["page-1"], "node_ids": ["node-1", "node-2"], "edge_ids": ["edge-1"], "movable_nodes": ["node-2"], "reroutable_edges": ["edge-1"]},
        "constraints": {"grid_size": 10, "node_separation": 40, "layer_separation": 80},
    }


def valid_layout_result():
    return {
        "schema_version": 1, "result_id": "result-1", "request_sha256": SHA, "backend": "builtin",
        "pages": valid_layout_request()["pages"],
        "metrics": {"crossings": 0, "overlaps": 0, "route_length": 100},
    }


def valid_repair_intent():
    return {
        "schema_version": 1, "role": "repair", "status": "ok", "run_id": "run-1",
        "baseline_sha256": SHA,
        "result": {"action": "reroute_edges", "page_id": "page-1", "node_ids": [], "edge_ids": ["edge-1"], "reason": "avoid crossing"},
    }


class LayoutContractTests(unittest.TestCase):
    def test_schemas_compile_and_accept_positive_documents(self):
        fixtures = {
            "diagram-intake": valid_intake(),
            "diagram-intake-analysis": valid_intake_analysis(),
            "layout-request": valid_layout_request(),
            "layout-result": valid_layout_result(),
            "layout-repair-intent": valid_repair_intent(),
        }
        for kind, document in fixtures.items():
            with self.subTest(kind=kind):
                schema = lifecycle_contracts.load_schema(kind, 1)
                jsonschema.Draft202012Validator.check_schema(schema)
                self.assertEqual(lifecycle_contracts.validate_contract(document, kind, 1), [])

    def test_all_contract_owned_objects_reject_unknown_fields(self):
        value = valid_layout_request()
        value["unknown"] = True
        self.assertTrue(layout_contracts.validate_layout_request(value))

    def test_layout_result_rejects_diagonal_route(self):
        value = valid_layout_result()
        value["pages"][0]["edges"][0]["waypoints"] = [
            {"x": 100, "y": 100},
            {"x": 140, "y": 130},
        ]
        diagnostics = layout_contracts.validate_layout_result(value)
        self.assertIn("waypoints", json.dumps(diagnostics))

    def test_layout_request_rejects_unlocked_out_of_scope_node(self):
        value = valid_layout_request(mode="local_reflow")
        value["pages"][0]["nodes"][1]["locked"] = False
        value["scope"]["movable_nodes"] = []
        diagnostics = layout_contracts.validate_layout_request(value)
        self.assertTrue(diagnostics)

    def test_layout_request_rejects_movable_node_outside_declared_scope(self):
        value = valid_layout_request(mode="local_reflow")
        value["scope"]["movable_nodes"] = ["outside"]
        codes = {item["code"] for item in layout_contracts.validate_layout_request(value)}
        self.assertIn("layout.scope.movable_node_outside", codes)

    def test_layout_request_rejects_overlapping_locked_and_movable_sets(self):
        value = valid_layout_request(mode="local_reflow")
        value["scope"]["movable_nodes"] = ["node-1", "node-2"]
        codes = {item["code"] for item in layout_contracts.validate_layout_request(value)}
        self.assertIn("layout.scope.locked_movable_overlap", codes)

    def test_layout_result_binds_to_immutable_request_digest(self):
        value = valid_layout_result()
        diagnostics = layout_contracts.validate_layout_result(value, expected_request_sha256="b" * 64)
        self.assertEqual(diagnostics[0]["code"], "layout.result.request_sha256_mismatch")

    def test_require_helpers_raise_contract_error_for_diagnostics(self):
        invalid = copy.deepcopy(valid_layout_result())
        invalid["pages"][0]["edges"][0]["waypoints"][1]["y"] = 10
        with self.assertRaises(lifecycle_contracts.ContractError):
            layout_contracts.require_layout_result(invalid)


if __name__ == "__main__":
    unittest.main()
