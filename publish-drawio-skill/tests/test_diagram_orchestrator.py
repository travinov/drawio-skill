import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


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


def routed_diagram():
    return """<?xml version="1.0" encoding="UTF-8"?>
<mxfile host="app.diagrams.net">
  <diagram id="page-1" name="Page-1"><mxGraphModel><root>
    <mxCell id="0"/><mxCell id="1" parent="0"/>
    <mxCell id="source" value="Source" parent="1" vertex="1">
      <mxGeometry x="0" y="0" width="80" height="60" as="geometry"/>
    </mxCell>
    <mxCell id="target" value="Target" parent="1" vertex="1">
      <mxGeometry x="300" y="0" width="80" height="60" as="geometry"/>
    </mxCell>
    <mxCell id="e-2" value="flow" style="html=1;" parent="1" source="source" target="target" edge="1">
      <mxGeometry relative="1" as="geometry"/>
    </mxCell>
  </root></mxGraphModel></diagram>
</mxfile>
"""


FAKE_GIGACODE = """#!/usr/bin/env python3
import json
import sys

HELP = (
    "GigaCode --model --prompt --output-format --approval-mode --auth-type "
    "--extensions --system-prompt --max-session-turns --core-tools --allowed-mcp-server-names --exclude-tools"
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

mcp_flag = "--allowed-mcp-server-names"
if mcp_flag not in sys.argv or sys.argv[sys.argv.index(mcp_flag) + 1] != "":
    print("global MCP registry was not disabled", file=sys.stderr)
    raise SystemExit(53)

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
    if payload.get("phase") == "intake":
        ambiguous = "зависимост" in payload["request"].lower()
        sequential = "последовательными вопросами" in payload["request"].lower()
        four_questions = "четырьмя вопросами" in payload["request"].lower()
        has_assumption = "допущением" in payload["request"].lower()
        blocking = [
            {
                "prompt": f"Вопрос {index}?",
                "reason": "Ответ меняет топологию.",
                "recommended": {"value": f"answer-{index}", "label": f"Ответ {index}"},
                "choices": [{"value": f"answer-{index}", "label": f"Ответ {index}"}],
                "allow_free_text": True,
            }
            for index in (
                (1, 2, 3, 4) if four_questions else (1, 2)
            )
        ] if (sequential or four_questions) else []
        emit({
            "schema_version": 1,
            "role": "semantic_analyst",
            "status": "needs_human" if ambiguous else "ok",
            "result": {
                "diagram_type": "dependency" if ambiguous else "generic",
                "confidence": 0.55 if ambiguous else 0.95,
                "alternatives": ["c4"] if ambiguous else [],
                "sufficient": not (sequential or four_questions),
                "blocking_questions": blocking,
                "assumptions": ["Использовать встроенный стиль"] if has_assumption else [],
            },
        })
        raise SystemExit(0)
    requires_human = payload["mode"] == "improve"
    emit({
        "schema_version": 2,
        "role": "semantic_analyst",
        "status": "needs_human" if requires_human else "ok",
        "result": {
            "mode": payload["mode"],
            "diagram_type": "generic",
            "title": f"Test {payload['mode']} analysis",
            "direction": "LR",
            "pages": [
                {
                    "page_id": "page-1",
                    "name": "Page 1",
                    "nodes": [
                        {
                            "stable_identity": {"page_id": "page-1", "cell_id": "node-a"},
                            "label": "A",
                            "semantic_type": "task",
                            "parent": None,
                            "style_hint": None,
                        },
                        {
                            "stable_identity": {"page_id": "page-1", "cell_id": "node-b"},
                            "label": "B",
                            "semantic_type": "task",
                            "parent": None,
                            "style_hint": None,
                        },
                    ],
                    "edges": [],
                }
            ],
            "assumptions": [],
            "requires_human": requires_human,
            "human_questions": ["Add approval branch"] if requires_human else [],
        },
    })
elif model == "vllm/DeepSeek-V4-Flash-262k":
    emit({
        "schema_version": 2,
        "role": "reviewer",
        "status": "ok",
        "analysis_id": "review-test",
        "verdict": "approve",
        "reviewed_at": "2026-07-20T12:00:00Z",
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
        intake_path = Path(result["command_resolution"]["intake"])
        self.assertTrue(intake_path.is_file())
        self.assertTrue(
            (Path(result["run_dir"]) / "inputs" / "diagram-intake.json").is_file()
        )
        self.assertTrue(
            (workspace / ".diagram-intake" / result["command_resolution"]["intake_id"]).is_dir()
        )
        workflow = json.loads(
            (Path(result["run_dir"]) / "workflow.json").read_text(encoding="utf-8")
        )
        self.assertEqual(workflow["renderer_adapter"]["options"]["backend"], "auto")
        self.assertEqual(workflow["renderer_adapter"]["options"]["reflow"], "full")
        self.assertTrue(workflow["layout_attempts"])

    def test_generic_create_persists_hash_bound_layout_attempt_evidence(self):
        root, workspace, cli = self.create_workspace()
        target = workspace / "layout-evidence.drawio"

        result = orchestrator.start_run(
            "create",
            target,
            "Create a deterministic two-step diagram.",
            workspace,
            cli,
            run_id="layout-evidence-run",
            max_iterations=1,
        )

        run_dir = Path(result["run_dir"])
        workflow = json.loads((run_dir / "workflow.json").read_text(encoding="utf-8"))
        plan = workflow["semantic_plan_v2"]
        attempts = workflow["layout_attempts"]
        schedule_path = Path(workflow["layout_schedule"]["path"])
        schedule = json.loads(schedule_path.read_text(encoding="utf-8"))
        self.assertEqual(schedule["deadline_at"], workflow["layout_deadline_at"])
        self.assertEqual(len(schedule["strategies"]), len(orchestrator.LAYOUT_STRATEGIES))
        self.assertGreaterEqual(len(attempts), 1)
        self.assertLessEqual(len(attempts), len(orchestrator.LAYOUT_STRATEGIES))
        self.assertEqual(len(workflow["layout_attempt_keys"]), len(set(workflow["layout_attempt_keys"])))
        for attempt in attempts:
            request_path = Path(attempt["layout_request"]["path"])
            result_path = Path(attempt["layout_result"]["path"])
            request = json.loads(request_path.read_text(encoding="utf-8"))
            layout_result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(request["semantic_plan_sha256"], plan["sha256"])
            self.assertEqual(
                layout_result["request_sha256"],
                orchestrator.canonical_json_sha256(request),
            )
            self.assertTrue(Path(attempt["backend_evidence"]["path"]).is_file())
            self.assertTrue(Path(attempt["candidate"]["path"]).is_file())
            self.assertTrue(Path(attempt["validation"]["report"]).is_file())
            self.assertTrue(Path(attempt["validation"]["receipt"]).is_file())
        events = orchestrator.lifecycle_v2.replay(run_dir)["events"]
        layout_events = [
            item["event"]
            for item in events
            if item["event"]["event_type"] == "tool_attempt"
            and item["event"]["payload"].get("tool") == "layout-engine"
        ]
        self.assertTrue(layout_events)
        self.assertTrue(
            any(
                event["payload"].get("artifact_snapshots", {}).get("layout_request")
                for event in layout_events
            )
        )

    def test_layout_strategy_schedule_is_finite_and_has_one_python_fallback(self):
        self.assertEqual(
            orchestrator.LAYOUT_STRATEGIES,
            (
                ("elk-default", {"spacing": 1.0, "port_separation": 1.0, "shared_penalty": 1.0}),
                ("elk-spacing", {"spacing": 1.35, "port_separation": 1.0, "shared_penalty": 1.0}),
                ("elk-separated", {"spacing": 1.35, "port_separation": 1.4, "shared_penalty": 1.6}),
                ("python-fallback", {}),
            ),
        )
        self.assertEqual(
            sum(name == "python-fallback" for name, _ in orchestrator.LAYOUT_STRATEGIES),
            1,
        )

    def test_generic_create_falls_back_from_elk_to_python_on_the_same_request_digest(self):
        root, workspace, cli = self.create_workspace()
        target = workspace / "elk-fallback.drawio"
        failure_evidence = {
            "request_sha256": None,
            "backend_requested": "elk",
            "fallback_reason": "elk_nonzero_exit",
        }
        observed = {}

        def fail_elk(request, **kwargs):
            digest = orchestrator.canonical_json_sha256(request)
            observed["elk"] = digest
            evidence = dict(failure_evidence)
            evidence["request_sha256"] = digest
            raise orchestrator.layout_backend.BackendExecutionError(
                "elk_nonzero_exit",
                evidence,
            )

        with mock.patch.object(
            orchestrator.layout_backend,
            "resolve_node",
            return_value=Path("/usr/bin/true"),
        ), mock.patch.object(
            orchestrator.layout_backend,
            "run_elk",
            side_effect=fail_elk,
        ):
            result = orchestrator.start_run(
                "create",
                target,
                "Create an ELK fallback diagram.",
                workspace,
                cli,
                run_id="elk-fallback-run",
                max_iterations=1,
            )

        workflow = json.loads(
            (Path(result["run_dir"]) / "workflow.json").read_text(encoding="utf-8")
        )
        first = workflow["layout_attempts"][0]
        evidence = json.loads(
            Path(first["backend_evidence"]["path"]).read_text(encoding="utf-8")
        )
        self.assertEqual(observed["elk"], evidence["request_sha256"])
        self.assertEqual(
            evidence["elk_attempt"]["request_sha256"],
            evidence["request_sha256"],
        )
        self.assertEqual(evidence["backend_selected"], "python-layered")

    def test_ambiguous_intake_returns_native_selection_without_allocating_run(self):
        root, workspace, cli = self.create_workspace()
        pending = self.run_host(
            "create", "--workspace", workspace, "--cli", cli,
            "Покажи сервисы и их зависимости",
        )
        self.assertEqual(pending.returncode, 0, pending.stderr)
        result = json.loads(pending.stdout)
        self.assertEqual(result["status"], "awaiting_input")
        self.assertEqual(
            result["classification"]["candidates"], ["c4", "dependency"]
        )
        self.assertEqual(
            result["selection_required"]["question"],
            result["questions"][0],
        )
        self.assertFalse((workspace / ".diagram-runs").exists())
        intake_dir = (
            workspace / ".diagram-intake" / result["intake_id"]
        )
        self.assertTrue((intake_dir / "roles" / "semantic-intake" / "input.json").is_file())
        self.assertTrue((intake_dir / "roles" / "semantic-intake" / "output.json").is_file())

    def test_intake_answer_replay_completes_then_allocates_run(self):
        root, workspace, cli = self.create_workspace()
        pending = self.run_host(
            "create", "--workspace", workspace, "--cli", cli,
            "Покажи сервисы и их зависимости",
        )
        self.assertEqual(pending.returncode, 0, pending.stderr)
        selection = json.loads(pending.stdout)
        question_id = selection["questions"][0]["question_id"]
        completed = self.run_host(
            "create", "--workspace", workspace, "--cli", cli,
            "--intake-id", selection["intake_id"],
            "--intake-answer", f"{question_id}=dependency",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertIn("run_id", result)
        completed_intake = (
            workspace / ".diagram-intake" / f"{selection['intake_id']}.json"
        )
        self.assertTrue(completed_intake.is_file())
        copied = Path(result["run_dir"]) / "inputs" / "diagram-intake.json"
        self.assertEqual(copied.read_bytes(), completed_intake.read_bytes())

    def test_sequential_replay_accumulates_prior_bound_answers(self):
        root, workspace, cli = self.create_workspace()
        first = self.run_host(
            "create", "--workspace", workspace, "--cli", cli,
            "Создай generic диаграмму с последовательными вопросами",
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        first_result = json.loads(first.stdout)
        first_id = first_result["questions"][0]["question_id"]
        second = self.run_host(
            "create", "--workspace", workspace, "--cli", cli,
            "--intake-id", first_result["intake_id"],
            "--intake-answer", f"{first_id}=answer-1",
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        second_result = json.loads(second.stdout)
        second_id = second_result["questions"][0]["question_id"]
        self.assertNotEqual(first_id, second_id)
        completed = self.run_host(
            "create", "--workspace", workspace, "--cli", cli,
            "--intake-id", first_result["intake_id"],
            "--intake-answer", f"{second_id}=answer-2",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("run_id", json.loads(completed.stdout))

    def test_assumption_acceptance_replays_through_bound_answer(self):
        root, workspace, cli = self.create_workspace()
        pending = self.run_host(
            "create", "--workspace", workspace, "--cli", cli,
            "Создай generic диаграмму с допущением",
        )
        self.assertEqual(pending.returncode, 0, pending.stderr)
        result = json.loads(pending.stdout)
        question = result["questions"][0]
        self.assertEqual(question["kind"], "assumption_acceptance")
        completed = self.run_host(
            "create", "--workspace", workspace, "--cli", cli,
            "--intake-id", result["intake_id"],
            "--intake-answer", f"{question['question_id']}=accept",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("run_id", json.loads(completed.stdout))

    def test_consolidated_free_text_replay_resolves_gap_without_accepting_it(self):
        root, workspace, cli = self.create_workspace()
        pending = self.run_host(
            "create", "--workspace", workspace, "--cli", cli,
            "Создай generic диаграмму с четырьмя вопросами",
        )
        self.assertEqual(pending.returncode, 0, pending.stderr)
        result = json.loads(pending.stdout)
        for index in range(1, 4):
            question = result["questions"][0]
            pending = self.run_host(
                "create", "--workspace", workspace, "--cli", cli,
                "--intake-id", result["intake_id"],
                "--intake-answer",
                f"{question['question_id']}=answer-{index}",
            )
            self.assertEqual(pending.returncode, 0, pending.stderr)
            result = json.loads(pending.stdout)
        consolidated = result["questions"][0]
        self.assertEqual(consolidated["kind"], "consolidated")
        completed = self.run_host(
            "create", "--workspace", workspace, "--cli", cli,
            "--intake-id", result["intake_id"],
            "--intake-answer",
            f"{consolidated['question_id']}=Возврат к проверке оплаты",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        completed_result = json.loads(completed.stdout)
        self.assertIn("run_id", completed_result)
        intake = json.loads(
            Path(completed_result["command_resolution"]["intake"]).read_text()
        )
        self.assertEqual(intake["assumptions"], [])

    def test_create_does_not_discover_workspace_openspec_as_source(self):
        root, workspace, cli = self.create_workspace()
        spec = workspace / "openspec" / "specs" / "payment-routing" / "spec.md"
        spec.parent.mkdir(parents=True)
        spec.write_text(
            """# Payment Routing Specification

