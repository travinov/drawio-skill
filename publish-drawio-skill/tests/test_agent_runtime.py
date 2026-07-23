import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import agent_runtime


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
