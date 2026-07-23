#!/usr/bin/env python3
"""Natural-language-first argument normalization for Draw.io commands."""
from __future__ import annotations

import hashlib
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
DEFAULT_IMPROVE_REQUEST = "Исправь найденные валидатором и Reviewer замечания"


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


def parse_intake_answers(values):
    """Parse repeatable hidden answer flags without executing command text."""
    answers = []
    by_id = {}
    for raw in values or []:
        value = str(raw)
        try:
            parsed = json.loads(value) if value.lstrip().startswith("{") else None
        except json.JSONDecodeError as exc:
            raise CommandUXError(
                "intake_answer_invalid",
                f"intake answer is not valid JSON: {exc}",
                hint='use "question-id=answer" or a JSON object',
            ) from exc
        if parsed is None:
            if "=" not in value:
                raise CommandUXError(
                    "intake_answer_invalid",
                    "intake answer must bind a question id with =",
                    hint='--intake-answer "question-<id>=answer"',
                )
            question_id, text = value.split("=", 1)
            parsed = {"question_id": question_id, "text": text}
        if not isinstance(parsed, dict) or set(parsed) != {"question_id", "text"}:
            raise CommandUXError(
                "intake_answer_invalid",
                "intake answer JSON must contain only question_id and text",
            )
        question_id = parsed.get("question_id")
        text = parsed.get("text")
        if (
            not isinstance(question_id, str)
            or not re.fullmatch(r"question-[a-f0-9]{20}", question_id)
            or not isinstance(text, str)
            or not text.strip()
        ):
            raise CommandUXError(
                "intake_answer_invalid",
                "intake answer requires a stable question id and non-empty text",
            )
        normalized = {"question_id": question_id, "text": text.strip()}
        if question_id in by_id and by_id[question_id] != normalized["text"]:
            raise CommandUXError(
                "intake_answer_conflict",
                f"conflicting answers supplied for {question_id}",
            )
        if question_id not in by_id:
            answers.append(normalized)
        by_id[question_id] = normalized["text"]
    return answers


def intake_awaiting_input(*, intake_id, question, command):
    """Return one native-selection instruction; headless callers use it unchanged."""
    if command not in {"create", "improve"}:
        raise CommandUXError("intake_command_invalid", f"unsupported intake command: {command}")
    answer_binding = f"{question['question_id']}=<answer>"
    replay = (
        f"/drawio:{command} --intake-id {quote_command_value(intake_id)} "
        f"--intake-answer {quote_command_value(answer_binding)}"
    )
    accept_command = (
        f"/drawio:{command} --intake-id {quote_command_value(intake_id)} "
        "--accept-intake-assumptions"
    )
    return {
        "schema_version": 1,
        "status": "awaiting_input",
        "intake_id": intake_id,
        "selection_required": {
            "question": question,
            "replay": {
                "intake_id": intake_id,
                "answer_flag": "--intake-answer",
                "accept_assumptions_flag": "--accept-intake-assumptions",
                "command": replay,
                "accept_assumptions_command": accept_command,
            },
        },
    }


def workspace_path(workspace):
    path = Path(workspace).expanduser().resolve()
    if not path.is_dir():
        raise CommandUXError("workspace_not_found", f"workspace is not a directory: {path}")
    return path


