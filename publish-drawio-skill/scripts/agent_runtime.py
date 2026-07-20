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


class RoleOutputContractError(SupervisorError):
    """Role output was model-proven JSON but failed its output contract."""

    def __init__(self, message, *, resolution, runtime_metadata, invalid_output_sha256=None):
        super().__init__(message)
        self.resolution = resolution
        self.runtime_metadata = runtime_metadata
        self.invalid_output_sha256 = invalid_output_sha256


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


def role_schema_name(role):
    if role == "reviewer":
        return "reviewer-verdict.v1.schema.json"
    if role == "repair":
        return "diagram-patch.v1.schema.json"
    return "agent-role-output.v1.schema.json"


def reviewer_evidence_bindings(payload):
    if payload.get("review_kind") == "baseline_audit":
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
    schema = load_json(ROOT / "data" / role_schema_name(role))
    sections = [
        "## Required output JSON Schema",
        "Return exactly one JSON object that validates against this schema. Do not omit required properties.",
        json.dumps(schema, ensure_ascii=False, sort_keys=True),
    ]
    bindings = reviewer_evidence_bindings(payload) if role == "reviewer" else None
    if bindings:
        sections.extend(
            [
                "## Mandatory reviewer evidence bindings",
                "Copy these four values exactly into the output object; they are not optional and must not be renamed:",
                json.dumps(bindings, ensure_ascii=False, sort_keys=True),
            ]
        )
    return "\n\n".join(sections)


def build_gemini_command(cli, model=None, auth_type=None):
    command = [cli]
    if auth_type:
        command.extend(["--auth-type", auth_type])
    if model:
        command.extend(["--model", model])
    command.extend([
        "--prompt", "Execute the supplied diagram role contract against the JSON input. Return role output only.",
        "--output-format", "json",
        "--approval-mode", "plan",
    ])
    return command


def minimal_environment():
    return {key: value for key, value in os.environ.items() if key in SAFE_ENV_KEYS}


def detect_cli_capabilities(cli):
    completed = subprocess.run(
        [cli, "--help"], text=True, capture_output=True, check=False, timeout=30,
        env=minimal_environment(),
    )
    help_text = completed.stdout + completed.stderr
    base_required = ("--prompt", "--output-format", "--approval-mode")
    required = ("--model", *base_required)
    missing = [flag for flag in required if flag not in help_text]
    inherited_missing = [flag for flag in base_required if flag not in help_text]
    supports_auth_type = "--auth-type" in help_text
    looks_like_gigacode = (
        "gigacode" in Path(cli).name.lower()
        or bool(re.search(r"(?i)\bGigaCode\b|--auth-type.{0,240}\bgigacode\b", help_text, re.DOTALL))
    )
    return {
        "available": completed.returncode == 0 and not missing,
        "inherited_available": completed.returncode == 0 and not inherited_missing,
        "supports_auth_type": supports_auth_type,
        "suggested_auth_type": "gigacode" if supports_auth_type and looks_like_gigacode else None,
        "exit_code": completed.returncode,
        "required_flags": list(required),
        "missing_flags": missing,
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


def _parse_gigacode_events(role, events):
    if not events or not all(isinstance(event, dict) for event in events):
        raise SupervisorError(f"isolated {role} GigaCode event output must be a non-empty object array")

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
    if system_model != assistant_model or system_model not in stats_models:
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
        "format": "gigacode_json_events",
        "stats": sanitized_metadata(stats),
        "errors": sanitized_metadata(result.get("error")),
        "reported_model": system_model,
        "model_proof": {
            "verified": True,
            "system_model": system_model,
            "assistant_model": assistant_model,
            "stats_models": sorted(stats_models),
        },
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
        outer = json.loads(output)
    except json.JSONDecodeError as exc:
        raise SupervisorError(f"isolated {role} output is not valid JSON: {exc}") from exc
    if isinstance(outer, list):
        return _parse_gigacode_events(role, outer)
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


def validate_role_output(role, parsed):
    schema = load_json(ROOT / "data" / role_schema_name(role))
    errors = sorted(
        jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).iter_errors(parsed),
        key=lambda error: (list(error.path), error.message),
    )
    if errors:
        raise SupervisorError(f"isolated {role} output schema failed: {errors[0].message}")
    if role in {"supervisor", "semantic_analyst"} and parsed.get("role") != role:
        raise SupervisorError(f"isolated {role} output role mismatch")
    return parsed


def validate_role_bindings(role, payload, parsed):
    expected = reviewer_evidence_bindings(payload) if role == "reviewer" else None
    if not expected:
        return parsed
    mismatches = [
        key for key, value in expected.items() if parsed.get(key) != value
    ]
    if mismatches:
        raise SupervisorError(
            "isolated reviewer output evidence binding mismatch: " + ", ".join(mismatches)
        )
    return parsed


def model_unavailable(stderr):
    return bool(re.search(r"(?i)(unknown|unsupported|unavailable|not[ -]found|no access).{0,80}model|model.{0,80}(unknown|unsupported|unavailable|not[ -]found|no access)", stderr or ""))


def record_failure(
    run_dir, role, phase, requested_model, *, exit_code=None, diagnostic=None,
    resolved_model=None, model_proof=None, reported_model=None, runtime_version=None,
    invalid_output_sha256=None,
):
    if run_dir:
        payload = {
            "role": role, "phase": phase, "requested_model": requested_model,
            "exit_code": exit_code, "diagnostic": redact(diagnostic or "")[-1000:],
        }
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
        append_event(
            run_dir, "role_failed",
            payload,
            actor={"kind": "tool", "id": "agent-runtime", "model": resolved_model},
        )


