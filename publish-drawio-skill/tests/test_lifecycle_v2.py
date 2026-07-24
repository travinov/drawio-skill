import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import agent_runtime
import diagram_model_v2
import evidence_v2
import lifecycle_host_v2
import renderer_adapters
import run_lock_v2
import source_bundle_v2


def write_text(path, value):
    path.write_text(value, encoding="utf-8")
    return path


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def minimal_document(path_name="document.json", content=None):
    content = {} if content is None else content
    return {
        "path": path_name,
        "sha256": hashlib.sha256(
            json.dumps(content, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "content": content,
    }


def reviewer_input_v2():
    artifact = {
        "path": "candidate.drawio",
        "sha256": "1" * 64,
        "byte_length": 12,
    }
    evidence = {
        "artifact": artifact,
        "report": minimal_document("report.json", {"result": "passed"}),
        "receipt": minimal_document("receipt.json", {"result": "passed"}),
        "strict_passed": True,
    }
    document = minimal_document("candidate.json")
    operation = {
        "operation_type": "parent",
        "element_kind": "node",
        "target": {"page_id": "page-1", "cell_id": "node-a"},
        "changes": [
            {"field": "parent", "before": None, "after": {"page_id": "page-1", "cell_id": "node-b"}}
        ],
    }
    operation["operation_id"] = diagram_model_v2.semantic_operation_id(operation)
    delta = {
        "sha256": "4" * 64,
        "content": {
            "schema_version": 2,
            "baseline_semantic_digest": "2" * 64,
            "source_bundle_sha256": "3" * 64,
            "operations": [operation],
        },
    }
    return {
        "schema_version": 2,
        "run_id": "run-1",
        "review_kind": "candidate_review",
        "baseline": evidence,
        "candidate": evidence,
        "baseline_spec": document,
        "candidate_spec": document,
        "patch": document,
        "semantic_plan": document,
        "semantic_delta": delta,
        "source_bundle": document,
        "comparison": {},
        "model_resolutions": [],
    }


def semantic_plan_v2(*, cycle=False):
    node_a_parent = {"page_id": "page-1", "cell_id": "node-b"} if cycle else None
    node_b_parent = {"page_id": "page-1", "cell_id": "node-a"} if cycle else None
    operation = {
        "operation_type": "parent",
        "element_kind": "node",
        "target": {"page_id": "page-1", "cell_id": "node-a"},
        "changes": [
            {
                "field": "parent",
                "before": None,
                "after": {"page_id": "page-1", "cell_id": "node-b"},
            }
        ],
    }
    operation["operation_id"] = diagram_model_v2.semantic_operation_id(operation)
    return {
        "schema_version": 2,
        "role": "semantic_analyst",
        "status": "ok",
        "run_id": "run-1",
        "source_bundle_sha256": "3" * 64,
        "baseline_semantic_digest": "2" * 64,
        "result": {
            "mode": "create",
            "diagram_type": "generic",
            "title": "Test",
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
                            "parent": node_a_parent,
                            "style_hint": None,
                        },
                        {
                            "stable_identity": {"page_id": "page-1", "cell_id": "node-b"},
                            "label": "B",
                            "semantic_type": "task",
                            "parent": node_b_parent,
                            "style_hint": None,
                        },
                    ],
                    "edges": [],
                }
            ],
            "semantic_delta": {
                "schema_version": 2,
                "baseline_semantic_digest": "2" * 64,
                "source_bundle_sha256": "3" * 64,
                "operations": [operation],
            },
            "assumptions": [],
            "requires_human": False,
            "human_questions": [],
        },
    }


def semantic_analysis_v2(*, cycle=False):
    analysis = semantic_plan_v2(cycle=cycle)
    analysis.pop("run_id")
    analysis.pop("source_bundle_sha256")
    analysis.pop("baseline_semantic_digest")
    analysis["result"].pop("semantic_delta")
    return analysis


def semantic_input_v2():
    source_bundle = source_bundle_v2.build_source_bundle(
        bundle_id="bundle-1",
        run_id="run-1",
        sources=[
            source_bundle_v2.source_record(
                source_id="request",
                kind="original_user_request",
                uri="urn:diagram-run:run-1:request",
                content={"request": "create"},
            ),
        ],
        transaction_id="tx-1",
    )
    baseline_spec = diagramspec_v2()
    return {
        "schema_version": 2,
        "run_id": "run-1",
        "mode": "create",
        "request": "Create a diagram.",
        "feedback": None,
        "source_bundle": {
            "sha256": source_bundle_v2.source_bundle_sha256(source_bundle),
            "content": source_bundle,
        },
        "baseline": {
            "semantic_digest": baseline_spec["semantic_digest"]["value"],
            "model_view": baseline_spec["model_view"],
            "evidence": source_bundle["evidence"],
        },
        "source_priority": list(source_bundle_v2.SOURCE_PRIORITY),
        "requirements": {
            "complete_desired_graph": True,
            "compare_request_to_existing": False,
            "return_complete_plan_for_create": True,
            "preserve_page_scoped_ids": True,
        },
    }


def diagramspec_v2():
    spec = {
        "schema_version": 2,
        "diagram_id": "diagram-1",
        "title": "Diagram",
        "artifact": {
            "path": "diagram.drawio",
            "sha256": "1" * 64,
            "byte_length": 1,
            "format": "drawio-xml",
            "imported_at": "2026-07-21T00:00:00Z",
            "preservation_policy": "patch-original-xml",
        },
        "pages": [
            {
                "id": "page-1",
                "name": "Page 1",
                "cells": [
                    {
                        "id": "root",
                        "stable_identity": {"page_id": "page-1", "cell_id": "root"},
                        "kind": "root",
                        "semantic_type": "root",
                        "label": "",
                        "technical": True,
                        "raw_attributes": {},
                    }
                ],
            }
        ],
    }
    spec["model_view"] = diagram_model_v2.build_model_view(spec)
    spec["semantic_digest"] = {
        "algorithm": "sha256",
        "canonicalization": "diagramspec-model-view-v2",
        "value": diagram_model_v2.semantic_digest(spec["model_view"]),
    }
    return spec


