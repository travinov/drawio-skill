#!/usr/bin/env python3
"""Deterministic extension-host entry points for corporate GigaCode commands."""
from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import jsonschema

import agent_runtime
import diagram_supervisor as supervisor


ROOT = Path(__file__).resolve().parent.parent
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def utc_slug():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"review-{stamp}-{uuid.uuid4().hex[:8]}"


def require_workspace_artifact(workspace, artifact):
    workspace = Path(workspace).expanduser().resolve()
    artifact = Path(artifact).expanduser().resolve()
    if not workspace.is_dir():
        raise supervisor.SupervisorError(f"workspace is not a directory: {workspace}")
    if not artifact.is_file():
        raise supervisor.SupervisorError(f"diagram artifact is not a file: {artifact}")
    if artifact.suffix.lower() != ".drawio":
        raise supervisor.SupervisorError("review requires a .drawio artifact")
    if not supervisor._is_within(artifact, workspace):
        raise supervisor.SupervisorError("diagram artifact must be inside the current workspace")
    return workspace, artifact


def audit_input(run_dir, artifact, spec_path, report_path, receipt_path):
    spec = supervisor.load_json(spec_path)
    report = supervisor.load_json(report_path)
    receipt = supervisor.load_json(receipt_path)
    verification = supervisor.verify_receipt(receipt_path, artifact)
    if not verification["valid"]:
        raise supervisor.SupervisorError(
            f"review audit receipt evidence failed: {verification['checks']}"
        )
    result = {
        "schema_version": 1,
        "review_kind": "baseline_audit",
        "run_id": receipt["run_id"],
        "artifact": {
            "path": str(artifact),
            "sha256": supervisor.sha256_file(artifact),
        },
        "spec": {
            "path": str(spec_path),
            "sha256": supervisor.sha256_file(spec_path),
            "content": spec,
        },
        "report": {
            "path": str(report_path),
            "sha256": supervisor.sha256_file(report_path),
            "content": report,
        },
        "receipt": {
            "path": str(receipt_path),
            "sha256": supervisor.sha256_file(receipt_path),
            "content": receipt,
        },
        "strict_passed": verification["passed"],
        "context": {
            "source_refs": spec.get("source_refs", []),
            "requested_reviewer_model": supervisor.load_json(
                ROOT / "data" / "model-routing.default.json"
            )["roles"]["reviewer"]["requested_model"],
        },
    }
    schema = supervisor.load_json(ROOT / "data" / "reviewer-audit-input.v1.schema.json")
    jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker()
    ).validate(result)
    return result


