#!/usr/bin/env python3
"""Natural-language-first argument normalization for Draw.io commands."""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
import unicodedata
from pathlib import Path


DECISIONS = (
    "continue", "approve", "approve_with_findings", "pause", "stop", "manual_handoff",
)
QWEN_COMMAND_ARGS_ENV = "DRAWIO_COMMAND_ARGS"
HOST_OWNED_OPTIONS = ("--workspace", "--cli")


class CommandUXError(ValueError):
    def __init__(self, code, message, *, candidates=None, hint=None):
        super().__init__(message)
        self.code = code
        self.candidates = list(candidates or [])
        self.hint = hint

    def payload(self):
        result = {"code": self.code, "message": str(self)}
        if self.candidates:
            result["candidates"] = self.candidates
        if self.hint:
            result["hint"] = self.hint
        return result


def _normalize_qwen_token(token):
    """Accept Qwen's file-reference marker only for Draw.io path tokens."""
    if token.startswith("@") and token[1:].lower().endswith(".drawio"):
        return token[1:]
    return token


def qwen_command_tokens(raw):
    """Parse one shell-escaped custom-command transport value without executing it."""
    try:
        tokens = shlex.split(raw or "", posix=True)
    except ValueError as exc:
        raise CommandUXError(
            "command_arguments_invalid",
            f"could not parse command arguments: {exc}",
            hint="check that every quoted value has a closing quote",
        ) from exc
    for token in tokens:
        option = token.split("=", 1)[0]
        if token == "--":
            raise CommandUXError(
                "command_arguments_invalid",
                "the argument separator -- is not supported by Draw.io commands",
            )
        if option in HOST_OWNED_OPTIONS:
            raise CommandUXError(
                "host_option_forbidden",
                f"{option} is owned by the extension host and cannot be overridden",
            )
    return [_normalize_qwen_token(token) for token in tokens]


def argv_with_qwen_command_args(argv=None, environ=None):
    """Insert parsed Qwen arguments before fixed host-owned command arguments."""
    argv = list(sys.argv[1:] if argv is None else argv)
    environ = os.environ if environ is None else environ
    if QWEN_COMMAND_ARGS_ENV not in environ:
        return argv
    if not argv:
        raise CommandUXError(
            "command_arguments_invalid", "Draw.io command name is missing"
        )
    tokens = qwen_command_tokens(environ.get(QWEN_COMMAND_ARGS_ENV, ""))
    return [argv[0], *tokens, *argv[1:]]


def quote_command_value(value):
    """Quote one value for the raw custom-command grammar parsed by shlex."""
    return shlex.quote(str(value))


def workspace_path(workspace):
    path = Path(workspace).expanduser().resolve()
    if not path.is_dir():
        raise CommandUXError("workspace_not_found", f"workspace is not a directory: {path}")
    return path


def positional_request(parts, explicit=None):
    if explicit is not None:
        if parts:
            raise CommandUXError(
                "duplicate_request",
                "supply the request either as conversational text or with --request, not both",
            )
        value = explicit
    else:
        value = " ".join(parts)
    value = (value or "").strip()
    if not value:
        raise CommandUXError("request_required", "diagram request must not be empty")
    return value


def split_diagram_request(parts, *, diagram=None, request=None):
    parts = list(parts or [])
    short_diagram = None
    if diagram is None and parts and parts[0].lower().endswith(".drawio"):
        short_diagram = parts.pop(0)
    return diagram or short_diagram, positional_request(parts, request)


def _slug_words(request):
    value = unicodedata.normalize("NFKC", request).strip()
    value = re.sub(
        r"(?iu)^\s*(?:создай|построй|сформируй|нарисуй|create|build|generate|draw)\s+"
        r"(?:диаграмм\w*|схем\w*|diagram|chart|flowchart)\s*[:—–-]?\s*",
        "",
        value,
    )
    return re.findall(r"[^\W_]+", value, flags=re.UNICODE)


