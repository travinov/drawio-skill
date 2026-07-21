import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import diagram_host
import diagram_supervisor as supervisor


def clean_diagram():
    return """<?xml version="1.0" encoding="UTF-8"?>
<mxfile host="app.diagrams.net">
  <diagram id="page-1" name="Page-1"><mxGraphModel><root>
    <mxCell id="0"/><mxCell id="1" parent="0"/>
    <mxCell id="node" value="Step" parent="1" vertex="1">
      <mxGeometry x="100" y="100" width="160" height="60" as="geometry"/>
    </mxCell>
  </root></mxGraphModel></diagram>
</mxfile>
"""


def successful_reviewer(role, input_path, output_path, **kwargs):
    audit = supervisor.load_json(input_path)
    verdict = {
        "schema_version": 1,
        "verdict_id": "audit-verdict",
        "run_id": audit["run_id"],
        "candidate_sha256": audit["artifact"]["sha256"],
        "report_sha256": audit["report"]["sha256"],
        "receipt_sha256": audit["receipt"]["sha256"],
        "verdict": "approve",
        "reviewed_at": "2026-07-20T12:00:00+00:00",
        "reviewer": {
            "resolved_model": "vllm/DeepSeek-V4-Flash-262k",
            "provider": "vllm",
            "resolution_mode": "isolated_cli",
        },
        "findings": [],
    }
    supervisor.write_json(output_path, verdict)
    return {
        "resolution": {
            "requested_model": "vllm/DeepSeek-V4-Flash-262k",
            "resolved_model": "vllm/DeepSeek-V4-Flash-262k",
            "resolution_mode": "isolated_cli",
            "fallback_used": False,
        },
        "runtime_metadata": {
            "model_proof": {
                "verified": True,
                "system_model": "vllm/DeepSeek-V4-Flash-262k",
                "assistant_model": "vllm/DeepSeek-V4-Flash-262k",
                "stats_models": ["vllm/DeepSeek-V4-Flash-262k"],
            }
        },
    }