def invoke_role(
    role, input_path, output_path, *, cli=None, policy_path=DEFAULT_POLICY,
    run_dir=None, timeout=600, cwd=None, dry_run=False,
    current_model=None, current_provider=None, auth_type=None,
):
    cli = cli or DEFAULT_CLI
    policy = load_json(policy_path)
    config = policy.get("roles", {}).get(role)
    if config is None:
        raise SupervisorError(f"unknown role {role!r}")
    prompt_path = ROOT / config["prompt"]
    payload = load_json(input_path)
    stdin_text = (
        role_body(prompt_path)
        + "\n\n"
        + role_output_contract(role, payload)
        + "\n\n## Runtime input\n\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True)
    )
    try:
        capabilities = detect_cli_capabilities(cli)
    except (OSError, subprocess.TimeoutExpired) as exc:
        record_failure(run_dir, role, "capability_detection", config["requested_model"], diagnostic=str(exc))
        raise SupervisorError(f"CLI {cli!r} capability detection failed: {exc}") from exc
    inherited_without_override = not capabilities["available"] and current_model and capabilities["inherited_available"]
    if not capabilities["available"] and not inherited_without_override:
        record_failure(run_dir, role, "capability_detection", config["requested_model"], diagnostic=str(capabilities))
        raise SupervisorError(f"CLI {cli!r} lacks isolated-role capabilities: {capabilities}")
    if auth_type and not capabilities["supports_auth_type"]:
        record_failure(
            run_dir, role, "capability_detection", config["requested_model"],
            diagnostic=f"CLI {cli!r} does not support explicit --auth-type",
        )
        raise SupervisorError(f"CLI {cli!r} does not support explicit --auth-type")
    auth_type = auth_type or capabilities["suggested_auth_type"]
    command = build_gemini_command(
        cli, None if inherited_without_override else config["requested_model"], auth_type=auth_type,
    )
    resolution = resolve_model(
        policy_path, role, isolated_available=not inherited_without_override,
        current_model=("unknown/default" if inherited_without_override else None),
        current_provider=("unknown" if inherited_without_override else current_provider), run_dir=None,
    )
    result = {
        "role": role,
        "command": command,
        "resolution": resolution,
        "capabilities": capabilities,
        "started_at": utc_now(),
        "dry_run": dry_run,
    }
    if dry_run:
        return result
    output_path = Path(output_path)
    output_path.unlink(missing_ok=True)
    try:
        completed = subprocess.run(
            command, input=stdin_text, text=True, capture_output=True, check=False,
            timeout=timeout, cwd=cwd, env=minimal_environment(),
        )
    except subprocess.TimeoutExpired as exc:
        record_failure(run_dir, role, "execution_timeout", config["requested_model"], diagnostic=str(exc))
        raise SupervisorError(f"isolated {role} process timed out after {timeout}s") from exc
    failure_diagnostic = (completed.stderr or "") + "\n" + (completed.stdout or "")
    if completed.returncode != 0 and not inherited_without_override and current_model and model_unavailable(failure_diagnostic):
        record_failure(
            run_dir, role, "requested_model_unavailable", config["requested_model"],
            exit_code=completed.returncode, diagnostic=failure_diagnostic,
        )
        inherited_without_override = False
        command = build_gemini_command(cli, current_model, auth_type=auth_type)
        try:
            completed = subprocess.run(
                command, input=stdin_text, text=True, capture_output=True, check=False,
                timeout=timeout, cwd=cwd, env=minimal_environment(),
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
    if completed.returncode != 0:
        failure_diagnostic = (completed.stderr or "") + "\n" + (completed.stdout or "")
        record_failure(
            run_dir, role, "execution", config["requested_model"],
            exit_code=completed.returncode, diagnostic=failure_diagnostic,
        )
        raise SupervisorError(
            f"isolated {role} process failed with exit code {completed.returncode}: "
            f"{redact(failure_diagnostic[-1000:])}"
        )
    parsed_output = None
    runtime_metadata = None
    try:
        parsed_output, runtime_metadata = parse_runtime_output(role, completed.stdout)
        reported_model = runtime_metadata.get("reported_model")
        expected_model = (
            None
            if inherited_without_override and not result.get("fallback_from")
            else current_model if result.get("fallback_from")
            else config["requested_model"]
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
            parsed_output = validate_role_output(role, parsed_output)
            parsed_output = validate_role_bindings(role, payload, parsed_output)
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
            ) from exc
    except SupervisorError as exc:
        record_failure(
            run_dir, role, "output_validation", config["requested_model"],
            diagnostic=str(exc),
            resolved_model=(resolution.get("resolved_model") if runtime_metadata else None),
            model_proof=(runtime_metadata.get("model_proof") if runtime_metadata else None),
            reported_model=(runtime_metadata.get("reported_model") if runtime_metadata else None),
            runtime_version=(runtime_metadata.get("runtime_version") if runtime_metadata else None),
            invalid_output_sha256=getattr(exc, "invalid_output_sha256", None),
        )
        raise
    output_path.parent.mkdir(parents=True, exist_ok=True)
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
            "review_verdict" if role == "reviewer" else "patch_proposed" if role in {"repair", "semantic_analyst"} else "state_transition",
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
