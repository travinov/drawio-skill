#!/usr/bin/env python3
"""Pure v2 evidence readers, receipt verification, and ledger replay."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from lifecycle_contracts import (
    ContractError,
    canonical_json_sha256,
    contained_path,
    file_sha256,
    require_valid_contract,
    validate_contract,
    verify_snapshot_descriptor,
)


def invalid(code: str, pointer: str, message: str, **details: Any) -> dict[str, Any]:
    value = {"code": code, "pointer": pointer, "message": message}
    value.update(details)
    return value


def read_json(path: Path | str) -> dict[str, Any]:
    """Read JSON without throwing; this function never writes or recovers."""
    source = Path(path)
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        return {"valid": False, "value": None, "diagnostics": [invalid("evidence.read_failed", "", str(exc), path=str(source))]}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {"valid": False, "value": None, "diagnostics": [invalid("evidence.json_invalid", "", str(exc), path=str(source))]}
    if not isinstance(value, dict):
        return {"valid": False, "value": value, "diagnostics": [invalid("evidence.type_invalid", "", "top-level JSON value must be an object", path=str(source))]}
    return {"valid": True, "value": value, "diagnostics": []}


def read_jsonl(path: Path | str) -> dict[str, Any]:
    """Read every nonblank JSONL line and retain exact line diagnostics."""
    source = Path(path)
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        return {"valid": False, "records": [], "diagnostics": [invalid("evidence.read_failed", "", str(exc), path=str(source))]}
    records = []
    diagnostics = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            diagnostics.append(invalid("evidence.jsonl_invalid", f"/lines/{line_number}", str(exc), line=line_number, path=str(source)))
            continue
        if not isinstance(value, dict):
            diagnostics.append(invalid("evidence.jsonl_type_invalid", f"/lines/{line_number}", "JSONL event must be an object", line=line_number, path=str(source)))
            continue
        records.append({"line": line_number, "value": value})
    return {"valid": not diagnostics, "records": records, "diagnostics": diagnostics}


def _parse_timestamp(value: Any, pointer: str, diagnostics: list[dict[str, Any]]) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        diagnostics.append(invalid("receipt.timestamp_invalid", pointer, f"invalid RFC 3339 timestamp {value!r}"))
        return None


def _verify_file_descriptor(
    descriptor: dict[str, Any],
    *,
    root: Path,
    pointer: str,
) -> list[dict[str, Any]]:
    diagnostics = []
    try:
        path = contained_path(root, descriptor.get("path", ""))
    except ContractError as exc:
        return [invalid("receipt.path_escape", f"{pointer}/path", str(exc))]
    try:
        length = path.stat().st_size
        digest = file_sha256(path)
    except OSError as exc:
        return [invalid("receipt.output_missing", f"{pointer}/path", str(exc))]
    if length != descriptor.get("byte_length"):
        diagnostics.append(invalid("receipt.byte_length_mismatch", f"{pointer}/byte_length", f"expected {descriptor.get('byte_length')}, found {length}"))
    if digest != descriptor.get("sha256"):
        diagnostics.append(invalid("receipt.hash_mismatch", f"{pointer}/sha256", f"expected {descriptor.get('sha256')}, found {digest}"))
    return diagnostics


def _verify_tool_artifact_snapshots(
    event: dict[str, Any],
    *,
    root: Path,
    pointer: str,
) -> list[dict[str, Any]]:
    snapshots = event.get("payload", {}).get("artifact_snapshots", {})
    if not isinstance(snapshots, dict):
        return [
            invalid(
                "tool_attempt.artifacts_invalid",
                f"{pointer}/payload/artifact_snapshots",
                "tool attempt artifact_snapshots must be an object",
            )
        ]
    diagnostics: list[dict[str, Any]] = []
    for name, descriptor in sorted(snapshots.items()):
        descriptor_pointer = f"{pointer}/payload/artifact_snapshots/{name}"
        if not isinstance(descriptor, dict):
            diagnostics.append(
                invalid(
                    "tool_attempt.artifact_invalid",
                    descriptor_pointer,
                    "tool attempt artifact descriptor must be an object",
                )
            )
            continue
        try:
            path = contained_path(root, descriptor.get("path", ""))
        except ContractError as exc:
            diagnostics.append(
                invalid(
                    "tool_attempt.artifact_path_invalid",
                    f"{descriptor_pointer}/path",
                    str(exc),
                )
            )
            continue
        try:
            byte_length = path.stat().st_size
            digest = file_sha256(path)
        except OSError as exc:
            diagnostics.append(
                invalid(
                    "tool_attempt.artifact_missing",
                    f"{descriptor_pointer}/path",
                    str(exc),
                )
            )
            continue
        if byte_length != descriptor.get("byte_length"):
            diagnostics.append(
                invalid(
                    "tool_attempt.artifact_length_mismatch",
                    f"{descriptor_pointer}/byte_length",
                    "tool attempt artifact byte length changed",
                )
            )
        if digest != descriptor.get("sha256"):
            diagnostics.append(
                invalid(
                    "tool_attempt.artifact_hash_mismatch",
                    f"{descriptor_pointer}/sha256",
                    "tool attempt artifact hash changed",
                )
            )
    return diagnostics


def verify_validation_receipt(
    receipt: dict[str, Any],
    *,
    run_dir: Path | str,
    trusted_validator: dict[str, Any],
    extension_root: Path | str,
    expected_run_id: str | None = None,
) -> dict[str, Any]:
    diagnostics: list[dict[str, Any]] = []
    installation_diagnostics: list[dict[str, Any]] = []
    schema_diagnostics = validate_contract(receipt, "validation-receipt", 2)
    if schema_diagnostics:
        return {
            "valid": False,
            "integrity_valid": False,
            "strict_passed": False,
            "diagnostics": schema_diagnostics,
            "implementation_changed": False,
            "installation_diagnostics": [],
        }
    if expected_run_id is not None and receipt["run_id"] != expected_run_id:
        diagnostics.append(invalid("receipt.run_id_mismatch", "/run_id", "receipt belongs to another run"))
    run_root = Path(run_dir).resolve()
    try:
        attempt_root = contained_path(run_root, receipt["attempt_dir"])
    except ContractError as exc:
        return {
            "valid": False,
            "integrity_valid": False,
            "strict_passed": False,
            "diagnostics": [invalid("receipt.attempt_escape", "/attempt_dir", str(exc))],
            "implementation_changed": False,
            "installation_diagnostics": [],
        }
    started = _parse_timestamp(receipt["started_at"], "/started_at", diagnostics)
    finished = _parse_timestamp(receipt["finished_at"], "/finished_at", diagnostics)
    if started is not None and finished is not None and finished < started:
        diagnostics.append(invalid("receipt.timestamp_order", "/finished_at", "finished_at precedes started_at"))
    if receipt["bindings"]["candidate_sha256"] != receipt["artifact"]["sha256"]:
        diagnostics.append(invalid("receipt.candidate_binding_mismatch", "/bindings/candidate_sha256", "candidate binding differs from artifact hash"))
    if receipt["result"] == "passed" and receipt["exit_code"] != 0:
        diagnostics.append(invalid("receipt.result_exit_mismatch", "/exit_code", "passed receipt requires exit code 0"))
    if receipt["result"] == "failed" and receipt["exit_code"] == 0:
        diagnostics.append(invalid("receipt.result_exit_mismatch", "/exit_code", "failed receipt requires nonzero exit code"))
    actual_validator = receipt["validator"]
    for field in ("name", "version", "path", "file_sha256"):
        if actual_validator.get(field) != trusted_validator.get(field):
            diagnostics.append(invalid("receipt.validator_mismatch", f"/validator/{field}", f"receipt validator {field} differs from captured trusted validator"))
    # Historical authority is the descriptor captured in the run snapshot.  A
    # later extension upgrade must not invalidate internally consistent old
    # evidence; installed-file drift is reported separately.
    try:
        validator_path = contained_path(extension_root, actual_validator["path"])
        if file_sha256(validator_path) != actual_validator["file_sha256"]:
            installation_diagnostics.append(invalid("implementation.validator_changed", "/validator/file_sha256", "installed validator bytes differ from the captured historical validator"))
    except (ContractError, OSError) as exc:
        installation_diagnostics.append(invalid("implementation.validator_unavailable", "/validator/path", str(exc)))
    command = receipt["command"]
    expected_validator_path = str((Path(extension_root).resolve() / trusted_validator["path"]).resolve())
    if expected_validator_path not in command:
        diagnostics.append(invalid("receipt.command_validator_mismatch", "/command", "command does not invoke the captured trusted validator path"))
    if command.count("--strict") != 1 or command.count("--json") != 1:
        diagnostics.append(invalid("receipt.command_mode_mismatch", "/command", "command must invoke strict JSON validation exactly once"))
    command_candidate_bound = False
    for argument in command:
        try:
            candidate = contained_path(run_root, argument)
        except (ContractError, OSError, TypeError):
            continue
        if candidate.is_file() and file_sha256(candidate) == receipt["artifact"]["sha256"]:
            command_candidate_bound = True
            break
    if not command_candidate_bound:
        diagnostics.append(invalid("receipt.command_candidate_mismatch", "/command", "command has no run-contained candidate matching the receipt artifact hash"))
    diagnostics.extend(_verify_file_descriptor(receipt["artifact"], root=attempt_root, pointer="/artifact"))
    for name in ("report", "stdout", "stderr"):
        diagnostics.extend(_verify_file_descriptor(receipt["outputs"][name], root=attempt_root, pointer=f"/outputs/{name}"))
    try:
        report_path = contained_path(attempt_root, receipt["outputs"]["report"]["path"])
        stdout_path = contained_path(attempt_root, receipt["outputs"]["stdout"]["path"])
        report = json.loads(report_path.read_text(encoding="utf-8"))
        stdout_report = json.loads(stdout_path.read_text(encoding="utf-8"))
        if report != stdout_report:
            diagnostics.append(invalid("receipt.report_stdout_mismatch", "/outputs", "captured report differs from validator stdout"))
        if report.get("artifact_sha256") != receipt["artifact"]["sha256"]:
            diagnostics.append(invalid("receipt.report_artifact_mismatch", "/outputs/report", "report artifact hash differs from the receipt candidate"))
        report_validator = report.get("validator") or {}
        if any(report_validator.get(field) != actual_validator.get(field) for field in ("name", "version")):
            diagnostics.append(invalid("receipt.report_validator_mismatch", "/outputs/report", "report validator identity differs from the receipt"))
        if (report.get("summary") or {}).get("status") != receipt["result"]:
            diagnostics.append(invalid("receipt.report_result_mismatch", "/outputs/report", "report status differs from the receipt result"))
    except (ContractError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        diagnostics.append(invalid("receipt.report_unreadable", "/outputs/report", str(exc)))
    integrity_valid = not diagnostics
    # ``valid`` remains the compatibility alias for evidence integrity.  A
    # schema-valid, hash-bound receipt can truthfully describe a strict
    # validation failure; callers must use ``strict_passed`` for that result.
    strict_passed = bool(
        integrity_valid
        and receipt.get("strict") is True
        and receipt.get("result") == "passed"
        and receipt.get("exit_code") == 0
    )
    return {
        "valid": integrity_valid,
        "integrity_valid": integrity_valid,
        "strict_passed": strict_passed,
        "diagnostics": diagnostics,
        "implementation_changed": bool(installation_diagnostics),
        "installation_diagnostics": installation_diagnostics,
    }


def verify_event_ledger(
    manifest_path: Path | str,
    *,
    run_dir: Path | str,
    expected_run_id: str | None = None,
) -> dict[str, Any]:
    """Validate and replay event-bound snapshots without changing any file."""
    parsed = read_jsonl(manifest_path)
    diagnostics = list(parsed["diagnostics"])
    previous_hash = None
    expected_sequence = 0
    latest_snapshots: dict[str, dict[str, Any]] = {}
    events = []
    for record in parsed["records"]:
        line = record["line"]
        event = record["value"]
        event_pointer = f"/lines/{line}"
        schema_diagnostics = validate_contract(event, "run-event", 2)
        diagnostics.extend(
            invalid(item["code"], event_pointer + item["pointer"], item["message"], line=line)
            for item in schema_diagnostics
        )
        if schema_diagnostics:
            continue
        if expected_run_id is not None and event["run_id"] != expected_run_id:
            diagnostics.append(invalid("event.run_id_mismatch", f"{event_pointer}/run_id", "event belongs to another run", line=line))
        if event["sequence"] != expected_sequence:
            diagnostics.append(invalid("event.sequence_mismatch", f"{event_pointer}/sequence", f"expected sequence {expected_sequence}, found {event['sequence']}", line=line))
        if event["previous_event_sha256"] != previous_hash:
            diagnostics.append(invalid("event.previous_hash_mismatch", f"{event_pointer}/previous_event_sha256", "event predecessor does not match canonical prior event", line=line))
        event_hash = canonical_json_sha256(event)
        previous_hash = event_hash
        expected_sequence = event["sequence"] + 1
        for snapshot_index, descriptor in enumerate(event["snapshots"]):
            pointer = f"{event_pointer}/snapshots/{snapshot_index}"
            if descriptor["transaction_id"] != event["transaction_id"]:
                diagnostics.append(invalid("event.snapshot_transaction_mismatch", f"{pointer}/transaction_id", "snapshot transaction differs from event transaction", line=line))
            prior = latest_snapshots.get(descriptor["schema_kind"])
            expected_predecessor = prior["canonical_sha256"] if prior else None
            if descriptor["previous_snapshot_sha256"] != expected_predecessor:
                diagnostics.append(invalid("event.snapshot_predecessor_mismatch", f"{pointer}/previous_snapshot_sha256", "snapshot predecessor differs from replayed prior snapshot", line=line))
            for item in verify_snapshot_descriptor(descriptor, trusted_root=run_dir):
                diagnostics.append(invalid(item["code"], pointer + item.get("pointer", ""), item["message"], line=line))
            latest_snapshots[descriptor["schema_kind"]] = descriptor
        if event["event_type"] in {
            "tool_attempt",
            "candidate_accepted",
            "candidate_rejected",
        } and "artifact_snapshots" in event.get("payload", {}):
            diagnostics.extend(
                _verify_tool_artifact_snapshots(
                    event,
                    root=Path(run_dir).resolve(),
                    pointer=event_pointer,
                )
            )
        events.append({"line": line, "sha256": event_hash, "event": event})
    return {
        "valid": not diagnostics,
        "status": "verified" if not diagnostics else "tampered_or_incomplete",
        "event_count": len(events),
        "latest_event_sha256": previous_hash,
        "latest_snapshots": latest_snapshots,
        "events": events,
        "diagnostics": diagnostics,
    }


def verify_replayed_current_snapshots(
    replay: dict[str, Any],
    *,
    run_dir: Path | str,
    current_paths: dict[str, Path | str],
) -> list[dict[str, Any]]:
    diagnostics = []
    root = Path(run_dir).resolve()
    for kind, candidate in current_paths.items():
        descriptor = replay.get("latest_snapshots", {}).get(kind)
        if descriptor is None:
            diagnostics.append(invalid("snapshot.binding_missing", f"/{kind}", f"ledger has no {kind} snapshot"))
            continue
        try:
            current = contained_path(root, candidate)
            digest = file_sha256(current)
        except (ContractError, OSError) as exc:
            diagnostics.append(invalid("snapshot.current_unavailable", f"/{kind}", str(exc)))
            continue
        if digest != descriptor["sha256"]:
            diagnostics.append(invalid("snapshot.current_mismatch", f"/{kind}", "current snapshot differs from latest event-bound snapshot"))
    return diagnostics
