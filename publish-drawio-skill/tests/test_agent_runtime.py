import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import agent_runtime
from test_lifecycle_v2 import reviewer_input_v2, semantic_analysis_v2


def reviewer_v2_input_with_blocker():
    payload = reviewer_input_v2()
    payload["candidate"]["report"]["content"] = {
        "result": "failed",
        "findings": [{
            "code": "artifact.xml.invalid",
            "severity": "error",
            "blocking": True,
            "deterministic": True,
        }],
    }
    payload["candidate"]["receipt"]["content"] = {
        "strict": True, "exit_code": 1, "result": "failed",
    }
    payload["candidate"]["strict_passed"] = False
    return payload


def legacy_reviewer_approve_for_v2_input(payload):
    return {
        "schema_version": 1,
        "verdict_id": "legacy-review-1",
        "run_id": payload["run_id"],
        "candidate_sha256": payload["candidate"]["artifact"]["sha256"],
        "report_sha256": payload["candidate"]["report"]["sha256"],
        "receipt_sha256": payload["candidate"]["receipt"]["sha256"],
        "verdict": "approve",
        "reviewed_at": "2026-07-24T00:00:00Z",
        "findings": [],
    }


def semantic_v2_output_with_route():
    output = semantic_analysis_v2()
    identity_a = output["result"]["pages"][0]["nodes"][0]["stable_identity"]
    identity_b = output["result"]["pages"][0]["nodes"][1]["stable_identity"]
    output["result"]["pages"][0]["edges"].append({
        "stable_identity": {"page_id": "page-1", "cell_id": "edge-a-b"},
        "source": identity_a,
        "target": identity_b,
        "label": "next",
        "relationship": "sequence",
        "route": {
            "orthogonal": True,
            "source_pin": {"x": 1.0, "y": 0.5},
            "target_pin": {"x": 0.0, "y": 0.5},
            "waypoints": [{"x": 200, "y": 40}, {"x": 200, "y": 160}],
        },
    })
    return output