def write_role_cli(path, behavior):
    path.parent.mkdir(parents=True, exist_ok=True)
    script = f"""#!/usr/bin/env python3
import hashlib
import json
import sys
from pathlib import Path

HELP = "--model --prompt --output-format --approval-mode --auth-type --extensions --system-prompt --max-session-turns --core-tools --allowed-mcp-server-names --exclude-tools"


def emit(value):
    model = sys.argv[sys.argv.index("--model") + 1]
    encoded = json.dumps(value, ensure_ascii=False)
    print(json.dumps([
        {{"type": "system", "subtype": "init", "model": model, "qwen_code_version": "0.13.1"}},
        {{"type": "assistant", "message": {{"model": model, "content": [{{"type": "text", "text": encoded}}]}}}},
        {{"type": "result", "subtype": "success", "is_error": False, "result": encoded, "stats": {{"models": {{model: {{}}}}}}}},
    ]))


if "--help" in sys.argv:
    print(HELP)
    raise SystemExit(0)
if "--version" in sys.argv:
    print("26.5.17-test")
    raise SystemExit(0)

payload = json.loads(sys.stdin.read())
model = sys.argv[sys.argv.index("--model") + 1]
call_log = Path(__file__).with_suffix(".calls.log")
input_hash = hashlib.sha256(
    json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
).hexdigest()
with call_log.open("a", encoding="utf-8") as handle:
    handle.write(f"{{model}} {{input_hash}}\\n")
call_no = len(call_log.read_text(encoding="utf-8").splitlines())
{behavior}
    """
    write_text(path, script)
    os.chmod(path, 0o755)
    return path


def c4_source_v1():
    return {
        "title": "Banking Platform",
        "levels": [
            {
                "name": "System Context",
                "elements": [
                    {
                        "id": "customer",
                        "type": "person",
                        "label": "Customer",
                        "desc": "Uses the bank system",
                    },
                    {
                        "id": "bank",
                        "type": "system",
                        "label": "Banking Platform",
                        "desc": "Primary system",
                        "children": "Containers",
                    },
                ],
                "relations": [
                    {"from": "customer", "to": "bank", "label": "Uses"},
                ],
            },
            {
                "name": "Containers",
                "elements": [
                    {
                        "id": "web",
                        "type": "container",
                        "label": "Web App",
                        "tech": "React",
                        "desc": "Browser front-end",
                    },
                    {
                        "id": "api",
                        "type": "container",
                        "label": "API",
                        "tech": "Python",
                    },
                    {
                        "id": "db",
                        "type": "database",
                        "label": "Database",
                        "tech": "PostgreSQL",
                    },
                ],
                "relations": [
                    {"from": "web", "to": "api", "label": "Calls"},
                    {"from": "api", "to": "db", "label": "Reads/Writes"},
                ],
            },
        ],
    }


def generic_plan_v2():
    return {
        "schema_version": 2,
        "role": "semantic_analyst",
        "status": "ok",
        "run_id": "run-1",
        "source_bundle_sha256": "3" * 64,
        "baseline_semantic_digest": "2" * 64,
        "result": {
            "mode": "create",
            "diagram_type": "generic",
            "title": "Generic v2",
            "direction": "LR",
            "pages": [
                {
                    "page_id": "page-1",
                    "name": "Page 1",
                    "nodes": [
                        {
                            "stable_identity": {"page_id": "page-1", "cell_id": "container"},
                            "label": "Container",
                            "semantic_type": "container",
                            "parent": None,
                            "style_hint": "rounded=1;fillColor=#ffeeaa;",
                        },
                        {
                            "stable_identity": {"page_id": "page-1", "cell_id": "child"},
                            "label": "Child",
                            "semantic_type": "task",
                            "parent": {"page_id": "page-1", "cell_id": "container"},
                            "style_hint": "shadow=1;",
                        },
                        {
                            "stable_identity": {"page_id": "page-1", "cell_id": "peer"},
                            "label": "Peer",
                            "semantic_type": "task",
                            "parent": None,
                            "style_hint": None,
                        },
                    ],
                    "edges": [
                        {
                            "stable_identity": {"page_id": "page-1", "cell_id": "branch"},
                            "source": {"page_id": "page-1", "cell_id": "container"},
                            "target": {"page_id": "page-1", "cell_id": "peer"},
                            "label": "branch",
                            "relationship": "sequence",
                            "parent": None,
                            "style_hint": "dashed=1;",
                            "route": {
                                "orthogonal": True,
                                "source_pin": {"x": 1.0, "y": 0.25},
                                "target_pin": {"x": 0.0, "y": 0.75},
                                "waypoints": [{"x": 420, "y": 90}, {"x": 420, "y": 220}],
                            },
                        },
                        {
                            "stable_identity": {"page_id": "page-1", "cell_id": "loop"},
                            "source": {"page_id": "page-1", "cell_id": "peer"},
                            "target": {"page_id": "page-1", "cell_id": "peer"},
                            "label": "self",
                            "relationship": "sequence",
                            "parent": None,
                            "style_hint": "strokeColor=#ff0000;",
                            "route": {
                                "orthogonal": True,
                                "source_pin": {"x": 0.5, "y": 1.0},
                                "target_pin": {"x": 0.5, "y": 0.0},
                                "waypoints": [{"x": 560, "y": 260}, {"x": 610, "y": 260}],
                            },
                        },
                    ],
                },
                {
                    "page_id": "page-2",
                    "name": "Page 2",
                    "nodes": [
                        {
                            "stable_identity": {"page_id": "page-2", "cell_id": "container"},
                            "label": "Container",
                            "semantic_type": "container",
                            "parent": None,
                            "style_hint": "rounded=1;fillColor=#ffeeaa;",
                        },
                        {
                            "stable_identity": {"page_id": "page-2", "cell_id": "child"},
                            "label": "Child",
                            "semantic_type": "task",
                            "parent": {"page_id": "page-2", "cell_id": "container"},
                            "style_hint": "shadow=1;",
                        },
                    ],
                    "edges": [],
                },
            ],
            "semantic_delta": {
                "schema_version": 2,
                "baseline_semantic_digest": "2" * 64,
                "source_bundle_sha256": "3" * 64,
                "operations": [],
            },
            "assumptions": [],
            "requires_human": False,
            "human_questions": [],
        },
    }


