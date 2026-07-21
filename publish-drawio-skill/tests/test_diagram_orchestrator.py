import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import diagram_orchestrator as orchestrator


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


FAKE_GIGACODE = """#!/usr/bin/env python3
import json
import sys

HELP = (
    "GigaCode --model --prompt --output-format --approval-mode --auth-type "
    "--extensions --system-prompt --max-session-turns --core-tools --exclude-tools"
)


def runtime_input():
    raw = sys.stdin.read()
    if not raw.strip():
        raise SystemExit("missing runtime input")
    return json.loads(raw)


def emit(payload):
    model = sys.argv[sys.argv.index("--model") + 1]
    encoded = json.dumps(payload, ensure_ascii=False)
    print(json.dumps([
        {"type": "system", "subtype": "init", "model": model, "qwen_code_version": "0.13.1"},
        {"type": "assistant", "message": {"model": model, "content": [{"type": "text", "text": encoded}]}},
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": encoded,
            "stats": {"models": {model: {"api": {"totalRequests": 1}}}},
        },
    ]))


if "--help" in sys.argv:
    print(HELP)
    raise SystemExit(0)
if "--version" in sys.argv:
    print("26.5.17-test")
    raise SystemExit(0)

payload = runtime_input()
model = sys.argv[sys.argv.index("--model") + 1]

if model == "GigaChat-3-Ultra":
    emit({
        "schema_version": 1,
        "role": "supervisor",
        "status": "ok",
        "result": {
            "action": "create" if payload["mode"] == "create" else "analyze",
            "reason": "schema-valid supervisor decision",
            "required_roles": ["supervisor", "semantic_analyst", "reviewer"],
            "max_iterations": 1,
        },
    })
elif model == "vllm/Qwen3.6-35B-262k":
    requires_human = payload["mode"] == "improve"
    emit({
        "schema_version": 1,
        "role": "semantic_analyst",
        "status": "needs_human" if requires_human else "ok",
        "result": {
            "mode": payload["mode"],
            "diagram_type": "flowchart",
            "title": f"Test {payload['mode']} plan",
            "direction": "LR",
            "nodes": [
                {"id": "start", "label": "Start", "semantic_type": "start"},
                {"id": "end", "label": "End", "semantic_type": "end"},
            ],
            "edges": [
                {
                    "id": "flow",
                    "source_id": "start",
                    "target_id": "end",
                    "label": "flow",
                    "relationship": "sequence",
                }
            ],
            "source_refs": [],
            "assumptions": [],
            "semantic_changes": ["Add approval branch"] if requires_human else [],
            "requires_human": requires_human,
        },
    })
elif model == "vllm/DeepSeek-V4-Flash-262k":
    candidate = payload.get("candidate")
    if isinstance(candidate, dict) and isinstance(candidate.get("artifact"), dict):
        candidate_sha256 = candidate["artifact"]["sha256"]
        report_sha256 = candidate["report"]["sha256"]
        receipt_sha256 = candidate["receipt"]["sha256"]
    elif isinstance(candidate, dict) and "sha256" in candidate:
        candidate_sha256 = candidate["sha256"]
        report_sha256 = payload["validation_report"]["sha256"]
        receipt_sha256 = payload["validation_receipt"]["sha256"]
    else:
        candidate_sha256 = payload["artifact"]["sha256"]
        report_sha256 = payload["report"]["sha256"]
        receipt_sha256 = payload["receipt"]["sha256"]
    emit({
        "schema_version": 1,
        "verdict_id": "review-test",
        "run_id": payload["run_id"],
        "candidate_sha256": candidate_sha256,
        "report_sha256": report_sha256,
        "receipt_sha256": receipt_sha256,
        "verdict": "approve",
        "reviewed_at": "2026-07-20T12:00:00Z",
        "reviewer": {
            "resolved_model": model,
            "provider": "vllm",
            "resolution_mode": "isolated_cli",
        },
        "findings": [],
    })
else:
    raise SystemExit(f"unexpected model: {model}")
"""


