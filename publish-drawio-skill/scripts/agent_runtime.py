#!/usr/bin/env python3
"""Portable isolated-role adapter for Gemini/GigaCode-compatible CLIs.

Native extension subagents use agents/*.md. This adapter is the executable
fallback when the host cannot assign a model per nested role. It never changes
the interactive session's global model and never executes model output.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import jsonschema

from diagram_supervisor import SupervisorError, append_event, load_json, resolve_model, utc_now


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_POLICY = ROOT / "data" / "model-routing.default.json"
CORPORATE_GIGACODE = Path.home() / ".gigacode" / "bin" / "gigacode"
DEFAULT_CLI = os.environ.get("GIGACODE_BIN") or (
    str(CORPORATE_GIGACODE) if CORPORATE_GIGACODE.is_file() else "gemini"
)
SAFE_ENV_KEYS = {
    "HOME", "LANG", "LC_ALL", "LC_CTYPE", "NO_COLOR", "PATH",
    "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "WINDIR",
}
ROLE_MAX_SESSION_TURNS = 4
ROLE_APPROVAL_MODE = "default"
ROLE_CORE_TOOL_SENTINEL = "__drawio_isolated_role_has_no_tools__"
ROLE_ALLOWED_MCP_SERVERS = ()
ROLE_EXCLUDED_TOOLS = (
    "agent",
    "task",
    "skill",
    "ask_user_question",
    "ask_user",
    "todo_write",
    "write_todos",
    "list_directory",
    "read_file",
    "read_many_files",
    "grep_search",
    "glob",
    "run_shell_command",
    "write_file",
    "edit",
    "replace",
    "save_memory",
    "web_fetch",
    "web_search",
    "lsp",
    "mcp__*",
    "exit_plan_mode",
)
SUPERVISOR_LAYOUT_ACTIONS = (
    "create_layout",
    "reroute_edges",
    "expand_local_scope",
    "retry_layout_strategy",
    "request_semantic_clarification",
    "finish_best_effort",
)


class RoleOutputContractError(SupervisorError):
    """Role output was model-proven JSON but failed its output contract."""

    def __init__(
        self,
        message,
        *,
        resolution,
        runtime_metadata,
        invalid_output_sha256=None,
        diagnostics=(),
        failure_kind="output_schema",
        original_input_sha256=None,
    ):
        super().__init__(message)
        self.resolution = resolution
        self.runtime_metadata = runtime_metadata
        self.invalid_output_sha256 = invalid_output_sha256
        self.diagnostics = [dict(item) for item in diagnostics]
        self.failure_kind = failure_kind
        self.original_input_sha256 = original_input_sha256


def role_isolation_controls():
    return {
        "approval_mode": ROLE_APPROVAL_MODE,
        "extensions": ["none"],
        "core_tools": [ROLE_CORE_TOOL_SENTINEL],
        "allowed_mcp_servers": list(ROLE_ALLOWED_MCP_SERVERS),
        "excluded_tools": list(ROLE_EXCLUDED_TOOLS),
        "max_session_turns": ROLE_MAX_SESSION_TURNS,
    }


def redact(text):
    text = re.sub(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/-]+", r"\1 [REDACTED]", text or "")
    return re.sub(
        r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|password|authorization)\b(\s*[:=]\s*)[^\s,;]+",
        r"\1\2[REDACTED]",
        text,
    )


def role_body(path):
    text = Path(path).read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise SupervisorError(f"agent prompt {path} has no YAML frontmatter")
    marker = text.find("\n---\n", 4)
    if marker < 0:
        raise SupervisorError(f"agent prompt {path} has unterminated YAML frontmatter")
    return text[marker + 5:].strip()


def contract_version(payload=None):
    if (
        isinstance(payload, dict)
        and type(payload.get("schema_version")) is int
        and payload["schema_version"] == 2
    ):
        return 2
    return 1


def role_schema_name(role, payload=None):
    version = contract_version(payload)
    if role == "reviewer":
        return f"reviewer-analysis.v{version}.schema.json"
    if role == "repair":
        if isinstance(payload, dict) and payload.get("repair_mode") == "layout_intent":
            return "layout-repair-intent.v1.schema.json"
        return "diagram-patch.v1.schema.json"
    if role == "supervisor":
        return "supervisor-decision.v1.schema.json"
    if role == "semantic_analyst":
        if isinstance(payload, dict) and payload.get("phase") == "intake":
            return "diagram-intake-analysis.v1.schema.json"
        return (
            "semantic-analysis.v2.schema.json"
            if version == 2 else "semantic-plan.v1.schema.json"
        )
    raise SupervisorError(f"unknown role {role!r}")


def _supervisor_layout_schema():
    """Return the host-enforced, non-coordinating Supervisor output contract."""
    return {
        "type": "object",
        "required": ["schema_version", "role", "status", "result"],
        "additionalProperties": False,
        "properties": {
            "schema_version": {"const": 1},
            "role": {"const": "supervisor"},
            "status": {"enum": ["ok", "needs_human"]},
            "result": {
                "type": "object",
                "required": ["action", "reason"],
                "additionalProperties": False,
                "properties": {
                    "action": {"type": "string", "minLength": 1},
                    "reason": {"type": "string", "minLength": 1},
                    "max_iterations": {"type": "integer", "minimum": 1, "maximum": 12},
                    "findings": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    }


def role_output_schema(role, payload):
    """Return the exact output schema enforced by this isolated-role runtime."""
    if role == "supervisor":
        return _supervisor_layout_schema()
    return load_json(ROOT / "data" / role_schema_name(role, payload))


def role_input_schema_name(role, payload=None):
    if role == "reviewer" and contract_version(payload) == 2:
        return "reviewer-input.v2.schema.json"
    return None


def reviewer_evidence_bindings(payload):
    if payload.get("review_kind") == "baseline_audit" and isinstance(payload.get("artifact"), dict):
        return {
            "run_id": payload["run_id"],
            "candidate_sha256": payload["artifact"]["sha256"],
            "report_sha256": payload["report"]["sha256"],
            "receipt_sha256": payload["receipt"]["sha256"],
        }
    candidate = payload.get("candidate")
    if isinstance(candidate, dict) and isinstance(candidate.get("artifact"), dict):
        return {
            "run_id": payload["run_id"],
            "candidate_sha256": candidate["artifact"]["sha256"],
            "report_sha256": candidate["report"]["sha256"],
            "receipt_sha256": candidate["receipt"]["sha256"],
        }
    if isinstance(candidate, dict) and "sha256" in candidate:
        return {
            "run_id": payload["run_id"],
            "candidate_sha256": candidate["sha256"],
            "report_sha256": payload["validation_report"]["sha256"],
            "receipt_sha256": payload["validation_receipt"]["sha256"],
        }
    return None


def role_output_contract(role, payload):
    schema_name = role_schema_name(role, payload)
    schema = role_output_schema(role, payload)
    schema_label = (
        "the runtime-enforced supervisor layout schema"
        if role == "supervisor" else schema_name
    )
    sections = [
        "## Required output JSON Schema",
        (
            f"Return exactly one JSON object that validates against {schema_label}. "
            "Do not omit required properties."
        ),
        json.dumps(schema, ensure_ascii=False, sort_keys=True),
    ]
    if role == "reviewer":
        if contract_version(payload) == 2:
            reviewer_binding = (
                "Return analysis only. Do not assert reviewer model, provider, "
                "resolution mode, runtime proof, role input/output hashes, or final "
                "evidence bindings. The deterministic host derives those fields "
                "from verified runtime evidence and constructs reviewer-verdict.v2."
            )
        else:
            reviewer_binding = (
                "Return the analytical verdict and findings. Do not copy run_id, "
                "candidate_sha256, report_sha256, or receipt_sha256. The deterministic "
                "host derives those fields from the validated runtime input and "
                "constructs the final hash-bound verdict. Optional legacy declarations "
                "are diagnostic only."
            )
        sections.extend(["## Host-owned reviewer evidence bindings", reviewer_binding])
    if role == "supervisor":
        sections.extend([
            "## Host-owned layout strategy policy",
            (
                "Choose only one allowlisted layout strategy action. Do not declare "
                "roles, coordinate siblings, or schedule execution; the deterministic "
                "host owns all orchestration."
            ),
        ])
    if role == "repair" and isinstance(payload, dict) and payload.get("repair_mode") == "layout_intent":
        sections.extend([
            "## Bounded layout-repair intent",
            (
                "Return the scoped layout-repair-intent only. It must name the exact "
                "page, target edges, movable nodes, and locked nodes; never return "
                "coordinates, waypoints, XML, or an unbounded reflow request."
            ),
        ])
    if (
        role == "semantic_analyst"
        and isinstance(payload, dict)
        and payload.get("phase") == "intake"
    ):
        sections.extend([
            "## Host-owned intake bindings",
            (
                "Propose classification and semantic completeness only. The host "
                "assigns intake and question ids, validates the diagram-type "
                "allowlist, caps and sequences questions, binds human answers, "
                "records accepted assumptions, and decides completion. Do not "
                "return host ids, request hashes, answers, completeness state, "
                "decision fields, XML, or tool calls."
            ),
        ])
    elif role == "semantic_analyst" and contract_version(payload) == 2:
        sections.extend([
            "## Host-owned semantic bindings",
            (
                "Return only the complete desired page-scoped graph, assumptions, "
                "and human questions. Do not return run or source hashes, a semantic "
                "delta, operation IDs, approval claims, or any model-owned evidence "
                "binding. The deterministic host binds the exact source bundle and "
                "baseline, derives removals as well as additions/changes, assigns "
                "operation IDs, records assumption sources, and constructs the "
                "canonical semantic-plan.v2 artifact. Route is layout context and is "
                "not a semantic operation."
            ),
        ])
    return "\n\n".join(sections)


def correction_contract(original_input_sha256, diagnostics):
    return {
        "correction_attempt": 1,
        "original_input_sha256": original_input_sha256,
        "diagnostics": [dict(item) for item in diagnostics],
        "requirements": {
            "same_role": True,
            "same_model": True,
            "input_is_unchanged": True,
            "return_json_only": True,
        },
    }


def isolated_role_system_prompt(role, prompt_path, payload, *, correction=None):
    prompt = (
        "You are the isolated diagram role itself. Complete exactly one bounded "
        "JSON decision. Do not call or delegate to any agent. Do not call any tool, "
        "slash command, skill, interactive question, todo facility, filesystem "
        "operation, shell command, or network service. Do not ask for confirmation. "
        "The canonical runtime input is the JSON document supplied on standard input. "
        "Return exactly one JSON object and no prose.\n\n"
        + role_body(prompt_path)
        + "\n\n"
        + role_output_contract(role, payload)
    )
    if correction is not None:
        prompt += (
            "\n\n## Bounded contract correction\n\n"
            "Your prior model-proven, isolated, tool-free response failed only the "
            "declared output contract. Correct those diagnostics against the unchanged "
            "canonical input. Do not reinterpret, expand, or replace the input. This is "
            "the only correction attempt.\n\n"
            + json.dumps(correction, ensure_ascii=False, sort_keys=True)
        )
    return prompt


def build_gemini_command(
    cli, model=None, auth_type=None, *, system_prompt, output_format="json"
):
    command = [cli]
    if auth_type:
        command.extend(["--auth-type", auth_type])
    if model:
        command.extend(["--model", model])
    command.extend([
        "--extensions", "none",
        "--system-prompt", system_prompt,
        "--max-session-turns", str(ROLE_MAX_SESSION_TURNS),
        "--core-tools", ROLE_CORE_TOOL_SENTINEL,
        # Qwen Code 0.13.1 treats a present flag with one empty value as an
        # explicit empty allowlist. This removes globally configured MCP
        # servers before their tool schemas are advertised to the role.
        "--allowed-mcp-server-names", "",
        "--exclude-tools", ",".join(ROLE_EXCLUDED_TOOLS),
        "--prompt", (
            "Process the canonical runtime JSON supplied on standard input. "
            "Return exactly one object satisfying the system contract."
        ),
        "--output-format", output_format,
        "--approval-mode", ROLE_APPROVAL_MODE,
    ])
    return command


def command_model_argument(command):
    if "--model" not in command:
        return None
    index = command.index("--model") + 1
    return command[index] if index < len(command) else None


def minimal_environment():
    return {key: value for key, value in os.environ.items() if key in SAFE_ENV_KEYS}


def run_captured_process(command, stdin_text, *, timeout, cwd):
    """Run with stdout/stderr written continuously to temporary files."""
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stdout_file, \
            tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr_file:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=stdout_file,
            stderr=stderr_file,
            text=True,
            cwd=cwd,
            env=minimal_environment(),
        )
        try:
            process.communicate(input=stdin_text, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.communicate()
            stdout_file.seek(0)
            stderr_file.seek(0)
            exc.stdout = stdout_file.read()
            exc.stderr = stderr_file.read()
            raise
        stdout_file.seek(0)
        stderr_file.seek(0)
        return subprocess.CompletedProcess(
            command,
            process.returncode,
            stdout=stdout_file.read(),
            stderr=stderr_file.read(),
        )


def _atomic_write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as tmp:
        tmp.write(text)
        tmp.flush()
        os.fsync(tmp.fileno())
        temp_name = tmp.name
    os.replace(temp_name, path)
    return path


def persist_runtime_captures(
    output_path, stdout, stderr, *, output_format="json", attempt_id=None
):
    output_path = Path(output_path)
    capture_dir = output_path.parent
    if attempt_id:
        capture_dir = capture_dir / "attempts" / attempt_id
    capture_name = (
        "runtime-output.jsonl" if output_format == "stream-json"
        else "runtime-output.json"
    )
    runtime_capture = _atomic_write_text(
        capture_dir / capture_name, stdout or ""
    )
    stderr_capture = _atomic_write_text(
        capture_dir / "runtime-stderr.txt", redact(stderr or "")
    )
    return {
        "runtime_capture": str(runtime_capture.resolve()),
        "runtime_capture_sha256": hashlib.sha256(runtime_capture.read_bytes()).hexdigest(),
        "stderr_capture": str(stderr_capture.resolve()),
        "stderr_capture_sha256": hashlib.sha256(stderr_capture.read_bytes()).hexdigest(),
    }


def detect_cli_capabilities(cli):
    completed = subprocess.run(
        [cli, "--help"], text=True, capture_output=True, check=False, timeout=30,
        env=minimal_environment(),
    )
    help_text = completed.stdout + completed.stderr
    isolation_required = (
        "--extensions", "--system-prompt", "--max-session-turns", "--core-tools",
        "--allowed-mcp-server-names", "--exclude-tools",
    )
    base_required = (
        "--prompt", "--output-format", "--approval-mode", *isolation_required,
    )
    required = ("--model", *base_required)
    missing = [flag for flag in required if flag not in help_text]
    inherited_missing = [flag for flag in base_required if flag not in help_text]
    supports_auth_type = "--auth-type" in help_text
    looks_like_gigacode = (
        "gigacode" in Path(cli).name.lower()
        or bool(re.search(r"(?i)\bGigaCode\b|--auth-type.{0,240}\bgigacode\b", help_text, re.DOTALL))
    )
    supports_stream_json = bool(re.search(r"(?<![\w-])stream-json(?![\w-])", help_text))
    return {
        "available": completed.returncode == 0 and not missing,
        "inherited_available": completed.returncode == 0 and not inherited_missing,
        "supports_auth_type": supports_auth_type,
        "suggested_auth_type": "gigacode" if supports_auth_type and looks_like_gigacode else None,
        "exit_code": completed.returncode,
        "required_flags": list(required),
        "missing_flags": missing,
        "isolation_flags": list(isolation_required),
        "isolation_available": completed.returncode == 0 and not [
            flag for flag in isolation_required if flag not in help_text
        ],
        "supports_stream_json": supports_stream_json,
    }


def _parse_json_role_text(role, value, source):
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        raise SupervisorError(f"isolated {role} {source} must be a JSON object or encoded JSON string")
    stripped = value.strip()
    if stripped.startswith("```") or stripped.endswith("```"):
        fenced = re.fullmatch(
            r"```(?:json)?[ \t]*\r?\n(?P<payload>[\s\S]*?)\r?\n```",
            stripped,
            flags=re.IGNORECASE,
        )
        if not fenced:
            raise SupervisorError(
                f"isolated {role} {source} has an ambiguous Markdown JSON fence"
            )
        stripped = fenced.group("payload").strip()
        if "```" in stripped:
            raise SupervisorError(
                f"isolated {role} {source} has an ambiguous Markdown JSON fence"
            )
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise SupervisorError(f"isolated {role} {source} is not role JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SupervisorError(f"isolated {role} {source} must decode to a JSON object")
    return parsed


def _gigacode_isolation_proof(role, events):
    if not events or not all(isinstance(event, dict) for event in events):
        return {
            "verified": False,
            "tool_calls": None,
            "tool_names": [],
            "drawio_agents": [],
            "drawio_commands": [],
            "system_init_events": 0,
            "diagnostic": (
                f"isolated {role} GigaCode event output must be a non-empty "
                "object array"
            ),
        }

    leaked_agents = set()
    leaked_commands = set()
    tool_calls = []
    init_events = 0
    assistant_events = 0
    system_models = set()
    assistant_models = set()
    for event in events:
        if event.get("type") == "system" and event.get("subtype") == "init":
            init_events += 1
            if event.get("model"):
                system_models.add(event["model"])
            agents = event.get("agents") if isinstance(event.get("agents"), list) else []
            commands = (
                event.get("slash_commands")
                if isinstance(event.get("slash_commands"), list)
                else []
            )
            leaked_agents.update(
                value for value in agents
                if isinstance(value, str) and value.startswith("diagram-")
            )
            leaked_commands.update(
                value for value in commands
                if isinstance(value, str) and value.startswith("drawio:")
            )
        if event.get("type") == "assistant" and isinstance(event.get("message"), dict):
            assistant_events += 1
            if event["message"].get("model"):
                assistant_models.add(event["message"]["model"])
            content = event["message"].get("content")
        else:
            content = None
        if isinstance(content, list):
            tool_calls.extend(
                item.get("name") or "unknown"
                for item in content
                if isinstance(item, dict) and item.get("type") == "tool_use"
            )

    diagnostics = []
    if init_events != 1:
        diagnostics.append(
            f"expected exactly one system init event, observed {init_events}"
        )
    if leaked_agents or leaked_commands:
        diagnostics.append(
            "customization isolation failed: "
            f"agents={sorted(leaked_agents)}, commands={sorted(leaked_commands)}"
        )
    if tool_calls:
        diagnostics.append(
            "violated the tool-free role contract: "
            + ", ".join(map(str, tool_calls[:10]))
        )
    return {
        "verified": not diagnostics,
        "tool_calls": len(tool_calls),
        "tool_names": list(map(str, tool_calls[:10])),
        "drawio_agents": sorted(leaked_agents),
        "drawio_commands": sorted(leaked_commands),
        "system_init_events": init_events,
        "event_count": len(events),
        "assistant_events": assistant_events,
        "last_event_type": events[-1].get("type"),
        "system_models": sorted(system_models),
        "assistant_models": sorted(assistant_models),
        "diagnostic": "; ".join(diagnostics) if diagnostics else None,
    }


def _decode_runtime_events(output):
    """Decode either buffered JSON events or newline-delimited stream-json."""
    try:
        outer = json.loads(output)
    except json.JSONDecodeError as buffered_error:
        events = []
        for line_number, line in enumerate((output or "").splitlines(), 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SupervisorError(
                    f"runtime capture JSONL line {line_number} is invalid: {exc}"
                ) from exc
            if not isinstance(event, dict):
                raise SupervisorError(
                    f"runtime capture JSONL line {line_number} is not an object"
                )
            events.append(event)
        if not events:
            raise SupervisorError(
                f"runtime capture is neither buffered JSON nor JSONL: {buffered_error}"
            ) from buffered_error
        return events, "stream-json"
    if isinstance(outer, list):
        return outer, "json"
    if isinstance(outer, dict) and outer.get("type") in {"system", "assistant", "result"}:
        return [outer], "stream-json"
    return outer, "json"


def inspect_runtime_isolation(role, output):
    try:
        outer, _ = _decode_runtime_events(output)
    except SupervisorError as exc:
        return {
            "verified": False,
            "tool_calls": None,
            "tool_names": [],
            "drawio_agents": [],
            "drawio_commands": [],
            "system_init_events": 0,
            "diagnostic": str(exc),
        }
    if not isinstance(outer, list):
        return {
            "verified": False,
            "tool_calls": None,
            "tool_names": [],
            "drawio_agents": [],
            "drawio_commands": [],
            "system_init_events": 0,
            "diagnostic": "runtime capture is not a GigaCode event array",
        }
    return _gigacode_isolation_proof(role, outer)


def _parse_gigacode_events(role, events, *, stream=False):
    isolation_proof = _gigacode_isolation_proof(role, events)
    if not isolation_proof["verified"]:
        raise SupervisorError(
            f"isolated {role} GigaCode {isolation_proof['diagnostic']}"
        )

    system_models = {
        event.get("model")
        for event in events
        if event.get("type") == "system" and event.get("subtype") == "init" and event.get("model")
    }
    assistant_models = set()
    assistant_texts = []
    for event in events:
        if event.get("type") != "assistant" or not isinstance(event.get("message"), dict):
            continue
        message = event["message"]
        if message.get("model"):
            assistant_models.add(message["model"])
        content = message.get("content")
        if isinstance(content, list):
            text_parts = [
                item.get("text") for item in content
                if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
            ]
            if text_parts:
                assistant_texts.append("".join(text_parts))

    results = [event for event in events if event.get("type") == "result"]
    if not results:
        raise SupervisorError(f"isolated {role} GigaCode output has no result event")
    result = results[-1]
    if result.get("is_error") is True or result.get("subtype") not in (None, "success"):
        raise SupervisorError(
            f"isolated {role} GigaCode result reports failure: "
            f"{redact(str(result.get('result') or result.get('error') or result.get('subtype')))}"
        )

    stats = result.get("stats") if isinstance(result.get("stats"), dict) else {}
    stats_models_value = stats.get("models") if isinstance(stats.get("models"), dict) else {}
    stats_models = set(stats_models_value)
    if len(system_models) != 1 or len(assistant_models) != 1:
        raise SupervisorError(
            f"isolated {role} GigaCode model proof is ambiguous: "
            f"system={sorted(system_models)}, assistant={sorted(assistant_models)}"
        )
    system_model = next(iter(system_models))
    assistant_model = next(iter(assistant_models))
    stats_proof_required = not stream or bool(stats_models_value)
    if (
        system_model != assistant_model
        or (stats_proof_required and system_model not in stats_models)
    ):
        raise SupervisorError(
            f"isolated {role} GigaCode model proof mismatch: system={system_model!r}, "
            f"assistant={assistant_model!r}, stats={sorted(stats_models)!r}"
        )

    role_value = result.get("result")
    if role_value in (None, ""):
        if not assistant_texts:
            raise SupervisorError(f"isolated {role} GigaCode output has no assistant role payload")
        role_value = assistant_texts[-1]
    parsed = _parse_json_role_text(role, role_value, "GigaCode result")
    return parsed, {
        "format": "gigacode_stream_json" if stream else "gigacode_json_events",
        "stats": sanitized_metadata(stats),
        "errors": sanitized_metadata(result.get("error")),
        "reported_model": system_model,
        "model_proof": {
            "verified": True,
            "system_model": system_model,
            "assistant_model": assistant_model,
            "stats_models": sorted(stats_models),
            "stats_required": stats_proof_required,
            "sources": [
                "system.init.model", "assistant.message.model",
                *(["result.stats.models"] if stats_models_value else []),
            ],
        },
        "isolation_proof": isolation_proof,
        "runtime_version": next(
            (
                event.get("qwen_code_version")
                for event in events
                if event.get("type") == "system" and event.get("subtype") == "init"
            ),
            None,
        ),
    }


def sanitized_metadata(value):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if re.fullmatch(
                r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|authorization|secret)",
                str(key),
            ):
                result[key] = "[REDACTED]"
            else:
                result[key] = sanitized_metadata(item)
        return result
    if isinstance(value, list):
        return [sanitized_metadata(item) for item in value]
    if isinstance(value, str):
        return redact(value)
    return value


def parse_runtime_output(role, output):
    try:
        outer, encoding = _decode_runtime_events(output)
    except SupervisorError as exc:
        raise SupervisorError(f"isolated {role} output is not valid JSON: {exc}") from exc
    if isinstance(outer, list):
        return _parse_gigacode_events(role, outer, stream=encoding == "stream-json")
    if isinstance(outer, dict) and "response" in outer:
        errors = outer.get("errors")
        error = outer.get("error")
        if errors not in (None, [], {}, "") or error not in (None, [], {}, "", False):
            raise SupervisorError(f"isolated {role} Gemini envelope reports errors")
        parsed = _parse_json_role_text(role, outer["response"], "Gemini response")
        reported_model = outer.get("model") or (outer.get("stats") or {}).get("model")
        metadata = {
            "format": "gemini_json_envelope",
            "stats": sanitized_metadata(outer.get("stats")),
            "errors": sanitized_metadata(errors if errors is not None else error),
            "reported_model": reported_model,
            "model_proof": {"verified": bool(reported_model), "envelope_model": reported_model},
        }
        return parsed, metadata
    return outer, {
        "format": "direct_role_json", "stats": None, "errors": None,
        "reported_model": None, "model_proof": {"verified": False},
    }


def _json_pointer(parts):
    encoded = [str(part).replace("~", "~0").replace("/", "~1") for part in parts]
    return "" if not encoded else "/" + "/".join(encoded)


def _schema_contract_diagnostics(schema, parsed):
    validator = jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker()
    )
    diagnostics = []
    for error in sorted(
        validator.iter_errors(parsed),
        key=lambda item: (
            list(item.absolute_path), item.validator or "", item.message
        ),
    ):
        diagnostics.append(
            {
                "code": f"schema.{error.validator or 'invalid'}",
                "pointer": _json_pointer(error.absolute_path),
                "rule": str(error.validator or "invalid"),
                "message": error.message,
            }
        )
    return diagnostics


def validate_role_input(role, payload):
    """Fail closed on versioned role inputs before any isolated model call."""
    if (
        role == "semantic_analyst"
        and payload.get("phase") != "intake"
        and contract_version(payload) == 2
    ):
        from diagram_model_v2 import validate_semantic_analysis_input

        diagnostics = [
            {
                "code": item["code"],
                "pointer": item.get("pointer", ""),
                "rule": item["code"],
                "message": item["message"],
            }
            for item in validate_semantic_analysis_input(payload)
        ]
        if diagnostics:
            first = diagnostics[0]
            error = SupervisorError(
                f"isolated {role} input contract failed at "
                f"{first['pointer'] or '/'}: {first['message']}"
            )
            error.contract_diagnostics = diagnostics
            error.contract_failure_kind = "input_schema"
            raise error
        return payload
    schema_name = role_input_schema_name(role, payload)
    if schema_name is None:
        return payload

    try:
        schema = load_json(ROOT / "data" / schema_name)
        jsonschema.Draft202012Validator.check_schema(schema)
    except (OSError, json.JSONDecodeError, jsonschema.SchemaError) as exc:
        diagnostic = {
            "code": "input_schema.unavailable",
            "pointer": "",
            "rule": "schema_availability",
            "message": f"{schema_name} is unavailable or invalid: {exc}",
        }
        error = SupervisorError(
            f"isolated {role} input contract cannot be verified: {diagnostic['message']}"
        )
        error.contract_diagnostics = [diagnostic]
        error.contract_failure_kind = "input_schema"
        raise error from exc

    diagnostics = _schema_contract_diagnostics(schema, payload)
    if diagnostics:
        first = diagnostics[0]
        error = SupervisorError(
            f"isolated {role} input contract failed at "
            f"{first['pointer'] or '/'}: {first['message']}"
        )
        error.contract_diagnostics = diagnostics
        error.contract_failure_kind = "input_schema"
        raise error
    return payload


def _cross_field_contract_diagnostics(role, payload, parsed):
    diagnostics = []
    version = contract_version(payload)
    if role in {"supervisor", "semantic_analyst"} and parsed.get("role") != role:
        diagnostics.append(
            {
                "code": "role.identity_mismatch",
                "pointer": "/role",
                "rule": "role_identity",
                "message": f"role must equal {role!r}",
            }
        )
    if role == "supervisor":
        action = parsed.get("result", {}).get("action")
        if action not in SUPERVISOR_LAYOUT_ACTIONS:
            diagnostics.append(
                {
                    "code": "supervisor.action_not_allowlisted",
                    "pointer": "/result/action",
                    "rule": "host_layout_strategy_allowlist",
                    "message": "supervisor action must be an allowlisted host layout strategy",
                }
            )
    if (
        role == "semantic_analyst"
        and payload.get("phase") != "intake"
        and version == 2
    ):
        from diagram_model_v2 import validate_semantic_analysis_cross_fields

        diagnostics.extend(
            {
                "code": item["code"],
                "pointer": item.get("pointer", ""),
                "rule": item["code"],
                "message": item["message"],
            }
            for item in validate_semantic_analysis_cross_fields(parsed)
        )
        if parsed.get("result", {}).get("mode") != payload.get("mode"):
            diagnostics.append({
                "code": "semantic.analysis.mode_mismatch",
                "pointer": "/result/mode",
                "rule": "host_mode_binding",
                "message": "analysis mode differs from the immutable host input",
            })
    if role == "reviewer" and version == 2:
        status = parsed.get("status")
        verdict = parsed.get("verdict")
        if (status == "needs_human") != (verdict == "needs_human"):
            diagnostics.append(
                {
                    "code": "reviewer.status_verdict_mismatch",
                    "pointer": "/verdict",
                    "rule": "status_verdict_consistency",
                    "message": (
                        "status and verdict must both be needs_human or both describe "
                        "a completed analytical decision"
                    ),
                }
            )
        if verdict == "approve" and any(
            item.get("severity") == "error" for item in parsed.get("findings", [])
        ):
            diagnostics.append(
                {
                    "code": "reviewer.approve_with_error",
                    "pointer": "/verdict",
                    "rule": "approve_requires_no_error_findings",
                    "message": "approve is invalid while any error-level finding remains",
                }
            )
    if role == "reviewer" and parsed.get("verdict") == "approve":
        candidate = payload.get("candidate") if isinstance(payload, dict) else None
        report = candidate.get("report") if isinstance(candidate, dict) else None
        content = report.get("content") if isinstance(report, dict) else None
        findings = content.get("findings", []) if isinstance(content, dict) else []
        for finding in findings:
            if not isinstance(finding, dict) or finding.get("severity") != "error":
                continue
            code = str(finding.get("code", ""))
            deterministic = (
                finding.get("deterministic") is True
                or finding.get("blocking") is True
                or code.startswith(("artifact.", "semantic.", "security."))
            )
            if deterministic:
                diagnostics.append(
                    {
                        "code": "reviewer.approve_with_blocking_deterministic_finding",
                        "pointer": "/verdict",
                        "rule": "deterministic_validator_authority",
                        "message": "approve is invalid while a blocking deterministic validator finding remains",
                    }
                )
                break
    if role == "repair" and isinstance(payload, dict) and payload.get("repair_mode") == "layout_intent":
        result = parsed.get("result", {})
        if not all(key in result for key in ("target_edges", "movable_nodes", "locked_nodes")):
            diagnostics.append(
                {
                    "code": "repair.layout_intent_not_bounded",
                    "pointer": "/result",
                    "rule": "bounded_layout_repair_intent",
                    "message": "layout repair must return a bounded layout-repair-intent",
                }
            )
    diagnostics.sort(
        key=lambda item: (
            item.get("pointer", ""), item.get("code", ""), item.get("message", "")
        )
    )
    return diagnostics


def _legacy_reviewer_analysis_v2(parsed, payload):
    normalized_findings = []
    candidate_report = (payload.get("candidate") or {}).get("report") or {}
    for index, finding in enumerate(parsed.get("findings", []), 1):
        summary = finding.get("reason") or finding.get("message") or "Reviewer finding"
        lowered = summary.lower()
        category = (
            "semantic" if "semantic" in lowered
            else "routing" if any(token in lowered for token in ("route", "waypoint", "cross"))
            else "layout" if any(token in lowered for token in ("layout", "overlap", "label"))
            else "validation"
        )
        severity = finding.get("severity", "warning")
        normalized_findings.append({
            "finding_id": finding.get("finding_id") or f"review-finding-{index}",
            "category": category,
            "severity": severity,
            "summary": summary,
            "elements": [],
            "evidence": [{
                "kind": "validation_report",
                "path": candidate_report.get("path"),
                "sha256": candidate_report.get("sha256"),
                "pointer": None,
                "message": summary,
            }],
            "remediation": {
                "class": "repair" if severity in {"warning", "error"} else "none",
                "action": summary,
            },
        })
    return {
        "schema_version": 2,
        "role": "reviewer",
        "status": "needs_human" if parsed.get("verdict") == "needs_human" else "ok",
        "analysis_id": parsed["verdict_id"],
        "verdict": parsed["verdict"],
        "reviewed_at": parsed["reviewed_at"],
        "findings": normalized_findings,
    }


def validate_role_output(role, parsed, payload=None):
    if (
        role == "reviewer"
        and contract_version(payload) == 2
        and isinstance(parsed, dict)
        and parsed.get("schema_version") == 1
    ):
        # Compatibility is analysis-only: validate the legacy shape, then
        # discard its self-reported identity and evidence bindings.  The v2
        # lifecycle host derives those exclusively from verified runtime proof
        # and the hash-bound reviewer input.
        legacy_schema = load_json(ROOT / "data" / "reviewer-verdict.v1.schema.json")
        legacy_diagnostics = _schema_contract_diagnostics(legacy_schema, parsed)
        if legacy_diagnostics:
            first = legacy_diagnostics[0]
            error = SupervisorError(
                f"isolated reviewer legacy compatibility output failed at "
                f"{first['pointer'] or '/'}: {first['message']}"
            )
            error.contract_diagnostics = legacy_diagnostics
            error.contract_failure_kind = "output_schema"
            raise error
        return parsed
    schema = role_output_schema(role, payload)
    diagnostics = _schema_contract_diagnostics(schema, parsed)
    failure_kind = "output_schema"
    if not diagnostics:
        diagnostics = _cross_field_contract_diagnostics(role, payload, parsed)
        failure_kind = "cross_field"
    if diagnostics:
        first = diagnostics[0]
        error = SupervisorError(
            f"isolated {role} output contract failed at "
            f"{first['pointer'] or '/'}: {first['message']}"
        )
        error.contract_diagnostics = diagnostics
        error.contract_failure_kind = failure_kind
        raise error
    return parsed


def finalize_role_output(role, payload, parsed):
    """Construct host-owned envelopes after validating the model decision."""
    if role != "reviewer":
        return parsed, None
    if contract_version(payload) == 2:
        # Reviewer v2 is analysis-only. The lifecycle host binds runtime proof,
        # input/output hashes, candidate evidence, and source/semantic hashes in
        # reviewer-verdict.v2 after this isolated role completes.
        if parsed.get("schema_version") == 1:
            expected = reviewer_evidence_bindings(payload)
            declared = {key: parsed[key] for key in expected if key in parsed}
            mismatches = sorted(
                key for key, value in declared.items() if value != expected[key]
            )
            analysis = _legacy_reviewer_analysis_v2(parsed, payload)
            schema = load_json(ROOT / "data" / "reviewer-analysis.v2.schema.json")
            errors = _schema_contract_diagnostics(schema, analysis)
            if errors:
                raise SupervisorError(
                    f"legacy Reviewer compatibility normalization failed: {errors[0]['message']}"
                )
            return analysis, {
                "verified": True,
                "source": "host-derived-v1-compatibility",
                "declared_mismatches": mismatches,
                "expected": expected,
            }
        return parsed, None
    expected = reviewer_evidence_bindings(payload)
    if not expected:
        raise SupervisorError("isolated reviewer input has no supported evidence binding")
    declared = {
        key: parsed[key] for key in expected if key in parsed
    }
    mismatches = sorted(
        key for key, value in declared.items() if value != expected[key]
    )
    final = {
        key: parsed[key]
        for key in ("schema_version", "verdict_id", "verdict", "reviewed_at", "findings")
    }
    if "reviewer" in parsed:
        final["reviewer"] = parsed["reviewer"]
    final.update(expected)
    schema = load_json(ROOT / "data" / "reviewer-verdict.v1.schema.json")
    errors = sorted(
        jsonschema.Draft202012Validator(
            schema, format_checker=jsonschema.FormatChecker()
        ).iter_errors(final),
        key=lambda error: (list(error.path), error.message),
    )
    if errors:
        raise SupervisorError(
            f"host-bound reviewer verdict schema failed: {errors[0].message}"
        )
    proof = {
        "source": "validated_role_input",
        "expected": expected,
        "model_declared": declared,
        "declared_mismatches": mismatches,
        "verified": True,
    }
    return final, proof


def model_unavailable(stderr):
    return bool(re.search(r"(?i)(unknown|unsupported|unavailable|not[ -]found|no access).{0,80}model|model.{0,80}(unknown|unsupported|unavailable|not[ -]found|no access)", stderr or ""))


def runtime_fallback_for(config, failure_kind):
    for fallback in config.get("runtime_fallbacks", []):
        if failure_kind in fallback.get("on_failure", []):
            return fallback
    return None


def record_failure(
    run_dir, role, phase, requested_model, *, exit_code=None, diagnostic=None,
    resolved_model=None, model_proof=None, reported_model=None, runtime_version=None,
    invalid_output_sha256=None, failure_kind=None, capture_evidence=None,
    isolation_proof=None, terminal=True, attempted_model=None,
    fallback_model=None, attempt_id=None, output_format=None,
    contract_diagnostics=None, original_input_sha256=None,
    correction_attempt_id=None,
):
    if run_dir:
        payload = {
            "role": role, "phase": phase, "requested_model": requested_model,
            "exit_code": exit_code, "diagnostic": redact(diagnostic or "")[-1000:],
            "isolation_controls": role_isolation_controls(),
            "terminal": bool(terminal),
        }
        if attempted_model is not None:
            payload["attempted_model"] = attempted_model
        if fallback_model is not None:
            payload["fallback_model"] = fallback_model
        if attempt_id is not None:
            payload["attempt_id"] = attempt_id
        if output_format is not None:
            payload["output_format"] = output_format
        if contract_diagnostics is not None:
            payload["contract_diagnostics"] = [
                dict(item) for item in contract_diagnostics
            ]
        if original_input_sha256 is not None:
            payload["original_input_sha256"] = original_input_sha256
        if correction_attempt_id is not None:
            payload["correction_attempt_id"] = correction_attempt_id
        if resolved_model is not None:
            payload["resolved_model"] = resolved_model
        if model_proof is not None:
            payload["model_proof"] = sanitized_metadata(model_proof)
        if reported_model is not None:
            payload["reported_model"] = reported_model
        if runtime_version is not None:
            payload["runtime_version"] = runtime_version
        if invalid_output_sha256 is not None:
            payload["invalid_output_sha256"] = invalid_output_sha256
        if failure_kind is not None:
            payload["failure_kind"] = failure_kind
        if capture_evidence:
            payload.update(capture_evidence)
        if isolation_proof is not None:
            payload["isolation_proof"] = sanitized_metadata(isolation_proof)
        append_event(
            run_dir, "role_failed",
            payload,
            actor={"kind": "tool", "id": "agent-runtime", "model": resolved_model},
        )


def invoke_role(
    role, input_path, output_path, *, cli=None, policy_path=DEFAULT_POLICY,
    run_dir=None, timeout=600, cwd=None, dry_run=False,
    current_model=None, current_provider=None, auth_type=None,
    _attempted_model=None, _attempt_id=None, _allow_runtime_fallback=True,
    _fallback_failure_kind=None,
    _contract_correction=None, _allow_contract_correction=True,
):
    cli = cli or DEFAULT_CLI
    policy = load_json(policy_path)
    config = policy.get("roles", {}).get(role)
    if config is None:
        raise SupervisorError(f"unknown role {role!r}")
    input_path = Path(input_path)
    input_bytes = input_path.read_bytes()
    input_sha256 = hashlib.sha256(input_bytes).hexdigest()
    payload = load_json(input_path)
    try:
        validate_role_input(role, payload)
    except SupervisorError as exc:
        record_failure(
            run_dir, role, "input_validation", config["requested_model"],
            diagnostic=str(exc), failure_kind="input_schema", terminal=True,
            contract_diagnostics=getattr(exc, "contract_diagnostics", None),
            original_input_sha256=input_sha256,
        )
        raise
    correction_attempt = _contract_correction is not None
    if (
        correction_attempt
        and input_sha256 != _contract_correction["original_input_sha256"]
    ):
        record_failure(
            run_dir, role, "correction_input_verification",
            config["requested_model"], diagnostic="role input changed before correction",
            failure_kind="evidence_integrity", terminal=True,
            attempted_model=_contract_correction["expected_model"],
            attempt_id=_contract_correction["attempt_id"],
            original_input_sha256=_contract_correction["original_input_sha256"],
        )
        raise SupervisorError(
            "isolated role input changed before bounded contract correction"
        )
    primary_model = config["requested_model"]
    attempted_model = (
        _contract_correction["expected_model"]
        if correction_attempt else _attempted_model or primary_model
    )
    has_runtime_fallback = bool(config.get("runtime_fallbacks"))
    if has_runtime_fallback and role not in {"supervisor", "repair"}:
        raise SupervisorError(
            "runtime model fallback is permitted only for supervisor or repair"
        )
    attempt_id = (
        _contract_correction["attempt_id"]
        if correction_attempt else _attempt_id or (
            "primary" if has_runtime_fallback else
            "contract-attempt-1" if contract_version(payload) == 2 else None
        )
    )
    prompt_path = ROOT / config["prompt"]
    stdin_text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    correction_prompt = None
    if correction_attempt:
        correction_prompt = correction_contract(
            _contract_correction["original_input_sha256"],
            _contract_correction["diagnostics"],
        )
    system_prompt = isolated_role_system_prompt(
        role, prompt_path, payload, correction=correction_prompt
    )
    try:
        capabilities = detect_cli_capabilities(cli)
    except (OSError, subprocess.TimeoutExpired) as exc:
        record_failure(run_dir, role, "capability_detection", config["requested_model"], diagnostic=str(exc))
        raise SupervisorError(f"CLI {cli!r} capability detection failed: {exc}") from exc
    if correction_attempt:
        command_model = _contract_correction["model_argument"]
        inherited_without_override = command_model is None
        correction_capable = (
            capabilities["inherited_available"]
            if inherited_without_override else capabilities["available"]
        )
    else:
        inherited_without_override = (
            not capabilities["available"] and current_model
            and capabilities["inherited_available"]
        )
        correction_capable = capabilities["available"] or inherited_without_override
        command_model = None if inherited_without_override else attempted_model
    if not correction_capable:
        record_failure(run_dir, role, "capability_detection", config["requested_model"], diagnostic=str(capabilities))
        raise SupervisorError(f"CLI {cli!r} lacks isolated-role capabilities: {capabilities}")
    if auth_type and not capabilities["supports_auth_type"]:
        record_failure(
            run_dir, role, "capability_detection", config["requested_model"],
            diagnostic=f"CLI {cli!r} does not support explicit --auth-type",
        )
        raise SupervisorError(f"CLI {cli!r} does not support explicit --auth-type")
    auth_type = auth_type or capabilities["suggested_auth_type"]
    output_format = (
        _contract_correction["output_format"]
        if correction_attempt else
        "stream-json" if capabilities["supports_stream_json"] else "json"
    )
    command = build_gemini_command(
        cli,
        command_model,
        auth_type=auth_type,
        system_prompt=system_prompt,
        output_format=output_format,
    )
    if correction_attempt:
        resolution = json.loads(json.dumps(_contract_correction["resolution"]))
        resolution["resolved_model"] = _contract_correction["expected_model"]
    else:
        resolution = resolve_model(
            policy_path, role, isolated_available=not inherited_without_override,
            current_model=("unknown/default" if inherited_without_override else None),
            current_provider=("unknown" if inherited_without_override else current_provider), run_dir=None,
        )
    if attempted_model != primary_model and not correction_attempt:
        fallback_config = next(
            (
                item for item in config.get("runtime_fallbacks", [])
                if item.get("model") == attempted_model
            ),
            None,
        )
        if fallback_config is None:
            raise SupervisorError(
                f"isolated {role} attempted undeclared runtime fallback {attempted_model!r}"
            )
        resolution.update({
            "resolved_model": attempted_model,
            "provider": fallback_config["provider"],
            "fallback_used": True,
            "degradation_reason": (
                f"primary {primary_model} failed with "
                f"{_fallback_failure_kind or 'an eligible runtime failure'}; "
                f"policy fallback {attempted_model} was used"
            ),
        })
    result = {
        "role": role,
        "command": command,
        "resolution": resolution,
        "capabilities": capabilities,
        "isolation_controls": role_isolation_controls(),
        "output_format": output_format,
        "attempt_id": attempt_id,
        "started_at": utc_now(),
        "dry_run": dry_run,
    }

    def invoke_declared_fallback(
        failure_kind, *, phase, diagnostic, capture_evidence, isolation_proof,
        exit_code=None,
    ):
        """Run one policy fallback after proving the attempted primary identity."""
        primary_identity_verified = bool(
            isolation_proof.get("verified") is True
            and isolation_proof.get("system_models") == [attempted_model]
            and isolation_proof.get("assistant_models") in ([], [attempted_model])
        )
        fallback = (
            runtime_fallback_for(config, failure_kind)
            if (
                _allow_runtime_fallback
                and attempted_model == primary_model
                and primary_identity_verified
            )
            else None
        )
        record_failure(
            run_dir, role, phase, primary_model,
            exit_code=exit_code, diagnostic=diagnostic,
            failure_kind=failure_kind,
            capture_evidence=capture_evidence,
            isolation_proof=isolation_proof,
            terminal=fallback is None,
            attempted_model=attempted_model,
            fallback_model=fallback.get("model") if fallback else None,
            attempt_id=attempt_id,
            output_format=output_format,
            original_input_sha256=input_sha256,
        )
        if fallback is None:
            return None
        fallback_result = invoke_role(
            role, input_path, output_path, cli=cli, policy_path=policy_path,
            run_dir=run_dir, timeout=timeout, cwd=cwd, dry_run=False,
            current_model=None, current_provider=None, auth_type=auth_type,
            _attempted_model=fallback["model"], _attempt_id="fallback-1",
            _allow_runtime_fallback=False,
            _fallback_failure_kind=failure_kind,
        )
        fallback_result["recovered_from"] = {
            "failure_kind": failure_kind,
            "attempted_model": attempted_model,
            "input_sha256": input_sha256,
            "runtime_capture": capture_evidence.get("runtime_capture"),
            "runtime_capture_sha256": capture_evidence.get("runtime_capture_sha256"),
            "stderr_capture": capture_evidence.get("stderr_capture"),
            "stderr_capture_sha256": capture_evidence.get("stderr_capture_sha256"),
        }
        return fallback_result

    if dry_run:
        return result
    if run_dir:
        append_event(
            run_dir,
            "role_started",
            {
                "role": role,
                "input": str(Path(input_path).resolve()),
                "input_sha256": input_sha256,
                "requested_model": config["requested_model"],
                "attempted_model": attempted_model,
                "attempt_id": attempt_id,
                "output_format": output_format,
                "resolution_mode": resolution["resolution_mode"],
                "fallback_used": resolution["fallback_used"],
                "contract_correction": correction_attempt,
                "isolation_controls": role_isolation_controls(),
            },
            actor={"kind": "system", "id": "diagram-orchestrator", "model": None},
        )
    output_path = Path(output_path)
    output_path.unlink(missing_ok=True)
    try:
        completed = run_captured_process(
            command, stdin_text, timeout=timeout, cwd=cwd,
        )
    except subprocess.TimeoutExpired as exc:
        capture_evidence = persist_runtime_captures(
            output_path, exc.stdout or "", exc.stderr or "",
            output_format=output_format, attempt_id=attempt_id,
        )
        isolation_proof = inspect_runtime_isolation(role, exc.stdout or "")
        fallback_result = invoke_declared_fallback(
            "timeout", phase="execution_timeout", diagnostic=str(exc),
            capture_evidence=capture_evidence,
            isolation_proof=isolation_proof,
        )
        if fallback_result is not None:
            return fallback_result
        raise SupervisorError(f"isolated {role} process timed out after {timeout}s") from exc
    failure_diagnostic = (completed.stderr or "") + "\n" + (completed.stdout or "")
    if (
        completed.returncode != 0
        and not correction_attempt
        and not inherited_without_override
        and model_unavailable(failure_diagnostic)
    ):
        capture_evidence = persist_runtime_captures(
            output_path, completed.stdout, completed.stderr,
            output_format=output_format, attempt_id=attempt_id,
        )
        isolation_proof = inspect_runtime_isolation(role, completed.stdout or "")
        fallback_result = invoke_declared_fallback(
            "model_unavailable", phase="requested_model_unavailable",
            diagnostic=failure_diagnostic,
            capture_evidence=capture_evidence,
            isolation_proof=isolation_proof,
            exit_code=completed.returncode,
        )
        if fallback_result is not None:
            return fallback_result
        if (
            _allow_runtime_fallback
            and attempted_model == primary_model
            and runtime_fallback_for(config, "model_unavailable") is not None
        ):
            raise SupervisorError(
                f"isolated {role} could not prove an eligible model fallback"
            )
    if (
        completed.returncode != 0
        and not correction_attempt
        and not inherited_without_override
        and current_model
        and model_unavailable(failure_diagnostic)
    ):
        inherited_without_override = False
        command = build_gemini_command(
            cli, current_model, auth_type=auth_type, system_prompt=system_prompt,
            output_format=output_format,
        )
        try:
            completed = run_captured_process(
                command, stdin_text, timeout=timeout, cwd=cwd,
            )
        except subprocess.TimeoutExpired as exc:
            record_failure(run_dir, role, "fallback_timeout", config["requested_model"], diagnostic=str(exc))
            raise SupervisorError(f"inherited fallback for {role} timed out after {timeout}s") from exc
        resolution = resolve_model(
            policy_path, role, current_model=current_model,
            current_provider=current_provider, run_dir=None,
        )
        result["command"] = command
        result["resolution"] = resolution
        result["fallback_from"] = config["requested_model"]
    capture_evidence = persist_runtime_captures(
        output_path, completed.stdout, completed.stderr,
        output_format=output_format, attempt_id=attempt_id,
    )
    runtime_capture_path = Path(capture_evidence["runtime_capture"])
    stderr_capture_path = Path(capture_evidence["stderr_capture"])
    isolation_proof = inspect_runtime_isolation(role, completed.stdout or "")
    if completed.returncode != 0:
        failure_diagnostic = (completed.stderr or "") + "\n" + (completed.stdout or "")
        turn_limited = "FatalTurnLimitedError" in failure_diagnostic
        fallback_result = (
            invoke_declared_fallback(
                "turn_limit", phase="execution", diagnostic=failure_diagnostic,
                capture_evidence=capture_evidence,
                isolation_proof=isolation_proof,
                exit_code=completed.returncode,
            )
            if turn_limited else None
        )
        if fallback_result is not None:
            return fallback_result
        if not turn_limited:
            record_failure(
                run_dir, role, "execution", primary_model,
                exit_code=completed.returncode, diagnostic=failure_diagnostic,
                failure_kind="process_exit",
                capture_evidence=capture_evidence,
                isolation_proof=isolation_proof,
                terminal=True,
                attempted_model=attempted_model,
                attempt_id=attempt_id,
                output_format=output_format,
                original_input_sha256=input_sha256,
            )
        if turn_limited:
            raise SupervisorError(
                f"isolated {role} exhausted its command-line turn budget; "
                f"do not change global maxSessionTurns. Runtime evidence: "
                f"{runtime_capture_path}; stderr: {stderr_capture_path}"
            )
        raise SupervisorError(
            f"isolated {role} process failed with exit code {completed.returncode}: "
            f"{redact(failure_diagnostic[-1000:])}. Runtime evidence: "
            f"{runtime_capture_path}; stderr: {stderr_capture_path}"
        )
    parsed_output = None
    runtime_metadata = None
    try:
        parsed_output, runtime_metadata = parse_runtime_output(role, completed.stdout)
        reported_model = runtime_metadata.get("reported_model")
        expected_model = (
            _contract_correction["expected_model"]
            if correction_attempt else None
            if inherited_without_override and not result.get("fallback_from")
            else current_model if result.get("fallback_from")
            else attempted_model
        )
        if not runtime_metadata.get("model_proof", {}).get("verified") or not reported_model:
            raise SupervisorError(f"isolated {role} runtime did not provide verifiable model evidence")
        if expected_model is not None and reported_model != expected_model:
            raise SupervisorError(
                f"isolated {role} reported model {reported_model!r}, expected {expected_model!r}"
            )
        resolution["resolved_model"] = reported_model
        resolution["fallback_used"] = (
            resolution["fallback_used"] or reported_model != resolution["requested_model"]
        )
        try:
            parsed_output = validate_role_output(role, parsed_output, payload)
            parsed_output, binding_proof = finalize_role_output(
                role, payload, parsed_output
            )
            if binding_proof is not None:
                runtime_metadata["binding_proof"] = binding_proof
        except SupervisorError as exc:
            invalid_output_sha256 = hashlib.sha256(
                json.dumps(
                    parsed_output, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest()
            raise RoleOutputContractError(
                str(exc),
                resolution=dict(resolution),
                runtime_metadata=runtime_metadata,
                invalid_output_sha256=invalid_output_sha256,
                diagnostics=getattr(exc, "contract_diagnostics", ()),
                failure_kind=getattr(
                    exc, "contract_failure_kind", "cross_field"
                ),
                original_input_sha256=input_sha256,
            ) from exc
    except SupervisorError as exc:
        empty_response = bool(
            not isinstance(exc, RoleOutputContractError)
            and (
                not (completed.stdout or "").strip()
                or "has no result event" in str(exc)
                or "has no assistant role payload" in str(exc)
            )
        )
        if (
            empty_response
            and not correction_attempt
            and _allow_runtime_fallback
            and attempted_model == primary_model
            and runtime_fallback_for(config, "empty_response") is not None
        ):
            fallback_result = invoke_declared_fallback(
                "empty_response", phase="output_validation",
                diagnostic=str(exc), capture_evidence=capture_evidence,
                isolation_proof=isolation_proof,
            )
            if fallback_result is not None:
                return fallback_result
            raise exc
        correction_eligible = bool(
            isinstance(exc, RoleOutputContractError)
            and contract_version(payload) == 2
            and _allow_contract_correction
            and not correction_attempt
            and exc.failure_kind in {"output_schema", "cross_field"}
            and runtime_metadata
            and runtime_metadata.get("model_proof", {}).get("verified") is True
            and runtime_metadata.get("isolation_proof", {}).get("verified") is True
            and runtime_metadata.get("isolation_proof", {}).get("tool_calls") == 0
        )
        correction_attempt_id = (
            "contract-correction-1" if correction_eligible else None
        )
        record_failure(
            run_dir, role, "output_validation", config["requested_model"],
            diagnostic=str(exc),
            resolved_model=(resolution.get("resolved_model") if runtime_metadata else None),
            model_proof=(runtime_metadata.get("model_proof") if runtime_metadata else None),
            reported_model=(runtime_metadata.get("reported_model") if runtime_metadata else None),
            runtime_version=(runtime_metadata.get("runtime_version") if runtime_metadata else None),
            invalid_output_sha256=getattr(exc, "invalid_output_sha256", None),
            capture_evidence=capture_evidence,
            isolation_proof=(
                runtime_metadata.get("isolation_proof")
                if runtime_metadata else isolation_proof
            ),
            attempted_model=attempted_model,
            attempt_id=attempt_id,
            output_format=output_format,
            terminal=not correction_eligible,
            failure_kind=(
                "empty_response" if empty_response
                else getattr(exc, "failure_kind", "output_validation")
            ),
            contract_diagnostics=getattr(exc, "diagnostics", None),
            original_input_sha256=(
                input_sha256 if isinstance(exc, RoleOutputContractError) else None
            ),
            correction_attempt_id=correction_attempt_id,
        )
        if correction_eligible:
            correction_context = {
                "attempt_id": correction_attempt_id,
                "diagnostics": exc.diagnostics,
                "expected_model": runtime_metadata["reported_model"],
                "model_argument": command_model_argument(command),
                "original_input_sha256": input_sha256,
                "output_format": output_format,
                "resolution": dict(resolution),
            }
            corrected = invoke_role(
                role, input_path, output_path, cli=cli, policy_path=policy_path,
                run_dir=run_dir, timeout=timeout, cwd=cwd, dry_run=False,
                current_model=current_model, current_provider=current_provider,
                auth_type=auth_type, _allow_runtime_fallback=False,
                _contract_correction=correction_context,
                _allow_contract_correction=False,
            )
            corrected["contract_correction"] = {
                "attempted": True,
                "original_input_sha256": input_sha256,
                "first_attempt_id": attempt_id,
                "correction_attempt_id": correction_attempt_id,
                "role": role,
                "model": runtime_metadata["reported_model"],
                "diagnostics": exc.diagnostics,
                "invalid_output_sha256": exc.invalid_output_sha256,
                "runtime_capture": capture_evidence["runtime_capture"],
                "runtime_capture_sha256": capture_evidence["runtime_capture_sha256"],
                "stderr_capture": capture_evidence["stderr_capture"],
                "stderr_capture_sha256": capture_evidence["stderr_capture_sha256"],
            }
            return corrected
        raise
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=output_path.parent, delete=False) as tmp:
        json.dump(parsed_output, tmp, ensure_ascii=False, indent=2, sort_keys=True)
        tmp.write("\n")
        tmp.flush()
        os.fsync(tmp.fileno())
        temp_name = tmp.name
    os.replace(temp_name, output_path)
    result.update({
        "finished_at": utc_now(),
        "exit_code": completed.returncode,
        "output": str(output_path.resolve()),
        "runtime_capture": str(runtime_capture_path.resolve()),
        "stderr_capture": str(stderr_capture_path.resolve()),
        "stderr": redact(completed.stderr[-4000:]),
        "runtime_metadata": runtime_metadata,
    })
    if run_dir:
        append_event(
            run_dir, "model_resolved", resolution,
            actor={"kind": "tool", "id": "agent-runtime", "model": resolution["resolved_model"]},
        )
        append_event(
            run_dir,
            "role_finished",
            {
                "role": role,
                "requested_model": resolution["requested_model"],
                "resolved_model": resolution["resolved_model"],
                "resolution_mode": resolution["resolution_mode"],
                "fallback_used": resolution["fallback_used"],
                "degradation_reason": resolution.get("degradation_reason"),
                "attempted_model": attempted_model,
                "attempt_id": attempt_id,
                "output_format": output_format,
                "output": result["output"],
                "output_sha256": hashlib.sha256(output_path.read_bytes()).hexdigest(),
                "runtime_capture": result["runtime_capture"],
                "runtime_capture_sha256": capture_evidence["runtime_capture_sha256"],
                "stderr_capture": result["stderr_capture"],
                "stderr_capture_sha256": capture_evidence["stderr_capture_sha256"],
                "model_proof": runtime_metadata["model_proof"],
                "binding_proof": runtime_metadata.get("binding_proof"),
                "isolation_proof": runtime_metadata.get("isolation_proof"),
                "isolation_controls": role_isolation_controls(),
                "runtime_version": runtime_metadata.get("runtime_version"),
                "exit_code": completed.returncode,
            },
            actor={"kind": "agent", "id": role, "model": resolution["resolved_model"]},
        )
        append_event(
            run_dir,
            "review_verdict" if role == "reviewer" else "patch_proposed" if role == "repair" else "source_selected" if role == "semantic_analyst" else "state_transition",
            {
                "role": role, "exit_code": completed.returncode,
                "output": result["output"], "runtime_metadata": runtime_metadata,
            },
            actor={"kind": "agent", "id": role, "model": resolution["resolved_model"]},
        )
    return result


def main():
    parser = argparse.ArgumentParser(description="Invoke an isolated diagram role without changing the global model")
    parser.add_argument("role", choices=("supervisor", "reviewer", "repair", "semantic_analyst"))
    parser.add_argument("input", help="JSON role input")
    parser.add_argument("--output", required=True)
    parser.add_argument("--cli", default=DEFAULT_CLI)
    parser.add_argument("--policy", default=str(DEFAULT_POLICY))
    parser.add_argument("--run-dir")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--cwd")
    parser.add_argument("--current-model")
    parser.add_argument("--current-provider")
    parser.add_argument("--auth-type")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        result = invoke_role(
            args.role, args.input, args.output, cli=args.cli, policy_path=args.policy,
            run_dir=args.run_dir, timeout=args.timeout, cwd=args.cwd, dry_run=args.dry_run,
            current_model=args.current_model, current_provider=args.current_provider,
            auth_type=args.auth_type,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, SupervisorError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