def run_review(artifact, workspace, cli, *, run_id=None, profile=None, source=None, timeout=600):
    workspace, artifact = require_workspace_artifact(workspace, artifact)
    run_id = run_id or utc_slug()
    if not RUN_ID_RE.fullmatch(run_id):
        raise supervisor.SupervisorError("run-id must be an opaque slug")
    run_dir = (workspace / ".diagram-runs" / run_id).resolve()
    if run_dir.exists():
        raise supervisor.SupervisorError(f"run directory already exists: {run_dir}")

    original_sha256 = supervisor.sha256_file(artifact)
    preflight = supervisor.host_preflight(workspace, run_dir, cli)
    spec_path = run_dir / "diagram-spec.json"
    supervisor.write_json(spec_path, supervisor.make_spec(artifact))
    supervisor.transition(run_dir, "analyzed", artifact=artifact)
    receipt = supervisor.run_validation(
        artifact, run_dir, profile=profile, source=source, attempt_id="baseline"
    )
    report_path = run_dir / "attempts" / "baseline" / "validation-report.json"
    receipt_path = run_dir / "attempts" / "baseline" / "validation-receipt.json"
    reviewer_input_path = run_dir / "reviewer-audit-input.json"
    supervisor.write_json(
        reviewer_input_path,
        audit_input(run_dir, artifact, spec_path, report_path, receipt_path),
    )
    reviewer_output_path = run_dir / "reviewer-verdict.json"

    try:
        runtime = agent_runtime.invoke_role(
            "reviewer",
            reviewer_input_path,
            reviewer_output_path,
            cli=str(Path(cli).expanduser()),
            run_dir=run_dir,
            timeout=timeout,
            cwd=workspace,
        )
        verdict = supervisor.load_reviewer_verdict(
            reviewer_output_path,
            receipt["run_id"],
            supervisor.sha256_file(artifact),
            report_path,
            receipt_path,
        )
        reviewer = {
            "status": "completed",
            "verdict": verdict["verdict"],
            "findings": verdict["findings"],
            "requested_model": runtime["resolution"]["requested_model"],
            "resolved_model": runtime["resolution"]["resolved_model"],
            "resolution_mode": runtime["resolution"]["resolution_mode"],
            "fallback_used": runtime["resolution"]["fallback_used"],
            "model_proof": runtime["runtime_metadata"]["model_proof"],
            "output": str(reviewer_output_path),
        }
    except agent_runtime.RoleOutputContractError as exc:
        reviewer = {
            "status": "failed",
            "error": str(exc),
            "requested_model": exc.resolution["requested_model"],
            "resolved_model": exc.resolution["resolved_model"],
            "resolution_mode": exc.resolution["resolution_mode"],
            "fallback_used": exc.resolution["fallback_used"],
            "model_proof": exc.runtime_metadata["model_proof"],
            "reported_model": exc.runtime_metadata.get("reported_model"),
            "runtime_version": exc.runtime_metadata.get("runtime_version"),
            "invalid_output_sha256": exc.invalid_output_sha256,
        }
    except (OSError, json.JSONDecodeError, supervisor.SupervisorError) as exc:
        reviewer = {
            "status": "failed",
            "error": str(exc),
            "requested_model": supervisor.load_json(
                ROOT / "data" / "model-routing.default.json"
            )["roles"]["reviewer"]["requested_model"],
        }

    if supervisor.sha256_file(artifact) != original_sha256:
        raise supervisor.SupervisorError("source diagram changed during read-only review")
    supervisor.transition(run_dir, "final_review", artifact=artifact)
    validation_passed = receipt["result"] == "passed"
    reviewer_passed = reviewer.get("status") == "completed" and reviewer.get("verdict") == "approve"
    result = {
        "schema_version": 1,
        "status": "passed" if validation_passed and reviewer_passed else "findings",
        "run_id": preflight["run_id"],
        "run_dir": str(run_dir),
        "artifact": {"path": str(artifact), "sha256": original_sha256, "modified": False},
        "validation": {
            "passed": validation_passed,
            "exit_code": receipt["exit_code"],
            "summary": supervisor.load_json(report_path).get("summary", {}),
            "report": str(report_path),
            "receipt": str(receipt_path),
        },
        "reviewer": reviewer,
        "evidence": {
            "host_preflight": str(run_dir / "host-preflight.json"),
            "manifest": str(run_dir / "run-manifest.jsonl"),
            "diagram_spec": str(spec_path),
            "reviewer_input": str(reviewer_input_path),
        },
        "next_action": (
            "user_final_review"
            if validation_passed and reviewer_passed
            else "inspect_findings_before_any_repair"
        ),
    }
    supervisor.write_json(run_dir / "host-result.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description="Deterministic Draw.io extension command host")
    sub = parser.add_subparsers(dest="command", required=True)
    review = sub.add_parser("review", help="run strict validation and isolated independent review")
    review.add_argument("--artifact", required=True)
    review.add_argument("--workspace", default=str(Path.cwd()))
    review.add_argument("--cli", default=str(Path.home() / ".gigacode/bin/gigacode"))
    review.add_argument("--run-id")
    review.add_argument("--profile", choices=("roadmap", "gitflow"))
    review.add_argument("--source")
    review.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    try:
        result = run_review(
            args.artifact,
            args.workspace,
            args.cli,
            run_id=args.run_id,
            profile=args.profile,
            source=args.source,
            timeout=args.timeout,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    except (OSError, ValueError, json.JSONDecodeError, supervisor.SupervisorError) as exc:
        print(
            json.dumps({"schema_version": 1, "status": "error", "message": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        raise SystemExit(2)


if __name__ == "__main__":
    main()
