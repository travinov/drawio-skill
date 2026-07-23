#!/usr/bin/env python3
"""Integrated v2 control plane layered over the compatible runtime pipeline.

The existing v1 files remain a runtime shadow for proven deterministic tools.
Every new mutable run is authorized by the immutable snapshots and event ledger
owned here; a run without this control plane is trace/manual-handoff only.
"""
from __future__ import annotations

import copy
import json
import os
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from evidence_v2 import verify_event_ledger, verify_validation_receipt
from implementation_snapshot_v2 import capture_implementation_snapshot
from lifecycle_contracts import (
    ContractError,
    atomic_write_bytes,
    canonical_json_bytes,
    canonical_json_sha256,
    contained_path,
    file_sha256,
    require_valid_contract,
    write_snapshot,
)
from source_bundle_v2 import (
    append_source_revision,
    build_source_bundle,
    source_bundle_sha256,
    source_record,
)


CONTROL_DIR = "lifecycle-v2"
MANIFEST_FILE = "run-manifest.v2.jsonl"
CHECKPOINT_TYPES = {
    "semantic_approval": "semantic_approval",
    "final_acceptance": "final_acceptance",
    "plateau": "plateau",
    "publication_conflict": "publication_conflict",
    "role_contract": "role_contract",
    "manual_handoff": "manual_handoff",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def control_dir(run_dir: Path | str) -> Path:
    return Path(run_dir).resolve() / CONTROL_DIR


def manifest_path(run_dir: Path | str) -> Path:
    return control_dir(run_dir) / MANIFEST_FILE


def _snapshot_path(run_dir: Path | str, kind: str, sequence: int) -> Path:
    return control_dir(run_dir) / "snapshots" / kind / f"{sequence:06d}.json"


def _read_descriptor_document(run_dir: Path | str, descriptor: dict[str, Any]) -> dict[str, Any]:
    path = contained_path(Path(run_dir).resolve(), descriptor["path"])
    document = json.loads(path.read_text(encoding="utf-8"))
    require_valid_contract(document, descriptor["schema_kind"], 2)
    if canonical_json_sha256(document) != descriptor["canonical_sha256"]:
        raise ContractError("snapshot.canonical_hash_mismatch", f"snapshot changed: {path}")
    return document


def replay(run_dir: Path | str, expected_run_id: str | None = None) -> dict[str, Any]:
    return verify_event_ledger(
        manifest_path(run_dir), run_dir=Path(run_dir).resolve(), expected_run_id=expected_run_id,
    )


def require_mutable(run_dir: Path | str, expected_run_id: str | None = None) -> dict[str, Any]:
    path = manifest_path(run_dir)
    if not path.is_file():
        raise ContractError(
            "contract.v1_mutation_refused",
            "this run has no v2 control plane; use trace or manual handoff",
            pointer="/schema_version",
        )
    result = replay(run_dir, expected_run_id)
    if not result["valid"]:
        first = result["diagnostics"][0]
        raise ContractError(first["code"], first["message"], pointer=first.get("pointer", ""))
    for required in ("workflow", "run-state", "source-bundle", "implementation-snapshot"):
        if required not in result["latest_snapshots"]:
            raise ContractError("lifecycle.snapshot_missing", f"v2 ledger has no {required} snapshot")
    return result


def latest_document(run_dir: Path | str, kind: str, replay_result: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    result = replay_result or require_mutable(run_dir)
    descriptor = result["latest_snapshots"].get(kind)
    if descriptor is None:
        raise ContractError("lifecycle.snapshot_missing", f"v2 ledger has no {kind} snapshot")
    return _read_descriptor_document(run_dir, descriptor), descriptor


def _append_event(
    run_dir: Path | str,
    *,
    run_id: str,
    event_type: str,
    transaction_id: str,
    snapshots: Iterable[dict[str, Any]] = (),
    payload: dict[str, Any] | None = None,
    actor: dict[str, Any] | None = None,
) -> dict[str, Any]:
    path = manifest_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    previous_hash = None
    sequence = 0
    if path.is_file():
        parsed = verify_event_ledger(path, run_dir=Path(run_dir).resolve(), expected_run_id=run_id)
        if not parsed["valid"]:
            first = parsed["diagnostics"][0]
            raise ContractError(first["code"], first["message"], pointer=first.get("pointer", ""))
        previous_hash = parsed["latest_event_sha256"]
        sequence = parsed["event_count"]
    event = {
        "schema_version": 2,
        "run_id": run_id,
        "event_id": str(uuid.uuid4()),
        "sequence": sequence,
        "timestamp": utc_now(),
        "event_type": event_type,
        "actor": actor or {"kind": "system", "id": "diagram-lifecycle-v2"},
        "transaction_id": transaction_id,
        "previous_event_sha256": previous_hash,
        "snapshots": list(snapshots),
        "payload": copy.deepcopy(payload or {}),
    }
    require_valid_contract(event, "run-event", 2)
    line = canonical_json_bytes(event) + b"\n"
    descriptor = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, line)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return event


def _write_next_snapshot(
    run_dir: Path | str,
    *,
    kind: str,
    document: dict[str, Any],
    transaction_id: str,
    predecessor: dict[str, Any] | None,
    sequence: int,
) -> dict[str, Any]:
    prior_hash = predecessor["canonical_sha256"] if predecessor else None
    document["transaction_id"] = transaction_id
    document["previous_snapshot_sha256"] = prior_hash
    return write_snapshot(
        _snapshot_path(run_dir, kind, sequence),
        document,
        kind=kind,
        trusted_root=Path(run_dir).resolve(),
        predecessor_sha256=prior_hash,
        transaction_id=transaction_id,
    )


def initialize(
    *,
    run_dir: Path | str,
    workspace: Path | str,
    target: Path | str,
    run_id: str,
    mode: str,
    request: str,
    extension_root: Path | str,
    explicit_documents: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    run_root = Path(run_dir).resolve()
    workspace_root = Path(workspace).resolve()
    target_path = contained_path(workspace_root, target)
    if manifest_path(run_root).exists():
        raise ContractError("lifecycle.already_initialized", "v2 control plane already exists")
    transaction_id = str(uuid.uuid4())
    request_source = source_record(
        source_id="original-user-request",
        kind="original_user_request",
        uri=f"urn:diagram-run:{run_id}:request",
        content=request,
    )
    sources = [request_source]
    if mode in {"improve", "review"} and target_path.is_file():
        sources.append(source_record(
            source_id="existing-diagram",
            kind="existing_diagram",
            uri=f"urn:diagram-run:{run_id}:workspace-artifact",
            content={
                "path": target_path.relative_to(workspace_root).as_posix(),
                "sha256": file_sha256(target_path),
                "byte_length": target_path.stat().st_size,
            },
        ))
    for index, document in enumerate(explicit_documents, 1):
        sources.append(source_record(
            source_id=document.get("source_id") or f"explicit-document-{index}",
            kind="explicit_user_document",
            uri=document["uri"],
            content=document["content"],
            revision=document.get("revision"),
            fragment=document.get("fragment"),
            confidence=float(document.get("confidence", 1.0)),
        ))
    source = build_source_bundle(
        bundle_id=str(uuid.uuid4()), run_id=run_id, sources=sources,
        transaction_id=transaction_id,
    )
    source_descriptor = _write_next_snapshot(
        run_root, kind="source-bundle", document=source, transaction_id=transaction_id,
        predecessor=None, sequence=1,
    )
    implementation = capture_implementation_snapshot(
        extension_root=extension_root, run_id=run_id, snapshot_id=str(uuid.uuid4()),
        transaction_id=transaction_id,
    )
    implementation_descriptor = _write_next_snapshot(
        run_root, kind="implementation-snapshot", document=implementation,
        transaction_id=transaction_id, predecessor=None, sequence=1,
    )
    state = {
        "schema_version": 2,
        "run_id": run_id,
        "status": "initialized",
        "iteration": 0,
        "updated_at": utc_now(),
        "transaction_id": transaction_id,
        "source_bundle_sha256": source_descriptor["canonical_sha256"],
        "previous_snapshot_sha256": None,
    }
    state_descriptor = _write_next_snapshot(
        run_root, kind="run-state", document=state, transaction_id=transaction_id,
        predecessor=None, sequence=1,
    )
    workflow = {
        "schema_version": 2,
        "run_id": run_id,
        "mode": mode,
        "workspace": str(workspace_root),
        "run_dir": run_root.relative_to(workspace_root).as_posix(),
        "target_path": target_path.relative_to(workspace_root).as_posix(),
        "target_initial_sha256": file_sha256(target_path) if target_path.is_file() else None,
        "created_at": utc_now(),
        "transaction_id": transaction_id,
        "state_snapshot": state_descriptor,
        "source_bundle": source_descriptor,
        "implementation_snapshot": implementation_descriptor,
        "latest_checkpoint": None,
        "publication_transaction": None,
        "previous_snapshot_sha256": None,
        "quality_profile_version": 2,
    }
    workflow_descriptor = _write_next_snapshot(
        run_root, kind="workflow", document=workflow, transaction_id=transaction_id,
        predecessor=None, sequence=1,
    )
    _append_event(
        run_root, run_id=run_id, event_type="run_created", transaction_id=transaction_id,
        snapshots=[source_descriptor, implementation_descriptor, state_descriptor, workflow_descriptor],
        payload={"mode": mode, "target_path": workflow["target_path"]},
    )
    return {
        "workflow": workflow_descriptor,
        "state": state_descriptor,
        "source_bundle": source_descriptor,
        "implementation_snapshot": implementation_descriptor,
    }


def transition(
    run_dir: Path | str,
    status: str,
    *,
    iteration: int | None = None,
    accepted_artifact: dict[str, Any] | None = None,
    validation_report: dict[str, Any] | None = None,
    validation_receipt: dict[str, Any] | None = None,
    reviewer_verdict: dict[str, Any] | None = None,
    checkpoint_id: str | None = None,
    last_error: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    replayed = require_mutable(run_dir)
    workflow, workflow_descriptor = latest_document(run_dir, "workflow", replayed)
    state, state_descriptor = latest_document(run_dir, "run-state", replayed)
    transaction_id = str(uuid.uuid4())
    state["status"] = status
    state["updated_at"] = utc_now()
    if iteration is not None:
        state["iteration"] = int(iteration)
    for key, value in (
        ("accepted_artifact", accepted_artifact),
        ("validation_report", validation_report),
        ("validation_receipt", validation_receipt),
        ("reviewer_verdict", reviewer_verdict),
    ):
        if value is not None:
            state[key] = copy.deepcopy(value)
    state["checkpoint_id"] = checkpoint_id
    state["last_error"] = copy.deepcopy(last_error)
    next_sequence = replayed["event_count"] + 1
    next_state_descriptor = _write_next_snapshot(
        run_dir, kind="run-state", document=state, transaction_id=transaction_id,
        predecessor=state_descriptor, sequence=next_sequence,
    )
    workflow["state_snapshot"] = next_state_descriptor
    next_workflow_descriptor = _write_next_snapshot(
        run_dir, kind="workflow", document=workflow, transaction_id=transaction_id,
        predecessor=workflow_descriptor, sequence=next_sequence,
    )
    _append_event(
        run_dir, run_id=workflow["run_id"], event_type="state_transition",
        transaction_id=transaction_id,
        snapshots=[next_state_descriptor, next_workflow_descriptor],
        payload={"status": status, **(payload or {})},
    )
    return state


def revise_sources(
    run_dir: Path | str,
    *,
    new_sources: Iterable[dict[str, Any]] = (),
    evidence: dict[str, Any] | None = None,
    event_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    replayed = require_mutable(run_dir)
    workflow, workflow_descriptor = latest_document(run_dir, "workflow", replayed)
    state, state_descriptor = latest_document(run_dir, "run-state", replayed)
    source, source_descriptor = latest_document(run_dir, "source-bundle", replayed)
    transaction_id = str(uuid.uuid4())
    next_source = append_source_revision(
        source, new_sources=list(new_sources), evidence=evidence,
        transaction_id=transaction_id,
        previous_snapshot_sha256=source_descriptor["canonical_sha256"],
    )
    sequence = replayed["event_count"] + 1
    next_source_descriptor = _write_next_snapshot(
        run_dir, kind="source-bundle", document=next_source,
        transaction_id=transaction_id, predecessor=source_descriptor, sequence=sequence,
    )
    state["source_bundle_sha256"] = next_source_descriptor["canonical_sha256"]
    next_state_descriptor = _write_next_snapshot(
        run_dir, kind="run-state", document=state, transaction_id=transaction_id,
        predecessor=state_descriptor, sequence=sequence,
    )
    workflow["source_bundle"] = next_source_descriptor
    workflow["state_snapshot"] = next_state_descriptor
    next_workflow_descriptor = _write_next_snapshot(
        run_dir, kind="workflow", document=workflow, transaction_id=transaction_id,
        predecessor=workflow_descriptor, sequence=sequence,
    )
    _append_event(
        run_dir, run_id=workflow["run_id"], event_type="source_revision",
        transaction_id=transaction_id,
        snapshots=[next_source_descriptor, next_state_descriptor, next_workflow_descriptor],
        payload={"revision": next_source["revision"], **(event_payload or {})},
    )
    return next_source


def add_feedback_source(run_dir: Path | str, feedback: str, decision_id: str) -> dict[str, Any]:
    if not feedback.strip():
        raise ValueError("empty feedback does not create a source revision")
    return revise_sources(
        run_dir,
        new_sources=[source_record(
            source_id=f"confirmed-clarification-{decision_id}",
            kind="confirmed_clarification",
            uri=f"urn:diagram-run:feedback:{decision_id}",
            content=feedback.strip(),
        )],
        event_payload={"kind": "confirmed_clarification", "decision_id": decision_id},
    )


def create_checkpoint(
    run_dir: Path | str,
    *,
    checkpoint_type: str,
    allowed_decisions: Iterable[str],
    context: dict[str, Any],
    baseline_semantic_digest: str | None = None,
    semantic_plan_sha256: str | None = None,
    semantic_delta_sha256: str | None = None,
    accepted_artifact: dict[str, Any] | None = None,
    processed_decision_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    replayed = require_mutable(run_dir)
    workflow, workflow_descriptor = latest_document(run_dir, "workflow", replayed)
    state, state_descriptor = latest_document(run_dir, "run-state", replayed)
    source_descriptor = replayed["latest_snapshots"]["source-bundle"]
    transaction_id = str(uuid.uuid4())
    checkpoint_id = str(uuid.uuid4())
    checkpoint = {
        "schema_version": 2,
        "checkpoint_id": checkpoint_id,
        "run_id": workflow["run_id"],
        "checkpoint_type": CHECKPOINT_TYPES.get(checkpoint_type, checkpoint_type),
        "created_at": utc_now(),
        "transaction_id": transaction_id,
        "state_sha256": state_descriptor["canonical_sha256"],
        "source_bundle_sha256": source_descriptor["canonical_sha256"],
        "baseline_semantic_digest": baseline_semantic_digest,
        "semantic_plan_sha256": semantic_plan_sha256,
        "semantic_delta_sha256": semantic_delta_sha256,
        "accepted_artifact": copy.deepcopy(accepted_artifact),
        "allowed_decisions": list(allowed_decisions),
        "context": copy.deepcopy(context),
        "previous_snapshot_sha256": None,
    }
    sequence = replayed["event_count"] + 1
    prior_checkpoint = replayed["latest_snapshots"].get("checkpoint")
    checkpoint_descriptor = _write_next_snapshot(
        run_dir, kind="checkpoint", document=checkpoint, transaction_id=transaction_id,
        predecessor=prior_checkpoint, sequence=sequence,
    )
    state["status"] = "awaiting_decision" if checkpoint_type == "semantic_approval" else "awaiting_feedback"
    if checkpoint_type == "final_acceptance":
        state["status"] = "final_review"
    state["checkpoint_id"] = checkpoint_id
    state["updated_at"] = utc_now()
    next_state_descriptor = _write_next_snapshot(
        run_dir, kind="run-state", document=state, transaction_id=transaction_id,
        predecessor=state_descriptor, sequence=sequence,
    )
    workflow["state_snapshot"] = next_state_descriptor
    workflow["latest_checkpoint"] = checkpoint_descriptor
    next_workflow_descriptor = _write_next_snapshot(
        run_dir, kind="workflow", document=workflow, transaction_id=transaction_id,
        predecessor=workflow_descriptor, sequence=sequence,
    )
    _append_event(
        run_dir, run_id=workflow["run_id"], event_type="checkpoint_created",
        transaction_id=transaction_id,
        snapshots=[checkpoint_descriptor, next_state_descriptor, next_workflow_descriptor],
        payload={
            "checkpoint_id": checkpoint_id,
            "checkpoint_type": checkpoint_type,
            "allowed_decisions": checkpoint["allowed_decisions"],
            "processed_decision_id": processed_decision_id,
        },
    )
    return checkpoint, checkpoint_descriptor


def mark_decision_processed(
    run_dir: Path | str, *, decision_id: str, outcome: str,
) -> bool:
    """Append one durable processed marker after an outcome is already durable."""
    replayed = require_mutable(run_dir)
    workflow, _ = latest_document(run_dir, "workflow", replayed)
    committed = any(
        event_record["event"].get("event_type") == "decision_committed"
        and event_record["event"].get("payload", {}).get("decision_id")
        == decision_id
        for event_record in replayed["events"]
    )
    if not committed:
        raise ContractError(
            "decision.processed_without_commit",
            "cannot mark an unknown decision as processed",
        )
    for event_record in replayed["events"]:
        event = event_record["event"]
        if (
            event.get("event_type") == "decision_processed"
            and event.get("payload", {}).get("decision_id") == decision_id
        ):
            return False
        if (
            event.get("event_type") == "checkpoint_created"
            and event.get("payload", {}).get("processed_decision_id")
            == decision_id
        ):
            return False
    _append_event(
        run_dir,
        run_id=workflow["run_id"],
        event_type="decision_processed",
        transaction_id=str(uuid.uuid4()),
        snapshots=[],
        payload={"decision_id": decision_id, "outcome": outcome},
        actor={"kind": "system", "id": "diagram-orchestrator"},
    )
    return True


def record_tool_attempt(
    run_dir: Path | str,
    *,
    tool: str,
    attempt_id: str,
    status: str,
    artifact_snapshots: dict[str, dict[str, Any]] | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one hash-bound tool attempt without inventing a snapshot schema."""
    if status not in {"started", "completed", "failed", "skipped"}:
        raise ValueError(f"unsupported tool attempt status {status!r}")
    replayed = require_mutable(run_dir)
    workflow, _ = latest_document(run_dir, "workflow", replayed)
    artifacts = copy.deepcopy(artifact_snapshots or {})
    for name, descriptor in artifacts.items():
        if not isinstance(name, str) or not name:
            raise ValueError("tool attempt artifact names must be non-empty strings")
        path = contained_path(Path(run_dir).resolve(), descriptor.get("path", ""))
        expected = make_file_descriptor(path, root=run_dir)
        if descriptor != expected:
            raise ContractError(
                "tool_attempt.artifact_descriptor_invalid",
                f"tool attempt artifact descriptor differs from {name!r}",
            )
    return _append_event(
        run_dir,
        run_id=workflow["run_id"],
        event_type="tool_attempt",
        transaction_id=str(uuid.uuid4()),
        snapshots=[],
        payload={
            "tool": tool,
            "attempt_id": attempt_id,
            "status": status,
            "artifact_snapshots": artifacts,
            **copy.deepcopy(payload or {}),
        },
        actor={"kind": "tool", "id": tool},
    )


def record_candidate_evidence(
    run_dir: Path | str,
    *,
    attempt_id: str,
    accepted: bool,
    artifact_snapshots: dict[str, dict[str, Any]],
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append immutable accepted/rejected layout evidence to the v2 ledger."""
    replayed = require_mutable(run_dir)
    workflow, _ = latest_document(run_dir, "workflow", replayed)
    artifacts = copy.deepcopy(artifact_snapshots)
    for name, descriptor in artifacts.items():
        path = contained_path(Path(run_dir).resolve(), descriptor.get("path", ""))
        if descriptor != make_file_descriptor(path, root=run_dir):
            raise ContractError(
                "candidate.artifact_descriptor_invalid",
                f"candidate artifact descriptor differs from {name!r}",
            )
    return _append_event(
        run_dir,
        run_id=workflow["run_id"],
        event_type="candidate_accepted" if accepted else "candidate_rejected",
        transaction_id=str(uuid.uuid4()),
        snapshots=[],
        payload={
            "attempt_id": attempt_id,
            "artifact_snapshots": artifacts,
            **copy.deepcopy(payload or {}),
        },
        actor={"kind": "system", "id": "diagram-orchestrator"},
    )


def commit_decision(
    run_dir: Path | str,
    *,
    decision: str,
    feedback: str,
    decision_id: str | None = None,
    actor_id: str = "user",
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    replayed = require_mutable(run_dir)
    workflow, workflow_descriptor = latest_document(run_dir, "workflow", replayed)
    state, state_descriptor = latest_document(run_dir, "run-state", replayed)
    checkpoint, checkpoint_descriptor = latest_document(run_dir, "checkpoint", replayed)
    if workflow.get("latest_checkpoint", {}).get("canonical_sha256") != checkpoint_descriptor["canonical_sha256"]:
        raise ContractError("decision.checkpoint_stale", "workflow no longer points to this checkpoint")
    effective_id = decision_id or (
        "decision-"
        + canonical_json_sha256({
            "run_id": workflow["run_id"],
            "checkpoint_sha256": checkpoint_descriptor["canonical_sha256"],
            "decision": decision,
            "feedback": feedback,
        })[:24]
    )
    for event in replayed["events"]:
        if event["event"].get("event_type") != "decision_committed":
            continue
        if event["event"].get("payload", {}).get("decision_id") == effective_id:
            existing_descriptor = event["event"]["snapshots"][0]
            existing = _read_descriptor_document(run_dir, existing_descriptor)
            expected_payload = {
                "run_id": workflow["run_id"],
                "checkpoint_id": checkpoint["checkpoint_id"],
                "decision": decision,
                "feedback": feedback or None,
            }
            mismatches = [key for key, value in expected_payload.items() if existing.get(key) != value]
            if mismatches:
                raise ContractError(
                    "decision.id_collision",
                    "existing decision_id is bound to different payload fields: " + ", ".join(mismatches),
                )
            return existing, existing_descriptor, True
    if decision not in checkpoint["allowed_decisions"]:
        raise ContractError("decision.not_allowed", f"decision {decision!r} is not executable at this checkpoint")
    source_descriptor = replayed["latest_snapshots"]["source-bundle"]
    transaction_id = str(uuid.uuid4())
    value = {
        "schema_version": 2,
        "decision_id": effective_id,
        "run_id": workflow["run_id"],
        "checkpoint_id": checkpoint["checkpoint_id"],
        "decision": decision,
        "feedback": feedback or None,
        "decided_at": utc_now(),
        "actor": {"kind": "human", "id": actor_id},
        "bindings": {
            "checkpoint_sha256": checkpoint_descriptor["canonical_sha256"],
            "state_sha256": state_descriptor["canonical_sha256"],
            "source_bundle_sha256": source_descriptor["canonical_sha256"],
            "baseline_semantic_digest": checkpoint.get("baseline_semantic_digest"),
            "semantic_plan_sha256": checkpoint.get("semantic_plan_sha256"),
            "semantic_delta_sha256": checkpoint.get("semantic_delta_sha256"),
        },
        "transaction_id": transaction_id,
        "previous_snapshot_sha256": None,
    }
    sequence = replayed["event_count"] + 1
    prior_decision = replayed["latest_snapshots"].get("decision")
    descriptor = _write_next_snapshot(
        run_dir, kind="decision", document=value, transaction_id=transaction_id,
        predecessor=prior_decision, sequence=sequence,
    )
    _append_event(
        run_dir, run_id=workflow["run_id"], event_type="decision_committed",
        transaction_id=transaction_id, snapshots=[descriptor],
        payload={"decision_id": effective_id, "checkpoint_id": checkpoint["checkpoint_id"], "decision": decision},
        actor={"kind": "human", "id": actor_id},
    )
    # The decision snapshot is the authorization record.  The following source
    # revision makes the same actual human input available to downstream roles
    # without changing what the decision itself was hash-bound to.
    revise_sources(
        run_dir,
        new_sources=[source_record(
            source_id=f"human-decision-{effective_id}",
            kind="explicit_user_decision",
            uri=f"urn:diagram-run:{workflow['run_id']}:decision:{effective_id}",
            content={
                "decision_id": effective_id,
                "checkpoint_id": checkpoint["checkpoint_id"],
                "decision": decision,
                "feedback": feedback or None,
                "decided_at": value["decided_at"],
            },
        )],
        event_payload={"kind": "human_decision", "decision_id": effective_id},
    )
    return value, descriptor, False


def record_lock_recovery(
    run_dir: Path | str,
    recovery_records: Iterable[dict[str, Any]],
) -> None:
    """Append immutable evidence for stale lock recovery under the held lock."""
    records = [copy.deepcopy(record) for record in recovery_records]
    if not records:
        return
    replayed = require_mutable(run_dir)
    workflow, _ = latest_document(run_dir, "workflow", replayed)
    for record in records:
        _append_event(
            run_dir,
            run_id=workflow["run_id"],
            event_type="recovery",
            transaction_id=str(uuid.uuid4()),
            snapshots=[],
            payload={"kind": "stale_run_lock", "record": record},
            actor={"kind": "system", "id": "run-lock-v2"},
        )


def create_semantic_approval_from_decision(
    run_dir: Path | str,
    *,
    decision: dict[str, Any],
) -> tuple[dict[str, Any], Path]:
    """Serialize actual human authorization; the host never invents an actor."""
    replayed = require_mutable(run_dir, decision["run_id"])
    for event_record in replayed["events"]:
        event = event_record["event"]
        if (
            event.get("event_type") != "semantic_approval"
            or event.get("payload", {}).get("decision_id")
            != decision["decision_id"]
        ):
            continue
        descriptor = event["payload"].get("approval") or {}
        existing_path = contained_path(run_dir, descriptor.get("path", ""))
        if (
            not existing_path.is_file()
            or file_sha256(existing_path) != descriptor.get("sha256")
        ):
            raise ContractError(
                "semantic_approval.replay_evidence_changed",
                "the existing semantic approval artifact is missing or changed",
            )
        existing = json.loads(existing_path.read_text(encoding="utf-8"))
        require_valid_contract(existing, "semantic-approval", 2)
        if existing.get("decision_id") != decision["decision_id"]:
            raise ContractError(
                "semantic_approval.replay_mismatch",
                "the existing semantic approval belongs to another decision",
            )
        return existing, existing_path
    checkpoint, checkpoint_descriptor = latest_document(run_dir, "checkpoint", replayed)
    if checkpoint["checkpoint_type"] != "semantic_approval":
        raise ContractError("semantic_approval.checkpoint_invalid", "semantic approval requires a semantic_approval checkpoint")
    if decision["checkpoint_id"] != checkpoint["checkpoint_id"]:
        raise ContractError("semantic_approval.checkpoint_mismatch", "decision belongs to another checkpoint")
    if decision["bindings"]["checkpoint_sha256"] != checkpoint_descriptor["canonical_sha256"]:
        raise ContractError("semantic_approval.binding_mismatch", "decision is not hash-bound to the semantic checkpoint")
    required_bindings = (
        "baseline_semantic_digest", "semantic_plan_sha256",
        "source_bundle_sha256", "semantic_delta_sha256",
    )
    missing = [key for key in required_bindings if not decision["bindings"].get(key)]
    if missing:
        raise ContractError("semantic_approval.binding_missing", f"semantic decision lacks bindings: {', '.join(missing)}")
    if decision["decision"] not in {"continue", "reject"}:
        raise ContractError("semantic_approval.decision_invalid", "only continue or reject can resolve semantic approval")
    value = {
        "schema_version": 2,
        "approval_id": str(uuid.uuid4()),
        "decision_id": decision["decision_id"],
        "run_id": decision["run_id"],
        "checkpoint_id": decision["checkpoint_id"],
        "decision": "approve" if decision["decision"] == "continue" else "reject",
        "decided_at": decision["decided_at"],
        "approver": copy.deepcopy(decision["actor"]),
        "baseline_semantic_digest": decision["bindings"]["baseline_semantic_digest"],
        "semantic_plan_sha256": decision["bindings"]["semantic_plan_sha256"],
        "source_bundle_sha256": decision["bindings"]["source_bundle_sha256"],
        "semantic_delta_sha256": decision["bindings"]["semantic_delta_sha256"],
    }
    require_valid_contract(value, "semantic-approval", 2)
    path = control_dir(run_dir) / "approvals" / f"{value['approval_id']}.json"
    atomic_write_bytes(path, canonical_json_bytes(value) + b"\n")
    _append_event(
        run_dir, run_id=value["run_id"], event_type="semantic_approval",
        transaction_id=str(uuid.uuid4()), snapshots=[],
        payload={
            "approval_id": value["approval_id"],
            "decision_id": value["decision_id"],
            "approval": make_file_descriptor(path, root=run_dir),
            "semantic_delta_sha256": value["semantic_delta_sha256"],
        },
        actor=value["approver"],
    )
    return value, path


def make_file_descriptor(path: Path | str, *, root: Path | str) -> dict[str, Any]:
    resolved = contained_path(root, path)
    return {
        "path": resolved.relative_to(Path(root).resolve()).as_posix(),
        "sha256": file_sha256(resolved),
        "byte_length": resolved.stat().st_size,
    }


def mirror_validation_receipt(
    run_dir: Path | str,
    *,
    legacy_receipt_path: Path | str,
) -> tuple[dict[str, Any], Path]:
    run_root = Path(run_dir).resolve()
    replayed = require_mutable(run_root)
    legacy_path = contained_path(run_root, legacy_receipt_path)
    legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
    attempt_root = legacy_path.parent
    source_descriptor = replayed["latest_snapshots"]["source-bundle"]
    implementation_descriptor = replayed["latest_snapshots"]["implementation-snapshot"]
    artifact_path = contained_path(run_root, legacy["artifact"]["path"])
    captured_artifact_path = attempt_root / "validated-artifact.drawio"
    atomic_write_bytes(captured_artifact_path, artifact_path.read_bytes())
    if file_sha256(captured_artifact_path) != legacy["artifact"]["sha256"]:
        raise ContractError("receipt.artifact_hash_mismatch", "captured validation artifact differs from the legacy receipt")
    report_path = contained_path(attempt_root, legacy["outputs"]["report"]["path"])
    stdout_path = attempt_root / "validator.stdout"
    stderr_path = attempt_root / "validator.stderr"
    extension_root = Path(__file__).resolve().parent.parent
    validator_path = contained_path(extension_root, legacy["validator"]["path"])
    value = {
        "schema_version": 2,
        "receipt_id": legacy["receipt_id"],
        "run_id": legacy["run_id"],
        "attempt_id": legacy["attempt_id"],
        "attempt_dir": attempt_root.relative_to(run_root).as_posix(),
        "started_at": legacy["started_at"],
        "finished_at": legacy["finished_at"],
        "strict": True,
        "artifact": make_file_descriptor(captured_artifact_path, root=attempt_root),
        "command": legacy["command"],
        "exit_code": legacy["exit_code"],
        "validator": {
            "name": legacy["validator"]["name"],
            "version": legacy["validator"]["version"],
            "path": validator_path.relative_to(extension_root).as_posix(),
            "file_sha256": file_sha256(validator_path),
        },
        "bindings": {
            "implementation_snapshot_sha256": implementation_descriptor["canonical_sha256"],
            "source_bundle_sha256": source_descriptor["canonical_sha256"],
            "candidate_sha256": file_sha256(captured_artifact_path),
        },
        "outputs": {
            "report": make_file_descriptor(report_path, root=attempt_root),
            "stdout": make_file_descriptor(stdout_path, root=attempt_root),
            "stderr": make_file_descriptor(stderr_path, root=attempt_root),
        },
        "result": legacy["result"],
    }
    require_valid_contract(value, "validation-receipt", 2)
    destination = attempt_root / "validation-receipt.v2.json"
    atomic_write_bytes(destination, canonical_json_bytes(value) + b"\n")
    return value, destination


def verify_v2_receipt(run_dir: Path | str, receipt_path: Path | str) -> dict[str, Any]:
    run_root = Path(run_dir).resolve()
    replayed = require_mutable(run_root)
    receipt = json.loads(contained_path(run_root, receipt_path).read_text(encoding="utf-8"))
    workflow, _ = latest_document(run_root, "workflow", replayed)
    implementation, _ = latest_document(run_root, "implementation-snapshot", replayed)
    trusted = implementation["trusted_validator"]
    result = verify_validation_receipt(
        receipt, run_dir=run_root, trusted_validator=trusted,
        extension_root=Path(__file__).resolve().parent.parent,
        expected_run_id=workflow["run_id"],
    )
    bindings = receipt.get("bindings") if isinstance(receipt, dict) else None
    if isinstance(bindings, dict):
        historical = {
            "implementation-snapshot": set(),
            "source-bundle": set(),
        }
        for event_record in replayed["events"]:
            for descriptor in event_record["event"]["snapshots"]:
                if descriptor["schema_kind"] in historical:
                    historical[descriptor["schema_kind"]].add(descriptor["canonical_sha256"])
        for field, kind in (
            ("implementation_snapshot_sha256", "implementation-snapshot"),
            ("source_bundle_sha256", "source-bundle"),
        ):
            if bindings.get(field) not in historical[kind]:
                result["diagnostics"].append({
                    "code": "receipt.lifecycle_binding_mismatch",
                    "pointer": f"/bindings/{field}",
                    "message": f"receipt binding does not identify an event-bound {kind} snapshot",
                })
    integrity_valid = not result["diagnostics"]
    result["valid"] = integrity_valid
    result["integrity_valid"] = integrity_valid
    result["strict_passed"] = bool(
        integrity_valid
        and receipt.get("strict") is True
        and receipt.get("result") == "passed"
        and receipt.get("exit_code") == 0
    )
    return result


BEST_EFFORT_LAYOUT_CODES = {
    "artifact.readability.crossing",
    "artifact.readability.route_through",
    "artifact.readability.overlap",
    "artifact.readability.text_overflow",
    "artifact.layout.container_overflow",
    "artifact.layout.container_overlap",
    "artifact.layout.lane_size",
    "artifact.layout.lane_title_collision",
    "artifact.layout.routing_uncertain",
    "artifact.layout.terminal_segment",
}


def verify_best_effort_candidate(
    run_dir: Path | str, *, artifact: Path | str, report: Path | str,
    receipt: Path | str, reviewer_verdict: Path | str | None = None,
    require_accepted_binding: bool = False,
) -> dict[str, Any]:
    """Classify degraded delivery without relaxing structural integrity."""
    run_root = Path(run_dir).resolve()
    diagnostics = []
    try:
        artifact_path = contained_path(run_root, artifact)
        report_path = contained_path(run_root, report)
        receipt_path = contained_path(run_root, receipt)
    except (ContractError, OSError, ValueError) as exc:
        return {
            "safe": False,
            "strict_passed": False,
            "artifact_sha256": None,
            "report_sha256": None,
            "receipt_sha256": None,
            "reviewer_status": "not_run",
            "findings": [],
            "reviewer_findings": [],
            "diagnostics": [{
                "code": "best_effort.evidence_path_invalid",
                "message": str(exc),
            }],
        }
    if not artifact_path.is_file():
        diagnostics.append({"code": "best_effort.artifact_missing"})
    else:
        try:
            ET.fromstring(artifact_path.read_bytes())
        except (OSError, ET.ParseError) as exc:
            diagnostics.append({
                "code": "best_effort.xml_invalid", "message": str(exc),
            })
    report_value = {}
    try:
        report_value = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        diagnostics.append({
            "code": "best_effort.report_invalid", "message": str(exc),
        })
    artifact_sha = file_sha256(artifact_path) if artifact_path.is_file() else None
    if report_value.get("artifact_sha256") != artifact_sha:
        diagnostics.append({"code": "best_effort.report_artifact_mismatch"})
    receipt_check = verify_v2_receipt(run_root, receipt_path)
    if not receipt_check.get("integrity_valid"):
        diagnostics.append({
            "code": "best_effort.receipt_invalid",
            "details": receipt_check.get("diagnostics", []),
        })
    try:
        receipt_value = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        receipt_value = {}
    if (receipt_value.get("bindings") or {}).get("candidate_sha256") != artifact_sha:
        diagnostics.append({"code": "best_effort.receipt_artifact_mismatch"})
    if require_accepted_binding:
        try:
            replayed = require_mutable(run_root)
            state, _ = latest_document(run_root, "run-state", replayed)
            accepted = state.get("accepted_artifact") or {}
            accepted_report = state.get("validation_report") or {}
            accepted_receipt = state.get("validation_receipt") or {}
            if any((
                accepted.get("sha256") != artifact_sha,
                accepted_report.get("sha256")
                != (file_sha256(report_path) if report_path.is_file() else None),
                accepted_receipt.get("sha256")
                != (file_sha256(receipt_path) if receipt_path.is_file() else None),
            )):
                diagnostics.append({
                    "code": "best_effort.accepted_state_binding_mismatch",
                })
        except (ContractError, OSError, KeyError, json.JSONDecodeError) as exc:
            diagnostics.append({
                "code": "best_effort.accepted_state_invalid",
                "message": str(exc),
            })
    findings = copy.deepcopy(report_value.get("findings", []))
    unsafe_findings = [
        item for item in findings
        if (
            item.get("remediation_class") == "structural"
            or item.get("layer") in {"artifact-parse", "round-trip"}
            or (
                item.get("severity") == "error"
                and item.get("code") not in BEST_EFFORT_LAYOUT_CODES
            )
        )
    ]
    if unsafe_findings:
        diagnostics.append({
            "code": "best_effort.unsafe_validator_findings",
            "finding_ids": [item.get("finding_id") for item in unsafe_findings],
        })
    reviewer_findings = []
    reviewer_status = "not_run"
    if reviewer_verdict is not None:
        reviewer_path = contained_path(run_root, reviewer_verdict)
        try:
            reviewer_value = json.loads(reviewer_path.read_text(encoding="utf-8"))
            require_valid_contract(reviewer_value, "reviewer-verdict", 2)
            reviewer_findings = copy.deepcopy(reviewer_value.get("findings", []))
            reviewer_status = "completed"
            bindings = reviewer_value.get("bindings") or {}
            if any((
                reviewer_value.get("run_id") != receipt_value.get("run_id"),
                bindings.get("candidate_sha256") != artifact_sha,
                bindings.get("report_sha256") != file_sha256(report_path),
                bindings.get("receipt_sha256") != file_sha256(receipt_path),
            )):
                diagnostics.append({"code": "best_effort.reviewer_binding_mismatch"})
            reviewer_blockers = [
                item for item in reviewer_findings
                if item.get("category") in {"integrity", "semantic"}
                and item.get("severity") == "error"
            ]
            if reviewer_blockers:
                diagnostics.append({
                    "code": "best_effort.reviewer_blocked",
                    "finding_ids": [
                        item.get("finding_id") for item in reviewer_blockers
                    ],
                })
        except (OSError, json.JSONDecodeError, ContractError) as exc:
            reviewer_status = "invalid"
            diagnostics.append({
                "code": "best_effort.reviewer_invalid", "message": str(exc),
            })
    return {
        "safe": not diagnostics,
        "strict_passed": bool(receipt_check.get("strict_passed")),
        "artifact_sha256": artifact_sha,
        "report_sha256": file_sha256(report_path) if report_path.is_file() else None,
        "receipt_sha256": file_sha256(receipt_path) if receipt_path.is_file() else None,
        "reviewer_status": reviewer_status,
        "findings": findings,
        "reviewer_findings": reviewer_findings,
        "diagnostics": diagnostics,
    }


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _advance_publication(
    run_dir: Path | str,
    publication: dict[str, Any],
    *,
    status: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    replayed = require_mutable(run_dir, publication["run_id"])
    workflow, workflow_descriptor = latest_document(run_dir, "workflow", replayed)
    state, state_descriptor = latest_document(run_dir, "run-state", replayed)
    prior_publication = replayed["latest_snapshots"].get("publication-transaction")
    transaction_id = str(uuid.uuid4())
    value = copy.deepcopy(publication)
    value["status"] = status
    value["updated_at"] = utc_now()
    value["state_sha256"] = state_descriptor["canonical_sha256"]
    sequence = replayed["event_count"] + 1
    publication_descriptor = _write_next_snapshot(
        run_dir, kind="publication-transaction", document=value,
        transaction_id=transaction_id, predecessor=prior_publication,
        sequence=sequence,
    )
    state["status"] = (
        "publication_conflict" if status == "conflict"
        else "completed" if status == "committed" and value["decision"] == "approve"
        else "best_effort_completed" if status == "committed" and value["decision"] == "best_effort"
        else "approved_with_findings" if status == "committed"
        else "publication_pending"
    )
    state["publication_transaction_id"] = value["publication_id"]
    state["updated_at"] = utc_now()
    next_state_descriptor = _write_next_snapshot(
        run_dir, kind="run-state", document=state,
        transaction_id=transaction_id, predecessor=state_descriptor,
        sequence=sequence,
    )
    workflow["state_snapshot"] = next_state_descriptor
    workflow["publication_transaction"] = publication_descriptor
    next_workflow_descriptor = _write_next_snapshot(
        run_dir, kind="workflow", document=workflow,
        transaction_id=transaction_id, predecessor=workflow_descriptor,
        sequence=sequence,
    )
    _append_event(
        run_dir, run_id=value["run_id"], event_type=event_type,
        transaction_id=transaction_id,
        snapshots=[publication_descriptor, next_state_descriptor, next_workflow_descriptor],
        payload={"publication_id": value["publication_id"], "status": status, **(payload or {})},
        actor={"kind": "system", "id": "transactional-publication"},
    )
    return value, publication_descriptor


def _publication_conflict(run_dir: Path | str, publication: dict[str, Any], message: str) -> None:
    if publication.get("status") != "conflict":
        publication, _ = _advance_publication(
            run_dir, publication, status="conflict", event_type="publication_conflict",
            payload={"message": message},
        )
    raise ContractError("publication.conflict", message, pointer="/target_path")


def _validate_publication_evidence(
    run_dir: Path, publication: dict[str, Any], *, require_current_source: bool = True,
) -> tuple[Path, Path, Path]:
    replayed = require_mutable(run_dir, publication["run_id"])
    accepted = contained_path(run_dir, publication["accepted_artifact"]["path"])
    report = contained_path(run_dir, publication["validation_report"]["path"])
    receipt = contained_path(run_dir, publication["validation_receipt"]["path"])
    reviewer = (
        contained_path(run_dir, publication["reviewer_verdict"]["path"])
        if publication.get("reviewer_verdict") else None
    )
    evidence_files = [
        (accepted, publication["accepted_artifact"]["sha256"], "publication.accepted_changed"),
        (report, publication["validation_report"]["sha256"], "publication.report_changed"),
        (receipt, publication["validation_receipt"]["sha256"], "publication.receipt_changed"),
    ]
    if reviewer is not None:
        evidence_files.append(
            (reviewer, publication["reviewer_verdict"]["sha256"], "publication.reviewer_changed")
        )
    for path, expected, code in evidence_files:
        if not path.is_file() or file_sha256(path) != expected:
            raise ContractError(code, f"publication evidence changed: {path}")
    report_value = json.loads(report.read_text(encoding="utf-8"))
    receipt_value = json.loads(receipt.read_text(encoding="utf-8"))
    if publication["decision"] == "best_effort":
        classification = verify_best_effort_candidate(
            run_dir,
            artifact=accepted,
            report=report,
            receipt=receipt,
            reviewer_verdict=reviewer,
            require_accepted_binding=True,
        )
        if not classification["safe"]:
            raise ContractError(
                "publication.best_effort_unsafe",
                f"best-effort evidence is unsafe: {classification['diagnostics']}",
            )
        if publication.get("strict_passed") != classification["strict_passed"]:
            raise ContractError(
                "publication.strict_result_mismatch",
                "publication strict_passed differs from best-effort evidence",
            )
        if require_current_source:
            current_source = replayed["latest_snapshots"]["source-bundle"]["canonical_sha256"]
            if current_source != publication["source_bundle_sha256"]:
                raise ContractError(
                    "publication.source_changed",
                    "source bundle changed after publication preparation",
                )
        return accepted, report, receipt
    if publication["decision"] == "approve_with_findings" and any(
        item.get("severity") == "error"
        for item in report_value.get("findings", [])
    ):
        raise ContractError(
            "publication.structural_findings_forbidden",
            "approve_with_findings requires strict pass with warnings only",
        )
    if receipt_value.get("result") != "passed":
        raise ContractError(
            "publication.strict_validation_required",
            (
                "approve requires a passed strict receipt"
                if publication["decision"] == "approve"
                else "approve_with_findings requires a passed strict receipt"
            ),
        )
    receipt_verification = verify_v2_receipt(run_dir, receipt)
    if not receipt_verification["valid"]:
        raise ContractError(
            "publication.receipt_invalid",
            f"validation receipt failed immediately before publication: {receipt_verification['diagnostics']}",
        )
    if publication.get("strict_passed") is not None and publication["strict_passed"] != receipt_verification["strict_passed"]:
        raise ContractError("publication.strict_result_mismatch", "publication strict_passed differs from verified receipt result")
    if not receipt_verification["strict_passed"]:
        raise ContractError(
            "publication.strict_validation_required",
            "publication requires a passed strict validation receipt",
        )
    if receipt_value["bindings"]["candidate_sha256"] != file_sha256(accepted):
        raise ContractError("publication.receipt_candidate_mismatch", "receipt is not bound to accepted artifact")
    structural_errors = [
        item for item in report_value.get("findings", [])
        if item.get("severity") == "error" and (
            item.get("remediation_class") == "structural"
            or item.get("layer") == "artifact-parse"
            or str(item.get("code", "")).startswith((
                "artifact.structure.", "artifact.id.", "artifact.cell.",
                "artifact.geometry.invalid", "artifact.edge.dangling",
            ))
        )
    ]
    reviewer_value = None
    if reviewer is None:
        raise ContractError(
            "publication.reviewer_approval_required",
            "publication requires a hash-bound Reviewer approve verdict",
        )
    if reviewer is not None:
        reviewer_value = json.loads(reviewer.read_text(encoding="utf-8"))
        require_valid_contract(reviewer_value, "reviewer-verdict", 2)
        if any((
            reviewer_value["run_id"] != publication["run_id"],
            reviewer_value["bindings"]["candidate_sha256"] != file_sha256(accepted),
            reviewer_value["bindings"]["report_sha256"] != file_sha256(report),
            reviewer_value["bindings"]["receipt_sha256"] != file_sha256(receipt),
        )):
            raise ContractError("publication.reviewer_binding_mismatch", "Reviewer verdict differs from publication evidence")
        reviewer_blockers = [
            item for item in reviewer_value["findings"]
            if item.get("severity") == "error" or item.get("category") == "integrity"
        ]
        if reviewer_blockers:
            raise ContractError("publication.reviewer_findings_forbidden", "Reviewer integrity/error findings forbid publication")
        if reviewer_value["verdict"] != "approve":
            raise ContractError(
                "publication.reviewer_approval_required",
                f"{publication['decision']} requires Reviewer approve",
            )
    error_findings = [
        item for item in report_value.get("findings", [])
        if item.get("severity") == "error"
    ]
    if publication["decision"] == "approve_with_findings" and (structural_errors or error_findings):
        raise ContractError(
            "publication.structural_findings_forbidden",
            "approve_with_findings requires strict pass with warnings only",
        )
    if require_current_source:
        current_source = replayed["latest_snapshots"]["source-bundle"]["canonical_sha256"]
        if current_source != publication["source_bundle_sha256"]:
            raise ContractError("publication.source_changed", "source bundle changed after publication preparation")
    return accepted, report, receipt


def _continue_publication(run_dir: Path, publication: dict[str, Any]) -> dict[str, Any]:
    replayed = require_mutable(run_dir, publication["run_id"])
    workflow, _ = latest_document(run_dir, "workflow", replayed)
    workspace = Path(workflow["workspace"]).resolve()
    target = contained_path(workspace, workspace / publication["target_path"])
    accepted, _, _ = _validate_publication_evidence(run_dir, publication)
    staging = contained_path(workspace, workspace / publication["staging_path"])
    backup = (
        contained_path(run_dir, run_dir / publication["backup_path"])
        if publication.get("backup_path") else None
    )

    if publication["status"] == "committed":
        if not target.is_file() or file_sha256(target) != publication["published_sha256"]:
            raise ContractError("publication.committed_target_changed", "published target no longer matches committed hash")
        return publication
    if publication["status"] == "conflict":
        target_before = file_sha256(target) if target.is_file() else None
        conflict_resolved = (
            publication["mode"] == "create" and not target.exists()
        ) or (
            publication["mode"] == "improve"
            and target_before == publication["target_before_sha256"]
        )
        if not conflict_resolved:
            raise ContractError("publication.conflict", "publication is waiting for target conflict resolution")
        publication, _ = _advance_publication(
            run_dir, publication, status="prepared", event_type="recovery",
            payload={"reason": "target_conflict_resolved"},
        )

    if publication["status"] == "prepared":
        if staging.exists():
            if not staging.is_file() or file_sha256(staging) != publication["accepted_artifact"]["sha256"]:
                _publication_conflict(run_dir, publication, "publication staging path contains different bytes")
        else:
            staging.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(staging, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                with open(accepted, "rb") as source:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        pending = memoryview(chunk)
                        while pending:
                            pending = pending[os.write(descriptor, pending):]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        publication, _ = _advance_publication(
            run_dir, publication, status="staged", event_type="publication_staged",
            payload={"staging_sha256": file_sha256(staging)},
        )

    if publication["status"] == "staged":
        # A crash may occur after the atomic target update but before the
        # bytes_published event. Detect that exact state and finish the journal.
        already_written = (
            target.is_file()
            and file_sha256(target) == publication["accepted_artifact"]["sha256"]
            and not staging.exists()
        )
        if not already_written:
            _validate_publication_evidence(run_dir, publication)
            if publication["mode"] == "create":
                if target.exists():
                    _publication_conflict(run_dir, publication, "create target appeared before publication")
                try:
                    os.link(staging, target)
                except FileExistsError:
                    _publication_conflict(run_dir, publication, "create target appeared during publication")
                staging.unlink(missing_ok=True)
            else:
                if not target.is_file() or file_sha256(target) != publication["target_before_sha256"]:
                    _publication_conflict(run_dir, publication, "improve target changed after run start")
                if backup is None:
                    raise ContractError("publication.backup_missing", "improve publication has no rollback copy")
                atomic_write_bytes(backup, target.read_bytes())
                os.replace(staging, target)
            _fsync_directory(target.parent)
        if not target.is_file() or file_sha256(target) != publication["accepted_artifact"]["sha256"]:
            if backup is not None and backup.is_file():
                os.replace(backup, target)
                _fsync_directory(target.parent)
            raise ContractError("publication.target_verification_failed", "target bytes differ after atomic publication")
        publication["published_sha256"] = file_sha256(target)
        publication, _ = _advance_publication(
            run_dir, publication, status="bytes_published",
            event_type="publication_bytes_written",
            payload={"target_sha256": publication["published_sha256"]},
        )

    if publication["status"] == "bytes_published":
        if not target.is_file() or file_sha256(target) != publication["published_sha256"]:
            raise ContractError("publication.target_changed", "target changed before publication commit")
        publication, _ = _advance_publication(
            run_dir, publication, status="committed", event_type="publication_committed",
            payload={"target_sha256": publication["published_sha256"], "decision": publication["decision"]},
        )
    staging.unlink(missing_ok=True)
    return publication


def publish_transaction(
    run_dir: Path | str,
    *,
    accepted_artifact: Path | str,
    validation_report: Path | str,
    validation_receipt: Path | str,
    reviewer_verdict: Path | str | None = None,
    unresolved_findings: Iterable[dict[str, Any]] = (),
    decision: str,
    target_override: Path | str | None = None,
) -> dict[str, Any]:
    run_root = Path(run_dir).resolve()
    replayed = require_mutable(run_root)
    workflow, _ = latest_document(run_root, "workflow", replayed)
    state, state_descriptor = latest_document(run_root, "run-state", replayed)
    if workflow["mode"] not in {"create", "improve"}:
        raise ContractError("publication.mode_invalid", "read-only review runs cannot publish")
    if decision not in {"approve", "approve_with_findings", "best_effort"}:
        raise ContractError("publication.decision_invalid", f"unsupported publication decision {decision!r}")
    if target_override is not None and decision != "best_effort":
        raise ContractError(
            "publication.target_override_forbidden",
            "only best-effort publication may use a separate target",
        )
    if target_override is not None and workflow["mode"] != "create":
        raise ContractError(
            "publication.target_override_forbidden",
            "a separate best-effort target is only valid for create",
        )
    existing_descriptor = replayed["latest_snapshots"].get("publication-transaction")
    if existing_descriptor is not None:
        existing = _read_descriptor_document(run_root, existing_descriptor)
        supplied_sha = file_sha256(contained_path(run_root, accepted_artifact))
        if existing["decision"] != decision or existing["accepted_artifact"]["sha256"] != supplied_sha:
            raise ContractError("publication.replay_mismatch", "existing publication belongs to another decision or artifact")
        if target_override is not None:
            expected_target = contained_path(
                Path(workflow["workspace"]).resolve(),
                target_override,
            )
            if existing["target_path"] != expected_target.relative_to(
                Path(workflow["workspace"]).resolve()
            ).as_posix():
                raise ContractError(
                    "publication.replay_mismatch",
                    "existing publication belongs to another target",
                )
        return _continue_publication(run_root, existing)

    workspace = Path(workflow["workspace"]).resolve()
    target = contained_path(
        workspace,
        target_override
        if target_override is not None
        else workspace / workflow["target_path"],
    )
    original_target = contained_path(
        workspace,
        workspace / workflow["target_path"],
    )
    if target_override is not None and target == original_target:
        raise ContractError(
            "publication.target_override_forbidden",
            "separate best-effort target must differ from the requested target",
        )
    target_path = target.relative_to(workspace).as_posix()
    target_before = file_sha256(target) if target.is_file() else None
    if target_override is not None and target.exists():
        raise ContractError(
            "publication.conflict",
            "separate best-effort target already exists",
        )
    if target_override is None and workflow["mode"] == "create" and target.exists():
        raise ContractError("publication.conflict", "create target appeared after the run started")
    if target_override is None and workflow["mode"] == "improve" and target_before != workflow["target_initial_sha256"]:
        raise ContractError("publication.conflict", "improve target changed after the run started")
    publication_id = str(uuid.uuid4())
    stage_name = f".{target.name}.{publication_id}.drawio-stage"
    staging_path = (target.parent / stage_name).relative_to(workspace).as_posix()
    backup_path = (
        f"{CONTROL_DIR}/publication/{publication_id}/target-before.drawio"
        if workflow["mode"] == "improve" else None
    )
    value = {
        "schema_version": 2,
        "publication_id": publication_id,
        "run_id": workflow["run_id"],
        "mode": workflow["mode"],
        "decision": decision,
        "status": "prepared",
        "target_path": target_path,
        "target_before_sha256": target_before,
        "accepted_artifact": make_file_descriptor(accepted_artifact, root=run_root),
        "validation_report": make_file_descriptor(validation_report, root=run_root),
        "validation_receipt": make_file_descriptor(validation_receipt, root=run_root),
        "reviewer_verdict": (
            make_file_descriptor(reviewer_verdict, root=run_root)
            if reviewer_verdict is not None else None
        ),
        "strict_passed": verify_v2_receipt(run_root, validation_receipt)["strict_passed"],
        "unresolved_findings": copy.deepcopy(list(unresolved_findings)),
        "source_bundle_sha256": replayed["latest_snapshots"]["source-bundle"]["canonical_sha256"],
        "state_sha256": state_descriptor["canonical_sha256"],
        "staging_path": staging_path,
        "backup_path": backup_path,
        "published_sha256": None,
        "prepared_at": utc_now(),
        "updated_at": utc_now(),
        "transaction_id": str(uuid.uuid4()),
        "previous_snapshot_sha256": None,
    }
    require_valid_contract(value, "publication-transaction", 2)
    _validate_publication_evidence(run_root, value, require_current_source=False)
    value, _ = _advance_publication(
        run_root, value, status="prepared", event_type="publication_prepared",
        payload={"decision": decision, "target_path": target_path},
    )
    return _continue_publication(run_root, value)


def recover_publication(run_dir: Path | str) -> dict[str, Any] | None:
    """Resume an interrupted journal; callers must already hold the run lock."""
    run_root = Path(run_dir).resolve()
    replayed = require_mutable(run_root)
    descriptor = replayed["latest_snapshots"].get("publication-transaction")
    if descriptor is None:
        return None
    publication = _read_descriptor_document(run_root, descriptor)
    if publication["status"] == "committed":
        return publication
    result = _continue_publication(run_root, publication)
    _append_event(
        run_root, run_id=result["run_id"], event_type="recovery",
        transaction_id=str(uuid.uuid4()), snapshots=[],
        payload={"publication_id": result["publication_id"], "status": result["status"]},
    )
    return result