def explicit_renderer_document(workspace, source):
    """Load one deliberately supplied JSON/YAML renderer source document."""
    workspace = workspace_path(workspace)
    path = Path(source).expanduser().resolve()
    if not path.is_file() or not _inside(path, workspace):
        raise CommandUXError(
            "renderer_source_invalid",
            f"renderer source must be a file inside the workspace: {path}",
        )
    try:
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".json":
            content = json.loads(text)
        elif path.suffix.lower() in {".yaml", ".yml"}:
            # Reuse the established scalar normalizer so YAML timestamps enter
            # the immutable JSON source bundle as stable ISO strings.
            from roadmap_validate import load_yaml
            content = load_yaml(path)
        else:
            raise CommandUXError(
                "renderer_source_invalid",
                "renderer source must use .json, .yaml, or .yml",
            )
    except CommandUXError:
        raise
    except (ImportError, OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise CommandUXError(
            "renderer_source_invalid",
            f"could not parse renderer source {path}: {exc}",
        ) from exc
    if not isinstance(content, dict):
        raise CommandUXError(
            "renderer_source_invalid",
            "renderer source top level must be an object",
        )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return path, {
        "source_id": f"renderer-source-{digest[:20]}",
        "uri": path.as_uri(),
        "content": content,
        "revision": digest,
    }


def positional_request(parts, explicit=None, *, required=True):
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
    if not value and (required or explicit is not None):
        raise CommandUXError("request_required", "diagram request must not be empty")
    return value or None


def split_diagram_request(parts, *, diagram=None, request=None, request_required=True):
    parts = list(parts or [])
    short_diagram = None
    if diagram is None and parts and parts[0].lower().endswith(".drawio"):
        short_diagram = parts.pop(0)
    return diagram or short_diagram, positional_request(
        parts, request, required=request_required
    )


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


def _inside(path, parent):
    try:
        Path(path).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def latest_review_handoff(workspace):
    """Return the latest completed, hash-matching read-only review handoff."""
    workspace = workspace_path(workspace)
    runs_root = workspace / ".diagram-runs"
    if not runs_root.is_dir():
        return None
    eligible = []
    for result_path in runs_root.glob("*/host-result.json"):
        run_dir = result_path.parent.resolve()
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
            artifact = result["artifact"]
            reviewer = result["reviewer"]
            evidence = result["evidence"]
            result_run_dir = Path(result["run_dir"]).expanduser().resolve()
            reviewed_path = Path(artifact["path"]).expanduser().resolve()
            reviewed_sha256 = artifact["sha256"]
            reviewer_input_path = Path(evidence["reviewer_input"]).expanduser().resolve()
            reviewer_input = json.loads(
                reviewer_input_path.read_text(encoding="utf-8")
            )
            reviewer_input_v2 = reviewer_input.get("schema_version") == 2
            audit_artifact = (
                reviewer_input["candidate"]["artifact"]
                if reviewer_input_v2 else reviewer_input["artifact"]
            )
            audit_path = Path(audit_artifact["path"]).expanduser()
            if reviewer_input_v2:
                audit_path = run_dir / audit_path
            audit_path = audit_path.resolve()
            audit_sha256 = audit_artifact["sha256"]
            report_path = Path(result["validation"]["report"]).expanduser().resolve()
            receipt_path_value = evidence.get("validation_receipt_v2") or result["validation"]["receipt"]
            receipt_path = Path(receipt_path_value).expanduser().resolve()
            verdict_path_value = evidence.get("reviewer_verdict")
            verdict_path = Path(verdict_path_value).expanduser().resolve() if verdict_path_value else None
            result_mtime_ns = result_path.stat().st_mtime_ns
        except (KeyError, TypeError, OSError, json.JSONDecodeError):
            continue
        if not _inside(run_dir, runs_root):
            continue
        if result.get("mode") not in (None, "review"):
            continue
        if result.get("status") not in {"passed", "findings"}:
            continue
        if result_run_dir != run_dir:
            continue
        if artifact.get("modified") is not False:
            continue
        if reviewer.get("status") != "completed":
            continue
        if reviewer.get("verdict") not in {"approve", "reject"}:
            continue
        if reviewer.get("model_proof", {}).get("verified") is not True:
            continue
        if not _inside(reviewed_path, workspace) or reviewed_path.suffix.lower() != ".drawio":
            continue
        if not reviewed_path.is_file() or not _inside(reviewer_input_path, run_dir):
            continue
        if reviewer_input.get("run_id") != result["run_id"]:
            continue
        if (not reviewer_input_v2 and audit_path != reviewed_path) or audit_sha256 != reviewed_sha256:
            continue
        handoff = result.get("improve_handoff")
        if handoff is not None and (
            not isinstance(handoff, dict)
            or handoff.get("diagram") != str(reviewed_path)
            or handoff.get("artifact_sha256") != reviewed_sha256
        ):
            continue
        if not isinstance(reviewed_sha256, str) or _sha256_file(reviewed_path) != reviewed_sha256:
            continue
        if (
            not report_path.is_file() or not receipt_path.is_file()
            or not _inside(report_path, run_dir) or not _inside(receipt_path, run_dir)
        ):
            continue
        if reviewer_input_v2:
            if verdict_path is None or not verdict_path.is_file() or not _inside(verdict_path, run_dir):
                continue
            candidate_evidence = reviewer_input["candidate"]
            if (
                candidate_evidence["report"]["sha256"] != _sha256_file(report_path)
                or candidate_evidence["receipt"]["sha256"] != _sha256_file(receipt_path)
            ):
                continue
        findings = reviewer.get("findings", [])
        findings_sha256 = hashlib.sha256(
            json.dumps(findings, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        eligible.append((result_mtime_ns, run_dir.name, {
            "run_id": result["run_id"],
            "run_dir": str(run_dir),
            "host_result": str(result_path.resolve()),
            "diagram": str(reviewed_path),
            "artifact_sha256": reviewed_sha256,
            "review_status": result["status"],
            "reviewer_verdict": reviewer.get("verdict"),
            "findings": findings,
            "findings_sha256": findings_sha256,
            "validation_report": str(report_path),
            "validation_report_sha256": _sha256_file(report_path),
            "validation_receipt": str(receipt_path),
            "validation_receipt_sha256": _sha256_file(receipt_path),
            "reviewer_verdict_path": str(verdict_path) if verdict_path else None,
            "reviewer_verdict_sha256": _sha256_file(verdict_path) if verdict_path else None,
        }))
    if not eligible:
        return None
    return max(eligible, key=lambda item: (item[0], item[1]))[2]


def resolve_improve_inputs(workspace, *, diagram=None, request=None):
    """Resolve omitted improve inputs without invoking a model or mutating a run."""
    handoff = None
    if diagram:
        resolved_diagram, diagram_selection = select_diagram(workspace, diagram)
    else:
        handoff = latest_review_handoff(workspace)
        if handoff:
            resolved_diagram = Path(handoff["diagram"])
            diagram_selection = "latest_completed_review"
        else:
            resolved_diagram, diagram_selection = select_diagram(workspace)
    if request:
        resolved_request = request
    else:
        resolved_request = DEFAULT_IMPROVE_REQUEST
    return resolved_diagram, diagram_selection, resolved_request, handoff


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
