#!/usr/bin/env python3
"""Portable isolated-role adapter for Gemini/GigaCode-compatible CLIs.

Native extension subagents use agents/*.md. This adapter is the executable
fallback when the host cannot assign a model per nested role. It never changes
the interactive session's global model and never executes model output.
"""
from __future__ import annotations

import argparse
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
SAFE_ENV_KEYS = {
    "HOME", "LANG", "LC_ALL", "LC_CTYPE", "NO_COLOR", "PATH",
    "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "WINDIR",
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


def build_gemini_command(cli, model=None):
    command = [cli]
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
    return {
        "available": completed.returncode == 0 and not missing,
        "inherited_available": completed.returncode == 0 and not inherited_missing,
        "exit_code": completed.returncode,
        "required_flags": list(required),
        "missing_flags": missing,
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
    if isinstance(outer, dict) and "response" in outer:
        errors = outer.get("errors")
        error = outer.get("error")
        if errors not in (None, [], {}, "") or error not in (None, [], {}, "", False):
            raise SupervisorError(f"isolated {role} Gemini envelope reports errors")
        response = outer["response"]
        if isinstance(response, str):
            try:
                parsed = json.loads(response)
            except json.JSONDecodeError as exc:
                raise SupervisorError(f"isolated {role} Gemini response is not role JSON: {exc}") from exc
        elif isinstance(response, dict):
            parsed = response
        else:
            raise SupervisorError(f"isolated {role} Gemini response must be a JSON object or encoded JSON string")
        metadata = {
            "format": "gemini_json_envelope",
            "stats": sanitized_metadata(outer.get("stats")),
            "errors": sanitized_metadata(errors if errors is not None else error),
            "reported_model": outer.get("model") or (outer.get("stats") or {}).get("model"),
        }
        return parsed, metadata
    return outer, {"format": "direct_role_json", "stats": None, "errors": None, "reported_model": None}


def validate_role_output(role, parsed):
    if role == "reviewer":
        schema_name = "reviewer-verdict.v1.schema.json"
    elif role == "repair":
        schema_name = "diagram-patch.v1.schema.json"
    else:
        schema_name = "agent-role-output.v1.schema.json"
    schema = load_json(ROOT / "data" / schema_name)
    errors = sorted(
        jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).iter_errors(parsed),
        key=lambda error: (list(error.path), error.message),
    )
    if errors:
        raise SupervisorError(f"isolated {role} output schema failed: {errors[0].message}")
    if role in {"supervisor", "semantic_analyst"} and parsed.get("role") != role:
        raise SupervisorError(f"isolated {role} output role mismatch")
    return parsed


def model_unavailable(stderr):
    return bool(re.search(r"(?i)(unknown|unsupported|unavailable|not[ -]found|no access).{0,80}model|model.{0,80}(unknown|unsupported|unavailable|not[ -]found|no access)", stderr or ""))


def record_failure(run_dir, role, phase, requested_model, *, exit_code=None, diagnostic=None):
    if run_dir:
        append_event(
            run_dir, "role_failed",
            {
                "role": role, "phase": phase, "requested_model": requested_model,
                "exit_code": exit_code, "diagnostic": redact(diagnostic or "")[-1000:],
            },
            actor={"kind": "tool", "id": "agent-runtime", "model": None},
        )


def invoke_role(
    role, input_path, output_path, *, cli="gemini", policy_path=DEFAULT_POLICY,
    run_dir=None, timeout=600, cwd=None, dry_run=False,
    current_model=None, current_provider=None,
):
    policy = load_json(policy_path)
    config = policy.get("roles", {}).get(role)
    if config is None:
        raise SupervisorError(f"unknown role {role!r}")
    prompt_path = ROOT / config["prompt"]
    payload = load_json(input_path)
    stdin_text = role_body(prompt_path) + "\n\n## Runtime input\n\n" + json.dumps(payload, ensure_ascii=False, sort_keys=True)
    try:
        capabilities = detect_cli_capabilities(cli)
    except (OSError, subprocess.TimeoutExpired) as exc:
        record_failure(run_dir, role, "capability_detection", config["requested_model"], diagnostic=str(exc))
        raise SupervisorError(f"CLI {cli!r} capability detection failed: {exc}") from exc
    inherited_without_override = not capabilities["available"] and current_model and capabilities["inherited_available"]
    if not capabilities["available"] and not inherited_without_override:
        record_failure(run_dir, role, "capability_detection", config["requested_model"], diagnostic=str(capabilities))
        raise SupervisorError(f"CLI {cli!r} lacks isolated-role capabilities: {capabilities}")
    command = build_gemini_command(cli, None if inherited_without_override else config["requested_model"])
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
    if completed.returncode != 0 and not inherited_without_override and current_model and model_unavailable(completed.stderr):
        record_failure(
            run_dir, role, "requested_model_unavailable", config["requested_model"],
            exit_code=completed.returncode, diagnostic=completed.stderr,
        )
        inherited_without_override = False
        command = build_gemini_command(cli, current_model)
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
        record_failure(
            run_dir, role, "execution", config["requested_model"],
            exit_code=completed.returncode, diagnostic=completed.stderr,
        )
        raise SupervisorError(f"isolated {role} process failed with exit code {completed.returncode}: {redact(completed.stderr[-1000:])}")
    try:
        parsed_output, runtime_metadata = parse_runtime_output(role, completed.stdout)
        parsed_output = validate_role_output(role, parsed_output)
        reported_model = runtime_metadata.get("reported_model")
        if reported_model:
            if current_model and result.get("fallback_from") and reported_model != current_model:
                raise SupervisorError(
                    f"inherited fallback reported model {reported_model!r}, expected {current_model!r}"
                )
            resolution["resolved_model"] = reported_model
            resolution["fallback_used"] = resolution["resolution_mode"] != "native_per_agent" or reported_model != resolution["requested_model"]
    except SupervisorError as exc:
        record_failure(run_dir, role, "output_validation", config["requested_model"], diagnostic=str(exc))
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
    parser.add_argument("--cli", default="gemini")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY))
    parser.add_argument("--run-dir")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--cwd")
    parser.add_argument("--current-model")
    parser.add_argument("--current-provider")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        result = invoke_role(
            args.role, args.input, args.output, cli=args.cli, policy_path=args.policy,
            run_dir=args.run_dir, timeout=args.timeout, cwd=args.cwd, dry_run=args.dry_run,
            current_model=args.current_model, current_provider=args.current_provider,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, SupervisorError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