def generated_target(workspace, request):
    workspace = workspace_path(workspace)
    words = _slug_words(request)
    slug = "-".join(words[:6]).lower()[:72].strip("-.") or "diagram"
    candidate = workspace / f"{slug}.drawio"
    number = 2
    while candidate.exists():
        candidate = workspace / f"{slug}-{number}.drawio"
        number += 1
        if number > 10000:
            raise CommandUXError("target_exhausted", "could not allocate a collision-safe diagram filename")
    return candidate.resolve(), "generated_from_request"


def workspace_diagrams(workspace):
    workspace = workspace_path(workspace)
    return sorted(
        path.resolve()
        for path in workspace.glob("*.drawio")
        if path.is_file() and not path.name.startswith(".")
    )


def select_diagram(workspace, explicit=None):
    workspace = workspace_path(workspace)
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            path = workspace / path
        return path.resolve(), "explicit"
    candidates = workspace_diagrams(workspace)
    if len(candidates) == 1:
        return candidates[0], "only_drawio_in_workspace"
    names = [str(path) for path in candidates]
    if not candidates:
        raise CommandUXError(
            "diagram_not_found",
            "no .drawio file exists at the workspace root",
            hint='specify one with --diagram "file.drawio"',
        )
    raise CommandUXError(
        "diagram_selection_ambiguous",
        "more than one .drawio file exists; explicit selection is required",
        candidates=names,
        hint='rerun with --diagram "one-of-the-listed-files.drawio"',
    )


def _workflow_dirs(workspace):
    root = workspace_path(workspace) / ".diagram-runs"
    if not root.is_dir():
        return []
    return sorted(path.parent for path in root.glob("*/workflow.json") if path.is_file())


def select_pending_run(workspace, explicit=None):
    if explicit:
        return explicit, "explicit"
    pending = []
    for run_dir in _workflow_dirs(workspace):
        try:
            workflow = json.loads((run_dir / "workflow.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if workflow.get("checkpoint"):
            pending.append((run_dir, workflow.get("run_id", run_dir.name)))
    if len(pending) == 1:
        return str(pending[0][0].resolve()), "only_pending_run"
    if not pending:
        raise CommandUXError(
            "pending_run_not_found",
            "no run with a pending human checkpoint was found",
            hint='supply --run "run-id" if you want to address a specific run',
        )
    raise CommandUXError(
        "pending_run_selection_ambiguous",
        "more than one run is waiting for a human decision",
        candidates=[run_id for _, run_id in pending],
        hint='rerun with --run "one-of-the-listed-run-ids"',
    )


def select_latest_run(workspace, explicit=None):
    if explicit:
        return explicit, "explicit"
    runs = _workflow_dirs(workspace)
    if not runs:
        raise CommandUXError("run_not_found", "no diagram workflow run exists in this workspace")
    latest = max(runs, key=lambda path: (path / "workflow.json").stat().st_mtime_ns)
    return str(latest.resolve()), "latest_updated_run"


def parse_resume(parts, *, run=None, decision=None, feedback=None):
    parts = list(parts or [])
    positional_run = None
    positional_decision = None
    if decision is None:
        if parts and parts[0] in DECISIONS:
            positional_decision = parts.pop(0)
        elif len(parts) >= 2 and parts[1] in DECISIONS:
            positional_run = parts.pop(0)
            positional_decision = parts.pop(0)
        else:
            raise CommandUXError(
                "decision_required",
                "resume requires a decision",
                candidates=list(DECISIONS),
                hint='/drawio:resume continue "optional feedback"',
            )
    effective_decision = decision or positional_decision
    if effective_decision not in DECISIONS:
        raise CommandUXError("decision_invalid", f"unsupported decision: {effective_decision}", candidates=list(DECISIONS))
    if feedback is not None and parts:
        raise CommandUXError("duplicate_feedback", "supply feedback positionally or with --feedback, not both")
    effective_feedback = feedback if feedback is not None else " ".join(parts)
    return run or positional_run, effective_decision, (effective_feedback or "").strip()


def error_result(exc):
    if isinstance(exc, CommandUXError):
        return {"schema_version": 1, "status": "selection_required", **exc.payload()}
    return {"schema_version": 1, "status": "error", "message": str(exc)}