class DiagramHostTests(unittest.TestCase):
    def test_review_command_owns_the_complete_read_only_workflow(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            artifact = workspace / "diagram.drawio"
            artifact.write_text(clean_diagram(), encoding="utf-8")
            original = hashlib.sha256(artifact.read_bytes()).hexdigest()

            with mock.patch.object(diagram_host.agent_runtime, "invoke_role", side_effect=successful_reviewer):
                result = diagram_host.run_review(
                    artifact, workspace, sys.executable, run_id="review-test"
                )

            run_dir = workspace / ".diagram-runs" / "review-test"
            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["reviewer"]["status"], "completed")
            self.assertEqual(result["reviewer"]["resolved_model"], "vllm/DeepSeek-V4-Flash-262k")
            self.assertTrue(result["reviewer"]["model_proof"]["verified"])
            self.assertFalse(result["artifact"]["modified"])
            self.assertEqual(hashlib.sha256(artifact.read_bytes()).hexdigest(), original)
            self.assertTrue(supervisor.verify_host_preflight(run_dir)["valid"])
            self.assertEqual(supervisor.load_state(run_dir)["state"], "final_review")
            self.assertEqual(supervisor.load_json(run_dir / "host-result.json"), result)
            audit = supervisor.load_json(run_dir / "reviewer-audit-input.json")
            schema = supervisor.load_json(ROOT / "data" / "reviewer-audit-input.v1.schema.json")
            self.assertFalse(list(jsonschema_validator(schema).iter_errors(audit)))

    def test_review_records_reviewer_failure_without_claiming_success(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            artifact = workspace / "diagram.drawio"
            artifact.write_text(clean_diagram(), encoding="utf-8")
            with mock.patch.object(
                diagram_host.agent_runtime,
                "invoke_role",
                side_effect=supervisor.SupervisorError("isolated reviewer failed"),
            ):
                result = diagram_host.run_review(
                    artifact, workspace, sys.executable, run_id="review-failed"
                )
            self.assertEqual(result["status"], "findings")
            self.assertEqual(result["reviewer"]["status"], "failed")
            self.assertEqual(result["next_action"], "inspect_findings_before_any_repair")

    def test_review_preserves_model_proof_when_reviewer_json_breaks_schema(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            artifact = workspace / "diagram.drawio"
            artifact.write_text(clean_diagram(), encoding="utf-8")
            proof = {
                "verified": True,
                "system_model": "vllm/DeepSeek-V4-Flash-262k",
                "assistant_model": "vllm/DeepSeek-V4-Flash-262k",
                "stats_models": ["vllm/DeepSeek-V4-Flash-262k"],
            }
            failure = diagram_host.agent_runtime.RoleOutputContractError(
                "isolated reviewer output schema failed: candidate_sha256 is required",
                resolution={
                    "requested_model": "vllm/DeepSeek-V4-Flash-262k",
                    "resolved_model": "vllm/DeepSeek-V4-Flash-262k",
                    "resolution_mode": "isolated_cli",
                    "fallback_used": False,
                },
                runtime_metadata={"model_proof": proof},
                invalid_output_sha256="d" * 64,
            )
            with mock.patch.object(
                diagram_host.agent_runtime, "invoke_role", side_effect=failure
            ):
                result = diagram_host.run_review(
                    artifact, workspace, sys.executable, run_id="review-schema-failed"
                )

            reviewer = result["reviewer"]
            self.assertEqual(reviewer["status"], "failed")
            self.assertEqual(
                reviewer["resolved_model"], "vllm/DeepSeek-V4-Flash-262k"
            )
            self.assertTrue(reviewer["model_proof"]["verified"])
            self.assertEqual(reviewer["invalid_output_sha256"], "d" * 64)

    def test_review_rejects_artifact_outside_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "project"
            workspace.mkdir()
            artifact = root / "outside.drawio"
            artifact.write_text(clean_diagram(), encoding="utf-8")
            with self.assertRaisesRegex(supervisor.SupervisorError, "inside the current workspace"):
                diagram_host.run_review(artifact, workspace, sys.executable)
            self.assertFalse((workspace / ".diagram-runs").exists())

    def test_qwen_extension_command_executes_host_before_model_prompt(self):
        prompt = (ROOT / "commands" / "drawio" / "review.md").read_text(encoding="utf-8")
        self.assertTrue(prompt.startswith("---\ndescription:"))
        self.assertIn("!{PYTHON=python3", prompt)
        self.assertIn("scripts/diagram_host.py", prompt)
        self.assertIn("{{args}}", prompt)
        self.assertNotIn("--artifact {{args}}", prompt)
        self.assertIn("Do not call any tools", prompt)
        self.assertIn("GIGACODE_EXTENSIONS_DIR", prompt)
        self.assertIn("GIGACODE_BIN", prompt)
        self.assertIn("PYTHON_BIN", prompt)
        self.assertNotIn("|| true", prompt)

    def test_cli_review_runs_end_to_end_with_gigacode_event_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            artifact = workspace / "diagram.drawio"
            artifact.write_text(clean_diagram(), encoding="utf-8")
            cli = workspace / "fake-gigacode"
            cli.write_text(
                """#!/usr/bin/env python3
import json, sys
if '--help' in sys.argv:
    print('GigaCode --model --prompt --output-format --approval-mode --auth-type --extensions --system-prompt --max-session-turns --core-tools --exclude-tools')
    raise SystemExit(0)
if '--version' in sys.argv:
    print('26.5.17-test')
    raise SystemExit(0)
raw = sys.stdin.read()
payload = json.loads(raw)
model = payload['context']['requested_reviewer_model']
verdict = {
    'schema_version': 1,
    'verdict_id': 'real-adapter-test',
    'run_id': payload['run_id'],
    'candidate_sha256': payload['artifact']['sha256'],
    'report_sha256': payload['report']['sha256'],
    'receipt_sha256': payload['receipt']['sha256'],
    'verdict': 'approve',
    'reviewed_at': '2026-07-20T12:00:00+00:00',
    'reviewer': {'resolved_model': model, 'provider': 'vllm', 'resolution_mode': 'isolated_cli'},
    'findings': [],
}
encoded = json.dumps(verdict)
events = [
    {'type': 'system', 'subtype': 'init', 'model': model, 'qwen_code_version': '0.13.1-test'},
    {'type': 'assistant', 'message': {'model': model, 'content': [{'type': 'text', 'text': encoded}]}},
    {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': encoded, 'stats': {'models': {model: {'tokens': 1}}}},
]
print(json.dumps(events))
""",
                encoding="utf-8",
            )
            cli.chmod(0o700)

            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "diagram_host.py"),
                    "review",
                    "--workspace",
                    str(workspace),
                    "--cli",
                    str(cli),
                    "--run-id",
                    "cli-e2e",
                ],
                text=True,
                capture_output=True,
                check=False,
                cwd=workspace,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["artifact"]["path"], str(artifact.resolve()))
            self.assertEqual(result["command_resolution"]["diagram_selection"], "only_drawio_in_workspace")
            self.assertEqual(result["reviewer"]["resolution_mode"], "isolated_cli")
            self.assertTrue(result["reviewer"]["model_proof"]["verified"])
            manifest = [
                json.loads(line)
                for line in Path(result["evidence"]["manifest"]).read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn("model_resolved", [event["event_type"] for event in manifest])
            self.assertIn("review_verdict", [event["event_type"] for event in manifest])


def jsonschema_validator(schema):
    import jsonschema
    return jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())


if __name__ == "__main__":
    unittest.main()