## Purpose
Route approved payments to settlement.

## Requirements
- OPEN_SPEC_DISCOVERY_SENTINEL must appear in the settlement node.
""",
            encoding="utf-8",
        )
        request = "Create a two-step customer onboarding diagram."

        completed = self.run_host(
            "create",
            "--workspace",
            workspace,
            "--cli",
            cli,
            "--diagram",
            workspace / "onboarding.drawio",
            "--request",
            request,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        source_bundle, _ = orchestrator.lifecycle_v2.latest_document(
            Path(result["run_dir"]), "source-bundle"
        )
        self.assertEqual(
            [source["kind"] for source in source_bundle["sources"]],
            ["original_user_request"],
        )
        self.assertEqual(source_bundle["sources"][0]["content"], request)
        serialized_sources = json.dumps(source_bundle["sources"], ensure_ascii=False)
        self.assertNotIn("explicit_user_document", serialized_sources)
        self.assertNotIn("OPEN_SPEC_DISCOVERY_SENTINEL", serialized_sources)
        self.assertNotIn("openspec/specs/payment-routing/spec.md", serialized_sources)

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

    def test_bare_improve_uses_latest_hash_matching_completed_review(self):
        root, workspace, cli = self.create_workspace()
        reviewed = workspace / "reviewed.drawio"
        reviewed.write_text(clean_diagram(), encoding="utf-8")
        (workspace / "other.drawio").write_text(clean_diagram(), encoding="utf-8")
        review = orchestrator.diagram_host.run_review(
            reviewed, workspace, cli, run_id="review-handoff"
        )
        self.assertEqual(review["status"], "passed")

        completed = self.run_host(
            "improve", "--workspace", workspace, "--cli", cli, qwen_args=""
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        resolution = result["command_resolution"]
        self.assertEqual(resolution["diagram"], str(reviewed.resolve()))
        self.assertEqual(resolution["diagram_selection"], "latest_completed_review")
        self.assertEqual(
            resolution["request"], orchestrator.command_ux.DEFAULT_IMPROVE_REQUEST
        )
        self.assertEqual(
            resolution["request_source"], "default_review_findings_request"
        )
        self.assertEqual(resolution["review_handoff"]["run_id"], review["run_id"])
        self.assertEqual(
            resolution["review_handoff"]["artifact_sha256"],
            hashlib.sha256(reviewed.read_bytes()).hexdigest(),
        )
        source_bundle, _ = orchestrator.lifecycle_v2.latest_document(Path(result["run_dir"]), "source-bundle")
        handoff = source_bundle["evidence"]["eligible_review_handoff"]
        self.assertIsNotNone(handoff)
        self.assertEqual(
            handoff["artifact"]["path"],
            "inputs/review-handoff/artifact.drawio",
        )
        self.assertEqual(
            handoff["verdict"]["path"],
            "inputs/review-handoff/verdict.json",
        )
        self.assertNotIn(
            "explicit_user_document",
            [source["kind"] for source in source_bundle["sources"]],
        )

    def test_bare_improve_falls_back_to_only_workspace_diagram(self):
        root, workspace, cli = self.create_workspace()
        target = workspace / "only.drawio"
        target.write_text(clean_diagram(), encoding="utf-8")

        completed = self.run_host(
            "improve", "--workspace", workspace, "--cli", cli, qwen_args=""
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        resolution = result["command_resolution"]
        self.assertEqual(resolution["diagram"], str(target.resolve()))
        self.assertEqual(resolution["diagram_selection"], "only_drawio_in_workspace")
        self.assertEqual(
            resolution["request_source"], "default_review_findings_request"
        )
        self.assertNotIn("review_handoff", resolution)

    def test_improve_applies_host_role_policy_to_captured_supervisor_decision(self):
        root, workspace, _ = self.create_workspace()
        target = workspace / "corporate-supervisor-output.drawio"
        target.write_text(clean_diagram(), encoding="utf-8")
        cli = root / "corporate-supervisor-output-cli.py"
        cli.write_text(
            FAKE_GIGACODE.replace(
                '"action": "create" if payload["mode"] == "create" else "analyze",',
                '"action": "repair",',
                1,
            ).replace(
                '"required_roles": ["supervisor", "semantic_analyst", "reviewer"],',
                '"required_roles": ["repair", "reviewer"],',
                1,
            ),
            encoding="utf-8",
        )
        cli.chmod(0o755)

        completed = self.run_host(
            "improve", "--workspace", workspace, "--cli", cli, qwen_args=""
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertNotEqual(result["status"], "error")
        self.assertEqual(
            result["role_policy"],
            {
                "supervisor_action": "repair",
                "supervisor_action_normalized": False,
                "supervisor_declared_action": "repair",
                "supervisor_declared_roles": ["repair", "reviewer"],
                "host_mandatory_roles": ["repair", "reviewer", "semantic_analyst", "supervisor"],
                "effective_required_roles": ["repair", "reviewer", "semantic_analyst", "supervisor"],
            },
        )
        workflow = json.loads(Path(result["evidence"]["workflow"]).read_text())
        self.assertEqual(workflow["supervisor_decision"]["result"]["action"], "repair")
        self.assertEqual(
            workflow["supervisor_decision"]["result"]["required_roles"],
            ["repair", "reviewer"],
        )
        self.assertEqual(
            workflow["supervisor_declared_roles"], ["repair", "reviewer"]
        )
        self.assertEqual(
            workflow["host_mandatory_roles"],
            ["repair", "reviewer", "semantic_analyst", "supervisor"],
        )
        self.assertEqual(
            workflow["required_roles"],
            ["repair", "reviewer", "semantic_analyst", "supervisor"],
        )
        trace = orchestrator.trace_run(result["run_dir"], workspace)
        self.assertEqual(trace["role_policy"], result["role_policy"])

    def test_initial_create_host_policy_authorizes_conditional_repair(self):
        workflow = {"mode": "create"}
        decision = {
            "schema_version": 1,
            "role": "supervisor",
            "status": "ok",
            "result": {
                "action": "create",
                "reason": "Create the requested diagram",
                "required_roles": ["semantic_analyst", "reviewer"],
            },
        }

        orchestrator.consume_supervisor_decision(
            workflow, decision, phase="initial", requested_max_iterations=3,
        )

        self.assertEqual(workflow["supervisor_declared_roles"], ["reviewer", "semantic_analyst"])
        self.assertEqual(
            workflow["host_mandatory_roles"],
            ["repair", "reviewer", "semantic_analyst", "supervisor"],
        )
        self.assertIn("repair", workflow["required_roles"])

    def test_review_candidate_returns_host_bound_v2_verdict_path(self):
        root, workspace, cli = self.create_workspace()
        run_dir = workspace / ".diagram-runs" / "review-candidate-path"
        run_dir.mkdir(parents=True)
        candidate = run_dir / "candidate.drawio"
        report = run_dir / "validation-report.json"
        receipt = run_dir / "validation-receipt.json"
        patch = run_dir / "patch.json"
        baseline_receipt_v2 = run_dir / "baseline-validation-receipt.v2.json"
        input_path = run_dir / "input.json"
        output_path = run_dir / "output.json"
        verdict_v2_path = run_dir / "verdict.v2.json"
        for path in (candidate, report, receipt, patch, baseline_receipt_v2, input_path, output_path):
            path.write_text("{}", encoding="utf-8")
        verdict_v2_path.write_text('{"schema_version": 2}', encoding="utf-8")
        workflow = {
            "workspace": str(workspace),
            "validation_receipt_v2": {"path": str(baseline_receipt_v2)},
            "accepted_artifact": {"path": str(candidate)},
            "accepted_validation": {"report": str(report)},
        }
        analysis = {"schema_version": 2, "verdict": "approve", "findings": []}
        runtime = {"resolution": {"resolved_model": "vllm/DeepSeek-V4-Flash-262k"}}

        with mock.patch.object(orchestrator, "_reviewer_input_v2", return_value={}), \
             mock.patch.object(orchestrator, "role_call", return_value=(analysis, runtime, input_path, output_path)), \
             mock.patch.object(orchestrator, "_verify_reviewer_runtime"), \
             mock.patch.object(orchestrator, "_bind_reviewer_v2", return_value=({}, verdict_v2_path)):
            _, _, returned_path = orchestrator._review_candidate(
                run_dir, workflow, candidate, report, receipt, patch, cli, 30, "reviewer-1",
            )

        self.assertEqual(returned_path, verdict_v2_path)
        self.assertNotEqual(returned_path, output_path)
        self.assertEqual(workflow["candidate_reviewer_verdict_v2"]["path"], str(verdict_v2_path.resolve()))

    def test_resume_host_policy_does_not_rerun_semantic_analyst(self):
        workflow = {"mode": "improve"}
        decision = {
            "schema_version": 1,
            "role": "supervisor",
            "status": "ok",
            "result": {
                "action": "repair",
                "reason": "Continue from the approved semantic plan",
                "required_roles": ["reviewer"],
            },
        }

        orchestrator.consume_supervisor_decision(
            workflow, decision, phase="resume", requested_max_iterations=3,
        )

        self.assertEqual(
            workflow["host_mandatory_roles"], ["repair", "reviewer", "supervisor"]
        )
        self.assertNotIn("semantic_analyst", workflow["required_roles"])

    def test_host_role_policy_still_rejects_phase_incompatible_action(self):
        workflow = {"mode": "create"}
        decision = {
            "schema_version": 1,
            "role": "supervisor",
            "status": "ok",
            "result": {
                "action": "repair",
                "reason": "Repair is incompatible with initial create",
                "required_roles": ["repair", "reviewer"],
            },
        }

        with self.assertRaisesRegex(
            orchestrator.supervisor.SupervisorError,
            "not executable during the initial phase",
        ):
            orchestrator.consume_supervisor_decision(
                workflow, decision, phase="initial", requested_max_iterations=3,
            )

        self.assertNotIn("host_mandatory_roles", workflow)

    def test_bare_improve_rejects_stale_review_in_ambiguous_workspace(self):
        root, workspace, cli = self.create_workspace()
        reviewed = workspace / "reviewed.drawio"
        reviewed.write_text(clean_diagram(), encoding="utf-8")
        (workspace / "other.drawio").write_text(clean_diagram(), encoding="utf-8")
        orchestrator.diagram_host.run_review(
            reviewed, workspace, cli, run_id="stale-review"
        )
        reviewed.write_text(clean_diagram().replace("Step", "Changed"), encoding="utf-8")

        completed = self.run_host(
            "improve", "--workspace", workspace, "--cli", cli, qwen_args=""
        )
        self.assertEqual(completed.returncode, 2)
        result = json.loads(completed.stderr)
        self.assertEqual(result["status"], "selection_required")
        self.assertEqual(result["code"], "diagram_selection_ambiguous")
        self.assertEqual(
            sorted(path.name for path in (workspace / ".diagram-runs").iterdir()),
            ["stale-review"],
        )

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

        resumed_again = orchestrator.resume_run(run_dir, "approve", "", workspace, cli)
        self.assertIn(resumed_again["status"], {"completed", "already_applied"})
        self.assertEqual(resumed_again["accepted_artifact"]["path"], resumed["accepted_artifact"]["path"])
        self.assertEqual(
            hashlib.sha256(Path(resumed_again["accepted_artifact"]["path"]).read_bytes()).hexdigest(),
            hashlib.sha256(target.read_bytes()).hexdigest(),
        )

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

    def test_checkpointed_generic_create_resumes_through_layout_pipeline(self):
        root, workspace, _ = self.create_workspace()
        cli = root / "checkpointed-create-cli.py"
        cli.write_text(
            FAKE_GIGACODE.replace(
                'requires_human = payload["mode"] == "improve"',
                'requires_human = payload["mode"] == "improve" or "clarify-create" in payload["request"]',
            ),
            encoding="utf-8",
        )
        cli.chmod(0o755)
        target = workspace / "checkpointed-create.drawio"

        pending = orchestrator.start_run(
            "create",
            target,
            "clarify-create before rendering",
            workspace,
            cli,
            run_id="checkpointed-create-run",
            max_iterations=1,
        )
        self.assertEqual(pending["checkpoint"]["kind"], "semantic_approval")
        resumed = orchestrator.resume_run(
            Path(pending["run_dir"]),
            "continue",
            "Approve the proposed create semantics.",
            workspace,
            cli,
        )

        workflow = json.loads(
            (Path(resumed["run_dir"]) / "workflow.json").read_text(encoding="utf-8")
        )
        self.assertTrue(workflow["layout_attempts"])
        self.assertEqual(
            workflow["renderer_adapter"]["command"],
            ["host:execute_layout_attempt"],
        )
        self.assertNotEqual(
            workflow["renderer_adapter"]["options"]["backend"],
            "legacy-generic-v2",
        )

    def test_create_with_explicit_roadmap_source_uses_roadmap_local_and_publishes(self):
        root, workspace, _ = self.create_workspace()
        target = workspace / "roadmap.drawio"
        source = workspace / "roadmap.yaml"
        source.write_text(
            (ROOT / "tests" / "fixtures" / "roadmap" / "basic.yaml").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        cli = root / "roadmap-gigacode.py"
        cli.write_text(
            FAKE_GIGACODE.replace('"diagram_type": "generic"', '"diagram_type": "roadmap"', 1),
            encoding="utf-8",
        )
        cli.chmod(0o755)

        created = self.run_host(
            "create",
            "--workspace",
            workspace,
            "--cli",
            cli,
            "--diagram",
            target,
            "--renderer-source",
            source,
            "--request",
            "Create a roadmap diagram from an explicit roadmap source.",
        )
        self.assertEqual(created.returncode, 0, created.stderr)
        result = json.loads(created.stdout)
        self.assertEqual(result["status"], "awaiting_human")
        self.assertEqual(result["checkpoint"]["kind"], "final_acceptance")
        self.assertEqual(result["command_resolution"]["renderer_source"], str(source.resolve()))
        workflow = json.loads((Path(result["run_dir"]) / "workflow.json").read_text(encoding="utf-8"))
        renderer = workflow["renderer_adapter"]
        self.assertEqual(renderer["adapter_id"], "roadmap-local")
        self.assertFalse(renderer["fallback"])
        self.assertEqual(renderer["validation_profile"], "roadmap")
        self.assertEqual(renderer["requested_semantic_diagram_type"], "roadmap")
        self.assertTrue(renderer["source_path"].endswith(".json"))
        self.assertIn("/inputs/renderer-sources/", renderer["source_path"])
        self.assertEqual(renderer["source_binding"]["kind"], "explicit_user_document")
        rendered_source = json.loads(Path(renderer["source_path"]).read_text(encoding="utf-8"))
        self.assertEqual(
            renderer["source_binding"]["content_sha256"],
            hashlib.sha256(
                json.dumps(
                    rendered_source,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        )
        self.assertTrue((Path(result["run_dir"]) / "attempts" / "baseline" / "validation-report.json").is_file())

        resumed = self.run_host(
            "resume",
            "--workspace",
            workspace,
            "--cli",
            cli,
            "--run",
            result["run_id"],
            "approve",
        )
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        resumed_result = json.loads(resumed.stdout)
        self.assertEqual(resumed_result["status"], "completed")
        self.assertTrue(target.is_file())
        trace = orchestrator.trace_run(result["run_dir"], workspace)
        self.assertTrue(trace["valid"])
        self.assertTrue(all(not role["fallback_used"] for role in trace["roles"]))

    def test_create_baseline_reviewer_needs_human_checkpoints_without_repair(self):
        root, workspace, _ = self.create_workspace()
        cli = root / "needs-human-reviewer.py"
        cli.write_text(
            FAKE_GIGACODE.replace(
                '"role": "reviewer",\n        "status": "ok",',
                '"role": "reviewer",\n        "status": "needs_human",',
                1,
            ).replace(
                '"analysis_id": "review-test",\n        "verdict": "approve",',
                '"analysis_id": "review-test",\n        "verdict": "needs_human",',
                1,
            ),
            encoding="utf-8",
        )
        cli.chmod(0o755)
        target = workspace / "needs-human.drawio"

        result = orchestrator.start_run(
            "create",
            target,
            "Create a diagram whose meaning needs human review.",
            workspace,
            cli,
            run_id="baseline-needs-human-run",
            max_iterations=4,
        )

        self.assertEqual(result["status"], "awaiting_human")
        self.assertEqual(result["state"], "awaiting_feedback")
        self.assertEqual(result["checkpoint"]["kind"], "feedback")
        self.assertEqual(
            result["checkpoint"]["evidence"]["failure_class"],
            "reviewer_needs_human",
        )
        self.assertIsNone(result["publishable_candidate"])
        self.assertFalse(target.exists())
        self.assertNotIn(
            "repair",
            {
                role["role"]
                for role in result["role_runs"]
            },
        )

    def test_publication_recovery_continues_an_interrupted_publish_transaction(self):
        root, workspace, cli = self.create_workspace()
        target = workspace / "recoverable.drawio"

        created = orchestrator.start_run(
            "create",
            target,
            "Create a publication recovery diagram.",
            workspace,
            cli,
            run_id="recoverable-run",
            max_iterations=1,
        )
        self.assertEqual(created["checkpoint"]["kind"], "final_acceptance")
        run_dir = workspace / ".diagram-runs" / "recoverable-run"

        with mock.patch.object(
            orchestrator.lifecycle_v2,
            "_continue_publication",
            side_effect=RuntimeError("simulated publication crash"),
        ):
            with self.assertRaises(RuntimeError):
                orchestrator.resume_run(run_dir, "approve", "", workspace, cli)

        replayed = orchestrator.lifecycle_v2.replay(run_dir)
        self.assertTrue(replayed["valid"])
        self.assertIn("publication-transaction", replayed["latest_snapshots"])

        recovered = orchestrator.lifecycle_v2.recover_publication(run_dir)
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered["status"], "committed")
        self.assertTrue(target.is_file())
        self.assertEqual(
            hashlib.sha256(target.read_bytes()).hexdigest(),
            recovered["published_sha256"],
        )

    def test_publication_recovery_commits_bytes_published_transaction_without_rewriting_target(self):
        _, workspace, _ = self.create_workspace()
        target = workspace / "bytes-published.drawio"
        run_dir = workspace / ".diagram-runs" / "bytes-published-run"
        orchestrator.lifecycle_v2.initialize(
            run_dir=run_dir,
            workspace=workspace,
            target=target,
            run_id="bytes-published-run",
            mode="create",
            request="Create a bytes-published recovery diagram.",
            extension_root=ROOT,
            explicit_documents=(),
        )
        accepted = run_dir / "accepted" / "baseline.drawio"
        accepted.parent.mkdir(parents=True, exist_ok=True)
        accepted.write_text(clean_diagram(), encoding="utf-8")
        orchestrator.supervisor.run_validation(accepted, run_dir, attempt_id="baseline")
        report = run_dir / "attempts" / "baseline" / "validation-report.json"
        legacy_receipt = run_dir / "attempts" / "baseline" / "validation-receipt.json"
        receipt_v2, receipt_v2_path = orchestrator.lifecycle_v2.mirror_validation_receipt(
            run_dir,
            legacy_receipt_path=legacy_receipt,
        )
        receipt_v2["run_id"] = "bytes-published-run"
        receipt_v2_path.write_text(json.dumps(receipt_v2, ensure_ascii=False), encoding="utf-8")
        receipt_v2_verification = orchestrator.lifecycle_v2.verify_v2_receipt(run_dir, receipt_v2_path)
        accepted_descriptor = orchestrator.lifecycle_v2.make_file_descriptor(accepted, root=run_dir)
        report_descriptor = orchestrator.lifecycle_v2.make_file_descriptor(report, root=run_dir)
        receipt_descriptor = orchestrator.lifecycle_v2.make_file_descriptor(receipt_v2_path, root=run_dir)
        reviewer_v2_path = run_dir / "reviewer-verdict.v2.json"
        reviewer_v2 = {
            "schema_version": 2,
            "verdict_id": "review-v2-bytes-published",
            "analysis_id": "analysis-v2-bytes-published",
            "run_id": "bytes-published-run",
            "analysis_sha256": "1" * 64,
            "role_input_sha256": orchestrator.supervisor.sha256_file(receipt_v2_path),
            "role_output_sha256": "3" * 64,
            "bindings": {
                "candidate_sha256": orchestrator.supervisor.sha256_file(accepted),
                "report_sha256": orchestrator.supervisor.sha256_file(report),
                "receipt_sha256": orchestrator.supervisor.sha256_file(receipt_v2_path),
                "source_bundle_sha256": "6" * 64,
                "semantic_plan_sha256": None,
                "semantic_delta_sha256": None,
            },
            "runtime_proof": {
                "requested_model": "vllm/DeepSeek-V4-Flash-262k",
                "resolved_model": "vllm/DeepSeek-V4-Flash-262k",
                "provider": "vllm",
                "resolution_mode": "isolated_cli",
                "attempt_id": "bytes-published-review-1",
                "evidence_sha256": "5" * 64,
            },
            "verdict": "approve",
            "reviewed_at": "2026-07-22T12:00:00+00:00",
            "findings": [],
        }
        reviewer_v2_path.write_text(json.dumps(reviewer_v2, ensure_ascii=False), encoding="utf-8")
        orchestrator.lifecycle_v2.transition(
            run_dir,
            "final_review",
            accepted_artifact=accepted_descriptor,
            validation_report=report_descriptor,
            validation_receipt=receipt_descriptor,
        )
        workflow, _ = orchestrator.lifecycle_v2.latest_document(run_dir, "workflow")
        workflow["status"] = "final_review"
        workflow["accepted_artifact"] = accepted_descriptor
        workflow["accepted_validation"] = {
            "report": str(report.resolve()),
            "receipt": str(receipt_v2_path.resolve()),
            "report_sha256": report_descriptor["sha256"],
            "receipt_sha256": receipt_descriptor["sha256"],
            "strict_passed": receipt_v2_verification["strict_passed"],
        }
        workflow["validation_receipt_v2"] = receipt_descriptor
        orchestrator.write_workflow(run_dir, workflow)

        real_advance_publication = orchestrator.lifecycle_v2._advance_publication

        def advance_publication_with_crash(run_dir, publication, *, status, event_type, payload=None):
            result = real_advance_publication(
                run_dir,
                publication,
                status=status,
                event_type=event_type,
                payload=payload,
            )
            if status == "bytes_published":
                raise RuntimeError("simulated publication crash after bytes were written")
            return result

        with mock.patch.object(
            orchestrator.lifecycle_v2,
            "_advance_publication",
            side_effect=advance_publication_with_crash,
        ):
            with self.assertRaises(RuntimeError):
                orchestrator.lifecycle_v2.publish_transaction(
                    run_dir,
                    accepted_artifact=accepted,
                    validation_report=report,
                    validation_receipt=receipt_v2_path,
                    reviewer_verdict=reviewer_v2_path,
                    decision="approve",
                )

        replayed = orchestrator.lifecycle_v2.replay(run_dir)
        self.assertTrue(replayed["valid"])
        publication, _ = orchestrator.lifecycle_v2.latest_document(run_dir, "publication-transaction", replayed)
        self.assertEqual(publication["status"], "bytes_published")
        target_hash_before = hashlib.sha256(target.read_bytes()).hexdigest()
        target_mtime_before = target.stat().st_mtime_ns

        trace_before = orchestrator.trace_run(run_dir, workspace)
        self.assertTrue(trace_before["control_plane_v2"]["valid"])
        self.assertTrue(trace_before["control_plane_v2"]["publication_valid"])
        self.assertEqual(trace_before["control_plane_v2"]["status"], "verified")

        recovered = orchestrator.lifecycle_v2.recover_publication(run_dir)
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered["status"], "committed")
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), target_hash_before)
        self.assertEqual(target.stat().st_mtime_ns, target_mtime_before)
        self.assertEqual(recovered["published_sha256"], target_hash_before)

    def test_publish_transaction_rejects_approve_with_findings_when_error_findings_remain(self):
        _, workspace, _ = self.create_workspace()
        target = workspace / "findings-blocked.drawio"
        run_dir = workspace / ".diagram-runs" / "findings-blocked-run"
        orchestrator.lifecycle_v2.initialize(
            run_dir=run_dir,
            workspace=workspace,
            target=target,
            run_id="findings-blocked-run",
            mode="create",
            request="Create a findings-blocked publication diagram.",
            extension_root=ROOT,
            explicit_documents=(),
        )
        accepted = run_dir / "accepted" / "baseline.drawio"
        accepted.parent.mkdir(parents=True, exist_ok=True)
        accepted.write_text(clean_diagram(), encoding="utf-8")
        orchestrator.supervisor.run_validation(accepted, run_dir, attempt_id="baseline")
        report = run_dir / "attempts" / "baseline" / "validation-report.json"
        legacy_receipt = run_dir / "attempts" / "baseline" / "validation-receipt.json"
        receipt_v2, receipt_v2_path = orchestrator.lifecycle_v2.mirror_validation_receipt(
            run_dir,
            legacy_receipt_path=legacy_receipt,
        )
        report_value = orchestrator.supervisor.load_json(report)
        report_value["findings"].append(
            {
                "layer": "artifact-parse",
                "severity": "error",
                "code": "artifact.id.duplicate",
                "path": "/pages/0",
                "message": "duplicate id",
            }
        )
        report.write_text(json.dumps(report_value, ensure_ascii=False), encoding="utf-8")
        stdout_path = run_dir / "attempts" / "baseline" / "validator.stdout"
        stdout_path.write_text(json.dumps(report_value, ensure_ascii=False), encoding="utf-8")
        receipt_v2["run_id"] = "final-approval-run"
        receipt_v2["outputs"]["report"]["sha256"] = orchestrator.supervisor.sha256_file(report)
        receipt_v2["outputs"]["stdout"]["sha256"] = orchestrator.supervisor.sha256_file(stdout_path)
        receipt_v2["outputs"]["report"]["byte_length"] = report.stat().st_size
        receipt_v2["outputs"]["stdout"]["byte_length"] = stdout_path.stat().st_size
        receipt_v2_path.write_text(json.dumps(receipt_v2, ensure_ascii=False), encoding="utf-8")
        receipt_v2_verification = orchestrator.lifecycle_v2.verify_v2_receipt(run_dir, receipt_v2_path)
        accepted_descriptor = orchestrator.lifecycle_v2.make_file_descriptor(accepted, root=run_dir)
        report_descriptor = orchestrator.lifecycle_v2.make_file_descriptor(report, root=run_dir)
        receipt_descriptor = orchestrator.lifecycle_v2.make_file_descriptor(receipt_v2_path, root=run_dir)
        orchestrator.lifecycle_v2.transition(
            run_dir,
            "final_review",
            accepted_artifact=accepted_descriptor,
            validation_report=report_descriptor,
            validation_receipt=receipt_descriptor,
        )
        workflow, _ = orchestrator.lifecycle_v2.latest_document(run_dir, "workflow")
        workflow["status"] = "final_review"
        workflow["accepted_artifact"] = accepted_descriptor
        workflow["accepted_validation"] = {
            "report": str(report.resolve()),
            "receipt": str(receipt_v2_path.resolve()),
            "report_sha256": report_descriptor["sha256"],
            "receipt_sha256": receipt_descriptor["sha256"],
            "strict_passed": receipt_v2_verification["strict_passed"],
        }
        workflow["validation_receipt_v2"] = receipt_descriptor
        orchestrator.write_workflow(run_dir, workflow)

        with self.assertRaisesRegex(orchestrator.lifecycle_v2.ContractError, "approve_with_findings requires strict pass with warnings only"):
            orchestrator.lifecycle_v2.publish_transaction(
                run_dir,
                accepted_artifact=accepted,
                validation_report=report,
                validation_receipt=receipt_v2_path,
                decision="approve_with_findings",
            )

    def test_final_approval_eligibility_allows_only_approve_with_findings_for_warning_only_findings(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            run_dir = temp / "run"
            orchestrator.lifecycle_v2.initialize(
                run_dir=run_dir,
                workspace=temp,
                target=temp / "diagram.drawio",
                run_id="final-approval-run",
                mode="create",
                request="Create a diagram with warning-only findings.",
                extension_root=ROOT,
                explicit_documents=(),
            )
            accepted = run_dir / "accepted" / "baseline.drawio"
            accepted.parent.mkdir(parents=True, exist_ok=True)
            accepted.write_text(clean_diagram(), encoding="utf-8")
            orchestrator.supervisor.run_validation(accepted, run_dir, attempt_id="baseline")
            report = run_dir / "attempts" / "baseline" / "validation-report.json"
            legacy_receipt = run_dir / "attempts" / "baseline" / "validation-receipt.json"
            receipt_v2, receipt_v2_path = orchestrator.lifecycle_v2.mirror_validation_receipt(
                run_dir,
                legacy_receipt_path=legacy_receipt,
            )
            receipt_v2["run_id"] = "final-approval-run"
            report_value = orchestrator.supervisor.load_json(report)
            report_value["findings"] = [
                {
                    "layer": "layout",
                    "severity": "warning",
                    "code": "test.warning",
                    "path": "/pages/0",
                    "message": "warning-only finding",
                },
                {
                    "layer": "layout",
                    "severity": "info",
                    "code": "test.info",
                    "path": "/pages/0",
                    "message": "info-only finding",
                },
            ]
            report.write_text(json.dumps(report_value, ensure_ascii=False), encoding="utf-8")
            stdout_path = run_dir / "attempts" / "baseline" / "validator.stdout"
            stdout_path.write_text(json.dumps(report_value, ensure_ascii=False), encoding="utf-8")
            receipt_v2["outputs"]["report"]["sha256"] = orchestrator.supervisor.sha256_file(report)
            receipt_v2["outputs"]["stdout"]["sha256"] = orchestrator.supervisor.sha256_file(stdout_path)
            receipt_v2["outputs"]["report"]["byte_length"] = report.stat().st_size
            receipt_v2["outputs"]["stdout"]["byte_length"] = stdout_path.stat().st_size
            receipt_v2_path.write_text(json.dumps(receipt_v2, ensure_ascii=False), encoding="utf-8")
            receipt_v2_verification = orchestrator.lifecycle_v2.verify_v2_receipt(run_dir, receipt_v2_path)
            accepted_descriptor = orchestrator.lifecycle_v2.make_file_descriptor(accepted, root=run_dir)
            report_descriptor = orchestrator.lifecycle_v2.make_file_descriptor(report, root=run_dir)
            receipt_descriptor = orchestrator.lifecycle_v2.make_file_descriptor(receipt_v2_path, root=run_dir)
            accepted_descriptor["path"] = str(accepted.resolve())
            receipt_descriptor["path"] = str(receipt_v2_path.resolve())
            verdict_v2_path = receipt_v2_path.with_name("reviewer-verdict.v2.json")
            source_bundle_path = run_dir / "source-bundle.v2.json"
            source_bundle_value = {"schema_version": 2, "bundle": "test"}
            source_bundle_path.write_text(json.dumps(source_bundle_value, ensure_ascii=False), encoding="utf-8")
            source_bundle_descriptor = orchestrator.lifecycle_v2.make_file_descriptor(source_bundle_path, root=run_dir)
            candidate_spec_path = run_dir / "candidate-spec.v2.json"
            baseline_spec_path = run_dir / "baseline-spec.v2.json"
            patch_path = run_dir / "patch.v1.json"
            semantic_plan_path = run_dir / "semantic-plan.v2.json"
            for path, value in (
                (candidate_spec_path, {"schema_version": 2, "diagram_id": "candidate"}),
                (baseline_spec_path, {"schema_version": 2, "diagram_id": "baseline"}),
                (patch_path, {"schema_version": 1, "patch_id": "test-patch"}),
                (semantic_plan_path, {"schema_version": 2, "role": "semantic_analyst"}),
            ):
                path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
            candidate_spec_descriptor = orchestrator.lifecycle_v2.make_file_descriptor(candidate_spec_path, root=run_dir)
            baseline_spec_descriptor = orchestrator.lifecycle_v2.make_file_descriptor(baseline_spec_path, root=run_dir)
            patch_descriptor = orchestrator.lifecycle_v2.make_file_descriptor(patch_path, root=run_dir)
            semantic_plan_descriptor = orchestrator.lifecycle_v2.make_file_descriptor(semantic_plan_path, root=run_dir)
            run_root = run_dir.resolve()
            review_input_path = run_dir / "reviewer-input.v2.json"
            review_input_value = {
                "schema_version": 2,
                "run_id": "final-approval-run",
                "review_kind": "candidate_review",
                "baseline": {
                    "artifact": {"path": str(accepted.resolve().relative_to(run_root)), "sha256": orchestrator.supervisor.sha256_file(accepted), "byte_length": accepted.stat().st_size},
                    "report": {"path": str(report.resolve().relative_to(run_root)), "sha256": orchestrator.supervisor.sha256_file(report), "byte_length": report.stat().st_size},
                    "receipt": {"path": str(receipt_v2_path.resolve().relative_to(run_root)), "sha256": orchestrator.supervisor.sha256_file(receipt_v2_path), "byte_length": receipt_v2_path.stat().st_size},
                    "strict_passed": True,
                },
                "candidate": {
                    "artifact": {"path": str(accepted.resolve().relative_to(run_root)), "sha256": orchestrator.supervisor.sha256_file(accepted), "byte_length": accepted.stat().st_size},
                    "report": {"path": str(report.resolve().relative_to(run_root)), "sha256": orchestrator.supervisor.sha256_file(report), "byte_length": report.stat().st_size},
                    "receipt": {"path": str(receipt_v2_path.resolve().relative_to(run_root)), "sha256": orchestrator.supervisor.sha256_file(receipt_v2_path), "byte_length": receipt_v2_path.stat().st_size},
                    "strict_passed": True,
                },
                "baseline_spec": {"path": str(baseline_spec_path.resolve().relative_to(run_root)), "sha256": orchestrator.supervisor.sha256_file(baseline_spec_path), "content": {"schema_version": 2, "diagram_id": "baseline"}},
                "candidate_spec": {"path": str(candidate_spec_path.resolve().relative_to(run_root)), "sha256": orchestrator.supervisor.sha256_file(candidate_spec_path), "content": {"schema_version": 2, "diagram_id": "candidate"}},
                "patch": {"path": str(patch_path.resolve().relative_to(run_root)), "sha256": orchestrator.supervisor.sha256_file(patch_path), "content": {"schema_version": 1, "patch_id": "test-patch"}},
                "semantic_plan": {"path": str(semantic_plan_path.resolve().relative_to(run_root)), "sha256": orchestrator.supervisor.sha256_file(semantic_plan_path), "content": {"schema_version": 2, "role": "semantic_analyst"}},
                "semantic_delta": None,
                "source_bundle": {"path": str(source_bundle_path.resolve().relative_to(run_root)), "sha256": orchestrator.supervisor.sha256_file(source_bundle_path), "content": source_bundle_value},
                "comparison": None,
                "model_resolutions": [],
            }
            review_input_path.write_text(json.dumps(review_input_value, ensure_ascii=False), encoding="utf-8")
            review_input_descriptor = orchestrator.lifecycle_v2.make_file_descriptor(review_input_path, root=run_dir)
            verdict_v2 = {
                "schema_version": 2,
                "verdict_id": "review-v2-candidate",
                "analysis_id": "analysis-v2-candidate",
                "run_id": "final-approval-run",
                "analysis_sha256": "1" * 64,
                "role_input_sha256": orchestrator.supervisor.sha256_file(review_input_path),
                "role_output_sha256": "3" * 64,
                "bindings": {
                    "candidate_sha256": orchestrator.supervisor.sha256_file(accepted),
                    "report_sha256": orchestrator.supervisor.sha256_file(report),
                    "receipt_sha256": orchestrator.supervisor.sha256_file(receipt_v2_path),
                    "source_bundle_sha256": orchestrator.canonical_json_sha256(source_bundle_value),
                    "semantic_plan_sha256": None,
                    "semantic_delta_sha256": None,
                },
                "runtime_proof": {
                    "requested_model": "vllm/DeepSeek-V4-Flash-262k",
                    "resolved_model": "vllm/DeepSeek-V4-Flash-262k",
                    "provider": "vllm",
                    "resolution_mode": "isolated_cli",
                    "attempt_id": "candidate-review-1",
                    "evidence_sha256": "5" * 64,
                },
                "verdict": "approve",
                "reviewed_at": "2026-07-22T12:00:00+00:00",
                "findings": [],
            }
            verdict_v2_path.write_text(json.dumps(verdict_v2, ensure_ascii=False), encoding="utf-8")
            verdict_descriptor = orchestrator.lifecycle_v2.make_file_descriptor(verdict_v2_path, root=run_dir)
            verdict_descriptor["path"] = str(verdict_v2_path.resolve())
            workflow = {
                "run_id": "final-approval-run",
                "workspace": str(temp),
                "publishable_candidate": None,
                "validation_receipt_v2": receipt_descriptor,
                "accepted_artifact": accepted_descriptor,
                "accepted_validation": {
                    "report": str(report.resolve()),
                    "receipt": str(receipt_v2_path.resolve()),
                    "report_sha256": report_descriptor["sha256"],
                    "receipt_sha256": receipt_descriptor["sha256"],
                    "strict_passed": receipt_v2_verification["strict_passed"],
                },
                "candidate_reviewer_verdict_v2": {
                    "path": str(verdict_v2_path.resolve()),
                    "sha256": verdict_descriptor["sha256"],
                },
                "candidate_review_input_v2": review_input_descriptor,
            }
            workflow["working_artifact"] = accepted_descriptor
            workflow["working_validation"] = {
                "report": str(report.resolve()),
                "receipt": str(receipt_v2_path.resolve()),
                "report_sha256": report_descriptor["sha256"],
                "receipt_sha256": receipt_descriptor["sha256"],
                "strict_passed": receipt_v2_verification["strict_passed"],
            }
            with mock.patch.object(orchestrator, "_reviewer_gate_binding_error", return_value=None):
                orchestrator._set_publishable_candidate(
                    workflow,
                    artifact=accepted_descriptor,
                    validation={
                        "report": str(report.resolve()),
                        "receipt": str(receipt_v2_path.resolve()),
                        "report_sha256": report_descriptor["sha256"],
                        "receipt_sha256": receipt_descriptor["sha256"],
                        "strict_passed": receipt_v2_verification["strict_passed"],
                    },
                    receipt_v2=receipt_descriptor,
                    verdict_v2=verdict_descriptor,
                )
                workflow["candidate_review_input_v2"] = review_input_descriptor
                eligibility = orchestrator._final_approval_eligibility(run_dir, workflow)

        self.assertFalse(eligibility["approve"])
        self.assertTrue(eligibility["approve_with_findings"])
        self.assertTrue(eligibility["strict_passed"])
        self.assertEqual(eligibility["reason"], None)

    def test_publication_requires_reviewer_approve_for_every_approval_decision(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"
            run_dir.mkdir()
            accepted = run_dir / "candidate.drawio"
            report = run_dir / "validation-report.json"
            receipt = run_dir / "validation-receipt.v2.json"
            reviewer = run_dir / "reviewer-verdict.v2.json"
            accepted.write_text(clean_diagram(), encoding="utf-8")
            artifact_sha = orchestrator.lifecycle_v2.file_sha256(accepted)
            report.write_text(json.dumps({
                "artifact_sha256": artifact_sha,
                "findings": [{"severity": "warning", "code": "layout.warning"}],
            }), encoding="utf-8")
            receipt.write_text(json.dumps({
                "schema_version": 2,
                "result": "passed",
                "bindings": {"candidate_sha256": artifact_sha},
            }), encoding="utf-8")

            def descriptor(path):
                return {
                    "path": path.relative_to(run_dir).as_posix(),
                    "sha256": orchestrator.lifecycle_v2.file_sha256(path),
                }

            publication = {
                "run_id": "publication-reviewer-run",
                "decision": "approve_with_findings",
                "accepted_artifact": descriptor(accepted),
                "validation_report": descriptor(report),
                "validation_receipt": descriptor(receipt),
                "reviewer_verdict": None,
                "strict_passed": True,
                "source_bundle_sha256": "6" * 64,
            }
            replayed = {
                "latest_snapshots": {
                    "source-bundle": {"canonical_sha256": "6" * 64},
                }
            }
            receipt_check = {
                "valid": True,
                "integrity_valid": True,
                "strict_passed": True,
                "diagnostics": [],
            }
            with mock.patch.object(
                orchestrator.lifecycle_v2, "require_mutable", return_value=replayed,
            ), mock.patch.object(
                orchestrator.lifecycle_v2, "verify_v2_receipt", return_value=receipt_check,
            ), mock.patch.object(
                orchestrator.lifecycle_v2, "require_valid_contract",
            ):
                with self.assertRaisesRegex(
                    orchestrator.lifecycle_v2.ContractError,
                    "requires a hash-bound Reviewer approve verdict",
                ):
                    orchestrator.lifecycle_v2._validate_publication_evidence(
                        run_dir, publication, require_current_source=False,
                    )

                for verdict in ("reject", "needs_human"):
                    with self.subTest(verdict=verdict):
                        reviewer_value = {
                            "schema_version": 2,
                            "run_id": publication["run_id"],
                            "bindings": {
                                "candidate_sha256": artifact_sha,
                                "report_sha256": orchestrator.lifecycle_v2.file_sha256(report),
                                "receipt_sha256": orchestrator.lifecycle_v2.file_sha256(receipt),
                            },
                            "verdict": verdict,
                            "findings": [],
                        }
                        reviewer.write_text(json.dumps(reviewer_value), encoding="utf-8")
                        publication["reviewer_verdict"] = descriptor(reviewer)
                        with self.assertRaisesRegex(
                            orchestrator.lifecycle_v2.ContractError,
                            "requires Reviewer approve",
                        ):
                            orchestrator.lifecycle_v2._validate_publication_evidence(
                                run_dir, publication, require_current_source=False,
                            )

                reviewer_value["verdict"] = "approve"
                reviewer.write_text(json.dumps(reviewer_value), encoding="utf-8")
                publication["reviewer_verdict"] = descriptor(reviewer)
                accepted_path, report_path, receipt_path = (
                    orchestrator.lifecycle_v2._validate_publication_evidence(
                        run_dir, publication, require_current_source=False,
                    )
                )
                self.assertEqual(accepted_path, accepted.resolve())
                self.assertEqual(report_path, report.resolve())
                self.assertEqual(receipt_path, receipt.resolve())

    def test_legacy_reviewer_analysis_status_matches_needs_human_only(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"
            attempt_dir = run_dir / "roles" / "reviewer"
            attempt_dir.mkdir(parents=True)
            candidate = attempt_dir / "candidate.drawio"
            report = attempt_dir / "report.json"
            receipt = attempt_dir / "receipt.json"
            input_path = attempt_dir / "input.json"
            runtime_capture = attempt_dir / "runtime.jsonl"
            for path, value in (
                (candidate, clean_diagram()),
                (report, "{}"),
                (receipt, "{}"),
                (input_path, "{}"),
                (runtime_capture, "{}\n"),
            ):
                path.write_text(value, encoding="utf-8")
            runtime = {
                "resolution": {
                    "requested_model": "vllm/DeepSeek-V4-Flash-262k",
                    "resolved_model": "vllm/DeepSeek-V4-Flash-262k",
                    "provider": "vllm",
                    "resolution_mode": "isolated_cli",
                },
                "runtime_capture": str(runtime_capture),
                "attempt_id": "legacy-reviewer-test",
            }
            workflow = {"run_id": "legacy-reviewer-run"}
            replayed = {
                "latest_snapshots": {
                    "source-bundle": {"canonical_sha256": "6" * 64},
                }
            }
            with mock.patch.object(
                orchestrator.lifecycle_v2, "require_mutable", return_value=replayed,
            ), mock.patch.object(orchestrator, "require_valid_contract"):
                for verdict, expected_status in (
                    ("reject", "ok"),
                    ("needs_human", "needs_human"),
                ):
                    with self.subTest(verdict=verdict):
                        output_path = attempt_dir / f"{verdict}.json"
                        output_path.write_text("{}", encoding="utf-8")
                        legacy = {
                            "schema_version": 1,
                            "verdict_id": f"legacy-{verdict}",
                            "verdict": verdict,
                            "reviewed_at": "2026-07-22T12:00:00Z",
                            "findings": [],
                        }
                        orchestrator._bind_reviewer_v2(
                            run_dir,
                            workflow,
                            legacy,
                            runtime,
                            input_path,
                            output_path,
                            candidate,
                            report,
                            receipt,
                        )
                        analysis = json.loads(
                            output_path.with_name("analysis.v2.json").read_text(
                                encoding="utf-8"
                            )
                        )
                        self.assertEqual(analysis["status"], expected_status)
                        self.assertEqual(analysis["verdict"], verdict)

    def test_layout_feedback_uses_exact_route_scope_and_skips_semantic_analyst(self):
        _, workspace, _ = self.create_workspace()
        run_dir = workspace / ".diagram-runs" / "layout-feedback-run"
        run_dir.mkdir(parents=True, exist_ok=True)
        workflow = {
            "run_id": "layout-feedback-run",
            "request": "Fix only edge e-2 with orthogonal waypoints.",
            "repair_scope": {
                "source": "typed_findings",
                "allowed_targets": ["e-2"],
                "allowed_operations": ["set_edge_route", "set_edge_pins"],
                "finding_ids": ["route-through"],
                "semantic_targets": [],
            },
            "findings": [],
        }

        with mock.patch.object(orchestrator.lifecycle_v2, "add_feedback_source"), \
             mock.patch.object(orchestrator.supervisor, "append_event"), \
             mock.patch.object(orchestrator.supervisor, "load_state", return_value={"state": "patching"}), \
             mock.patch.object(orchestrator, "write_workflow"), \
             mock.patch.object(orchestrator, "role_call", side_effect=AssertionError("semantic_analyst should not be called")):
            result = orchestrator._reconcile_feedback(
                run_dir,
                workflow,
                "Fix only edge e-2 with orthogonal waypoints.",
                "decision-1",
                workspace,
                sys.executable,
                600,
            )

        self.assertIsNone(result)
        self.assertEqual(workflow["repair_scope"]["allowed_targets"], ["e-2"])
        self.assertEqual(
            workflow["machine_repair_feedback"]["content"]["repair_scope"]["allowed_operations"],
            ["set_edge_pins", "set_edge_route"],
        )
        self.assertFalse(workflow["semantic_authorized"])
        self.assertNotIn("pending_semantic_approval", workflow)
        self.assertNotIn("approved_semantic_change", workflow)

    def test_layout_and_semantic_feedback_are_classified_differently(self):
        _, workspace, _ = self.create_workspace()
        workflow = {
            "run_id": "feedback-classification-run",
            "request": "Fix the route.",
            "repair_scope": {
                "source": "typed_findings",
                "allowed_targets": ["e-2"],
                "allowed_operations": ["set_edge_route", "set_edge_pins"],
                "finding_ids": ["route-through"],
                "semantic_targets": [],
            },
            "findings": [],
        }
        self.assertEqual(
            orchestrator._layout_feedback_scope(workflow, "Fix only edge e-2 with orthogonal waypoints."),
            {
                "source": "explicit_layout_feedback",
                "allowed_targets": ["e-2"],
                "allowed_operations": ["set_edge_pins", "set_edge_route"],
                "finding_ids": ["route-through"],
                "semantic_targets": [],
                "feedback": "Fix only edge e-2 with orthogonal waypoints.",
            },
        )
        self.assertIsNone(
            orchestrator._layout_feedback_scope(
                workflow,
                "Fix the edge route and also rename the process label.",
            )
        )
        self.assertIsNone(
            orchestrator._layout_feedback_scope(
                workflow,
                "Fix the route using orthogonal waypoints.",
            )
        )

    def test_finding_targets_for_route_through_keeps_the_edge_only(self):
        finding = {
            "code": "artifact.readability.route_through",
            "element": "e-2",
            "elements": [{"cell_id": "n-decision"}, {"cell_id": "e-2"}],
            "message": "edge 'e-2' routes through vertex 'n-decision'",
        }
        self.assertEqual(orchestrator._finding_targets(finding), ["e-2"])

    def test_host_bound_patch_rewrites_baseline_hashes_and_rejects_out_of_scope_target(self):
        _, workspace, _ = self.create_workspace()
        source = workspace / "route.drawio"
        source.write_text(routed_diagram(), encoding="utf-8")
        run_dir = workspace / ".diagram-runs" / "host-bound-run"
        run_dir.mkdir(parents=True, exist_ok=True)
        accepted = run_dir / "accepted" / "baseline.drawio"
        accepted.parent.mkdir(parents=True, exist_ok=True)
        accepted.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        workflow = {
            "run_id": "host-bound-run",
            "request": "Route e-2.",
            "accepted_artifact": {
                "path": str(accepted.resolve()),
                "sha256": orchestrator.supervisor.sha256_file(accepted),
            },
            "repair_scope": {
                "source": "typed_findings",
                "allowed_targets": ["e-2"],
                "allowed_operations": ["set_edge_route", "set_edge_pins"],
                "finding_ids": ["route-through"],
                "semantic_targets": [],
            },
            "findings": [],
        }
        patch = orchestrator.supervisor.route_patch(source, "e-2", ["route-through"])
        patch["baseline"]["artifact_sha256"] = "0" * 64
        patch["baseline"]["semantic_digest"] = "bogus"
        raw_patch_path = workspace / "raw-patch.json"
        raw_patch_path.write_text(json.dumps(patch, ensure_ascii=False), encoding="utf-8")
        raw_patch_sha256 = orchestrator.supervisor.sha256_file(raw_patch_path)

        with mock.patch.object(orchestrator.supervisor, "load_state", return_value={"state": "patching"}), \
             mock.patch.object(orchestrator.supervisor, "append_event"):
            bound_patch, bound_path = orchestrator._host_bind_patch(run_dir, workflow, patch, raw_patch_path)
        self.assertEqual(orchestrator.supervisor.sha256_file(raw_patch_path), raw_patch_sha256)
        self.assertEqual(bound_patch["baseline"]["artifact_sha256"], orchestrator.supervisor.sha256_file(source))
        self.assertEqual(bound_patch["baseline"]["semantic_digest"], orchestrator.supervisor.artifact_invariants(source)[0])
        self.assertTrue(bound_path.is_file())

        forbidden = json.loads(json.dumps(patch))
        forbidden["operations"][0]["target_id"] = "e-3"
        forbidden["operations"][1]["target_id"] = "e-3"
        forbidden["affected_region"]["cell_ids"] = ["e-3"]
        with mock.patch.object(orchestrator.supervisor, "load_state", return_value={"state": "patching"}), \
             mock.patch.object(orchestrator.supervisor, "append_event"):
            with self.assertRaisesRegex(orchestrator.supervisor.SupervisorError, "outside host scope"):
                orchestrator._host_bind_patch(run_dir, workflow, forbidden, raw_patch_path)

    def test_internal_repair_feedback_retries_once_for_same_failure_without_user_decision(self):
        _, workspace, _ = self.create_workspace()
        run_dir = workspace / ".diagram-runs" / "retry-run"
        run_dir.mkdir(parents=True, exist_ok=True)
        workflow = {
            "run_id": "retry-run",
            "iteration": 1,
            "max_iterations": 4,
            "repair_scope": {
                "source": "typed_findings",
                "allowed_targets": ["e-2"],
                "allowed_operations": ["set_edge_route", "set_edge_pins"],
                "finding_ids": ["route-through"],
                "semantic_targets": [],
            },
            "failure_signatures": {},
        }
        events = []

        def record_event(run_dir, event_type, payload, **kwargs):
            events.append(event_type)

        with mock.patch.object(orchestrator.supervisor, "append_event", side_effect=record_event), \
             mock.patch.object(orchestrator.supervisor, "load_state", return_value={"state": "retrying"}):
            retry1, descriptor1 = orchestrator._record_internal_repair_feedback(
                run_dir,
                workflow,
                failure_class="deterministic_tool",
                message="validator failed on attempt-1",
                evidence={"attempt_id": "iteration-1"},
            )
            retry2, descriptor2 = orchestrator._record_internal_repair_feedback(
                run_dir,
                workflow,
                failure_class="deterministic_tool",
                message="validator failed on attempt-1",
                evidence={"attempt_id": "iteration-1"},
            )

        self.assertTrue(retry1)
        self.assertFalse(retry2)
        self.assertEqual(workflow["failure_signatures"][descriptor1["content"]["failure_signature"]], 2)
        self.assertEqual(descriptor1["content"]["failure_signature"], descriptor2["content"]["failure_signature"])
        self.assertIn("internal_feedback_created", events)
        self.assertIn("auto_retry_scheduled", events)
        self.assertNotIn("user_decision", events)

    def test_strict_failed_working_candidate_clears_publishable_candidate(self):
        workflow = {
            "publishable_candidate": {
                "artifact": {"path": "/tmp/candidate.drawio", "sha256": "a" * 64},
                "validation": {"report": "/tmp/report.json", "receipt": "/tmp/receipt.json"},
            },
            "accepted_artifact": {"path": "/tmp/candidate.drawio", "sha256": "a" * 64},
            "accepted_validation": {"strict_passed": False},
        }
        orchestrator._set_workflow_accepted(
            workflow,
            {
                "accepted_artifact": workflow["accepted_artifact"],
                "accepted_validation": workflow["accepted_validation"],
            },
        )
        self.assertIsNone(workflow["publishable_candidate"])

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

    def test_finish_best_effort_marks_unchanged_improve_as_source_preserved(self):
        root, workspace, _ = self.create_workspace()
        run_dir = workspace / ".diagram-runs" / "source-preserved-run"
        run_dir.mkdir(parents=True)
        target = run_dir / "source-preserved.drawio"
        target.write_text(clean_diagram(), encoding="utf-8")
        original_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        report = run_dir / "validation-report.json"
        receipt = run_dir / "validation-receipt.json"
        reviewer = run_dir / "reviewer-verdict.json"
        report.write_text(json.dumps({"schema_version": 1, "findings": [], "metrics": {}}), encoding="utf-8")
        receipt.write_text(json.dumps({"strict_passed": False}), encoding="utf-8")
        reviewer.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "verdict_id": "review-1",
                    "run_id": "source-preserved-run",
                    "candidate_sha256": original_hash,
                    "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
                    "receipt_sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
                    "verdict": "approve",
                    "reviewed_at": "2026-07-23T00:00:00Z",
                    "findings": [],
                }
            ),
            encoding="utf-8",
        )
        workflow = {
            "run_id": "source-preserved-run",
            "mode": "improve",
            "target": str(target),
            "original_artifact": {"sha256": original_hash},
            "reviewer_verdict_v2": {"path": str(reviewer)},
        }
        candidate = {
            "safe": True,
            "artifact": {"path": str(target), "sha256": original_hash},
            "validation": {"report": str(report), "receipt": str(receipt)},
            "validation_receipt_v2": {"path": str(receipt)},
            "reviewer_verdict_v2": {"path": str(reviewer)},
            "classification": {
                "strict_passed": False,
                "findings": [],
                "reviewer_findings": [],
                "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
                "receipt_sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
            },
            "diagnostics": [],
        }

        with mock.patch.object(orchestrator, "_best_effort_candidate", return_value=candidate), \
            mock.patch.object(orchestrator, "write_workflow"), \
            mock.patch.object(orchestrator, "_complete_inflight_decision"), \
            mock.patch.object(orchestrator, "host_result", side_effect=lambda run_dir, workflow, error=None: {"status": workflow["status"]}), \
            mock.patch.object(orchestrator.supervisor, "transition"), \
            mock.patch.object(orchestrator.lifecycle_v2, "transition"), \
            mock.patch.object(orchestrator.lifecycle_v2, "publish_transaction") as publish_transaction:
            result = orchestrator._finish_best_effort(
                run_dir,
                workflow,
                cli=Path("/usr/bin/true"),
                timeout=1,
                reason="test source preserved",
            )

        self.assertEqual(result["status"], "best_effort_completed")
        self.assertEqual(workflow["status"], "best_effort_completed")
        self.assertEqual(workflow["best_effort"]["publication"]["disposition"], "source_preserved")
        self.assertEqual(workflow["published_artifact"]["sha256"], original_hash)
        self.assertEqual(workflow["final_artifact"]["sha256"], original_hash)
        publish_transaction.assert_not_called()

    def test_finish_best_effort_create_publishes_to_separate_no_clobber_target(self):
        root, workspace, _ = self.create_workspace()
        run_dir = workspace / ".diagram-runs" / "create-best-effort-run"
        run_dir.mkdir(parents=True)
        requested_target = workspace / "requested.drawio"
        artifact = run_dir / "candidate.drawio"
        artifact.write_text(clean_diagram(), encoding="utf-8")
        artifact_hash = hashlib.sha256(artifact.read_bytes()).hexdigest()
        report = run_dir / "validation-report.json"
        receipt = run_dir / "validation-receipt.json"
        report.write_text(json.dumps({"schema_version": 1, "findings": [], "metrics": {}}), encoding="utf-8")
        receipt.write_text(json.dumps({"strict_passed": False}), encoding="utf-8")
        workflow = {
            "run_id": "create-best-effort-run",
            "mode": "create",
            "target": str(requested_target),
        }
        candidate = {
            "safe": True,
            "artifact": {"path": str(artifact), "sha256": artifact_hash},
            "validation": {"report": str(report), "receipt": str(receipt)},
            "validation_receipt_v2": {"path": str(receipt)},
            "reviewer_verdict_v2": None,
            "classification": {
                "strict_passed": False,
                "findings": [],
                "reviewer_findings": [],
                "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
                "receipt_sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
            },
            "diagnostics": [],
        }

        def publish_side_effect(*args, **kwargs):
            target = Path(kwargs["target_override"])
            target.write_bytes(artifact.read_bytes())
            return {
                "status": "committed",
                "publication_id": "publication-best-effort",
                "target_path": str(target.relative_to(workspace.resolve())),
            }

        with mock.patch.object(orchestrator, "_best_effort_candidate", return_value=candidate), \
            mock.patch.object(orchestrator, "baseline_review", side_effect=orchestrator.supervisor.SupervisorError("review unavailable")), \
            mock.patch.object(orchestrator, "write_workflow"), \
            mock.patch.object(orchestrator, "_complete_inflight_decision"), \
            mock.patch.object(orchestrator, "host_result", side_effect=lambda run_dir, workflow, error=None: {"status": workflow["status"]}), \
            mock.patch.object(orchestrator.supervisor, "transition"), \
            mock.patch.object(orchestrator.supervisor, "append_event"), \
            mock.patch.object(orchestrator.lifecycle_v2, "transition"), \
            mock.patch.object(orchestrator.lifecycle_v2, "publish_transaction", side_effect=publish_side_effect) as publish_transaction:
            result = orchestrator._finish_best_effort(
                run_dir,
                workflow,
                cli=Path("/usr/bin/true"),
                timeout=1,
                reason="bounded layout exhausted",
            )

        self.assertEqual(result["status"], "best_effort_completed")
        self.assertFalse(requested_target.exists())
        published = Path(workflow["published_artifact"]["path"])
        self.assertTrue(published.is_file())
        self.assertEqual(
            published.name,
            "requested.best-effort-create-best-effort-run.drawio",
        )
        self.assertEqual(
            publish_transaction.call_args.kwargs["target_override"],
            published,
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

    def test_trace_does_not_attempt_recovery_or_mutate_run_artifacts(self):
        root, workspace, cli = self.create_workspace()
        target = workspace / "read-only-trace.drawio"

        orchestrator.start_run(
            "create",
            target,
            "Create a read-only trace diagram.",
            workspace,
            cli,
            run_id="read-only-trace-run",
            max_iterations=1,
        )
        run_dir = workspace / ".diagram-runs" / "read-only-trace-run"
        manifest = run_dir / "run-manifest.jsonl"
        workflow = run_dir / "workflow.json"
        manifest_before = manifest.read_text(encoding="utf-8")
        workflow_before = workflow.read_text(encoding="utf-8")
        manifest_mtime_before = manifest.stat().st_mtime_ns
        workflow_mtime_before = workflow.stat().st_mtime_ns

        with mock.patch.object(
            orchestrator.supervisor,
            "recover_pending_transaction",
            side_effect=AssertionError("trace must not recover transactions"),
        ):
            trace = orchestrator.trace_run(run_dir, workspace)

        self.assertTrue(trace["integrity_valid"])
        self.assertEqual(trace["status"], "verified")
        self.assertEqual(manifest.read_text(encoding="utf-8"), manifest_before)
        self.assertEqual(workflow.read_text(encoding="utf-8"), workflow_before)
        self.assertEqual(manifest.stat().st_mtime_ns, manifest_mtime_before)
        self.assertEqual(workflow.stat().st_mtime_ns, workflow_mtime_before)

    def test_trace_verifies_failed_turn_limit_capture_and_isolation_evidence(self):
        root, workspace, _ = self.create_workspace()
        cli = root / "turn-limited-gigacode.py"
        cli.write_text(
            f"#!{sys.executable}\n"
            "import json, sys\n"
            "if '--help' in sys.argv:\n"
            "    print('GigaCode --model --prompt --output-format --approval-mode --auth-type --extensions --system-prompt --max-session-turns --core-tools --allowed-mcp-server-names --exclude-tools')\n"
            "    raise SystemExit(0)\n"
            "if '--version' in sys.argv:\n"
            "    print('26.5.17-test')\n"
            "    raise SystemExit(0)\n"
            "model=sys.argv[sys.argv.index('--model')+1]\n"
            "payload=json.loads(sys.stdin.read())\n"
            "if model == 'vllm/Qwen3.6-35B-262k' and payload.get('phase') == 'intake':\n"
            "    result={'schema_version':1,'role':'semantic_analyst','status':'ok','result':{'diagram_type':'generic','confidence':0.95,'alternatives':[],'sufficient':True,'blocking_questions':[],'assumptions':[]}}\n"
            "    encoded=json.dumps(result)\n"
            "    print(json.dumps([{'type':'system','subtype':'init','model':model,'qwen_code_version':'0.13.1','agents':[],'slash_commands':[]},{'type':'assistant','message':{'model':model,'content':[{'type':'text','text':encoded}]}},{'type':'result','subtype':'success','is_error':False,'result':encoded,'stats':{'models':{model:{'api':{'totalRequests':1}}}}}]))\n"
            "    raise SystemExit(0)\n"
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
            "HELP = 'GigaCode --model --prompt --output-format stream-json --approval-mode --auth-type --extensions --system-prompt --max-session-turns --core-tools --allowed-mcp-server-names --exclude-tools'\n"
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
            "    if payload.get('phase') == 'intake':\n"
            "        emit_stream(model, {'schema_version': 1, 'role': 'semantic_analyst', 'status': 'ok', 'result': {'diagram_type': 'generic', 'confidence': 0.95, 'alternatives': [], 'sufficient': True, 'blocking_questions': [], 'assumptions': []}})\n"
            "    else:\n"
            "        requires_human = payload['mode'] == 'improve'\n"
            "        emit_stream(model, {'schema_version': 2, 'role': 'semantic_analyst', 'status': 'needs_human' if requires_human else 'ok', 'result': {'mode': payload['mode'], 'diagram_type': 'generic', 'title': f\"Test {payload['mode']} analysis\", 'direction': 'LR', 'pages': [{'page_id': 'page-1', 'name': 'Page 1', 'nodes': [{'stable_identity': {'page_id': 'page-1', 'cell_id': 'node-a'}, 'label': 'A', 'semantic_type': 'task', 'parent': None, 'style_hint': None}, {'stable_identity': {'page_id': 'page-1', 'cell_id': 'node-b'}, 'label': 'B', 'semantic_type': 'task', 'parent': None, 'style_hint': None}], 'edges': []}], 'assumptions': [], 'requires_human': requires_human, 'human_questions': ['Add approval branch'] if requires_human else []}})\n"
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

        first_resume = orchestrator.resume_run(
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
        self.assertNotEqual(first_resume["status"], "already_applied")

        resumed_again = orchestrator.resume_run(
            run_dir,
            "continue",
            "Approve the proposed semantic delta.",
            workspace,
            cli,
        )
        self.assertEqual(resumed_again["status"], "already_applied")

    def test_semantic_approval_replay_reuses_existing_approval_without_duplicate_event(self):
        root, workspace, _ = self.create_workspace()
        source_with_repair = FAKE_GIGACODE.replace(
            '["supervisor", "semantic_analyst", "reviewer"]',
            '["supervisor", "semantic_analyst", "repair", "reviewer"]',
        )
        cli = root / "semantic-replay-cli.py"
        cli.write_text(source_with_repair, encoding="utf-8")
        cli.chmod(0o755)
        source = workspace / "semantic-replay.drawio"
        source.write_text(clean_diagram(), encoding="utf-8")

        orchestrator.start_run(
            "improve",
            source,
            "Add a semantic approval branch.",
            workspace,
            cli,
            run_id="semantic-replay-run",
            max_iterations=1,
        )
        run_dir = workspace / ".diagram-runs" / "semantic-replay-run"
        decision, _, _ = orchestrator.lifecycle_v2.commit_decision(
            run_dir,
            decision="continue",
            feedback="Approve the proposed semantic delta.",
        )

        first_approval, first_path = orchestrator.lifecycle_v2.create_semantic_approval_from_decision(
            run_dir, decision=decision,
        )
        second_approval, second_path = orchestrator.lifecycle_v2.create_semantic_approval_from_decision(
            run_dir, decision=decision,
        )

        self.assertEqual(first_approval, second_approval)
        self.assertEqual(first_path, second_path)
        approval_files = sorted((run_dir / "lifecycle-v2" / "approvals").glob("*.json"))
        self.assertEqual(len(approval_files), 1)
        self.assertEqual(approval_files[0].resolve(), first_path.resolve())

    def test_resume_rejects_conflicting_feedback_for_unprocessed_committed_decision(self):
        root, workspace, _ = self.create_workspace()
        source = workspace / "semantic-feedback-conflict.drawio"
        source.write_text(clean_diagram(), encoding="utf-8")
        cli = root / "semantic-feedback-conflict-cli.py"
        cli.write_text(FAKE_GIGACODE, encoding="utf-8")
        cli.chmod(0o755)

        orchestrator.start_run(
            "improve",
            source,
            "Add a semantic approval branch.",
            workspace,
            cli,
            run_id="semantic-feedback-conflict-run",
            max_iterations=1,
        )
        run_dir = workspace / ".diagram-runs" / "semantic-feedback-conflict-run"
        orchestrator.lifecycle_v2.commit_decision(
            run_dir,
            decision="continue",
            feedback="Use only the approved edge.",
        )
        immutable_before = {
            path.relative_to(run_dir).as_posix(): path.read_bytes()
            for path in run_dir.rglob("*")
            if path.is_file()
        }

        with self.assertRaisesRegex(
            orchestrator.supervisor.SupervisorError,
            "feedback conflicts",
        ):
            orchestrator.resume_run(
                run_dir,
                "continue",
                "Change a different edge instead.",
                workspace,
                cli,
            )

        immutable_after = {
            path.relative_to(run_dir).as_posix(): path.read_bytes()
            for path in run_dir.rglob("*")
            if path.is_file()
        }
        self.assertEqual(immutable_after, immutable_before)

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