class DiagramOrchestratorTests(unittest.TestCase):
    def fake_cli(self, workspace: Path) -> Path:
        cli = workspace / "fake-gigacode.py"
        cli.write_text(FAKE_GIGACODE, encoding="utf-8")
        cli.chmod(0o755)
        return cli

    def create_workspace(self) -> tuple[Path, Path, Path]:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        workspace = root / "workspace"
        workspace.mkdir()
        cli = self.fake_cli(root)
        return root, workspace, cli

    def read_events(self, run_dir: Path):
        return [json.loads(line) for line in (run_dir / "run-manifest.jsonl").read_text(encoding="utf-8").splitlines()]

    def run_host(self, *args, qwen_args=None):
        env = {**os.environ, "PYTHONPATH": str(SCRIPTS)}
        if qwen_args is not None:
            env[orchestrator.command_ux.QWEN_COMMAND_ARGS_ENV] = qwen_args
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "diagram_orchestrator.py"), *map(str, args)],
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )

    def test_conversational_create_generates_safe_name_and_short_next_commands(self):
        root, workspace, cli = self.create_workspace()
        (workspace / "обработки-заказа.drawio").write_text(clean_diagram(), encoding="utf-8")
        completed = self.run_host(
            "create", "--workspace", workspace, "--cli", cli,
            "Создай диаграмму обработки заказа",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["command_resolution"]["request_source"], "conversational_text")
        self.assertEqual(result["command_resolution"]["diagram_selection"], "generated_from_request")
        self.assertTrue(result["command_resolution"]["diagram"].endswith("обработки-заказа-2.drawio"))
        self.assertEqual(result["next_commands"]["short"]["approve"], "/drawio:resume approve")
        self.assertFalse(Path(result["command_resolution"]["diagram"]).exists())

    def test_conversational_improve_fails_before_preflight_when_diagram_is_ambiguous(self):
        root, workspace, cli = self.create_workspace()
        (workspace / "one.drawio").write_text(clean_diagram(), encoding="utf-8")
        (workspace / "two.drawio").write_text(clean_diagram(), encoding="utf-8")
        completed = self.run_host(
            "improve", "--workspace", workspace, "--cli", cli,
            "Исправь маршруты стрелок",
        )
        self.assertEqual(completed.returncode, 2)
        result = json.loads(completed.stderr)
        self.assertEqual(result["status"], "selection_required")
        self.assertEqual(result["code"], "diagram_selection_ambiguous")
        self.assertEqual(len(result["candidates"]), 2)
        self.assertFalse((workspace / ".diagram-runs").exists())

    def test_short_resume_selects_only_pending_run_and_trace_selects_latest(self):
        root, workspace, cli = self.create_workspace()
        target = workspace / "short-resume.drawio"
        created = orchestrator.start_run(
            "create", target, "Create a short resume diagram.", workspace, cli,
            run_id="short-resume-run", max_iterations=1,
        )
        self.assertEqual(created["checkpoint"]["kind"], "final_acceptance")
        resumed = self.run_host(
            "resume", "--workspace", workspace, "--cli", cli,
            qwen_args="approve",
        )
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        resume_result = json.loads(resumed.stdout)
        self.assertEqual(resume_result["status"], "completed")
        self.assertEqual(resume_result["command_resolution"]["run_selection"], "only_pending_run")
        traced = self.run_host("trace", "--workspace", workspace)
        self.assertEqual(traced.returncode, 0, traced.stderr)
        trace_result = json.loads(traced.stdout)
        self.assertTrue(trace_result["valid"])
        self.assertEqual(trace_result["command_resolution"]["run_selection"], "latest_updated_run")

    def test_explicit_flags_remain_supported(self):
        root, workspace, cli = self.create_workspace()
        target = workspace / "explicit.drawio"
        completed = self.run_host(
            "create", "--workspace", workspace, "--cli", cli,
            "--diagram", target, "--request", "Create an explicit compatibility diagram.",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["command_resolution"]["diagram_selection"], "explicit")
        self.assertEqual(result["command_resolution"]["request_source"], "explicit_flag")

    def test_qwen_raw_args_reconstruct_advanced_create_flags(self):
        root, workspace, cli = self.create_workspace()
        completed = self.run_host(
            "create", "--workspace", workspace, "--cli", cli,
            qwen_args=(
                '--diagram "order process.drawio" '
                '--request "Создай процесс обработки заказа"'
            ),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(
            result["command_resolution"]["diagram"],
            str((workspace / "order process.drawio").resolve()),
        )
        self.assertEqual(
            result["command_resolution"]["request"],
            "Создай процесс обработки заказа",
        )
        self.assertEqual(result["command_resolution"]["request_source"], "explicit_flag")

    def test_qwen_raw_args_select_diagram_in_multi_diagram_improve(self):
        root, workspace, cli = self.create_workspace()
        target = workspace / "microservices-istio-kafka.drawio"
        target.write_text(clean_diagram(), encoding="utf-8")
        (workspace / "other.drawio").write_text(clean_diagram(), encoding="utf-8")
        completed = self.run_host(
            "improve", "--workspace", workspace, "--cli", cli,
            qwen_args=(
                '--diagram "microservices-istio-kafka.drawio" '
                '--request "Исправь найденные валидатором и Reviewer замечания"'
            ),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["command_resolution"]["diagram"], str(target.resolve()))
        self.assertEqual(result["command_resolution"]["diagram_selection"], "explicit")
        self.assertEqual(
            result["command_resolution"]["request"],
            "Исправь найденные валидатором и Reviewer замечания",
        )

    def test_qwen_raw_resume_feedback_and_explicit_trace_are_tokenized(self):
        tokens = orchestrator.command_ux.qwen_command_tokens(
            'continue "учти замечания пользователя"'
        )
        self.assertEqual(
            orchestrator.command_ux.parse_resume(tokens),
            (None, "continue", "учти замечания пользователя"),
        )

        root, workspace, cli = self.create_workspace()
        target = workspace / "trace-command.drawio"
        created = orchestrator.start_run(
            "create", target, "Create trace command coverage.", workspace, cli,
            run_id="qwen-trace-run", max_iterations=1,
        )
        traced = self.run_host(
            "trace", "--workspace", workspace,
            qwen_args='--run "qwen-trace-run"',
        )
        self.assertEqual(traced.returncode, 0, traced.stderr)
        result = json.loads(traced.stdout)
        self.assertEqual(result["run_id"], created["run_id"])
        self.assertEqual(result["command_resolution"]["run_selection"], "explicit")

    def test_qwen_raw_args_reject_host_owned_options_and_malformed_quotes(self):
        for raw, code in (
            ("--workspace /tmp", "host_option_forbidden"),
            ("--cli /tmp/fake", "host_option_forbidden"),
            ("-- request", "command_arguments_invalid"),
            ('--request "unterminated', "command_arguments_invalid"),
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(orchestrator.command_ux.CommandUXError) as caught:
                    orchestrator.command_ux.argv_with_qwen_command_args(
                        ["create", "--workspace", "/safe", "--cli", "/safe/cli"],
                        {orchestrator.command_ux.QWEN_COMMAND_ARGS_ENV: raw},
                    )
                self.assertEqual(caught.exception.code, code)

        root, workspace, cli = self.create_workspace()
        completed = self.run_host(
            "create", "--workspace", workspace, "--cli", cli,
            qwen_args='--request "unterminated',
        )
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(
            json.loads(completed.stderr)["code"], "command_arguments_invalid"
        )

    def test_create_publication_refuses_target_that_appeared_after_start(self):
        root, workspace, cli = self.create_workspace()
        target = workspace / "late-collision.drawio"
        result = orchestrator.start_run(
            "create", target, "Create a late collision diagram.", workspace, cli,
            run_id="late-collision-run", max_iterations=1,
        )
        self.assertEqual(result["checkpoint"]["kind"], "final_acceptance")
        target.write_text("user file", encoding="utf-8")
        with self.assertRaisesRegex(orchestrator.supervisor.SupervisorError, "will not be overwritten"):
            orchestrator.resume_run(
                workspace / ".diagram-runs" / "late-collision-run",
                "approve", "", workspace, cli,
            )
        self.assertEqual(target.read_text(encoding="utf-8"), "user file")

    def test_create_refuses_existing_explicit_target_before_preflight(self):
        root, workspace, cli = self.create_workspace()
        target = workspace / "existing-target.drawio"
        target.write_text(clean_diagram(), encoding="utf-8")
        completed = self.run_host(
            "create", "--workspace", workspace, "--cli", cli,
            "--diagram", target, "--request", "Do not overwrite this file.",
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("will not be overwritten", json.loads(completed.stderr)["message"])
        self.assertFalse((workspace / ".diagram-runs").exists())

    def test_short_resume_refuses_ambiguous_pending_runs_without_mutation(self):
        root, workspace, cli = self.create_workspace()
        runs = workspace / ".diagram-runs"
        for run_id in ("pending-one", "pending-two"):
            run_dir = runs / run_id
            run_dir.mkdir(parents=True)
            (run_dir / "workflow.json").write_text(
                json.dumps({"run_id": run_id, "checkpoint": {"kind": "final_acceptance"}}),
                encoding="utf-8",
            )
        completed = self.run_host("resume", "--workspace", workspace, "--cli", cli, "approve")
        self.assertEqual(completed.returncode, 2)
        result = json.loads(completed.stderr)
        self.assertEqual(result["code"], "pending_run_selection_ambiguous")
        self.assertEqual(result["candidates"], ["pending-one", "pending-two"])
        self.assertFalse(any((runs / run_id / "decisions").exists() for run_id in result["candidates"]))

    def test_create_reaches_final_checkpoint_without_repair_and_resume_publishes(self):
        root, workspace, cli = self.create_workspace()
        target = workspace / "diagram.drawio"

        created = orchestrator.start_run(
            "create",
            target,
            "Create a small verified diagram.",
            workspace,
            cli,
            run_id="create-run",
            max_iterations=1,
        )
        run_dir = workspace / ".diagram-runs" / "create-run"
        self.assertEqual(created["checkpoint"]["kind"], "final_acceptance")
        self.assertEqual(created["status"], "awaiting_human")
        self.assertFalse(target.exists())
        self.assertEqual(
            [event["event_type"] for event in self.read_events(run_dir) if event["event_type"] == "role_finished"],
            ["role_finished", "role_finished", "role_finished"],
        )
        self.assertNotIn(
            "repair",
            {event["payload"]["role"] for event in self.read_events(run_dir) if event["event_type"] == "role_finished"},
        )

        resumed = orchestrator.resume_run(run_dir, "approve", "", workspace, cli)
        accepted = Path(resumed["accepted_artifact"]["path"])
        self.assertEqual(resumed["status"], "completed")
        self.assertTrue(target.is_file())
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), hashlib.sha256(accepted.read_bytes()).hexdigest())

        trace = orchestrator.trace_run(run_dir, workspace)
        self.assertTrue(trace["valid"])
        self.assertEqual(
            [(role["role"], role["resolved_model"], role["fallback_used"]) for role in trace["roles"]],
            [
                ("supervisor", "GigaChat-3-Ultra", False),
                ("semantic_analyst", "vllm/Qwen3.6-35B-262k", False),
                ("reviewer", "vllm/DeepSeek-V4-Flash-262k", False),
            ],
        )

    def test_improve_pauses_for_semantic_approval_without_overwriting_source(self):
        root, workspace, cli = self.create_workspace()
        source = workspace / "existing.drawio"
        source.write_text(clean_diagram(), encoding="utf-8")
        original_hash = hashlib.sha256(source.read_bytes()).hexdigest()

        result = orchestrator.start_run(
            "improve",
            source,
            "Add a semantic approval branch.",
            workspace,
            cli,
            run_id="improve-run",
            max_iterations=1,
        )
        run_dir = workspace / ".diagram-runs" / "improve-run"
        self.assertEqual(result["checkpoint"]["kind"], "semantic_approval")
        self.assertEqual(result["status"], "awaiting_human")
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original_hash)
        self.assertTrue((run_dir / "accepted" / "baseline.drawio").is_file())
        self.assertEqual(
            [event["payload"]["role"] for event in self.read_events(run_dir) if event["event_type"] == "role_finished"],
            ["supervisor", "semantic_analyst"],
        )

    def test_trace_detects_tampered_role_output(self):
        root, workspace, cli = self.create_workspace()
        target = workspace / "tampered.drawio"

        orchestrator.start_run(
            "create",
            target,
            "Create a tamper test diagram.",
            workspace,
            cli,
            run_id="tamper-run",
            max_iterations=1,
        )
        run_dir = workspace / ".diagram-runs" / "tamper-run"
        reviewer_output = next((run_dir / "roles").glob("reviewer-*/output.json"))
        reviewer_value = json.loads(reviewer_output.read_text(encoding="utf-8"))
        reviewer_value["reviewed_at"] = "2026-07-20T13:00:00Z"
        reviewer_output.write_text(json.dumps(reviewer_value), encoding="utf-8")

        trace = orchestrator.trace_run(run_dir, workspace)
        self.assertFalse(trace["valid"])
        self.assertEqual(trace["status"], "tampered_or_incomplete")
        self.assertTrue(any(not item["valid"] for item in trace["artifact_checks"]))

    def test_trace_verifies_failed_turn_limit_capture_and_isolation_evidence(self):
        root, workspace, _ = self.create_workspace()
        cli = root / "turn-limited-gigacode.py"
        cli.write_text(
            f"#!{sys.executable}\n"
            "import json, sys\n"
            "if '--help' in sys.argv:\n"
            "    print('GigaCode --model --prompt --output-format --approval-mode --auth-type --extensions --system-prompt --max-session-turns --core-tools --exclude-tools')\n"
            "    raise SystemExit(0)\n"
            "if '--version' in sys.argv:\n"
            "    print('26.5.17-test')\n"
            "    raise SystemExit(0)\n"
            "model=sys.argv[sys.argv.index('--model')+1]\n"
            "print(json.dumps([\n"
            "  {'type':'system','subtype':'init','model':model,'qwen_code_version':'0.13.1','agents':[],'slash_commands':[]},\n"
            "  {'type':'assistant','message':{'model':model,'content':[{'type':'text','text':'bounded failure'}]}},\n"
            "  {'type':'result','subtype':'error','is_error':True,'error':'FatalTurnLimitedError'}\n"
            "]))\n"
            "print('FatalTurnLimitedError', file=sys.stderr)\n"
            "raise SystemExit(2)\n",
            encoding="utf-8",
        )
        cli.chmod(0o755)
        completed = self.run_host(
            "create", "--workspace", workspace, "--cli", cli,
            "--run-id", "turn-limited-run", "Create a bounded failure diagram.",
        )
        self.assertEqual(completed.returncode, 2)
        run_dir = workspace / ".diagram-runs" / "turn-limited-run"
        host_result = json.loads((run_dir / "host-result.json").read_text())
        self.assertEqual(len(host_result["failed_role_runs"]), 2)
        self.assertEqual(
            [item["terminal"] for item in host_result["failed_role_runs"]],
            [False, True],
        )
        self.assertEqual(
            [item["attempted_model"] for item in host_result["failed_role_runs"]],
            ["GigaChat-3-Ultra", "vllm/DeepSeek-V4-Flash-262k"],
        )
        self.assertEqual(
            [item.get("fallback_model") for item in host_result["failed_role_runs"]],
            ["vllm/DeepSeek-V4-Flash-262k", None],
        )

        trace = orchestrator.trace_run(run_dir, workspace)

        self.assertFalse(trace["valid"])
        self.assertTrue(trace["integrity_valid"])
        self.assertEqual(trace["status"], "failed_verified")
        self.assertEqual(trace["roles"], [])
        self.assertEqual(len(trace["failed_roles"]), 2)
        self.assertEqual(len(trace["terminal_failed_roles"]), 1)
        for failed in trace["failed_roles"]:
            self.assertEqual(failed["role"], "supervisor")
            self.assertTrue(failed["runtime_capture_valid"])
            self.assertTrue(failed["stderr_capture_valid"])
            self.assertTrue(failed["isolation_controls_valid"])
            self.assertTrue(failed["isolation_evidence_valid"])
            self.assertTrue(failed["isolation_proof"]["verified"])
            self.assertEqual(failed["isolation_proof"]["tool_calls"], 0)
        self.assertFalse(trace["failed_roles"][0]["terminal"])
        self.assertTrue(trace["failed_roles"][1]["terminal"])

    def test_trace_verifies_recovered_turn_limit_path(self):
        root, workspace, _ = self.create_workspace()
        cli = root / "recovered-turn-limit-gigacode.py"
        cli.write_text(
            f"#!{sys.executable}\n"
            "import json\n"
            "import sys\n"
            "HELP = 'GigaCode --model --prompt --output-format stream-json --approval-mode --auth-type --extensions --system-prompt --max-session-turns --core-tools --exclude-tools'\n"
            "def runtime_input():\n"
            "    raw = sys.stdin.read()\n"
            "    if not raw.strip():\n"
            "        raise SystemExit('missing runtime input')\n"
            "    return json.loads(raw)\n"
            "def emit_stream(model, payload):\n"
            "    encoded = json.dumps(payload, ensure_ascii=False)\n"
            "    print('\\n'.join([\n"
            "        json.dumps({'type':'system','subtype':'init','model':model,'qwen_code_version':'0.13.1'}),\n"
            "        json.dumps({'type':'assistant','message':{'model':model,'content':[{'type':'text','text':encoded}]}}),\n"
            "        json.dumps({'type':'result','subtype':'success','is_error':False,'result':encoded})\n"
            "    ]))\n"
            "def emit_turn_limit(model):\n"
            "    print('\\n'.join([\n"
            "        json.dumps({'type':'system','subtype':'init','model':model,'qwen_code_version':'0.13.1'}),\n"
            "        json.dumps({'type':'assistant','message':{'model':model,'content':[{'type':'text','text':'still deciding'}]}}),\n"
            "        json.dumps({'type':'result','subtype':'error','is_error':True,'error':'FatalTurnLimitedError'})\n"
            "    ]))\n"
            "    print('FatalTurnLimitedError', file=sys.stderr)\n"
            "    raise SystemExit(2)\n"
            "if '--help' in sys.argv:\n"
            "    print(HELP)\n"
            "    raise SystemExit(0)\n"
            "if '--version' in sys.argv:\n"
            "    print('26.5.17-test')\n"
            "    raise SystemExit(0)\n"
            "payload = runtime_input()\n"
            "model = sys.argv[sys.argv.index('--model') + 1]\n"
            "if model == 'GigaChat-3-Ultra' and 'recovered-turn-limit' in payload.get('request', ''):\n"
            "    emit_turn_limit(model)\n"
            "if model == 'GigaChat-3-Ultra':\n"
            "    emit_stream(model, {'schema_version': 1, 'role': 'supervisor', 'status': 'ok', 'result': {'action': 'create', 'reason': 'schema-valid supervisor decision', 'required_roles': ['supervisor', 'semantic_analyst', 'reviewer'], 'max_iterations': 1}})\n"
            "elif model == 'vllm/DeepSeek-V4-Flash-262k' and 'recovered-turn-limit' in payload.get('request', ''):\n"
            "    emit_stream(model, {'schema_version': 1, 'role': 'supervisor', 'status': 'ok', 'result': {'action': 'create', 'reason': 'fallback approval', 'required_roles': ['supervisor', 'semantic_analyst', 'reviewer'], 'max_iterations': 1}})\n"
            "elif model == 'vllm/Qwen3.6-35B-262k':\n"
            "    requires_human = payload['mode'] == 'improve'\n"
            "    emit_stream(model, {'schema_version': 1, 'role': 'semantic_analyst', 'status': 'needs_human' if requires_human else 'ok', 'result': {'mode': payload['mode'], 'diagram_type': 'flowchart', 'title': f\"Test {payload['mode']} plan\", 'direction': 'LR', 'nodes': [{'id': 'start', 'label': 'Start', 'semantic_type': 'start'}, {'id': 'end', 'label': 'End', 'semantic_type': 'end'}], 'edges': [{'id': 'flow', 'source_id': 'start', 'target_id': 'end', 'label': 'flow', 'relationship': 'sequence'}], 'source_refs': [], 'assumptions': [], 'semantic_changes': ['Add approval branch'] if requires_human else [], 'requires_human': requires_human}})\n"
            "elif model == 'vllm/DeepSeek-V4-Flash-262k':\n"
            "    candidate = payload.get('candidate')\n"
            "    if isinstance(candidate, dict) and isinstance(candidate.get('artifact'), dict):\n"
            "        candidate_sha256 = candidate['artifact']['sha256']\n"
            "        report_sha256 = candidate['report']['sha256']\n"
            "        receipt_sha256 = candidate['receipt']['sha256']\n"
            "    elif isinstance(candidate, dict) and 'sha256' in candidate:\n"
            "        candidate_sha256 = candidate['sha256']\n"
            "        report_sha256 = payload['validation_report']['sha256']\n"
            "        receipt_sha256 = payload['validation_receipt']['sha256']\n"
            "    else:\n"
            "        candidate_sha256 = payload['artifact']['sha256']\n"
            "        report_sha256 = payload['report']['sha256']\n"
            "        receipt_sha256 = payload['receipt']['sha256']\n"
            "    emit_stream(model, {'schema_version': 1, 'verdict_id': 'review-test', 'run_id': payload['run_id'], 'candidate_sha256': candidate_sha256, 'report_sha256': report_sha256, 'receipt_sha256': receipt_sha256, 'verdict': 'approve', 'reviewed_at': '2026-07-20T12:00:00Z', 'reviewer': {'resolved_model': model, 'provider': 'vllm', 'resolution_mode': 'isolated_cli'}, 'findings': []})\n"
            "else:\n"
            "    raise SystemExit(f'unexpected model: {model}')\n",
            encoding="utf-8",
        )
        cli.chmod(0o755)
        target = workspace / "recovered-turn-limit.drawio"
        completed = self.run_host(
            "create",
            "--workspace",
            workspace,
            "--cli",
            cli,
            "--run-id",
            "recovered-turn-limit-run",
            "Create a recovered-turn-limit diagram.",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        run_dir = workspace / ".diagram-runs" / "recovered-turn-limit-run"
        host_result = json.loads((run_dir / "host-result.json").read_text())
        self.assertEqual(host_result["status"], "awaiting_human")
        self.assertEqual(len(host_result["failed_role_runs"]), 1)
        self.assertFalse(host_result["failed_role_runs"][0]["terminal"])
        self.assertTrue(host_result["model_diversity_degraded"])
        resumed = orchestrator.resume_run(run_dir, "approve", "", workspace, cli)
        self.assertEqual(resumed["status"], "completed")
        trace = orchestrator.trace_run(run_dir, workspace)
        self.assertTrue(trace["valid"])
        self.assertEqual(trace["status"], "verified")
        self.assertEqual(trace["terminal_failed_roles"], [])
        self.assertEqual(len(trace["failed_roles"]), 1)
        self.assertFalse(trace["failed_roles"][0]["terminal"])
        self.assertTrue(trace["model_diversity_degraded"])
        self.assertTrue(
            any(role["role"] == "supervisor" and role["fallback_used"] for role in trace["roles"])
        )

    def test_trace_detects_tampered_model_proof_after_manifest_chain_is_rehashed(self):
        root, workspace, cli = self.create_workspace()
        target = workspace / "tampered-model-proof.drawio"

        orchestrator.start_run(
            "create",
            target,
            "Create a model-proof tamper test diagram.",
            workspace,
            cli,
            run_id="tampered-model-proof-run",
            max_iterations=1,
        )
        run_dir = workspace / ".diagram-runs" / "tampered-model-proof-run"
        manifest = run_dir / "run-manifest.jsonl"
        events = self.read_events(run_dir)
        role_finished = next(
            event
            for event in events
            if event["event_type"] == "role_finished"
            and event["payload"]["role"] == "semantic_analyst"
        )
        tampered_model = "vllm/Tampered-Model-262k"
        role_finished["actor"]["model"] = tampered_model
        role_finished["payload"]["resolved_model"] = tampered_model
        role_finished["payload"]["model_proof"] = {
            "verified": True,
            "system_model": tampered_model,
            "assistant_model": tampered_model,
            "stats_models": [tampered_model],
        }

        lines = []
        previous = None
        for event in events:
            event["previous_event_sha256"] = previous
            line = json.dumps(event, ensure_ascii=False, sort_keys=True)
            lines.append(line)
            previous = hashlib.sha256(line.encode("utf-8")).hexdigest()
        manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")

        trace = orchestrator.trace_run(run_dir, workspace)
        self.assertTrue(all(item["chain_valid"] for item in trace["event_checks"]))
        self.assertFalse(trace["valid"])
        self.assertEqual(trace["status"], "tampered_or_incomplete")

    def test_semantic_continue_binds_approved_plan_and_changes_into_repair_input(self):
        root, workspace, _ = self.create_workspace()
        source_with_repair = FAKE_GIGACODE.replace(
            '["supervisor", "semantic_analyst", "reviewer"]',
            '["supervisor", "semantic_analyst", "repair", "reviewer"]',
        )
        cli = root / "semantic-continue-cli.py"
        cli.write_text(source_with_repair, encoding="utf-8")
        cli.chmod(0o755)
        source = workspace / "semantic-continue.drawio"
        source.write_text(clean_diagram(), encoding="utf-8")

        result = orchestrator.start_run(
            "improve",
            source,
            "Add a semantic approval branch.",
            workspace,
            cli,
            run_id="semantic-continue-run",
            max_iterations=1,
        )
        self.assertEqual(result["checkpoint"]["kind"], "semantic_approval")
        run_dir = workspace / ".diagram-runs" / "semantic-continue-run"
        workflow = orchestrator.load_workflow(run_dir)
        pending_approval = workflow["pending_semantic_approval"]
        approved_plan = dict(pending_approval["semantic_plan"])
        approved_changes = list(pending_approval["semantic_changes"])
        approved_plan_path = Path(approved_plan["path"])
        self.assertEqual(
            hashlib.sha256(approved_plan_path.read_bytes()).hexdigest(),
            approved_plan["sha256"],
        )

        orchestrator.resume_run(
            run_dir,
            "continue",
            "Approve the proposed semantic delta.",
            workspace,
            cli,
        )
        repair_payload = json.loads(
            (run_dir / "roles" / "repair-1" / "input.json").read_text(encoding="utf-8")
        )

        def walk(value):
            yield value
            if isinstance(value, dict):
                for item in value.values():
                    yield from walk(item)
            elif isinstance(value, list):
                for item in value:
                    yield from walk(item)

        self.assertTrue(
            any(
                isinstance(value, dict)
                and value.get("path") == approved_plan["path"]
                and value.get("sha256") == approved_plan["sha256"]
                for value in walk(repair_payload)
            ),
            "Repair input must contain the immutable path and hash of the approved semantic plan",
        )
        self.assertTrue(
            any(value == approved_changes for value in walk(repair_payload)),
            "Repair input must contain the exact approved semantic changes",
        )
        self.assertEqual(
            hashlib.sha256(approved_plan_path.read_bytes()).hexdigest(),
            approved_plan["sha256"],
        )

    def test_unsupported_supervisor_plan_fails_before_unrequested_roles_run(self):
        root, workspace, _ = self.create_workspace()
        target = workspace / "unsupported-supervisor-plan.drawio"
        source = FAKE_GIGACODE.replace(
            '"action": "create" if payload["mode"] == "create" else "analyze",',
            '"action": "review",',
        ).replace(
            '"required_roles": ["supervisor", "semantic_analyst", "reviewer"],',
            '"required_roles": ["supervisor", "reviewer"],',
        ).replace('"max_iterations": 1,', '"max_iterations": 2,', 1)
        cli = root / "unsupported-supervisor-plan-cli.py"
        cli.write_text(source, encoding="utf-8")
        cli.chmod(0o755)

        with self.assertRaises(orchestrator.supervisor.SupervisorError):
            orchestrator.start_run(
                "create",
                target,
                "Create a diagram from an unsupported supervisor plan.",
                workspace,
                cli,
                run_id="unsupported-supervisor-plan-run",
                max_iterations=5,
            )

        run_dir = workspace / ".diagram-runs" / "unsupported-supervisor-plan-run"
        self.assertEqual(
            [
                event["payload"]["role"]
                for event in self.read_events(run_dir)
                if event["event_type"] == "role_finished"
            ],
            ["supervisor"],
        )


if __name__ == "__main__":
    unittest.main()