class AgentRuntimeV2Tests(unittest.TestCase):
    def test_validate_role_input_reviewerv2_passes_and_missing_required_fails_closed(self):
        valid = reviewer_input_v2()
        self.assertEqual(agent_runtime.validate_role_input("reviewer", valid), valid)

        missing = json.loads(json.dumps(valid))
        missing.pop("review_kind")
        with self.assertRaisesRegex(agent_runtime.SupervisorError, "review_kind"):
            agent_runtime.validate_role_input("reviewer", missing)

        v1 = {"schema_version": 1}
        self.assertEqual(agent_runtime.validate_role_input("reviewer", v1), v1)

        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            marker = temp / "cli-executed"
            cli = write_role_cli(
                temp / "noop-cli",
                f"Path({str(marker)!r}).write_text('executed')\nraise SystemExit(99)\n",
            )
            with self.assertRaises(agent_runtime.SupervisorError):
                agent_runtime.invoke_role(
                    "reviewer",
                    write_json(temp / "invalid.json", missing),
                    temp / "out.json",
                    cli=str(cli),
                    run_dir=temp / "run",
                )
            self.assertFalse(marker.exists())
            self.assertFalse((temp / "out.json").exists())

    def test_v2_dispatch_and_single_correction_retry_reuses_model_and_input_hash(self):
        reviewer_contract = agent_runtime.role_output_contract("reviewer", reviewer_input_v2())
        semantic_contract = agent_runtime.role_output_contract("semantic_analyst", semantic_analysis_v2())
        self.assertIn("reviewer-analysis.v2.schema.json", reviewer_contract)
        self.assertIn("semantic-analysis.v2.schema.json", semantic_contract)

        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            cli = write_role_cli(
                temp / "semantic-cli",
                f"""if call_no == 1:
    emit({semantic_analysis_v2(cycle=True)!r})
else:
    emit({semantic_analysis_v2(cycle=False)!r})
""",
            )
            input_path = write_json(temp / "input.json", semantic_input_v2())
            output_path = temp / "output.json"
            result = agent_runtime.invoke_role(
                "semantic_analyst",
                input_path,
                output_path,
                cli=str(cli),
                run_dir=temp / "run",
            )

            self.assertEqual(result["contract_correction"]["attempted"], True)
            self.assertEqual(result["contract_correction"]["first_attempt_id"], "contract-attempt-1")
            self.assertEqual(result["contract_correction"]["correction_attempt_id"], "contract-correction-1")
            self.assertEqual(result["resolution"]["resolved_model"], "vllm/Qwen3.6-35B-262k")
            self.assertTrue(output_path.exists())

            call_log = cli.with_suffix(".calls.log").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(call_log), 2)
            self.assertEqual(call_log[0], call_log[1])

    def test_v2_semantic_analysis_empty_result_triggers_one_bounded_correction_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            cli = write_role_cli(
                temp / "semantic-cli",
                f"""first = {semantic_analysis_v2()!r}
valid = {semantic_analysis_v2()!r}
if call_no == 1:
    first["result"] = []
    emit(first)
else:
    emit(valid)
""",
            )
            input_path = write_json(temp / "input.json", semantic_input_v2())
            output_path = temp / "output.json"
            result = agent_runtime.invoke_role(
                "semantic_analyst",
                input_path,
                output_path,
                cli=str(cli),
                run_dir=temp / "run",
            )

            self.assertTrue(result["contract_correction"]["attempted"])
            self.assertEqual(result["contract_correction"]["first_attempt_id"], "contract-attempt-1")
            self.assertEqual(result["contract_correction"]["correction_attempt_id"], "contract-correction-1")
            self.assertEqual(
                json.loads(output_path.read_text(encoding="utf-8")),
                semantic_analysis_v2(),
            )

            call_log = cli.with_suffix(".calls.log").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(call_log), 2)
            first_model, first_input_hash = call_log[0].split()
            second_model, second_input_hash = call_log[1].split()
            self.assertEqual(first_model, second_model)
            self.assertEqual(first_input_hash, second_input_hash)
            self.assertEqual(result["contract_correction"]["original_input_sha256"], first_input_hash)

    def test_v2_failure_paths_do_not_retry_after_proof_isolation_timeout_or_integrity_errors(self):
        cases = (
            (
                "proof-mismatch",
                f"""result = {semantic_plan_v2(cycle=False)!r}
encoded = json.dumps(result, ensure_ascii=False)
print(json.dumps([
    {{"type": "system", "subtype": "init", "model": model, "qwen_code_version": "0.13.1"}},
    {{"type": "assistant", "message": {{"model": "wrong-model", "content": [{{"type": "text", "text": encoded}}]}}}},
    {{"type": "result", "subtype": "success", "is_error": False, "result": encoded, "stats": {{"models": {{model: {{}}}}}}}},
]))\n""",
                "model proof mismatch",
            ),
            (
                "tool-use",
                """print(json.dumps([
    {"type": "system", "subtype": "init", "model": model, "qwen_code_version": "0.13.1"},
    {"type": "assistant", "message": {"model": model, "content": [{"type": "tool_use", "name": "agent", "input": {}}]}},
    {"type": "result", "subtype": "success", "is_error": False, "result": "{}","stats": {"models": {model: {}}}},
]))\n""",
                "tool-free role contract",
            ),
            (
                "integrity",
                """print("{not-json}")\n""",
                "output is not valid JSON",
            ),
            (
                "timeout",
                """import time
time.sleep(0.5)
raise SystemExit(0)
""",
                "timed out after 0.1s",
            ),
        )

        for name, behavior, message in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as temp:
                temp = Path(temp)
            cli = write_role_cli(temp / f"{name}.py", behavior)
            output_path = temp / "out.json"
            input_path = write_json(temp / "input.json", semantic_input_v2())
            with self.assertRaisesRegex(agent_runtime.SupervisorError, message):
                agent_runtime.invoke_role(
                        "semantic_analyst",
                        input_path,
                        output_path,
                        cli=str(cli),
                        run_dir=temp / "run",
                        timeout=0.1,
                    )
                if cli.with_suffix(".calls.log").exists():
                    self.assertEqual(
                        len(cli.with_suffix(".calls.log").read_text(encoding="utf-8").splitlines()),
                        1,
                    )
                self.assertFalse(output_path.exists())

    def test_v2_repair_turn_limit_fallback_rejects_missing_model_proof_and_preserves_input_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            cli = write_role_cli(
                temp / "repair-cli",
                """if model == 'vllm/MiniMax-M3-113k':
    if call_no == 1:
        print(json.dumps([
            {"type": "system", "subtype": "init", "model": model, "qwen_code_version": "0.13.1"},
            {"type": "assistant", "message": {"model": model, "content": [{"type": "text", "text": "turn limit"}]}},
            {"type": "result", "subtype": "error", "is_error": True, "error": "FatalTurnLimitedError"},
        ]))
        print("FatalTurnLimitedError", file=sys.stderr)
        raise SystemExit(2)
elif model == 'vllm/Qwen3.6-35B-262k':
    encoded = json.dumps({"schema_version": 1, "patch_id": "repair-fallback"}, ensure_ascii=False)
    print(json.dumps([
        {"type": "system", "subtype": "init", "model": model, "qwen_code_version": "0.13.1"},
        {"type": "assistant", "message": {"model": "wrong-model", "content": [{"type": "text", "text": encoded}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": encoded},
    ]))
else:
    raise SystemExit(f"unexpected model: {model}")
""",
            )
            input_path = write_json(temp / "input.json", semantic_input_v2())
            output_path = temp / "output.json"

            with self.assertRaisesRegex(agent_runtime.SupervisorError, "model proof"):
                agent_runtime.invoke_role(
                    "repair",
                    input_path,
                    output_path,
                    cli=str(cli),
                    run_dir=temp / "run",
                )

            self.assertFalse(output_path.exists())
            call_log = cli.with_suffix(".calls.log").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(call_log), 2)
            self.assertEqual(call_log[0].split()[1], call_log[1].split()[1])
            events = [
                json.loads(line)
                for line in (temp / "run" / "run-manifest.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            failed = [event for event in events if event["event_type"] == "role_failed"]
            self.assertEqual(
                [item["payload"]["attempted_model"] for item in failed],
                ["vllm/MiniMax-M3-113k", "vllm/Qwen3.6-35B-262k"],
            )
            self.assertEqual([item["payload"]["terminal"] for item in failed], [False, True])

    def test_v2_repair_turn_limit_fallback_succeeds_with_input_hash_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            cli = write_role_cli(
                temp / "repair-cli",
                """if model == 'vllm/MiniMax-M3-113k':
    if call_no == 1:
        print(json.dumps([
            {"type": "system", "subtype": "init", "model": model, "qwen_code_version": "0.13.1"},
            {"type": "assistant", "message": {"model": model, "content": [{"type": "text", "text": "turn limit"}]}},
            {"type": "result", "subtype": "error", "is_error": True, "error": "FatalTurnLimitedError"},
        ]))
        print("FatalTurnLimitedError", file=sys.stderr)
        raise SystemExit(2)
elif model == 'vllm/Qwen3.6-35B-262k':
    emit({
        "schema_version": 1,
        "patch_id": "repair-fallback",
        "created_at": "2026-07-20T12:00:00Z",
        "created_by": "repair",
        "baseline": {
            "artifact_sha256": "3" * 64,
            "semantic_digest": "2" * 64,
        },
        "affected_region": {
            "page_id": "page-1",
            "cell_ids": ["node-a"],
        },
        "operations": [
            {
                "operation_id": "repair-fallback-move-node-a",
                "op": "move_vertex",
                "target_id": "node-a",
                "precondition": {"target_exists": False},
                "proposed_value": {"x": 12, "y": 34},
                "semantic_effect": "layout_only",
                "reasons": ["turn-limit fallback"],
                "finding_ids": [],
                "rollback": {"action": "restore_value", "value": {}},
            }
        ],
    })
else:
    raise SystemExit(f"unexpected model: {model}")
""",
            )
            input_path = write_json(temp / "input.json", semantic_input_v2())
            output_path = temp / "output.json"

            result = agent_runtime.invoke_role(
                "repair",
                input_path,
                output_path,
                cli=str(cli),
                run_dir=temp / "run",
            )

            self.assertTrue(output_path.exists())
            self.assertEqual(result["resolution"]["resolved_model"], "vllm/Qwen3.6-35B-262k")
            self.assertTrue(result["resolution"]["fallback_used"])
            call_log = cli.with_suffix(".calls.log").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(call_log), 2)
            self.assertEqual(call_log[0].split()[1], call_log[1].split()[1])
            events = [
                json.loads(line)
                for line in (temp / "run" / "run-manifest.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            failed = [event for event in events if event["event_type"] == "role_failed"]
            self.assertEqual([item["payload"]["terminal"] for item in failed], [False])
            self.assertEqual(
                [item["payload"]["attempted_model"] for item in failed],
                ["vllm/MiniMax-M3-113k"],
            )

    def test_validate_role_input_v1_passthrough_and_invalid_v2_stops_before_cli_marker(self):
        self.assertEqual(agent_runtime.validate_role_input("reviewer", {}), {})

        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            workspace = temp / "workspace"
            workspace.mkdir()
            run_dir = workspace / "run"
            target = workspace / "diagram.drawio"
            target.write_text("{}", encoding="utf-8")
            marker = temp / "marker"
            cli = write_role_cli(
                temp / "marker-cli.py",
                f"Path({str(marker)!r}).write_text('executed')\nraise SystemExit(99)\n",
            )
            invalid = reviewer_input_v2()
            invalid.pop("review_kind")
            with self.assertRaises(agent_runtime.SupervisorError):
                agent_runtime.invoke_role(
                    "reviewer",
                    write_json(temp / "invalid.json", invalid),
                    temp / "out.json",
                    cli=str(cli),
                    run_dir=temp / "run",
                )
            self.assertFalse(marker.exists())


class LifecycleV2Tests(unittest.TestCase):
    def test_tool_attempt_artifact_snapshots_are_hash_bound_during_replay(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            workspace.mkdir()
            target = workspace / "diagram.drawio"
            run_dir = workspace / "run"
            lifecycle_host_v2.initialize(
                run_dir=run_dir,
                workspace=workspace,
                target=target,
                run_id="run-1",
                mode="create",
                request="Create a diagram.",
                extension_root=ROOT,
            )
            request_path = run_dir / "layout-attempts" / "a" / "layout-request.json"
            request_path.parent.mkdir(parents=True)
            request_path.write_text('{"schema_version":1}\n', encoding="utf-8")

            lifecycle_host_v2.record_tool_attempt(
                run_dir,
                tool="layout-engine",
                attempt_id="a",
                status="started",
                artifact_snapshots={
                    "layout_request": lifecycle_host_v2.make_file_descriptor(
                        request_path,
                        root=run_dir,
                    ),
                },
            )
            self.assertTrue(lifecycle_host_v2.replay(run_dir)["valid"])

            request_path.write_text('{"schema_version":1,"changed":true}\n', encoding="utf-8")
            replayed = lifecycle_host_v2.replay(run_dir)
            self.assertFalse(replayed["valid"])
            self.assertTrue(
                any(
                    diagnostic["code"] == "tool_attempt.artifact_hash_mismatch"
                    for diagnostic in replayed["diagnostics"]
                )
            )

    def test_separate_publication_target_is_reserved_for_create_best_effort(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            workspace.mkdir()
            target = workspace / "diagram.drawio"
            run_dir = workspace / "run"
            lifecycle_host_v2.initialize(
                run_dir=run_dir,
                workspace=workspace,
                target=target,
                run_id="run-1",
                mode="create",
                request="Create a diagram.",
                extension_root=ROOT,
            )
            common = {
                "accepted_artifact": run_dir / "missing.drawio",
                "validation_report": run_dir / "missing-report.json",
                "validation_receipt": run_dir / "missing-receipt.json",
            }
            with self.assertRaises(lifecycle_host_v2.ContractError) as strict:
                lifecycle_host_v2.publish_transaction(
                    run_dir,
                    decision="approve",
                    target_override=workspace / "separate.drawio",
                    **common,
                )
            self.assertEqual(
                strict.exception.code,
                "publication.target_override_forbidden",
            )
            with self.assertRaises(lifecycle_host_v2.ContractError) as same:
                lifecycle_host_v2.publish_transaction(
                    run_dir,
                    decision="best_effort",
                    target_override=target,
                    **common,
                )
            self.assertEqual(
                same.exception.code,
                "publication.target_override_forbidden",
            )

    def test_historical_workflow_without_quality_profile_replays_and_stays_legacy(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            workspace.mkdir()
            target = write_text(workspace / "diagram.drawio", "<mxfile/>")
            run_dir = workspace / "run"
            lifecycle_host_v2.initialize(
                run_dir=run_dir,
                workspace=workspace,
                target=target,
                run_id="run-1",
                mode="improve",
                request="Improve the diagram.",
                extension_root=ROOT,
            )
            replayed = lifecycle_host_v2.require_mutable(run_dir)
            workflow, descriptor = lifecycle_host_v2.latest_document(
                run_dir, "workflow", replayed,
            )
            workflow.pop("quality_profile_version")
            transaction_id = "historical-workflow"
            legacy_descriptor = lifecycle_host_v2._write_next_snapshot(
                run_dir,
                kind="workflow",
                document=workflow,
                transaction_id=transaction_id,
                predecessor=descriptor,
                sequence=replayed["event_count"] + 1,
            )
            lifecycle_host_v2._append_event(
                run_dir,
                run_id="run-1",
                event_type="recovery",
                transaction_id=transaction_id,
                snapshots=[legacy_descriptor],
            )

            legacy_replay = lifecycle_host_v2.require_mutable(run_dir, "run-1")
            legacy_workflow, _ = lifecycle_host_v2.latest_document(
                run_dir, "workflow", legacy_replay,
            )
            lifecycle_host_v2.transition(run_dir, "analyzed")
            resumed, _ = lifecycle_host_v2.latest_document(run_dir, "workflow")

            self.assertNotIn("quality_profile_version", legacy_workflow)
            self.assertNotIn("quality_profile_version", resumed)

    def test_new_workflow_persists_quality_profile_version_two(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            workspace.mkdir()
            target = write_text(workspace / "diagram.drawio", "<mxfile/>")
            run_dir = workspace / "run"

            lifecycle_host_v2.initialize(
                run_dir=run_dir,
                workspace=workspace,
                target=target,
                run_id="run-1",
                mode="improve",
                request="Improve the diagram.",
                extension_root=ROOT,
            )
            workflow, _ = lifecycle_host_v2.latest_document(run_dir, "workflow")
            lifecycle_host_v2.transition(run_dir, "analyzed")
            resumed, _ = lifecycle_host_v2.latest_document(run_dir, "workflow")

            self.assertEqual(workflow["quality_profile_version"], 2)
            self.assertEqual(resumed["quality_profile_version"], 2)

    def test_source_priority_and_v1_mutability_guard_do_not_reach_openspec_or_mutate_old_runs(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            workspace = temp / "workspace"
            workspace.mkdir()
            run_dir = workspace / "run"
            target = workspace / "diagram.drawio"
            target.write_text("{}", encoding="utf-8")

            with mock.patch("pathlib.Path.glob", side_effect=AssertionError("unexpected repo search")), mock.patch("pathlib.Path.rglob", side_effect=AssertionError("unexpected repo search")):
                bundle = source_bundle_v2.build_source_bundle(
                    bundle_id="bundle-1",
                    run_id="run-1",
                    sources=[
                        source_bundle_v2.source_record(
                            source_id="request",
                            kind="original_user_request",
                            uri="urn:diagram-run:run-1:request",
                            content={"request": "create"},
                        ),
                        source_bundle_v2.source_record(
                            source_id="doc",
                            kind="explicit_user_document",
                            uri="urn:docs:brief",
                            content={"title": "brief"},
                        ),
                    ],
                    transaction_id="tx-1",
                )

            self.assertEqual(bundle["source_priority"], list(source_bundle_v2.SOURCE_PRIORITY))
            self.assertEqual([source["kind"] for source in bundle["sources"]], ["original_user_request", "explicit_user_document"])

            with self.assertRaises(lifecycle_host_v2.ContractError) as raised:
                lifecycle_host_v2.require_mutable(run_dir)
            self.assertEqual(raised.exception.code, "contract.v1_mutation_refused")

    def test_lock_contention_is_reported_and_release_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            workspace = temp / "workspace"
            workspace.mkdir()
            run_dir = workspace / "run"
            first = run_lock_v2.RunLock(workspace=workspace, run_dir=run_dir, run_id="run-1", max_recovery_attempts=0)
            second = run_lock_v2.RunLock(workspace=workspace, run_dir=run_dir, run_id="run-1", max_recovery_attempts=0)

            first.acquire()
            with self.assertRaises(run_lock_v2.RunAlreadyLocked) as raised:
                second.acquire()
            self.assertFalse(raised.exception.stale)
            self.assertEqual(raised.exception.as_result()["code"], "run.locked")

            first.release()
            first.release()
            reacquired = run_lock_v2.RunLock(workspace=workspace, run_dir=run_dir, run_id="run-1")
            reacquired.acquire()
            reacquired.release()

    def test_v2_semantic_subset_and_schema_negative_cases_are_rejected(self):
        exact_delta = semantic_plan_v2(cycle=False)["result"]["semantic_delta"]
        self.assertEqual(diagram_model_v2.validate_semantic_delta(exact_delta), [])

        mutated_delta = json.loads(json.dumps(exact_delta))
        mutated_delta["operations"][0]["changes"][0]["after"] = {
            "page_id": "page-1",
            "cell_id": "node-c",
        }
        diagnostics = diagram_model_v2.validate_semantic_delta(mutated_delta)
        self.assertTrue(
            any(item["code"] == "semantic.operation.identity_mismatch" for item in diagnostics),
            diagnostics,
        )

        spec = diagramspec_v2()
        self.assertEqual(diagram_model_v2.validate_diagramspec(spec), [])

        with_unknown_property = json.loads(json.dumps(spec))
        with_unknown_property["unexpected"] = True
        diagnostics = diagram_model_v2.validate_diagramspec(with_unknown_property)
        self.assertTrue(diagnostics)

        path_escape = json.loads(json.dumps(spec))
        path_escape["artifact"]["path"] = "../escape.drawio"
        diagnostics = diagram_model_v2.validate_diagramspec(path_escape)
        self.assertTrue(diagnostics)

        invalid_reference = json.loads(json.dumps(spec))
        invalid_reference["pages"][0]["cells"].append(
            {
                "id": "node-a",
                "stable_identity": {"page_id": "page-1", "cell_id": "node-a"},
                "kind": "vertex",
                "semantic_type": "task",
                "label": "A",
                "technical": False,
                "parent": {"page_id": "page-1", "cell_id": "missing"},
                "raw_attributes": {},
            }
        )
        diagnostics = diagram_model_v2.validate_diagramspec_cross_fields(invalid_reference)
        self.assertTrue(
            any(item["code"] == "diagram.reference.parent_missing" for item in diagnostics),
            diagnostics,
        )

        parent_cycle = json.loads(json.dumps(spec))
        parent_cycle["pages"][0]["cells"] = [
            {
                "id": "node-a",
                "stable_identity": {"page_id": "page-1", "cell_id": "node-a"},
                "kind": "vertex",
                "semantic_type": "task",
                "label": "A",
                "technical": False,
                "parent": {"page_id": "page-1", "cell_id": "node-b"},
                "raw_attributes": {},
            },
            {
                "id": "node-b",
                "stable_identity": {"page_id": "page-1", "cell_id": "node-b"},
                "kind": "vertex",
                "semantic_type": "task",
                "label": "B",
                "technical": False,
                "parent": {"page_id": "page-1", "cell_id": "node-a"},
                "raw_attributes": {},
            },
        ]
        parent_cycle["model_view"] = diagram_model_v2.build_model_view(parent_cycle)
        parent_cycle["semantic_digest"]["value"] = diagram_model_v2.semantic_digest(parent_cycle["model_view"])
        diagnostics = diagram_model_v2.validate_diagramspec_cross_fields(parent_cycle)
        self.assertTrue(
            any(item["code"] == "diagram.parent.cycle" for item in diagnostics),
            diagnostics,
        )

        cross_page = semantic_plan_v2(cycle=False)
        cross_page["result"]["pages"].append(
            {
                "page_id": "page-2",
                "name": "Page 2",
                "nodes": [
                    {
                        "stable_identity": {"page_id": "page-2", "cell_id": "node-c"},
                        "label": "C",
                        "semantic_type": "task",
                        "parent": None,
                        "style_hint": None,
                    }
                ],
                "edges": [
                    {
                        "stable_identity": {"page_id": "page-2", "cell_id": "edge-c"},
                        "source": {"page_id": "page-1", "cell_id": "node-a"},
                        "target": {"page_id": "page-2", "cell_id": "node-c"},
                        "label": "cross",
                        "relationship": "sequence",
                        "parent": None,
                        "style_hint": None,
                    }
                ],
            }
        )
        diagnostics = diagram_model_v2.validate_semantic_plan_cross_fields(cross_page)
        self.assertTrue(
            any(item["code"] == "diagram.reference.source_cross_page" for item in diagnostics),
            diagnostics,
        )

    def test_ledger_and_receipt_verification_reject_malformed_inputs(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            run_dir = temp / "run"
            run_dir.mkdir()
            ledger = Path(run_dir) / "run-manifest.v2.jsonl"
            ledger.write_text("{not json}\n", encoding="utf-8")
            replay = evidence_v2.verify_event_ledger(ledger, run_dir=run_dir, expected_run_id="run-1")
            self.assertFalse(replay["valid"])
            self.assertTrue(any(item["code"].startswith("evidence.") for item in replay["diagnostics"]))

            attempt_dir = run_dir / "attempts" / "candidate"
            attempt_dir.mkdir(parents=True)
            artifact = write_text(attempt_dir / "candidate.drawio", "{}")
            report = write_text(attempt_dir / "report.json", "{}")
            stderr = write_text(attempt_dir / "stderr.txt", "")
            stdout = write_text(attempt_dir / "stdout.txt", "")
            trusted_validator = {
                "name": "validator",
                "version": "1.0.0",
                "path": "scripts/validate.py",
                "file_sha256": "7" * 64,
            }
            receipt = {
                "schema_version": 2,
                "receipt_id": "receipt-1",
                "run_id": "run-1",
                "attempt_id": "candidate",
                "attempt_dir": "attempts/candidate",
                "started_at": "2026-07-21T12:00:00Z",
                "finished_at": "2026-07-21T12:01:00Z",
                "strict": True,
                "artifact": {"path": "attempts/candidate/../candidate/candidate.drawio", "sha256": "1" * 64, "byte_length": artifact.stat().st_size},
                "command": ["validate.py"],
                "exit_code": 0,
                "validator": trusted_validator,
                "bindings": {
                    "implementation_snapshot_sha256": "2" * 64,
                    "source_bundle_sha256": "3" * 64,
                    "candidate_sha256": "1" * 64,
                },
                "outputs": {
                    "report": {"path": "attempts/candidate/report.json", "sha256": "1" * 64, "byte_length": report.stat().st_size},
                    "stdout": {"path": "attempts/candidate/stdout.txt", "sha256": "1" * 64, "byte_length": stdout.stat().st_size},
                    "stderr": {"path": "attempts/candidate/stderr.txt", "sha256": "1" * 64, "byte_length": stderr.stat().st_size},
                },
                "result": "passed",
            }
            result = evidence_v2.verify_validation_receipt(
                receipt,
                run_dir=run_dir,
                trusted_validator=trusted_validator,
                extension_root=ROOT,
                expected_run_id="run-1",
            )
            self.assertFalse(result["valid"])
            self.assertTrue(any(item["pointer"].endswith("/artifact/path") or item["pointer"].endswith("/outputs/report/path") for item in result["diagnostics"]))


class RendererAdapterV2Tests(unittest.TestCase):
    def test_registry_generic_passthrough_and_roadmap_gitflow_parity(self):
        self.assertIn("generic", renderer_adapters.ADAPTER_REGISTRY)
        self.assertEqual(renderer_adapters.select_adapter("drawio").adapter.adapter_id, "generic-v2")
        self.assertEqual(renderer_adapters.select_adapter("product-roadmap").adapter.adapter_id, "roadmap-local")
        self.assertEqual(renderer_adapters.select_adapter("gitflow").adapter.adapter_id, "git-flow-local")

        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            output = temp / "generic.drawio"
            plan = {
                "pages": [
                    {
                        "page_id": "page-1",
                        "nodes": [
                            {
                                "stable_identity": {"page_id": "page-1", "cell_id": "node-a"},
                                "parent": {"page_id": "page-1", "cell_id": "group-1"},
                                "route": None,
                            },
                            {
                                "stable_identity": {"page_id": "page-1", "cell_id": "group-1"},
                                "parent": {"page_id": "page-1", "cell_id": "node-a"},
                                "route": None,
                            },
                        ],
                        "edges": [
                            {
                                "stable_identity": {"page_id": "page-1", "cell_id": "edge-1"},
                                "source": {"page_id": "page-1", "cell_id": "node-a"},
                                "target": {"page_id": "page-1", "cell_id": "group-1"},
                                "parent": {"page_id": "page-1", "cell_id": "group-1"},
                                "route": {
                                    "orthogonal": True,
                                    "source_pin": {"x": 0.1, "y": 0.2},
                                    "target_pin": {"x": 0.8, "y": 0.9},
                                    "waypoints": [{"x": 0, "y": 0}, {"x": 0, "y": 5}, {"x": 3, "y": 5}],
                                },
                            }
                        ],
                    }
                ]
            }
            captured = {}

            def renderer(value, path):
                captured["value"] = value
                write_text(path, json.dumps(value, ensure_ascii=False, sort_keys=True))

            run = renderer_adapters.render_with_adapter(
                "drawio",
                plan,
                output,
                options={"backend": "legacy-generic-v2"},
                generic_renderer=renderer,
            )
            self.assertEqual(captured["value"], plan)
            self.assertIsNot(captured["value"], plan)
            self.assertEqual(run.selection.adapter.adapter_id, "generic-v2")
            self.assertEqual(run.source_path, None)

            roadmap_source = ROOT / "tests" / "fixtures" / "roadmap" / "basic.yaml"
            roadmap_output = temp / "roadmap.drawio"
            roadmap_report = temp / "roadmap-report.json"
            roadmap_run = renderer_adapters.render_with_adapter(
                "roadmap",
                roadmap_source,
                roadmap_output,
                options={"report": str(roadmap_report)},
            )
            roadmap_command = renderer_adapters.validation_command(roadmap_run)
            self.assertIn("--profile", roadmap_command)
            self.assertIn("roadmap", roadmap_command)
            self.assertIn("--source", roadmap_command)

            gitflow_output = temp / "gitflow.drawio"
            gitflow_run = renderer_adapters.render_with_adapter(
                "gitflow",
                ROOT / "tests" / "fixtures" / "gitflow" / "classic.json",
                gitflow_output,
                options={"route": "builtin"},
            )
            gitflow_command = renderer_adapters.validation_command(gitflow_run)
            self.assertIn("--profile", gitflow_command)
            self.assertIn("gitflow", gitflow_command)
            self.assertIn("--route", gitflow_run.command)
            self.assertIn("builtin", gitflow_run.command)

    def test_select_lifecycle_adapter_input_rejects_mismatched_source_bundle_binding(self):
        roadmap_source = {
            "lanes": [{"id": "lane-1", "title": "Lane 1"}],
            "tasks": [],
            "milestones": [],
            "dependencies": [],
            "outcomes": [],
            "schema_version": 2,
            "title": "Roadmap",
            "time_scale": "month",
        }
        bundle = source_bundle_v2.build_source_bundle(
            bundle_id="bundle-1",
            run_id="run-1",
            sources=[
                source_bundle_v2.source_record(
                    source_id="request",
                    kind="original_user_request",
                    uri="urn:diagram-run:run-1:request",
                    content={"request": "create a roadmap"},
                ),
                source_bundle_v2.source_record(
                    source_id="roadmap",
                    kind="explicit_user_document",
                    uri="urn:roadmap:1",
                    content=roadmap_source,
                ),
            ],
            transaction_id="tx-1",
        )
        plan = semantic_plan_v2(cycle=False)
        plan["result"]["diagram_type"] = "roadmap"
        plan["source_bundle_sha256"] = "0" * 64

        with self.assertRaisesRegex(
            renderer_adapters.AdapterConfigurationError,
            "semantic\\.delta\\.source_mismatch",
        ):
            renderer_adapters.select_lifecycle_adapter_input(plan, bundle, mode="create")

    def test_c4_local_adapter_renders_multi_page_output_and_falls_back_without_explicit_source(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            source_path = write_json(temp / "c4.json", c4_source_v1())
            output = temp / "c4.drawio"
            selection = renderer_adapters.select_adapter("c4")
            self.assertEqual(selection.adapter.adapter_id, "c4-local")
            self.assertFalse(selection.fallback)

            import c4 as c4_module

            layout_module = c4_module.load_autolayout()

            def fake_layout(dot_src):
                nodes = re.findall(r'"([^"]+)"\s+\[width=([0-9.]+) height=([0-9.]+)\];', dot_src)
                edges = re.findall(r'"([^"]+)"\s*->\s*"([^"]+)"', dot_src)
                pos = {
                    name: (1.5 + index * 2.0, 1.5 + index * 1.4)
                    for index, (name, _, _) in enumerate(nodes)
                }
                edge_pts = {
                    (src, dst): [(1.5 + index, 1.5 + index), (2.5 + index, 2.5 + index)]
                    for index, (src, dst) in enumerate(edges)
                }
                return 10.0, pos, edge_pts

            layout_module.layout = fake_layout
            with mock.patch.object(c4_module, "load_autolayout", return_value=layout_module), mock.patch.object(
                sys, "argv", [str(SCRIPTS / "c4.py"), str(source_path), "-o", str(output)]
            ):
                c4_module.main()
            self.assertTrue(output.is_file())

            tree = ET.parse(output)
            root = tree.getroot()
            diagrams = root.findall("diagram")
            self.assertEqual([diagram.get("name") for diagram in diagrams], ["System Context", "Containers"])
            self.assertEqual([diagram.get("id") for diagram in diagrams], ["system-context", "containers"])

            system_root = diagrams[0].find("./mxGraphModel/root")
            containers_root = diagrams[1].find("./mxGraphModel/root")
            self.assertIsNotNone(system_root)
            self.assertIsNotNone(containers_root)
            system_cells = {cell.get("id"): cell for cell in system_root.findall("mxCell")}
            container_cells = {cell.get("id"): cell for cell in containers_root.findall("mxCell")}
            self.assertIn("customer", system_cells)
            self.assertEqual(system_cells["customer"].get("value"), "Customer\n[Person]\nUses the bank system")
            self.assertGreater(len(container_cells), 2)

            fallback_plan = semantic_plan_v2(cycle=False)
            fallback_plan["result"]["diagram_type"] = "c4"
            fallback_bundle = source_bundle_v2.build_source_bundle(
                bundle_id="bundle-c4",
                run_id="run-c4",
                sources=[
                    source_bundle_v2.source_record(
                        source_id="request",
                        kind="original_user_request",
                        uri="urn:diagram-run:run-c4:request",
                        content={"request": "create a c4 diagram"},
                    ),
                ],
                transaction_id="tx-c4",
            )
            def fake_run(command, **kwargs):
                if "c4.py" not in command:
                    return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
                out_path = Path(command[command.index("-o") + 1])
                out_path.write_text(
                    """<?xml version='1.0' encoding='UTF-8'?>
<mxfile>
  <diagram id="system-context" name="System Context">
    <mxGraphModel>
      <root>
        <mxCell id="0"/>
        <mxCell id="1" parent="0"/>
        <mxCell id="title" value="Banking Platform" data-source-title="Banking Platform" parent="1" vertex="1"/>
        <mxCell id="customer" value="Customer&#xa;[Person]&#xa;Uses the bank system" parent="1" vertex="1"/>
        <mxCell id="bank" value="Banking Platform" link="data:page/id,containers" parent="1" vertex="1"/>
      </root>
    </mxGraphModel>
  </diagram>
  <diagram id="containers" name="Containers">
    <mxGraphModel>
      <root>
        <mxCell id="0"/>
        <mxCell id="1" parent="0"/>
        <mxCell id="title" value="Banking Platform" data-source-title="Banking Platform" parent="1" vertex="1"/>
        <mxCell id="web" value="Web App" parent="1" vertex="1"/>
        <mxCell id="api" value="API" parent="1" vertex="1"/>
        <mxCell id="db" value="Database" parent="1" vertex="1"/>
      </root>
    </mxGraphModel>
  </diagram>
</mxfile>
""",
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            with mock.patch.object(renderer_adapters.subprocess, "run", side_effect=fake_run):
                run = renderer_adapters.render_with_adapter("c4", source_path, output)
            self.assertEqual(run.selection.adapter.adapter_id, "c4-local")
            self.assertFalse(run.selection.fallback)
            self.assertTrue(output.is_file())
            bundle_hash = source_bundle_v2.source_bundle_sha256(fallback_bundle)
            fallback_plan["source_bundle_sha256"] = bundle_hash
            fallback_plan["result"]["semantic_delta"]["source_bundle_sha256"] = bundle_hash
            fallback = renderer_adapters.select_lifecycle_adapter_input(
                fallback_plan, fallback_bundle, mode="create",
            )
            self.assertEqual(fallback.selection.adapter.adapter_id, "generic-v2")
            self.assertTrue(fallback.selection.fallback)
            self.assertIsNone(fallback.source_record)
            self.assertEqual(fallback.fallback_reason, "specialized_source_missing_or_invalid")

    def test_generic_v2_renderer_preserves_page_scoped_ids_parents_routes_and_style_hints(self):
        plan = generic_plan_v2()
        self.assertEqual(diagram_model_v2.validate_semantic_plan(plan), [])

        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            output = temp / "generic.drawio"
            import diagram_orchestrator as orchestrator

            run = renderer_adapters.render_with_adapter(
                "drawio",
                plan,
                output,
                options={"backend": "legacy-generic-v2"},
                generic_renderer=orchestrator.render_generic,
            )
            self.assertEqual(run.selection.adapter.adapter_id, "generic-v2")
            self.assertFalse(run.selection.fallback)
            validate = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "validate.py"),
                    str(output),
                    "--strict",
                    "--json",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(validate.returncode, 1, validate.stderr + validate.stdout)
            legacy_findings = json.loads(validate.stdout)["findings"]
            self.assertEqual(
                {finding["code"] for finding in legacy_findings},
                {"artifact.layout.excessive_detour"},
            )
            tree = ET.parse(output)
            root = tree.getroot()
            diagrams = root.findall("diagram")
            self.assertEqual([diagram.get("id") for diagram in diagrams], ["page-1", "page-2"])
            self.assertEqual([diagram.get("name") for diagram in diagrams], ["Page 1", "Page 2"])

            page1 = {cell.get("id"): cell for cell in diagrams[0].find("./mxGraphModel/root").findall("mxCell")}
            page2 = {cell.get("id"): cell for cell in diagrams[1].find("./mxGraphModel/root").findall("mxCell")}
            self.assertEqual(page1["container"].get("parent"), "1")
            self.assertEqual(page1["child"].get("parent"), "container")
            self.assertEqual(page1["child"].get("data-style-hint"), "shadow=1;")
            self.assertEqual(page1["container"].get("data-style-hint"), "rounded=1;fillColor=#ffeeaa;")
            self.assertEqual(page2["child"].get("parent"), "container")
            self.assertEqual(page2["child"].get("data-style-hint"), "shadow=1;")

            branch = page1["branch"]
            self.assertEqual(branch.get("source"), "container")
            self.assertEqual(branch.get("target"), "peer")
            self.assertIn("dashed=1;", branch.get("style"))
            branch_points = [
                (point.get("x"), point.get("y"))
                for point in branch.find("./mxGeometry/Array[@as='points']").findall("mxPoint")
            ]
            self.assertEqual(branch_points, [("420", "90"), ("420", "220")])
            self.assertIn("exitX=1;", branch.get("style"))
            self.assertIn("entryX=0;", branch.get("style"))

            loop = page1["loop"]
            self.assertEqual(loop.get("source"), "peer")
            self.assertEqual(loop.get("target"), "peer")
            self.assertIn("strokeColor=#ff0000;", loop.get("style"))
            loop_points = [
                (point.get("x"), point.get("y"))
                for point in loop.find("./mxGeometry/Array[@as='points']").findall("mxPoint")
            ]
            self.assertEqual(loop_points, [("560", "260"), ("610", "260")])
            self.assertEqual(page1["container"].get("data-page-id"), "page-1")
            self.assertEqual(page2["container"].get("data-page-id"), "page-2")


if __name__ == "__main__":
    unittest.main()