class AgentRuntimeIntakeTests(unittest.TestCase):
    def test_supervisor_accepts_only_host_layout_strategy_actions(self):
        for action in (
            "create_layout",
            "reroute_edges",
            "expand_local_scope",
            "retry_layout_strategy",
            "request_semantic_clarification",
            "finish_best_effort",
        ):
            with self.subTest(action=action):
                output = {
                    "schema_version": 1,
                    "role": "supervisor",
                    "status": "ok",
                    "result": {"action": action, "reason": "bounded host decision"},
                }
                self.assertEqual(
                    agent_runtime.validate_role_output("supervisor", output, {}), output
                )

        with self.assertRaisesRegex(Exception, "allowlisted"):
            agent_runtime.validate_role_output(
                "supervisor",
                {
                    "schema_version": 1,
                    "role": "supervisor",
                    "status": "ok",
                    "result": {"action": "review", "reason": "coordinates roles"},
                },
                {},
            )

    def test_reviewer_cannot_approve_input_with_blocking_deterministic_finding(self):
        payload = {
            "schema_version": 1,
            "run_id": "run-1",
            "candidate": {
                "sha256": "a" * 64,
                "report": {
                    "content": {
                        "findings": [{
                            "code": "artifact.xml.invalid",
                            "severity": "error",
                            "blocking": True,
                            "deterministic": True,
                        }]
                    }
                },
            },
            "validation_report": {"sha256": "b" * 64},
            "validation_receipt": {"sha256": "c" * 64},
        }
        output = {
            "schema_version": 1,
            "verdict_id": "review-1",
            "verdict": "approve",
            "reviewed_at": "2026-07-24T00:00:00Z",
            "findings": [],
        }
        with self.assertRaisesRegex(Exception, "blocking deterministic"):
            agent_runtime.validate_role_output("reviewer", output, payload)

    def test_reviewer_v2_legacy_approve_cannot_bypass_blocking_evidence_or_finalize(self):
        payload = reviewer_v2_input_with_blocker()
        approve = legacy_reviewer_approve_for_v2_input(payload)
        self.assertEqual(agent_runtime.validate_role_input("reviewer", payload), payload)

        with self.assertRaisesRegex(Exception, "blocking deterministic"):
            agent_runtime.validate_role_output("reviewer", approve, payload)
        with self.assertRaisesRegex(Exception, "blocking deterministic"):
            agent_runtime.finalize_role_output("reviewer", payload, approve)

        corrected = {**approve, "verdict": "reject"}
        validated = agent_runtime.validate_role_output("reviewer", corrected, payload)
        finalized, proof = agent_runtime.finalize_role_output(
            "reviewer", payload, validated
        )
        self.assertEqual(finalized["schema_version"], 2)
        self.assertEqual(finalized["verdict"], "reject")
        self.assertTrue(proof["verified"])

    def test_semantic_v2_rejects_model_produced_pins_waypoints_and_coordinates(self):
        output = semantic_v2_output_with_route()
        payload = {"schema_version": 2, "mode": "create"}
        with self.assertRaisesRegex(Exception, "ordinary geometry"):
            agent_runtime.validate_role_output("semantic_analyst", output, payload)

    def test_semantic_v2_rejects_coordinate_bounds_with_role_specific_diagnostic(self):
        output = semantic_v2_output_with_route()
        output["result"]["pages"][0]["edges"][0].pop("route")
        output["result"]["pages"][0]["nodes"][0]["geometry"] = {
            "x": 10, "y": 20, "width": 120, "height": 60,
        }
        payload = {"schema_version": 2, "mode": "create"}
        with self.assertRaisesRegex(Exception, "coordinate/bounds"):
            agent_runtime.validate_role_output("semantic_analyst", output, payload)

    def test_semantic_v2_rejects_non_mapping_result_with_structured_diagnostics(self):
        payload = {"schema_version": 2, "mode": "create"}
        for bad_result in ([], None, "table"):
            with self.subTest(bad_result=bad_result):
                output = semantic_analysis_v2()
                output["result"] = bad_result
                with self.assertRaises(agent_runtime.SupervisorError) as ctx:
                    agent_runtime.validate_role_output(
                        "semantic_analyst", output, payload
                    )
                self.assertEqual(ctx.exception.contract_failure_kind, "output_schema")
                self.assertTrue(ctx.exception.contract_diagnostics)
                self.assertEqual(ctx.exception.contract_diagnostics[0]["pointer"], "/result")
                self.assertIn(
                    "schema",
                    ctx.exception.contract_diagnostics[0]["code"],
                )

    def test_layout_repair_rejects_legacy_unbounded_intent(self):
        payload = {
            "schema_version": 1,
            "repair_mode": "layout_intent",
            "run_id": "run-1",
        }
        legacy_intent = {
            "schema_version": 1,
            "role": "repair",
            "status": "ok",
            "run_id": "run-1",
            "baseline_sha256": "a" * 64,
            "result": {
                "action": "reroute_edges",
                "page_id": "page-1",
                "node_ids": [],
                "edge_ids": ["edge-1"],
                "reason": "legacy scope is not sufficient",
            },
        }
        with self.assertRaisesRegex(Exception, "bounded layout-repair-intent"):
            agent_runtime.validate_role_output("repair", legacy_intent, payload)

    def test_layout_repair_selects_intent_contract_without_changing_semantic_patch(self):
        layout_payload = {
            "schema_version": 1,
            "repair_mode": "layout_intent",
            "run_id": "run-1",
        }
        semantic_payload = {"schema_version": 1, "run_id": "run-1"}
        self.assertEqual(
            agent_runtime.role_schema_name("repair", layout_payload),
            "layout-repair-intent.v1.schema.json",
        )
        self.assertEqual(
            agent_runtime.role_schema_name("repair", semantic_payload),
            "diagram-patch.v1.schema.json",
        )
        self.assertIn(
            "layout-repair-intent.v1.schema.json",
            agent_runtime.role_output_contract("repair", layout_payload),
        )

    def test_semantic_intake_phase_selects_intake_analysis_schema(self):
        payload = {
            "schema_version": 1,
            "phase": "intake",
            "mode": "create",
            "request": "Покажи сервисы и их зависимости",
            "existing_evidence": None,
            "answers": [],
        }
        self.assertEqual(
            agent_runtime.role_schema_name("semantic_analyst", payload),
            "diagram-intake-analysis.v1.schema.json",
        )
        contract = agent_runtime.role_output_contract("semantic_analyst", payload)
        self.assertIn("diagram-intake-analysis.v1.schema.json", contract)
        self.assertIn("host assigns", contract.lower())

    def test_semantic_intake_classification_remains_geometry_free_and_valid(self):
        payload = {
            "schema_version": 1,
            "phase": "intake",
            "mode": "create",
            "request": "Покажи сервисы",
            "existing_evidence": None,
            "answers": [],
        }
        output = {
            "schema_version": 1,
            "role": "semantic_analyst",
            "status": "ok",
            "result": {
                "diagram_type": "dependency",
                "confidence": 0.9,
                "alternatives": [],
                "sufficient": True,
                "blocking_questions": [],
                "assumptions": [],
            },
        }
        self.assertEqual(
            agent_runtime.validate_role_output("semantic_analyst", output, payload),
            output,
        )

    def test_intake_analysis_contract_rejects_host_owned_fields(self):
        payload = {
            "schema_version": 1,
            "phase": "intake",
            "mode": "create",
            "request": "Покажи сервисы",
            "existing_evidence": None,
            "answers": [],
        }
        output = {
            "schema_version": 1,
            "role": "semantic_analyst",
            "status": "ok",
            "result": {
                "diagram_type": "dependency",
                "confidence": 0.9,
                "alternatives": [],
                "sufficient": True,
                "blocking_questions": [],
                "assumptions": [],
                "intake_id": "model-owned",
            },
        }
        with self.assertRaises(Exception):
            agent_runtime.validate_role_output(
                "semantic_analyst", json.loads(json.dumps(output)), payload
            )


if __name__ == "__main__":
    unittest.main()
