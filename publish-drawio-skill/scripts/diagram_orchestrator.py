#!/usr/bin/env python3
"""Persisted multi-model create/improve/resume/trace host for draw.io.

Models only return typed plans, patches, and verdicts. This host owns XML
rendering, patch application, validation, comparison, state, and publication.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import jsonschema

import agent_runtime
import command_ux
import diagram_host
import diagram_intake
import diagram_supervisor as supervisor
import layout_backend
import layout_contracts
import layout_model
import layout_renderer
import lifecycle_host_v2 as lifecycle_v2
import renderer_adapters
from diagram_model_v2 import (
    normalize_semantic_delta,
    semantic_analysis_to_plan,
    semantic_delta_sha256,
    semantic_digest,
    SOURCE_PRIORITY,
    validate_diagramspec,
    validate_semantic_analysis,
    validate_semantic_analysis_input,
    validate_semantic_plan,
    with_model_view,
)
from lifecycle_contracts import (
    ContractError,
    atomic_write_bytes,
    canonical_json_bytes,
    canonical_json_sha256,
    require_valid_contract,
)
from implementation_snapshot_v2 import verify_implementation_snapshot
from run_lock_v2 import RunAlreadyLocked, RunLock
from source_bundle_v2 import source_record


ROOT = Path(__file__).resolve().parent.parent
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
INTAKE_ID_RE = re.compile(r"^intake-[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
WORKFLOW_FILE = "workflow.json"
CHECKPOINT_FILE = "pending-checkpoint.json"
DEFAULT_MAX_ITERATIONS = 4
MAX_IDENTICAL_FAILURES = 2
LAYOUT_STRATEGIES = (
    ("elk-default", {"spacing": 1.0, "port_separation": 1.0, "shared_penalty": 1.0}),
    ("elk-spacing", {"spacing": 1.35, "port_separation": 1.0, "shared_penalty": 1.0}),
    ("elk-separated", {"spacing": 1.35, "port_separation": 1.4, "shared_penalty": 1.6}),
    ("python-fallback", {}),
)


def utc_slug(prefix):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:8]}"


def canonical_hash(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _working_artifact(workflow):
    """Return the canonical best working artifact, reading legacy v1 runs."""
    return workflow.get("working_artifact") or workflow.get("accepted_artifact") or {}


def _working_validation(workflow):
    """Return validation for the best working artifact, reading legacy v1 runs."""
    return workflow.get("working_validation") or workflow.get("accepted_validation") or {}


def _sync_working_candidate(workflow, artifact, validation):
    """Persist canonical working state and its backward-compatible v1 mirrors."""
    workflow["working_artifact"] = copy.deepcopy(artifact)
    workflow["working_validation"] = copy.deepcopy(validation)
    workflow["accepted_artifact"] = copy.deepcopy(artifact)
    workflow["accepted_validation"] = copy.deepcopy(validation)


def _clear_publishable_candidate(workflow):
    workflow["publishable_candidate"] = None
    workflow.pop("final_approval_eligibility", None)


def _set_publishable_candidate(workflow, *, artifact, validation, receipt_v2, verdict_v2):
    """Bind a publishable candidate only from strict-pass and Reviewer approve."""
    if not validation.get("strict_passed"):
        _clear_publishable_candidate(workflow)
        return None
    verdict_path = Path(verdict_v2.get("path", ""))
    if not verdict_path.is_file() or supervisor.sha256_file(verdict_path) != verdict_v2.get("sha256"):
        _clear_publishable_candidate(workflow)
        return None
    verdict = supervisor.load_json(verdict_path)
    require_valid_contract(verdict, "reviewer-verdict", 2)
    if verdict.get("verdict") != "approve":
        _clear_publishable_candidate(workflow)
        return None
    value = {
        "artifact": copy.deepcopy(artifact),
        "validation": copy.deepcopy(validation),
        "validation_receipt_v2": copy.deepcopy(receipt_v2),
        "reviewer_verdict_v2": copy.deepcopy(verdict_v2),
    }
    workflow["publishable_candidate"] = value
    return value


def _inside(path, parent):
    return supervisor._is_within(Path(path).resolve(), Path(parent).resolve())


def normalize_workspace(workspace):
    workspace = Path(workspace).expanduser().resolve()
    if not workspace.is_dir():
        raise supervisor.SupervisorError(f"workspace is not a directory: {workspace}")
    return workspace


def normalize_drawio(path, workspace, *, must_exist):
    path = Path(path).expanduser()
    if not path.is_absolute():
        path = workspace / path
    path = path.resolve()
    if path.suffix.lower() != ".drawio":
        raise supervisor.SupervisorError("diagram path must end in .drawio")
    if not _inside(path, workspace):
        raise supervisor.SupervisorError("diagram path must be inside the current workspace")
    if must_exist and not path.is_file():
        raise supervisor.SupervisorError(f"diagram artifact is not a file: {path}")
    if not must_exist and path.exists():
        raise supervisor.SupervisorError(f"create target already exists and will not be overwritten: {path}")
    return path


def run_dir_for(workspace, run_id):
    if not RUN_ID_RE.fullmatch(run_id):
        raise supervisor.SupervisorError("run-id must be an opaque slug")
    result = (workspace / ".diagram-runs" / run_id).resolve()
    if not _inside(result, workspace):
        raise supervisor.SupervisorError("run directory resolves outside the workspace")
    return result


def intake_dir_for(workspace, intake_id):
    if not INTAKE_ID_RE.fullmatch(intake_id):
        raise supervisor.SupervisorError("intake-id must be an opaque intake-* slug")
    result = (Path(workspace) / ".diagram-intake" / intake_id).resolve()
    if not _inside(result, workspace):
        raise supervisor.SupervisorError("intake directory resolves outside the workspace")
    return result


def _intake_request_sha256(request):
    return hashlib.sha256(str(request).encode("utf-8")).hexdigest()


def _intake_existing_evidence(mode, diagram):
    if mode != "improve":
        return None
    path = Path(diagram).resolve()
    evidence = diagram_intake.infer_existing_type(path, None)
    try:
        evidence["diagram_spec"] = supervisor.make_spec(path, [])
    except (OSError, ValueError, ET.ParseError, supervisor.SupervisorError):
        evidence["diagram_spec"] = None
    return evidence


def load_intake_request(workspace, intake_id, *, mode=None):
    """Load only a bound host request for replay before normal command parsing."""
    directory = intake_dir_for(normalize_workspace(workspace), intake_id)
    path = directory / "request.json"
    if not path.is_file():
        raise supervisor.SupervisorError(f"diagram intake was not found: {intake_id}")
    value = supervisor.load_json(path)
    if mode is not None and value.get("mode") != mode:
        raise supervisor.SupervisorError("intake mode differs from the replayed command")
    if value.get("intake_id") != intake_id:
        raise supervisor.SupervisorError("intake request id binding is invalid")
    return value


def run_preflight_intake(
    *,
    mode,
    diagram,
    request,
    workspace,
    cli,
    intake_id=None,
    answers=(),
    accept_assumptions=False,
    timeout=600,
):
    """Run or resume isolated Semantic Analyst intake without allocating a run."""
    workspace = normalize_workspace(workspace)
    diagram = normalize_drawio(
        diagram, workspace, must_exist=(mode == "improve")
    )
    intake_id = intake_id or utc_slug("intake")
    if not intake_id.startswith("intake-"):
        intake_id = f"intake-{intake_id}"
    intake_dir = intake_dir_for(workspace, intake_id)
    request_sha256 = _intake_request_sha256(request)
    request_path = intake_dir / "request.json"
    existing_evidence = _intake_existing_evidence(mode, diagram)
    request_value = {
        "schema_version": 1,
        "intake_id": intake_id,
        "mode": mode,
        "request": request,
        "request_sha256": request_sha256,
        "diagram": str(diagram),
        "existing_evidence": existing_evidence,
    }
    if request_path.is_file():
        persisted_request = supervisor.load_json(request_path)
        for field in ("intake_id", "mode", "request_sha256", "diagram"):
            if persisted_request.get(field) != request_value[field]:
                raise supervisor.SupervisorError(
                    f"intake replay changed immutable {field}"
                )
        request_value = persisted_request
        existing_evidence = persisted_request.get("existing_evidence")
    else:
        supervisor.write_json(request_path, request_value)

    accumulated_answers = []
    answer_values = {}
    prior_state_path = intake_dir / "state.json"
    if prior_state_path.is_file():
        prior_state = supervisor.load_json(prior_state_path)
        if (
            prior_state.get("intake_id") != intake_id
            or prior_state.get("request_sha256") != request_sha256
            or prior_state.get("mode") != mode
        ):
            raise supervisor.SupervisorError(
                "persisted intake state binding is invalid"
            )
        accumulated_answers.extend(prior_state.get("answers", []))
    accumulated_answers.extend(list(answers or []))
    bound_answers = []
    for answer in accumulated_answers:
        question_id = answer["question_id"]
        text = answer["text"]
        if question_id in answer_values and answer_values[question_id] != text:
            raise supervisor.SupervisorError(
                f"intake replay changed the answer for {question_id}"
            )
        if question_id not in answer_values:
            bound_answers.append({"question_id": question_id, "text": text})
        answer_values[question_id] = text

    role_dir = intake_dir / "roles" / "semantic-intake"
    role_input = role_dir / "input.json"
    role_output = role_dir / "output.json"
    if not role_output.is_file():
        payload = {
            "schema_version": 1,
            "phase": "intake",
            "mode": mode,
            "request": request,
            "diagram_types": list(diagram_intake.DIAGRAM_TYPES),
            "existing_evidence": existing_evidence,
            "answers": [],
        }
        supervisor.write_json(role_input, payload)
        agent_runtime.invoke_role(
            "semantic_analyst",
            role_input,
            role_output,
            cli=str(cli),
            run_dir=None,
            timeout=timeout,
            cwd=workspace,
        )
    analysis_envelope = supervisor.load_json(role_output)
    layout_contracts.require_diagram_intake_analysis(analysis_envelope)
    state = diagram_intake.advance(
        request=request,
        mode=mode,
        existing_evidence=existing_evidence,
        answers=bound_answers,
        analysis=analysis_envelope["result"],
        accept_assumptions=accept_assumptions,
    )
    state.update({
        "intake_id": intake_id,
        "request_sha256": request_sha256,
    })
    layout_contracts.require_diagram_intake(state)
    turns_dir = intake_dir / "turns"
    turn_number = 1
    if turns_dir.is_dir():
        turn_number += sum(1 for path in turns_dir.glob("*.json") if path.is_file())
    turn_path = turns_dir / f"{turn_number:03d}.json"
    supervisor.write_json(turn_path, state)
    supervisor.write_json(intake_dir / "state.json", state)
    completed_path = None
    if state["status"] == "complete":
        completed_path = workspace / ".diagram-intake" / f"{intake_id}.json"
        if completed_path.is_file():
            previous = supervisor.load_json(completed_path)
            if previous != state:
                raise supervisor.SupervisorError(
                    "completed intake replay differs from persisted evidence"
                )
        else:
            supervisor.write_json(completed_path, state)
    return state, completed_path, {
        "request": str(request_path.resolve()),
        "input": str(role_input.resolve()),
        "output": str(role_output.resolve()),
        "turn": str(turn_path.resolve()),
    }


def resolve_run(reference, workspace):
    candidate = Path(reference).expanduser()
    if candidate.exists():
        run_dir = candidate.resolve()
    else:
        direct = run_dir_for(workspace, reference)
        if direct.is_dir():
            run_dir = direct
        else:
            matches = []
            root = workspace / ".diagram-runs"
            if root.is_dir():
                for workflow_path in root.glob("*/workflow.json"):
                    try:
                        value = supervisor.load_json(workflow_path)
                    except (OSError, ValueError, json.JSONDecodeError):
                        continue
                    if value.get("run_id") == reference:
                        matches.append(workflow_path.parent.resolve())
            if len(matches) > 1:
                raise supervisor.SupervisorError(
                    f"diagram run id is ambiguous: {reference}"
                )
            run_dir = matches[0] if matches else direct
    if not run_dir.is_dir() or not (run_dir / WORKFLOW_FILE).is_file():
        raise supervisor.SupervisorError(f"diagram run was not found: {reference}")
    if not _inside(run_dir, workspace):
        raise supervisor.SupervisorError("run directory must be inside the current workspace")
    return run_dir


def atomic_copy(source, target):
    source, target = Path(source), Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as tmp:
        temp_name = Path(tmp.name)
    try:
        shutil.copyfile(source, temp_name)
        with open(temp_name, "rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    finally:
        temp_name.unlink(missing_ok=True)


def write_workflow(run_dir, workflow):
    workflow["updated_at"] = supervisor.utc_now()
    supervisor.write_json(Path(run_dir) / WORKFLOW_FILE, workflow)


def load_workflow(run_dir):
    return supervisor.load_json(Path(run_dir) / WORKFLOW_FILE)


def _relative_file(run_dir, path):
    return lifecycle_v2.make_file_descriptor(path, root=run_dir)


def _v1_spec_to_v2(run_dir, spec, source_bundle_hash):
    """Build the lossless/model-view split without exposing technical cells."""
    pages = []
    for page in spec["pages"]:
        cells = []
        page_id = page["id"]
        for cell in page["cells"]:
            cell_id = cell["id"]
            kind = cell["kind"]
            technical = cell_id in {"0", "1"} or kind in {"layer"}
            def identity(value):
                return None if value is None else {"page_id": page_id, "cell_id": value}
            preserved = {
                "id": cell_id,
                "stable_identity": {"page_id": page_id, "cell_id": cell_id},
                "kind": "root" if cell_id == "0" else kind,
                "semantic_type": cell["semantic_type"],
                "label": cell["label"],
                "technical": technical,
                "parent": identity(cell.get("parent_id")),
                "source": identity(cell.get("source_id")),
                "target": identity(cell.get("target_id")),
                "relationship": cell.get("relationship"),
                "style": cell.get("style", ""),
                "raw_attributes": {},
            }
            if "geometry" in cell:
                geometry = cell["geometry"]
                converted = {}
                if "bounds" in geometry:
                    converted.update(geometry["bounds"])
                if "waypoints" in geometry:
                    converted["waypoints"] = geometry["waypoints"]
                if "relative" in geometry:
                    converted["relative"] = geometry["relative"]
                preserved["geometry"] = converted
            cells.append(preserved)
        pages.append({"id": page_id, "name": page.get("name", ""), "cells": cells})
    artifact_path = Path(spec["artifact"]["uri"]).resolve()
    result = {
        "schema_version": 2,
        "diagram_id": spec["diagram_id"],
        "title": spec.get("title", spec["diagram_id"]),
        "artifact": {
            "path": artifact_path.relative_to(Path(run_dir).resolve()).as_posix(),
            "sha256": spec["artifact"]["sha256"],
            "byte_length": spec["artifact"]["byte_length"],
            "format": spec["artifact"]["format"],
            "imported_at": spec["artifact"]["imported_at"],
            "preservation_policy": "patch-original-xml",
        },
        "source_bundle_sha256": None,
        "pages": pages,
        "model_view": {"technical_cells_excluded": True, "pages": []},
        "semantic_digest": {"algorithm": "sha256", "canonicalization": "diagramspec-model-view-v2", "value": "0" * 64},
    }
    result = with_model_view(result)
    diagnostics = validate_diagramspec(result)
    if diagnostics:
        raise supervisor.SupervisorError(f"DiagramSpec v2 cross-field validation failed: {diagnostics[0]}")
    return result


def _plan_element_map(plan_pages):
    result = {}
    for page in plan_pages:
        for kind in ("nodes", "edges"):
            for element in page[kind]:
                identity = element["stable_identity"]
                result[(identity["page_id"], identity["cell_id"])] = (kind, element)
    return result


def _semantic_input_v2(run_dir, workflow, baseline_spec_v2, *, feedback=None):
    """Build the exact immutable input shown to Semantic Analyst v2."""
    source_bundle, source_descriptor = lifecycle_v2.latest_document(
        run_dir, "source-bundle"
    )
    source_hash = canonical_json_sha256(source_bundle)
    if source_hash != source_descriptor["canonical_sha256"]:
        raise supervisor.SupervisorError(
            "active source bundle differs from its lifecycle descriptor"
        )
    if baseline_spec_v2 is None:
        model_view = {"technical_cells_excluded": True, "pages": []}
        baseline_digest = semantic_digest(model_view)
    else:
        diagnostics = validate_diagramspec(baseline_spec_v2)
        if diagnostics:
            raise supervisor.SupervisorError(
                f"Semantic Analyst baseline DiagramSpec v2 is invalid: {diagnostics[0]}"
            )
        model_view = copy.deepcopy(baseline_spec_v2["model_view"])
        baseline_digest = baseline_spec_v2["semantic_digest"]["value"]
    payload = {
        "schema_version": 2,
        "run_id": workflow["run_id"],
        "mode": workflow["mode"],
        "request": workflow["request"],
        "feedback": feedback,
        "source_bundle": {
            "sha256": source_hash,
            "content": source_bundle,
        },
        "baseline": {
            "semantic_digest": baseline_digest,
            "model_view": model_view,
            "evidence": copy.deepcopy(source_bundle["evidence"]),
        },
        "source_priority": list(SOURCE_PRIORITY),
        "requirements": {
            "complete_desired_graph": True,
            "compare_request_to_existing": workflow["mode"] == "improve",
            "return_complete_plan_for_create": workflow["mode"] == "create",
            "preserve_page_scoped_ids": True,
        },
    }
    diagnostics = validate_semantic_analysis_input(payload)
    if diagnostics:
        raise supervisor.SupervisorError(
            f"Semantic Analyst v2 input binding failed: {diagnostics[0]}"
        )
    return payload


def _semantic_analysis_to_v2(run_dir, workflow, analysis, baseline_spec_v2):
    """Turn analysis-only model output into a host-bound canonical plan."""
    diagnostics = validate_semantic_analysis(analysis)
    if diagnostics:
        raise supervisor.SupervisorError(
            f"semantic analysis v2 cross-field validation failed: {diagnostics[0]}"
        )
    source_bundle = lifecycle_v2.latest_document(run_dir, "source-bundle")[0]
    assumption_texts = list(analysis["result"].get("assumptions", []))
    assumption_sources = [
        source_record(
            source_id=(
                f"agent-assumption-r{source_bundle['revision'] + 1}-{index + 1}-"
                f"{canonical_json_sha256(text)[:12]}"
            ),
            kind="agent_assumption",
            uri=(
                f"urn:diagram-run:{workflow['run_id']}:semantic-assumption:"
                f"{source_bundle['revision'] + 1}:{index + 1}"
            ),
            content=text,
            confidence=0.5,
        )
        for index, text in enumerate(assumption_texts)
    ]
    if assumption_sources:
        source_bundle = lifecycle_v2.revise_sources(
            run_dir,
            new_sources=assumption_sources,
            event_payload={
                "kind": "semantic_assumptions",
                "assumption_count": len(assumption_sources),
            },
        )
    source_bundle_hash = canonical_json_sha256(source_bundle)
    baseline_model_view = (
        copy.deepcopy(baseline_spec_v2["model_view"])
        if baseline_spec_v2 is not None
        else {"technical_cells_excluded": True, "pages": []}
    )
    baseline_digest = (
        baseline_spec_v2["semantic_digest"]["value"]
        if baseline_spec_v2 is not None else semantic_digest(baseline_model_view)
    )
    try:
        plan = semantic_analysis_to_plan(
            analysis,
            run_id=workflow["run_id"],
            mode=workflow["mode"],
            source_bundle_sha256=source_bundle_hash,
            baseline_semantic_digest=baseline_digest,
            baseline_model_view=baseline_model_view,
            assumption_source_ids=[item["source_id"] for item in assumption_sources],
        )
    except ValueError as exc:
        raise supervisor.SupervisorError(
            f"host semantic-plan v2 normalization failed: {exc}"
        ) from exc
    output = (
        Path(run_dir) / "semantic-plans"
        / f"{canonical_json_sha256(plan)}.semantic-plan.v2.json"
    )
    atomic_write_bytes(output, canonical_json_bytes(plan) + b"\n")
    return plan, output


def _legacy_plan_to_v2(run_dir, workflow, legacy_plan, baseline_spec_v2):
    page_id = baseline_spec_v2["model_view"]["pages"][0]["id"] if baseline_spec_v2 else "generated"
    nodes = []
    for node in legacy_plan["result"]["nodes"]:
        nodes.append({
            "stable_identity": {"page_id": page_id, "cell_id": node["id"]},
            "label": node["label"],
            "semantic_type": node["semantic_type"],
            "parent": None if not node.get("parent_id") else {"page_id": page_id, "cell_id": node["parent_id"]},
            "style_hint": node.get("style_hint"),
        })
    edges = []
    for edge in legacy_plan["result"]["edges"]:
        edges.append({
            "stable_identity": {"page_id": page_id, "cell_id": edge["id"]},
            "source": {"page_id": page_id, "cell_id": edge["source_id"]},
            "target": {"page_id": page_id, "cell_id": edge["target_id"]},
            "label": edge["label"],
            "relationship": edge["relationship"],
            "parent": None,
            "style_hint": None,
        })
    pages = [{"page_id": page_id, "name": legacy_plan["result"]["title"], "nodes": nodes, "edges": edges}]
    baseline_digest = (
        baseline_spec_v2["semantic_digest"]["value"]
        if baseline_spec_v2 else canonical_json_sha256({"technical_cells_excluded": True, "pages": []})
    )
    baseline = {}
    if baseline_spec_v2:
        for page in baseline_spec_v2["model_view"]["pages"]:
            for element in page["elements"]:
                identity = element["stable_identity"]
                baseline[(identity["page_id"], identity["cell_id"])] = element
    desired = _plan_element_map(pages)
    operations = []
    for key, (collection, element) in sorted(desired.items()):
        old = baseline.get(key)
        element_kind = "edge" if collection == "edges" else "group" if element.get("semantic_type") == "group" else "node"
        if old is None:
            operations.append({
                "operation_type": "add", "element_kind": element_kind,
                "target": element["stable_identity"],
                "changes": [{"field": "element", "before": None, "after": element}],
            })
            continue
        changes = []
        fields = ("label", "semantic_type", "parent") if collection == "nodes" else ("label", "source", "target", "relationship", "parent")
        for field in fields:
            before = old.get(field)
            after = element.get(field)
            if before != after:
                changes.append({"field": field, "before": before, "after": after})
        if changes:
            operation_type = "relationship" if any(item["field"] in {"source", "target", "relationship"} for item in changes) else "parent" if all(item["field"] == "parent" for item in changes) else "update"
            operations.append({"operation_type": operation_type, "element_kind": element_kind, "target": element["stable_identity"], "changes": changes})
    source_bundle_hash = lifecycle_v2.require_mutable(run_dir)["latest_snapshots"]["source-bundle"]["canonical_sha256"]
    delta = normalize_semantic_delta(
        baseline_semantic_digest=baseline_digest,
        source_bundle_sha256=source_bundle_hash,
        operations=operations,
    )
    requires_human = bool(
        legacy_plan.get("status") == "needs_human"
        or legacy_plan["result"].get("requires_human")
        or (workflow["mode"] == "improve" and delta["operations"])
    )
    assumption_texts = list(legacy_plan["result"].get("assumptions", []))
    source_bundle = lifecycle_v2.latest_document(run_dir, "source-bundle")[0]
    assumption_sources = [
        source_record(
            source_id=(
                f"agent-assumption-r{source_bundle['revision'] + 1}-{index + 1}-"
                f"{canonical_json_sha256(text)[:12]}"
            ),
            kind="agent_assumption",
            uri=f"urn:diagram-run:{workflow['run_id']}:semantic-assumption:{source_bundle['revision'] + 1}:{index + 1}",
            content=text,
            confidence=0.5,
        )
        for index, text in enumerate(assumption_texts)
    ]
    plan = {
        "schema_version": 2,
        "role": "semantic_analyst",
        "status": "needs_human" if requires_human else "ok",
        "run_id": workflow["run_id"],
        "source_bundle_sha256": source_bundle_hash,
        "baseline_semantic_digest": baseline_digest,
        "result": {
            "mode": workflow["mode"],
            "diagram_type": legacy_plan["result"]["diagram_type"],
            "title": legacy_plan["result"]["title"],
            "direction": legacy_plan["result"]["direction"],
            "pages": pages,
            "semantic_delta": delta,
            "assumptions": [
                {
                    "assumption_id": f"assumption-{index + 1}",
                    "text": text,
                    "source_id": assumption_sources[index]["source_id"],
                }
                for index, text in enumerate(assumption_texts)
            ],
            "requires_human": requires_human,
            "human_questions": (
                list(legacy_plan["result"].get("semantic_changes", []))
                or ["Подтвердите предложенную семантическую дельту перед изменением диаграммы."]
            ) if requires_human else [],
        },
    }
    diagnostics = validate_semantic_plan(plan)
    if diagnostics:
        raise supervisor.SupervisorError(f"semantic plan v2 cross-field validation failed: {diagnostics[0]}")
    if assumption_sources:
        source_bundle = lifecycle_v2.revise_sources(
            run_dir,
            new_sources=assumption_sources,
            event_payload={
                "kind": "semantic_assumptions",
                "assumption_count": len(assumption_sources),
            },
        )
        source_bundle_hash = canonical_json_sha256(source_bundle)
        plan["source_bundle_sha256"] = source_bundle_hash
        plan["result"]["semantic_delta"]["source_bundle_sha256"] = source_bundle_hash
        diagnostics = validate_semantic_plan(plan)
        if diagnostics:
            raise supervisor.SupervisorError(
                f"semantic plan v2 cross-field validation failed after source binding: {diagnostics[0]}"
            )
    output = (
        Path(run_dir) / "semantic-plans"
        / f"{canonical_json_sha256(plan)}.semantic-plan.v2.json"
    )
    atomic_write_bytes(output, canonical_json_bytes(plan) + b"\n")
    return plan, output


def _record_baseline_v2(run_dir, workflow, accepted, spec_v1, report_path, receipt_path):
    source_hash = lifecycle_v2.require_mutable(run_dir)["latest_snapshots"]["source-bundle"]["canonical_sha256"]
    spec_v2 = _v1_spec_to_v2(run_dir, spec_v1, source_hash)
    spec_path = Path(run_dir) / "diagram-spec.v2.json"
    atomic_write_bytes(spec_path, canonical_json_bytes(spec_v2) + b"\n")
    _, receipt_v2_path = lifecycle_v2.mirror_validation_receipt(
        run_dir, legacy_receipt_path=receipt_path,
    )
    verification = lifecycle_v2.verify_v2_receipt(run_dir, receipt_v2_path)
    if not verification["valid"]:
        raise supervisor.SupervisorError(f"v2 baseline receipt verification failed: {verification['diagnostics']}")
    current_evidence = lifecycle_v2.latest_document(run_dir, "source-bundle")[0]["evidence"]
    evidence = {
        "imported_diagramspec": _relative_file(run_dir, spec_path),
        "baseline_validation": {
            "artifact": _relative_file(run_dir, accepted),
            "report": _relative_file(run_dir, report_path),
            "receipt": _relative_file(run_dir, receipt_v2_path),
        },
        "eligible_review_handoff": copy.deepcopy(current_evidence.get("eligible_review_handoff")),
    }
    lifecycle_v2.revise_sources(
        run_dir, evidence=evidence,
        event_payload={"kind": "baseline_evidence", "artifact_sha256": supervisor.sha256_file(accepted)},
    )
    lifecycle_v2.transition(
        run_dir, "analyzed", iteration=workflow.get("iteration", 0),
        accepted_artifact=_relative_file(run_dir, accepted),
        validation_report=_relative_file(run_dir, report_path),
        validation_receipt=_relative_file(run_dir, receipt_v2_path),
        payload={"baseline_validation": True},
    )
    workflow["diagram_spec_v2"] = {"path": str(spec_path.resolve()), "sha256": supervisor.sha256_file(spec_path)}
    workflow["validation_receipt_v2"] = {"path": str(receipt_v2_path.resolve()), "sha256": supervisor.sha256_file(receipt_v2_path)}
    return spec_v2


def _source_bundle_bound_to_plan(run_dir, semantic_plan_v2):
    expected = semantic_plan_v2["source_bundle_sha256"]
    replayed = lifecycle_v2.require_mutable(run_dir)
    for record in reversed(replayed["events"]):
        for descriptor in reversed(record["event"]["snapshots"]):
            if (
                descriptor.get("schema_kind") == "source-bundle"
                and descriptor.get("canonical_sha256") == expected
            ):
                path = Path(run_dir) / descriptor["path"]
                value = supervisor.load_json(path)
                require_valid_contract(value, "source-bundle", 2)
                if canonical_json_sha256(value) != expected:
                    raise supervisor.SupervisorError(
                        "historical source bundle changed after semantic planning"
                    )
                return value
    raise supervisor.SupervisorError(
        "semantic plan source bundle is absent from the lifecycle ledger"
    )


def _finish_checkpointed_create(
    run_dir, workflow, semantic_plan, semantic_plan_v2, cli, timeout,
    *, recovering_patching=False,
):
    """Render and review a create plan that was explicitly approved pre-render."""
    accepted = Path(run_dir) / "accepted" / "baseline.drawio"
    current = supervisor.load_state(run_dir)["state"]
    if current == "patching" and recovering_patching:
        pass
    elif current in {"awaiting_decision", "awaiting_feedback"}:
        supervisor.transition(
            run_dir, "patching", decision="continue",
            reason="approved pre-render semantic plan",
        )
    else:
        raise supervisor.SupervisorError(
            f"checkpointed create cannot render from state {current}"
        )
    accepted.parent.mkdir(parents=True, exist_ok=True)
    source_bundle = _source_bundle_bound_to_plan(run_dir, semantic_plan_v2)
    adapter_input = renderer_adapters.select_lifecycle_adapter_input(
        semantic_plan_v2,
        source_bundle,
        mode="create",
    )
    lifecycle_state = lifecycle_v2.latest_document(run_dir, "run-state")[0]
    if lifecycle_state["status"] != "analyzing":
        lifecycle_v2.transition(
            run_dir,
            "analyzing",
            payload={
                "phase": "rendering",
                "renderer_adapter": adapter_input.record(),
            },
        )
    selected_adapter = adapter_input.selection.adapter
    if selected_adapter is renderer_adapters.GENERIC_ADAPTER:
        selected = _run_generic_create_layouts(
            run_dir,
            workflow,
            semantic_plan_v2,
            adapter_input,
            timeout=timeout,
        )
        accepted, _ = _adopt_create_layout_attempt(
            run_dir,
            workflow,
            selected,
            request=workflow["request"],
            max_iterations=workflow["max_iterations"],
        )
        workflow["renderer_adapter"] = {
            **adapter_input.record(),
            "options": {**dict(adapter_input.options), "reflow": "full"},
            "output_path": str(accepted.resolve()),
            "output_hash": supervisor.sha256_file(accepted),
            "command": ["host:execute_layout_attempt"],
            "layout_request_sha256": selected["request_sha256"],
            "layout_result_sha256": selected["layout_result"]["sha256"],
            "requested_semantic_diagram_type": semantic_plan["result"]["diagram_type"],
        }
    else:
        renderer_source = semantic_plan_v2
        if adapter_input.source_record is not None:
            source_path = (
                Path(run_dir) / "inputs" / "renderer-sources"
                / f"{adapter_input.source_record['content_sha256']}.json"
            )
            atomic_write_bytes(
                source_path,
                canonical_json_bytes(adapter_input.source_content) + b"\n",
            )
            renderer_source = source_path
        adapter_run = tool_step(
            run_dir,
            "renderer-adapter",
            renderer_adapters.render_with_adapter,
            selected_adapter.diagram_type,
            renderer_source,
            accepted,
            mode="create",
            options=dict(adapter_input.options),
            generic_renderer=render_generic,
            timeout=timeout,
        )
        workflow["renderer_adapter"] = {
            **adapter_input.record(),
            **adapter_run.record(),
            "requested_semantic_diagram_type": semantic_plan["result"]["diagram_type"],
        }
        supervisor.transition(run_dir, "validating")
        spec = supervisor.make_spec(
            accepted,
            [source_ref_for_request(workflow["run_id"], workflow["request"])],
        )
        supervisor.write_json(Path(run_dir) / "diagram-spec.json", spec)
        validation_profile = selected_adapter.validation_profile
        validation_source = (
            adapter_run.source_path
            if validation_profile in {"roadmap", "gitflow"}
            else None
        )
        supervisor.run_validation(
            accepted,
            run_dir,
            profile=None if validation_profile == "structural" else validation_profile,
            source=validation_source,
            attempt_id="baseline",
        )
        report_path = Path(run_dir) / "attempts" / "baseline" / "validation-report.json"
        receipt_path = Path(run_dir) / "attempts" / "baseline" / "validation-receipt.json"
        state = supervisor.record_initial_candidate(
            run_dir,
            accepted,
            report_path,
            receipt_path,
        )
        _set_workflow_accepted(workflow, state)
        _record_baseline_v2(
            run_dir,
            workflow,
            accepted,
            spec,
            report_path,
            receipt_path,
        )
    workflow.pop("pending_semantic_approval", None)
    write_workflow(run_dir, workflow)
    if not _working_validation(workflow).get("strict_passed"):
        _clear_publishable_candidate(workflow)
        workflow["findings"] = supervisor.load_json(
            _working_validation(workflow)["report"]
        ).get("findings", [])
        write_workflow(run_dir, workflow)
        if workflow.get("layout_strategy_exhausted"):
            best_effort = _finish_best_effort(
                run_dir,
                workflow,
                cli,
                timeout,
                reason=(
                    "Bounded deterministic layout strategies completed without "
                    "a strict pass; the safest validated candidate was retained"
                ),
            )
            if best_effort is not None:
                return best_effort
        return repair_loop(run_dir, workflow, cli, timeout)
    try:
        verdict, _ = baseline_review(run_dir, workflow, cli, timeout)
    except supervisor.SupervisorError as exc:
        supervisor.transition(run_dir, "final_review", artifact=accepted)
        supervisor.transition(run_dir, "awaiting_feedback", reason="independent review failed")
        workflow["findings"] = [str(exc)]
        return checkpoint(
            run_dir, workflow, "plateau",
            "Strict validation evidence was retained, but independent review did not produce a usable verdict.",
            workflow["findings"], ["continue", "pause", "stop", "manual_handoff"],
        )
    workflow["findings"] = verdict.get("findings", [])
    if verdict.get("verdict") == "needs_human":
        supervisor.transition(
            run_dir,
            "final_review",
            artifact=Path(_working_artifact(workflow)["path"]),
        )
        supervisor.transition(
            run_dir,
            "awaiting_feedback",
            reason="Reviewer found an ambiguity in the baseline",
        )
        write_workflow(run_dir, workflow)
        return checkpoint(
            run_dir,
            workflow,
            "feedback",
            "Reviewer found a real ambiguity that requires human judgment; no automatic repair was started.",
            workflow["findings"],
            ["continue", "pause", "stop", "manual_handoff"],
            evidence={"failure_class": "reviewer_needs_human"},
        )
    _set_publishable_candidate(
        workflow,
        artifact=_working_artifact(workflow),
        validation=_working_validation(workflow),
        receipt_v2=workflow["validation_receipt_v2"],
        verdict_v2=workflow["reviewer_verdict_v2"],
    )
    write_workflow(run_dir, workflow)
    eligibility = _final_approval_eligibility(run_dir, workflow)
    allowed_final = []
    if eligibility["approve"]:
        allowed_final.append("approve")
    if eligibility["approve_with_findings"]:
        allowed_final.append("approve_with_findings")
    if allowed_final:
        workflow["final_approval_eligibility"] = eligibility
        write_workflow(run_dir, workflow)
        supervisor.transition(run_dir, "final_review", artifact=accepted)
        return checkpoint(
            run_dir, workflow, "final_acceptance",
            (
                "The candidate passed strict validation and independent review."
                if eligibility["approve"]
                else "The candidate is structurally safe and evidence-valid, but unresolved findings require explicit approval."
            ),
            eligibility["unresolved_findings"],
            [*allowed_final, "continue", "pause", "stop", "manual_handoff"],
            evidence={"final_approval_eligibility": eligibility},
        )
    return repair_loop(run_dir, workflow, cli, timeout)


def _reconcile_feedback(
    run_dir, workflow, feedback, decision_id, workspace, cli, timeout,
    *, approved_proposal=None,
):
    lifecycle_v2.add_feedback_source(run_dir, feedback, decision_id)
    layout_scope = _layout_feedback_scope(workflow, feedback)
    if layout_scope is not None:
        workflow["repair_scope"] = layout_scope
        workflow["machine_repair_feedback"] = {
            "content": {
                "schema_version": 1,
                "kind": "explicit_layout_feedback",
                "feedback": feedback,
                "repair_scope": copy.deepcopy(layout_scope),
            }
        }
        workflow["semantic_authorized"] = False
        workflow.pop("pending_semantic_approval", None)
        workflow.pop("approved_semantic_change", None)
        _clear_publishable_candidate(workflow)
        write_workflow(run_dir, workflow)
        supervisor.append_event(
            run_dir,
            "internal_feedback_created",
            {
                "failure_class": "user_layout_feedback",
                "feedback_sha256": hashlib.sha256(feedback.encode("utf-8")).hexdigest(),
                "repair_scope": layout_scope,
            },
            state=supervisor.load_state(run_dir)["state"],
            actor={"kind": "system", "id": "diagram-orchestrator", "model": None},
        )
        return None
    # Mixed or semantic feedback is intentionally routed through Semantic
    # Analyst.  A scope from an earlier layout-only retry must not constrain
    # the newly proposed typed semantic delta.
    workflow.pop("repair_scope", None)
    workflow.pop("machine_repair_feedback", None)
    accepted_descriptor = _working_artifact(workflow)
    accepted_path = accepted_descriptor.get("path")
    accepted = Path(accepted_path) if accepted_path else None
    baseline_spec_v1 = (
        supervisor.make_spec(
            accepted, [source_ref_for_request(workflow["run_id"], workflow["request"])],
        )
        if accepted is not None else None
    )
    baseline_spec_v2 = (
        _v1_spec_to_v2(
            run_dir, baseline_spec_v1,
            lifecycle_v2.require_mutable(run_dir)["latest_snapshots"]["source-bundle"]["canonical_sha256"],
        )
        if baseline_spec_v1 is not None else None
    )
    payload = _semantic_input_v2(
        run_dir, workflow, baseline_spec_v2, feedback=feedback,
    )
    analysis, _, _, analysis_output_path = role_call(
        "semantic_analyst", payload, run_dir, workspace, cli, timeout,
        f"semantic-feedback-{len(workflow.get('decisions', [])) + 1}",
    )
    plan_v2, plan_path = _semantic_analysis_to_v2(
        run_dir, workflow, analysis, baseline_spec_v2,
    )
    delta = plan_v2["result"]["semantic_delta"]
    if (
        approved_proposal
        and delta["baseline_semantic_digest"]
        == approved_proposal["semantic_delta"]["baseline_semantic_digest"]
        and delta["operations"]
        == approved_proposal["semantic_delta"]["operations"]
    ):
        # A comment attached to an approval is still retained as source evidence,
        # but an identical semantic delta does not invalidate the decision or
        # force the user through the same checkpoint again.
        return None
    analysis_descriptor = {
        "path": str(Path(analysis_output_path).resolve()),
        "sha256": supervisor.sha256_file(analysis_output_path),
    }
    workflow["semantic_analysis_v2"] = analysis_descriptor
    # Retain the legacy workflow key as a read-only alias for checkpointed
    # create and older trace consumers; its content is now analysis-only v2.
    workflow["semantic_plan"] = dict(analysis_descriptor)
    workflow["semantic_plan_v2"] = {
        "path": str(plan_path.resolve()),
        "sha256": supervisor.sha256_file(plan_path),
        "semantic_delta_sha256": semantic_delta_sha256(delta),
    }
    if not delta["operations"] and not plan_v2["result"]["requires_human"]:
        write_workflow(run_dir, workflow)
        return None
    pending = {
        "semantic_plan": {"path": str(plan_path.resolve()), "sha256": supervisor.sha256_file(plan_path)},
        "baseline_semantic_digest": plan_v2["baseline_semantic_digest"],
        "source_bundle_sha256": plan_v2["source_bundle_sha256"],
        "semantic_delta": delta,
        "semantic_delta_sha256": semantic_delta_sha256(delta),
        "semantic_changes": plan_v2["result"]["human_questions"],
        "semantic_changes_sha256": canonical_hash(plan_v2["result"]["human_questions"]),
    }
    workflow["pending_semantic_approval"] = pending
    workflow["semantic_authorized"] = False
    workflow.pop("approved_semantic_change", None)
    current = supervisor.load_state(run_dir)["state"]
    if current == "final_review":
        supervisor.transition(run_dir, "awaiting_feedback", reason="new feedback requires semantic reconciliation")
        current = "awaiting_feedback"
    if current == "awaiting_feedback":
        supervisor.transition(
            run_dir, "awaiting_decision", artifact=accepted,
            decision="continue", reason="feedback produced a semantic delta",
        )
    elif current != "awaiting_decision":
        raise supervisor.SupervisorError(f"cannot request semantic approval from state {current}")
    write_workflow(run_dir, workflow)
    return checkpoint(
        run_dir, workflow, "semantic_approval",
        "Новые замечания пользователя сформировали семантическую дельту; подтвердите её перед Repair.",
        plan_v2["result"]["human_questions"],
        ["continue", "pause", "stop", "manual_handoff"],
        evidence=pending,
    )


def tool_step(run_dir, name, function, *args, evidence=None, **kwargs):
    supervisor.append_event(
        run_dir, "tool_started", {"tool": name, **(evidence or {})},
        actor={"kind": "tool", "id": name, "model": None},
    )
    try:
        result = function(*args, **kwargs)
    except Exception as exc:
        supervisor.append_event(
            run_dir, "tool_finished",
            {"tool": name, "status": "failed", "error": str(exc)[-1000:]},
            actor={"kind": "tool", "id": name, "model": None},
        )
        raise
    payload = {"tool": name, "status": "completed"}
    if isinstance(result, (str, Path)) and Path(result).is_file():
        payload.update({"output": str(Path(result).resolve()), "output_sha256": supervisor.sha256_file(result)})
    supervisor.append_event(
        run_dir, "tool_finished", payload,
        actor={"kind": "tool", "id": name, "model": None},
    )
    return result


def _workflow_file_descriptor(path):
    path = Path(path).resolve()
    return {
        "path": str(path),
        "sha256": supervisor.sha256_file(path),
        "byte_length": path.stat().st_size,
    }


def _relocate_backend_evidence(run_dir, attempt_dir, evidence):
    """Copy transient backend captures into the immutable run evidence tree."""
    run_dir = Path(run_dir).resolve()
    evidence_root = Path(attempt_dir).resolve() / "backend"
    evidence_root.mkdir(parents=True, exist_ok=True)
    value = copy.deepcopy(dict(evidence))

    def relocate(record, prefix):
        if not isinstance(record, dict):
            return
        for field, filename in (
            ("stdout_path", f"{prefix}runtime-output.json"),
            ("stderr_path", f"{prefix}runtime-stderr.txt"),
        ):
            source_value = record.get(field)
            if not source_value:
                continue
            source = Path(source_value).resolve()
            if not source.is_file():
                continue
            destination = evidence_root / filename
            atomic_copy(source, destination)
            record[field] = str(destination)
            record[field.replace("_path", "_sha256")] = supervisor.sha256_file(
                destination
            )

    relocate(value, "")
    relocate(value.get("elk_attempt"), "elk-")
    path = Path(attempt_dir).resolve() / "backend-evidence.json"
    atomic_write_bytes(path, canonical_json_bytes(value) + b"\n")
    if not _inside(path, run_dir):
        raise supervisor.SupervisorError("backend evidence escaped the run")
    return value, path


def _pin_point(side, position):
    numeric = float(position)
    if side == "north":
        return {"x": numeric, "y": 0.0}
    if side == "east":
        return {"x": 1.0, "y": numeric}
    if side == "south":
        return {"x": numeric, "y": 1.0}
    if side == "west":
        return {"x": 0.0, "y": numeric}
    raise supervisor.SupervisorError(f"unsupported layout result port {side!r}")


def _manhattan_internal_points(source, desired, target):
    route = [source]
    for raw in [*desired, target]:
        point = (float(raw[0]), float(raw[1]))
        current = route[-1]
        if current[0] != point[0] and current[1] != point[1]:
            route.append((point[0], current[1]))
        if route[-1] != point:
            route.append(point)
    canonical = []
    for point in route:
        if canonical and canonical[-1] == point:
            continue
        canonical.append(point)
        while len(canonical) >= 3:
            first, middle, last = canonical[-3:]
            if (
                (first[0] == middle[0] == last[0])
                or (first[1] == middle[1] == last[1])
            ):
                canonical.pop(-2)
            else:
                break
    internal = canonical[1:-1]
    if not internal:
        first, last = canonical[0], canonical[-1]
        internal = [((first[0] + last[0]) / 2.0, (first[1] + last[1]) / 2.0)]
    return [{"x": point[0], "y": point[1]} for point in internal]


def _layout_result_patch(baseline_path, request, result):
    """Translate host-owned layout geometry into a replayable local patch."""
    raw, root, _ = supervisor.safe_parse(baseline_path)
    page_results = {page["page_id"]: page for page in result["pages"]}
    movable = {
        (item["page_id"], item["cell_id"])
        for item in request["scope"]["movable_node_refs"]
    }
    reroutable = {
        (item["page_id"], item["cell_id"])
        for item in request["scope"]["reroutable_edge_refs"]
    }
    operations = []
    affected = set()
    for page_id, page in supervisor.page_scopes(root):
        layout_page = page_results.get(page_id)
        if layout_page is None:
            continue
        by_id = supervisor.page_by_id(page)
        layout_nodes = {item["node_id"]: item for item in layout_page["nodes"]}
        layout_edges = {item["edge_id"]: item for item in layout_page["edges"]}
        for scoped_page, cell_id in sorted(movable):
            if scoped_page != page_id:
                continue
            cell = by_id.get(cell_id)
            geometry = layout_nodes.get(cell_id)
            if cell is None or geometry is None or cell.get("vertex") != "1":
                raise supervisor.SupervisorError(
                    f"layout result cannot move unknown vertex {page_id}/{cell_id}"
                )
            operation = {
                "operation_id": f"layout-move-{page_id}-{cell_id}",
                "op": "move_vertex", "target_id": cell_id,
                "precondition": {"target_exists": True, "target_hash": supervisor.cell_hash(cell)},
                "proposed_value": {"x": geometry["x"], "y": geometry["y"]},
                "semantic_effect": "layout_only",
                "reasons": ["apply deterministic host layout result inside the approved local scope"],
                "finding_ids": [],
                "rollback": {"action": "restore_value", "value": {"cell_xml": ET.tostring(cell, encoding="unicode")}},
            }
            operations.append(operation)
            supervisor.require_geometry(cell).set("x", str(float(geometry["x"])))
            supervisor.require_geometry(cell).set("y", str(float(geometry["y"])))
            affected.add((page_id, cell_id))
        for scoped_page, cell_id in sorted(reroutable):
            if scoped_page != page_id:
                continue
            cell = by_id.get(cell_id)
            geometry = layout_edges.get(cell_id)
            if cell is None or geometry is None or cell.get("edge") != "1":
                raise supervisor.SupervisorError(
                    f"layout result cannot reroute unknown edge {page_id}/{cell_id}"
                )
            full_route = copy.deepcopy(geometry["waypoints"])
            pins = {
                "source": _pin_point(geometry["source_port"], geometry["source_pin"]),
                "target": _pin_point(geometry["target_port"], geometry["target_pin"]),
            }
            source_box = supervisor.absolute_rect(by_id.get(cell.get("source")), by_id)
            target_box = supervisor.absolute_rect(by_id.get(cell.get("target")), by_id)
            if source_box is None or target_box is None:
                raise supervisor.SupervisorError(
                    f"layout edge {page_id}/{cell_id} has no bounded endpoints"
                )
            source_endpoint = (
                source_box[0] + pins["source"]["x"] * source_box[2],
                source_box[1] + pins["source"]["y"] * source_box[3],
            )
            target_endpoint = (
                target_box[0] + pins["target"]["x"] * target_box[2],
                target_box[1] + pins["target"]["y"] * target_box[3],
            )
            desired_internal = [
                (float(point["x"]), float(point["y"]))
                for point in full_route[1:-1]
            ]
            internal_route = _manhattan_internal_points(
                source_endpoint, desired_internal, target_endpoint,
            )
            route = {
                "operation_id": f"layout-route-{page_id}-{cell_id}",
                "op": "set_edge_route", "target_id": cell_id,
                "precondition": {"target_exists": True, "target_hash": supervisor.cell_hash(cell)},
                "proposed_value": {"waypoints": internal_route, "orthogonal": True},
                "semantic_effect": "layout_only",
                "reasons": ["apply deterministic host routing inside the approved local scope"],
                "finding_ids": [],
                "rollback": {"action": "restore_value", "value": {"cell_xml": ET.tostring(cell, encoding="unicode")}},
            }
            operations.append(route)
            supervisor.set_points(cell, internal_route)
            supervisor.set_style(cell, {
                "edgeStyle": "orthogonalEdgeStyle", "rounded": "0", "orthogonalLoop": "1",
            })
            pin = {
                "operation_id": f"layout-pins-{page_id}-{cell_id}",
                "op": "set_edge_pins", "target_id": cell_id,
                "precondition": {"target_exists": True, "target_hash": supervisor.cell_hash(cell)},
                "proposed_value": pins,
                "semantic_effect": "layout_only",
                "reasons": ["bind deterministic endpoint ports from the layout result"],
                "finding_ids": [],
                "rollback": {"action": "restore_value", "value": {"cell_xml": ET.tostring(cell, encoding="unicode")}},
            }
            operations.append(pin)
            supervisor.set_style(cell, {
                "exitX": str(pins["source"]["x"]), "exitY": str(pins["source"]["y"]),
                "entryX": str(pins["target"]["x"]), "entryY": str(pins["target"]["y"]),
            })
            affected.add((page_id, cell_id))
    if not operations or len({page_id for page_id, _ in affected}) != 1:
        raise supervisor.SupervisorError(
            "local layout result must mutate at least one cell on exactly one page"
        )
    patch = {
        "schema_version": 1,
        "patch_id": "layout-" + request["request_id"],
        "created_at": supervisor.utc_now(),
        "created_by": "tool",
        "baseline": {
            "artifact_sha256": hashlib.sha256(raw).hexdigest(),
            "semantic_digest": supervisor.document_semantic_digest(root),
        },
        "affected_region": {
            "page_id": next(iter({page_id for page_id, _ in affected})),
            "cell_ids": sorted(cell_id for _, cell_id in affected),
        },
        "operations": operations,
    }
    supervisor.validate_patch_contract(patch)
    return patch


def _locked_cells_from_layout_request(request):
    return {
        page["page_id"]: sorted([
            *[node["node_id"] for node in page["nodes"] if node.get("locked")],
            *[edge["edge_id"] for edge in page["edges"] if edge.get("locked")],
        ])
        for page in request["pages"]
    }


def _verify_candidate_preservation(
    workflow, baseline, candidate, *, candidate_origin, locked_cells=None,
):
    """Apply locked-cell verification only to the current layout candidate."""
    if candidate_origin != "layout_intent":
        return {"valid": True, "reason": None, "mismatches": [], "skipped": True}
    if locked_cells is None:
        scope = workflow.get("layout_repair_scope") or {}
        locked_cells = {
            str(scope.get("page_id", "")): [
                *scope.get("locked_nodes", []), *scope.get("locked_edges", []),
            ]
        }
    return _verify_locked_cell_hashes(baseline, candidate, locked_cells)


def _existing_layout_attempt_artifacts(run_dir, paths):
    return {
        name: _relative_file(run_dir, path)
        for name, path in paths.items()
        if path is not None and Path(path).is_file()
    }


def _record_failed_layout_attempt(
    run_dir,
    workflow,
    *,
    request,
    request_sha256,
    attempt_key,
    context,
    stage,
    error,
    paths,
):
    attempt_id = request["request_id"]
    artifacts = _existing_layout_attempt_artifacts(run_dir, paths)
    failure_value = {
        "schema_version": 1,
        "status": "failed",
        "attempt_id": attempt_id,
        "request_sha256": request_sha256,
        "semantic_plan_sha256": request["semantic_plan_sha256"],
        "strategy": request["strategy"],
        "strategy_options": copy.deepcopy(
            request.get("strategy_options", {})
        ),
        **copy.deepcopy(context),
        "failure_stage": stage,
        "error": str(error)[-2000:],
        "artifacts": copy.deepcopy(artifacts),
    }
    failure_path = (
        Path(run_dir) / "layout-attempts" / attempt_id / "failure.json"
    )
    atomic_write_bytes(
        failure_path, canonical_json_bytes(failure_value) + b"\n",
    )
    artifacts["failure_evidence"] = _relative_file(run_dir, failure_path)
    lifecycle_v2.record_tool_attempt(
        run_dir,
        tool="layout-engine",
        attempt_id=attempt_id,
        status="failed",
        artifact_snapshots=artifacts,
        payload={
            "request_sha256": request_sha256,
            "semantic_plan_sha256": request["semantic_plan_sha256"],
            "strategy": request["strategy"],
            **copy.deepcopy(context),
            "failure_stage": stage,
            "error": str(error)[-1000:],
        },
    )
    attempt = {
        **copy.deepcopy(failure_value),
        "attempt_key": attempt_key,
        "layout_request": _workflow_file_descriptor(
            paths["layout_request"]
        ),
        "failure_evidence": _workflow_file_descriptor(failure_path),
    }
    workflow.setdefault("layout_attempts", []).append(copy.deepcopy(attempt))
    write_workflow(run_dir, workflow)
    return attempt


def execute_layout_attempt(
    workflow,
    semantic_plan,
    *,
    run_dir,
    adapter_input,
    mode,
    scope,
    strategy,
    timeout,
    baseline=None,
    baseline_artifact=None,
):
    """Execute one immutable request through backend, renderer and validator."""
    run_dir = Path(run_dir).resolve()
    strategy_id, strategy_options = strategy
    requested_backend = str(adapter_input.options.get("backend", "auto"))
    backend = "python" if strategy_id == "python-fallback" else requested_backend
    if backend == "legacy-generic-v2":
        raise supervisor.SupervisorError(
            "legacy-generic-v2 must use the explicit renderer adapter rollback path"
        )
    plan_descriptor = workflow.get("semantic_plan_v2") or {}
    semantic_plan_sha256 = plan_descriptor.get("sha256")
    if not semantic_plan_sha256:
        raise supervisor.SupervisorError(
            "layout attempt requires the persisted semantic-plan.v2 hash"
        )
    semantic_plan_path = Path(plan_descriptor.get("path", "")).resolve()
    if (
        not semantic_plan_path.is_file()
        or supervisor.sha256_file(semantic_plan_path) != semantic_plan_sha256
        or supervisor.load_json(semantic_plan_path) != semantic_plan
    ):
        raise supervisor.SupervisorError(
            "layout attempt semantic plan differs from persisted evidence"
        )
    request = layout_model.build_layout_request(
        semantic_plan,
        run_id=workflow["run_id"],
        semantic_plan_sha256=semantic_plan_sha256,
        mode=mode,
        backend=backend,
        strategy_id=strategy_id,
        strategy_options=strategy_options,
        quality_profile_version=workflow.get("quality_profile_version", 2),
        baseline=baseline,
        scope=scope,
    )
    layout_contracts.require_layout_request(request)
    request_sha256 = canonical_json_sha256(request)
    if mode == "local_reflow" and (baseline_artifact is None or not Path(baseline_artifact).is_file()):
        raise supervisor.SupervisorError("local layout attempt requires an immutable baseline artifact")
    context = {
        "mode": mode,
        "workflow_iteration": int(workflow.get("iteration", 0)),
        "scope_sha256": canonical_json_sha256(request["scope"]),
        "baseline_sha256": supervisor.sha256_file(baseline_artifact)
        if mode == "local_reflow" else None,
    }
    attempt_key = layout_backend.attempt_key(request)
    serialized_key = "|".join(attempt_key)
    if serialized_key in workflow.setdefault("layout_attempt_keys", []):
        lifecycle_v2.record_tool_attempt(
            run_dir,
            tool="layout-engine",
            attempt_id=request["request_id"],
            status="skipped",
            payload={
                "reason": "duplicate_attempt_key",
                "request_sha256": request_sha256,
                "strategy": strategy_id,
                **copy.deepcopy(context),
            },
        )
        return {
            "status": "skipped",
            "reason": "duplicate_attempt_key",
            "attempt_key": serialized_key,
        }

    attempt_dir = run_dir / "layout-attempts" / request["request_id"]
    attempt_dir.mkdir(parents=True, exist_ok=True)
    request_path = attempt_dir / "layout-request.json"
    validation_dir = run_dir / "attempts" / request["request_id"]
    paths = {
        "layout_request": request_path,
        "layout_baseline": (
            Path(baseline_artifact)
            if mode == "local_reflow" else None
        ),
        "baseline_validation_report": (
            Path(_working_validation(workflow)["report"])
            if mode == "local_reflow" else None
        ),
        "layout_result": attempt_dir / "layout-result.json",
        "backend_evidence": attempt_dir / "backend-evidence.json",
        "layout_patch": attempt_dir / "local-layout.patch.json",
        "candidate": attempt_dir / "candidate.drawio",
        "validation_report": validation_dir / "validation-report.json",
        "validation_receipt_legacy": (
            validation_dir / "validation-receipt.json"
        ),
        "validation_receipt": (
            validation_dir / "validation-receipt.v2.json"
        ),
        "validated_artifact": validation_dir / "validated-artifact.drawio",
        "validator_stdout": validation_dir / "validator.stdout",
        "validator_stderr": validation_dir / "validator.stderr",
        "preservation": attempt_dir / "preservation.json",
        "comparison": attempt_dir / "comparison.json",
    }
    atomic_write_bytes(request_path, canonical_json_bytes(request) + b"\n")
    request_event_descriptor = _relative_file(run_dir, request_path)
    started_artifacts = {"layout_request": request_event_descriptor}
    intake_descriptor = workflow.get("diagram_intake") or {}
    intake_relative = intake_descriptor.get("path")
    if intake_relative:
        intake_file = run_dir / intake_relative
        if intake_file.is_file():
            started_artifacts["diagram_intake"] = _relative_file(
                run_dir, intake_file
            )
    schedule_descriptor = workflow.get("layout_schedule") or {}
    schedule_path = Path(schedule_descriptor.get("path", ""))
    if schedule_path.is_file():
        started_artifacts["layout_schedule"] = _relative_file(
            run_dir,
            schedule_path,
        )
    lifecycle_v2.record_tool_attempt(
        run_dir,
        tool="layout-engine",
        attempt_id=request["request_id"],
        status="started",
        artifact_snapshots=started_artifacts,
        payload={
            "request_sha256": request_sha256,
            "semantic_plan_sha256": semantic_plan_sha256,
            "strategy": strategy_id,
            "attempt_key": list(attempt_key),
            **copy.deepcopy(context),
        },
    )
    workflow["layout_attempt_keys"].append(serialized_key)
    write_workflow(run_dir, workflow)

    backend_config = {
        "layout_backend": backend,
        "layout_timeout_seconds": max(0.1, min(float(timeout), 30.0)),
    }
    stage = "backend"
    try:
        backend_attempt = layout_backend.run_layout(
            request,
            config=backend_config,
            attempted_keys=frozenset(),
        )
        backend_evidence, backend_evidence_path = _relocate_backend_evidence(
            run_dir,
            attempt_dir,
            backend_attempt.evidence,
        )
        paths["backend_evidence"] = backend_evidence_path
        if backend_evidence.get("request_sha256") != request_sha256:
            raise supervisor.SupervisorError(
                "backend evidence differs from the immutable layout request"
            )
        stage = "result_validation"
        result = dict(backend_attempt.result)
        atomic_write_bytes(
            paths["layout_result"],
            canonical_json_bytes(result) + b"\n",
        )
        layout_contracts.require_layout_result(
            result,
            expected_request_sha256=request_sha256,
        )
        if mode == "local_reflow":
            if baseline is None or baseline_artifact is None:
                raise supervisor.SupervisorError(
                    "local layout attempt requires immutable baseline spec and artifact"
                )
            stage = "patch_synthesis"
            patch = _layout_result_patch(
                baseline_artifact, request, result,
            )
            atomic_write_bytes(
                paths["layout_patch"],
                canonical_json_bytes(patch) + b"\n",
            )
            stage = "patch_apply"
            supervisor.apply_patch_file(
                baseline_artifact,
                paths["layout_patch"],
                paths["candidate"],
            )
        else:
            stage = "render"
            layout_renderer.render_layout(
                semantic_plan, result, paths["candidate"],
            )
        stage = "strict_validation"
        receipt_value = supervisor.run_validation(
            paths["candidate"],
            run_dir,
            attempt_id=request["request_id"],
        )
        stage = "validation_receipt"
        _, receipt_v2_path = lifecycle_v2.mirror_validation_receipt(
            run_dir,
            legacy_receipt_path=paths["validation_receipt_legacy"],
        )
        paths["validation_receipt"] = receipt_v2_path
        verification = lifecycle_v2.verify_v2_receipt(
            run_dir, receipt_v2_path,
        )
        if not verification["valid"]:
            raise supervisor.SupervisorError(
                "layout validation receipt failed: "
                f"{verification['diagnostics']}"
            )
        report = supervisor.load_json(paths["validation_report"])
        quality = supervisor.quality_vector(
            report,
            profile_version=workflow.get("quality_profile_version", 2),
        )
        stage = "preservation"
        preservation = _verify_candidate_preservation(
            workflow,
            (
                baseline_artifact
                if baseline_artifact is not None
                else paths["candidate"]
            ),
            paths["candidate"],
            candidate_origin=(
                "layout_intent" if mode == "local_reflow" else "create"
            ),
            locked_cells=(
                _locked_cells_from_layout_request(request)
                if mode == "local_reflow" else None
            ),
        )
        atomic_write_bytes(
            paths["preservation"],
            canonical_json_bytes(preservation) + b"\n",
        )
        comparison = None
        if mode == "local_reflow":
            stage = "comparison"
            baseline_report_path = Path(
                _working_validation(workflow)["report"]
            )
            baseline_report = supervisor.load_json(baseline_report_path)
            baseline_semantic = supervisor.artifact_invariants(
                baseline_artifact
            )[0]
            candidate_semantic = supervisor.artifact_invariants(
                paths["candidate"]
            )[0]
            comparison = supervisor.compare_reports(
                baseline_report,
                report,
                semantic_equal=(
                    baseline_semantic == candidate_semantic
                ),
                untouched_equal=preservation["valid"],
                profile_version=workflow.get(
                    "quality_profile_version", 2,
                ),
            )
            atomic_write_bytes(
                paths["comparison"],
                canonical_json_bytes(comparison) + b"\n",
            )
    except Exception as exc:
        if not paths["backend_evidence"].is_file():
            backend_failure = copy.deepcopy(
                getattr(exc, "evidence", None) or {}
            )
            backend_failure.update({
                "request_sha256": request_sha256,
                "strategy": strategy_id,
                "status": "failed",
                "error": str(exc),
                "failure_stage": stage,
            })
            _, backend_evidence_path = _relocate_backend_evidence(
                run_dir, attempt_dir, backend_failure,
            )
            paths["backend_evidence"] = backend_evidence_path
        return _record_failed_layout_attempt(
            run_dir,
            workflow,
            request=request,
            request_sha256=request_sha256,
            attempt_key=serialized_key,
            context=context,
            stage=stage,
            error=exc,
            paths=paths,
        )
    artifacts = _existing_layout_attempt_artifacts(run_dir, paths)
    lifecycle_v2.record_tool_attempt(
        run_dir,
        tool="layout-engine",
        attempt_id=request["request_id"],
        status="completed",
        artifact_snapshots=artifacts,
        payload={
            "request_sha256": request_sha256,
            "result_sha256": canonical_json_sha256(result),
            "semantic_plan_sha256": semantic_plan_sha256,
            "strategy": strategy_id,
            "strict_passed": verification["strict_passed"],
            "quality_vector": quality,
            "validation_result": receipt_value["result"],
            "preservation_valid": preservation["valid"],
            "comparison": copy.deepcopy(comparison),
            **copy.deepcopy(context),
        },
    )
    if mode == "create":
        lifecycle_v2.record_candidate_evidence(
            run_dir,
            attempt_id=request["request_id"],
            accepted=verification["strict_passed"],
            artifact_snapshots=artifacts,
            payload={
                "reason": (
                    "strict_validation_passed"
                    if verification["strict_passed"]
                    else "strict_validation_failed"
                ),
                "request_sha256": request_sha256,
                "quality_vector": quality,
            },
        )
    return {
        "status": "completed",
        "attempt_id": request["request_id"],
        "strategy": strategy_id,
        "strategy_options": copy.deepcopy(strategy_options),
        "attempt_key": serialized_key,
        "request_sha256": request_sha256,
        "semantic_plan_sha256": semantic_plan_sha256,
        **copy.deepcopy(context),
        "layout_request": _workflow_file_descriptor(request_path),
        "layout_baseline": (
            _workflow_file_descriptor(paths["layout_baseline"])
            if mode == "local_reflow" else None
        ),
        "baseline_validation_report": (
            _workflow_file_descriptor(
                paths["baseline_validation_report"]
            )
            if mode == "local_reflow" else None
        ),
        "layout_result": _workflow_file_descriptor(
            paths["layout_result"]
        ),
        "backend_evidence": _workflow_file_descriptor(
            paths["backend_evidence"]
        ),
        "candidate": _workflow_file_descriptor(paths["candidate"]),
        "validation": {
            "report": str(paths["validation_report"].resolve()),
            "report_sha256": supervisor.sha256_file(
                paths["validation_report"]
            ),
            "receipt": str(
                paths["validation_receipt_legacy"].resolve()
            ),
            "receipt_sha256": supervisor.sha256_file(
                paths["validation_receipt_legacy"]
            ),
            "receipt_v2": str(paths["validation_receipt"].resolve()),
            "receipt_v2_sha256": supervisor.sha256_file(
                paths["validation_receipt"]
            ),
            "strict_passed": verification["strict_passed"],
        },
        "quality_vector": quality,
        "backend": backend_evidence.get("backend_selected"),
        "fallback_reason": backend_evidence.get("fallback_reason"),
        "layout_patch": (
            _workflow_file_descriptor(paths["layout_patch"])
            if mode == "local_reflow" else None
        ),
        "preservation": preservation,
        "preservation_evidence": _workflow_file_descriptor(
            paths["preservation"]
        ),
        "comparison": comparison,
        "comparison_evidence": (
            _workflow_file_descriptor(paths["comparison"])
            if mode == "local_reflow" else None
        ),
    }


def _layout_deadline_epoch(workflow, timeout):
    value = workflow.get("layout_deadline_epoch")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    deadline = time.time() + max(1.0, float(timeout))
    workflow["layout_deadline_epoch"] = deadline
    workflow["layout_deadline_at"] = datetime.fromtimestamp(
        deadline,
        tz=timezone.utc,
    ).isoformat().replace("+00:00", "Z")
    return deadline


def _quality_sort_key(attempt):
    vector = attempt["quality_vector"]
    return tuple(vector.values()) + (attempt["request_sha256"],)


def _canonical_layout_selection(completed):
    if not completed:
        raise supervisor.SupervisorError(
            "cannot select a layout without completed attempts"
        )
    strategy_order = {
        name: index for index, (name, _) in enumerate(LAYOUT_STRATEGIES)
    }
    ordered = sorted(
        completed,
        key=lambda attempt: (
            strategy_order.get(attempt["strategy"], len(strategy_order)),
            attempt["request_sha256"],
        ),
    )
    strict = [
        attempt
        for attempt in ordered
        if attempt["validation"]["strict_passed"]
    ]
    return strict[0] if strict else min(ordered, key=_quality_sort_key)


def _verified_layout_file(run_dir, descriptor, *, label):
    if not isinstance(descriptor, dict):
        raise supervisor.SupervisorError(
            f"persisted layout {label} descriptor is not an object"
        )
    path_value = descriptor.get("path")
    expected_sha256 = descriptor.get("sha256")
    if not isinstance(path_value, str) or not isinstance(expected_sha256, str):
        raise supervisor.SupervisorError(
            f"persisted layout {label} descriptor is incomplete"
        )
    path = Path(path_value).resolve()
    if not _inside(path, run_dir) or not path.is_file():
        raise supervisor.SupervisorError(
            f"persisted layout {label} escaped the run or is missing"
        )
    if supervisor.sha256_file(path) != expected_sha256:
        raise supervisor.SupervisorError(
            f"persisted layout {label} hash differs from its descriptor"
        )
    expected_length = descriptor.get("byte_length")
    if expected_length is not None and path.stat().st_size != expected_length:
        raise supervisor.SupervisorError(
            f"persisted layout {label} length differs from its descriptor"
        )
    return path


def _verified_layout_validation_file(
    run_dir,
    validation,
    *,
    path_field,
    hash_field,
    label,
):
    if not isinstance(validation, dict):
        raise supervisor.SupervisorError(
            "persisted layout validation descriptor is not an object"
        )
    return _verified_layout_file(
        run_dir,
        {
            "path": validation.get(path_field),
            "sha256": validation.get(hash_field),
        },
        label=label,
    )


def _layout_attempt_event_artifacts(run_dir, paths):
    return {
        "layout_request": _relative_file(run_dir, paths["layout_request"]),
        "layout_result": _relative_file(run_dir, paths["layout_result"]),
        "backend_evidence": _relative_file(run_dir, paths["backend_evidence"]),
        "candidate": _relative_file(run_dir, paths["candidate"]),
        "validation_report": _relative_file(run_dir, paths["validation_report"]),
        "validation_receipt": _relative_file(
            run_dir, paths["validation_receipt_v2"]
        ),
    }


def _verified_layout_event_artifacts(run_dir, attempt_id, event):
    snapshots = event.get("payload", {}).get("artifact_snapshots")
    if not isinstance(snapshots, dict):
        raise supervisor.SupervisorError(
            f"layout attempt {attempt_id} event artifacts are invalid"
        )
    for name, descriptor in snapshots.items():
        if not isinstance(descriptor, dict) or not isinstance(
            descriptor.get("byte_length"), int
        ):
            raise supervisor.SupervisorError(
                f"layout attempt {attempt_id} event artifact {name} is incomplete"
            )
        absolute = copy.deepcopy(descriptor)
        absolute["path"] = str(
            (Path(run_dir).resolve() / str(descriptor.get("path", ""))).resolve()
        )
        _verified_layout_file(
            run_dir, absolute, label=f"{attempt_id} event artifact {name}",
        )
    return snapshots


def _verify_failed_layout_attempt(run_dir, workflow, attempt, replayed):
    attempt_id = attempt.get("attempt_id") if isinstance(attempt, dict) else None
    if not attempt_id or attempt.get("status") != "failed":
        raise supervisor.SupervisorError("persisted failed layout attempt is invalid")
    request_path = _verified_layout_file(
        run_dir, attempt.get("layout_request"),
        label=f"{attempt_id} layout request",
    )
    failure_path = _verified_layout_file(
        run_dir, attempt.get("failure_evidence"),
        label=f"{attempt_id} failure evidence",
    )
    request = supervisor.load_json(request_path)
    layout_contracts.require_layout_request(request)
    failure = supervisor.load_json(failure_path)
    request_sha256 = canonical_json_sha256(request)
    context = {
        "mode": request.get("mode"),
        "workflow_iteration": attempt.get("workflow_iteration"),
        "baseline_sha256": attempt.get("baseline_sha256"),
        "scope_sha256": canonical_json_sha256(request["scope"]),
    }
    expected_key = "|".join(layout_backend.attempt_key(request))
    if any((
        request.get("request_id") != attempt_id,
        request_sha256 != attempt.get("request_sha256"),
        request.get("semantic_plan_sha256")
        != workflow["semantic_plan_v2"]["sha256"],
        attempt.get("semantic_plan_sha256")
        != request.get("semantic_plan_sha256"),
        request.get("strategy") != attempt.get("strategy"),
        request.get("strategy_options", {})
        != attempt.get("strategy_options", {}),
        attempt.get("attempt_key") != expected_key,
        expected_key not in workflow.get("layout_attempt_keys", []),
        any(attempt.get(key) != value for key, value in context.items()),
    )):
        raise supervisor.SupervisorError(
            f"failed layout attempt {attempt_id} differs from its request"
        )
    events = [
        record["event"] for record in replayed["events"]
        if record["event"].get("event_type") == "tool_attempt"
        and record["event"].get("payload", {}).get("tool") == "layout-engine"
        and record["event"].get("payload", {}).get("attempt_id") == attempt_id
    ]
    if [event["payload"].get("status") for event in events] != [
        "started", "failed",
    ]:
        raise supervisor.SupervisorError(
            f"failed layout attempt {attempt_id} has invalid event sequence"
        )
    started, failed_event = events
    started_artifacts = _verified_layout_event_artifacts(
        run_dir, attempt_id, started,
    )
    failed_artifacts = _verified_layout_event_artifacts(
        run_dir, attempt_id, failed_event,
    )
    expected_request = _relative_file(run_dir, request_path)
    expected_failure = _relative_file(run_dir, failure_path)
    failed_payload = failed_event["payload"]
    bound = {
        "request_sha256": request_sha256,
        "semantic_plan_sha256": request["semantic_plan_sha256"],
        "strategy": request["strategy"],
        **context,
        "failure_stage": failure.get("failure_stage"),
    }
    if any((
        started_artifacts.get("layout_request") != expected_request,
        failed_artifacts != {
            **failure.get("artifacts", {}),
            "failure_evidence": expected_failure,
        },
        any(failure.get(key) != attempt.get(key) for key in (
            "attempt_id", "request_sha256", "semantic_plan_sha256", "strategy",
            "strategy_options", "mode", "workflow_iteration", "baseline_sha256",
            "scope_sha256", "failure_stage", "error", "artifacts",
        )),
        any(failed_payload.get(key) != value for key, value in bound.items()),
        failed_payload.get("error") != failure.get("error", "")[-1000:],
        any(started["payload"].get(key) != value for key, value in {
            **bound, "attempt_key": layout_backend.attempt_key(request),
        }.items() if key != "failure_stage"),
    )):
        raise supervisor.SupervisorError(
            f"failed layout attempt {attempt_id} evidence differs"
        )
    return copy.deepcopy(attempt)


def _verify_persisted_layout_attempt(
    run_dir, workflow, attempt, replayed,
):
    """Fail closed unless a workflow attempt is bound to immutable run evidence."""
    if isinstance(attempt, dict) and attempt.get("status") == "failed":
        return _verify_failed_layout_attempt(
            run_dir, workflow, attempt, replayed,
        )
    if not isinstance(attempt, dict) or attempt.get("status") != "completed":
        raise supervisor.SupervisorError(
            "persisted layout attempt is not a completed object"
        )
    attempt_id = attempt.get("attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id:
        raise supervisor.SupervisorError(
            "persisted layout attempt has no attempt id"
        )
    paths = {
        name: _verified_layout_file(
            run_dir,
            attempt.get(name),
            label=f"{attempt_id} {name}",
        )
        for name in (
            "layout_request",
            "layout_result",
            "backend_evidence",
            "candidate",
        )
    }
    validation = attempt.get("validation")
    paths.update(
        {
            "validation_report": _verified_layout_validation_file(
                run_dir,
                validation,
                path_field="report",
                hash_field="report_sha256",
                label=f"{attempt_id} validation report",
            ),
            "validation_receipt": _verified_layout_validation_file(
                run_dir,
                validation,
                path_field="receipt",
                hash_field="receipt_sha256",
                label=f"{attempt_id} validation receipt",
            ),
            "validation_receipt_v2": _verified_layout_validation_file(
                run_dir,
                validation,
                path_field="receipt_v2",
                hash_field="receipt_v2_sha256",
                label=f"{attempt_id} validation receipt v2",
            ),
        }
    )
    request = supervisor.load_json(paths["layout_request"])
    layout_contracts.require_layout_request(request)
    mode = request.get("mode")
    if mode not in {"create", "local_reflow"}:
        raise supervisor.SupervisorError(
            f"persisted layout attempt {attempt_id} has unsupported mode"
        )
    request_sha256 = canonical_json_sha256(request)
    semantic_plan_sha256 = workflow["semantic_plan_v2"]["sha256"]
    if (
        request.get("request_id") != attempt_id
        or request_sha256 != attempt.get("request_sha256")
        or request.get("semantic_plan_sha256") != semantic_plan_sha256
        or attempt.get("semantic_plan_sha256") != semantic_plan_sha256
        or request.get("strategy") != attempt.get("strategy")
        or request.get("strategy_options", {})
        != attempt.get("strategy_options", {})
    ):
        raise supervisor.SupervisorError(
            f"persisted layout attempt {attempt_id} differs from its request"
        )
    expected_key = "|".join(layout_backend.attempt_key(request))
    attempt_keys = workflow.get("layout_attempt_keys")
    if (
        attempt.get("attempt_key") != expected_key
        or not isinstance(attempt_keys, list)
        or expected_key not in attempt_keys
    ):
        raise supervisor.SupervisorError(
            f"persisted layout attempt {attempt_id} has no matching progress key"
        )
    result = supervisor.load_json(paths["layout_result"])
    layout_contracts.require_layout_result(
        result,
        expected_request_sha256=request_sha256,
    )
    backend_evidence = supervisor.load_json(paths["backend_evidence"])
    if (
        backend_evidence.get("request_sha256") != request_sha256
        or attempt.get("backend") != backend_evidence.get("backend_selected")
        or attempt.get("fallback_reason")
        != backend_evidence.get("fallback_reason")
    ):
        raise supervisor.SupervisorError(
            f"persisted layout attempt {attempt_id} backend evidence differs"
        )
    receipt_verification = lifecycle_v2.verify_v2_receipt(
        run_dir,
        paths["validation_receipt_v2"],
    )
    if (
        not receipt_verification["valid"]
        or receipt_verification["strict_passed"]
        != validation.get("strict_passed")
    ):
        raise supervisor.SupervisorError(
            f"persisted layout attempt {attempt_id} validation receipt differs"
        )
    receipt_v2 = supervisor.load_json(paths["validation_receipt_v2"])
    candidate_sha256 = supervisor.sha256_file(paths["candidate"])
    if receipt_v2.get("bindings", {}).get("candidate_sha256") != candidate_sha256:
        raise supervisor.SupervisorError(
            f"persisted layout attempt {attempt_id} candidate is not receipt-bound"
        )
    report = supervisor.load_json(paths["validation_report"])
    quality = supervisor.quality_vector(
        report,
        profile_version=workflow.get("quality_profile_version", 2),
    )
    if quality != attempt.get("quality_vector"):
        raise supervisor.SupervisorError(
            f"persisted layout attempt {attempt_id} quality vector differs"
        )
    if mode == "local_reflow":
        for name, field in (
            ("layout_baseline", "layout_baseline"),
            ("baseline_validation_report", "baseline_validation_report"),
            ("layout_patch", "layout_patch"),
            ("preservation", "preservation_evidence"),
            ("comparison", "comparison_evidence"),
        ):
            paths[name] = _verified_layout_file(
                run_dir, attempt.get(field),
                label=f"{attempt_id} {name}",
            )
        context = {
            "workflow_iteration": attempt.get("workflow_iteration"),
            "baseline_sha256": supervisor.sha256_file(paths["layout_baseline"]),
            "scope_sha256": canonical_json_sha256(request["scope"]),
        }
        if (
            attempt.get("mode") != mode
            or any(attempt.get(key) != value for key, value in context.items())
        ):
            raise supervisor.SupervisorError(
                f"persisted layout attempt {attempt_id} local context differs"
            )
        patch = supervisor.load_json(paths["layout_patch"])
        supervisor.validate_patch_contract(patch)
        with tempfile.TemporaryDirectory(prefix="layout-attempt-replay-") as temporary:
            replay_candidate = Path(temporary) / "candidate.drawio"
            supervisor.apply_patch_file(
                paths["layout_baseline"], paths["layout_patch"], replay_candidate,
            )
            replay_matches = supervisor.sha256_file(replay_candidate) == candidate_sha256
        preservation = supervisor.load_json(paths["preservation"])
        comparison = supervisor.load_json(paths["comparison"])
        expected_preservation = _verify_locked_cell_hashes(
            paths["layout_baseline"],
            paths["candidate"],
            _locked_cells_from_layout_request(request),
        )
        expected_comparison = supervisor.compare_reports(
            supervisor.load_json(paths["baseline_validation_report"]),
            report,
            semantic_equal=supervisor.artifact_invariants(
                paths["layout_baseline"]
            )[0] == supervisor.artifact_invariants(paths["candidate"])[0],
            untouched_equal=preservation["valid"],
            profile_version=workflow.get("quality_profile_version", 2),
        )
        if any((
            not replay_matches,
            patch.get("baseline", {}).get("artifact_sha256")
            != context["baseline_sha256"],
            preservation != attempt.get("preservation"),
            preservation != expected_preservation,
            comparison != attempt.get("comparison"),
            comparison != expected_comparison,
        )):
            raise supervisor.SupervisorError(
                f"persisted layout attempt {attempt_id} preservation or comparison differs"
            )

    expected_artifacts = _layout_attempt_event_artifacts(run_dir, paths)
    if mode == "local_reflow":
        expected_artifacts.update({
            name: _relative_file(run_dir, paths[name])
            for name in ("layout_baseline", "baseline_validation_report",
                         "layout_patch", "preservation", "comparison")
        })
    completed_events = []
    candidate_events = []
    expected_candidate_event = (
        "candidate_accepted"
        if validation.get("strict_passed")
        else "candidate_rejected"
    )
    for record in replayed["events"]:
        event = record["event"]
        payload = event.get("payload", {})
        if (
            event.get("event_type") == "tool_attempt"
            and payload.get("tool") == "layout-engine"
            and payload.get("attempt_id") == attempt_id
            and payload.get("status") == "completed"
        ):
            completed_events.append(event)
        if (
            event.get("event_type") == expected_candidate_event
            and payload.get("attempt_id") == attempt_id
        ):
            candidate_events.append(event)
    if len(completed_events) != 1 or len(candidate_events) > 1:
        raise supervisor.SupervisorError(
            f"persisted layout attempt {attempt_id} lacks unique immutable evidence"
        )
    completed_payload = completed_events[0]["payload"]
    if (
        completed_payload.get("request_sha256") != request_sha256
        or completed_payload.get("semantic_plan_sha256")
        != semantic_plan_sha256
        or completed_payload.get("strategy") != attempt.get("strategy")
        or completed_payload.get("strict_passed")
        != validation.get("strict_passed")
        or completed_payload.get("quality_vector") != quality
        or (
            mode == "local_reflow"
            and any(
                completed_payload.get(key) != attempt.get(key)
                for key in ("mode", "workflow_iteration",
                            "baseline_sha256", "scope_sha256")
            )
        )
    ):
        raise supervisor.SupervisorError(
            f"persisted layout attempt {attempt_id} event bindings differ"
        )
    for event in completed_events + candidate_events:
        snapshots = event["payload"].get("artifact_snapshots", {})
        if any(
            snapshots.get(name) != descriptor
            for name, descriptor in expected_artifacts.items()
        ):
            raise supervisor.SupervisorError(
                f"persisted layout attempt {attempt_id} artifacts differ from the ledger"
            )
    return copy.deepcopy(attempt)


def _recover_layout_attempts_from_ledger(
    run_dir,
    workflow,
    replayed,
    *,
    known_attempt_ids,
    mode="create",
):
    """Rebuild the workflow index after a crash using terminal events."""
    recovered = []
    for record in replayed["events"]:
        event = record["event"]
        payload = event.get("payload", {})
        attempt_id = payload.get("attempt_id")
        status = payload.get("status")
        if (
            event.get("event_type") != "tool_attempt"
            or payload.get("tool") != "layout-engine"
            or status not in {"completed", "failed"}
            or not isinstance(attempt_id, str)
            or attempt_id in known_attempt_ids
        ):
            continue
        snapshots = payload.get("artifact_snapshots", {})
        if not isinstance(snapshots, dict) or "layout_request" not in snapshots:
            raise supervisor.SupervisorError(
                f"terminal layout attempt {attempt_id} has incomplete ledger evidence"
            )

        def event_path(name):
            path = (Path(run_dir).resolve() / snapshots[name]["path"]).resolve()
            if not _inside(path, run_dir):
                raise supervisor.SupervisorError(
                    f"completed layout attempt {attempt_id} escaped the run"
                )
            return path

        request_path = event_path("layout_request")
        request = supervisor.load_json(request_path)
        if request.get("mode") != mode:
            continue
        if status == "failed":
            failure_path = event_path("failure_evidence")
            failure = supervisor.load_json(failure_path)
            attempt = {
                **copy.deepcopy(failure),
                "attempt_key": "|".join(layout_backend.attempt_key(request)),
                "layout_request": _workflow_file_descriptor(request_path),
                "failure_evidence": _workflow_file_descriptor(failure_path),
            }
            recovered.append(_verify_persisted_layout_attempt(
                run_dir, workflow, attempt, replayed,
            ))
            known_attempt_ids.add(attempt_id)
            continue
        required = {
            "layout_request", "layout_result", "backend_evidence",
            "candidate", "validation_report", "validation_receipt",
        }
        if mode == "local_reflow":
            required.update({
                "layout_baseline", "baseline_validation_report",
                "layout_patch", "preservation", "comparison",
            })
        if not required.issubset(snapshots):
            raise supervisor.SupervisorError(
                f"completed layout attempt {attempt_id} has incomplete ledger evidence"
            )
        paths = {name: event_path(name) for name in required}
        semantic_plan_sha256 = workflow["semantic_plan_v2"]["sha256"]
        if (
            request.get("semantic_plan_sha256") != semantic_plan_sha256
            or payload.get("semantic_plan_sha256") != semantic_plan_sha256
        ):
            raise supervisor.SupervisorError(
                f"completed create layout attempt {attempt_id} belongs to another plan"
            )
        backend_evidence = supervisor.load_json(paths["backend_evidence"])
        receipt_v2 = supervisor.load_json(paths["validation_receipt"])
        attempt_root = Path(run_dir) / "attempts" / attempt_id
        legacy_receipt = attempt_root / "validation-receipt.json"
        if not legacy_receipt.is_file():
            raise supervisor.SupervisorError(
                f"completed layout attempt {attempt_id} lost its legacy receipt"
            )
        strict_passed = bool(payload.get("strict_passed"))
        attempt = {
            "status": "completed",
            "attempt_id": attempt_id,
            "strategy": payload.get("strategy"),
            "strategy_options": copy.deepcopy(
                request.get("strategy_options", {})
            ),
            "attempt_key": "|".join(layout_backend.attempt_key(request)),
            "request_sha256": payload.get("request_sha256"),
            "semantic_plan_sha256": payload.get("semantic_plan_sha256"),
            "layout_request": _workflow_file_descriptor(
                paths["layout_request"]
            ),
            "layout_result": _workflow_file_descriptor(paths["layout_result"]),
            "backend_evidence": _workflow_file_descriptor(
                paths["backend_evidence"]
            ),
            "candidate": _workflow_file_descriptor(paths["candidate"]),
            "validation": {
                "report": str(paths["validation_report"]),
                "report_sha256": supervisor.sha256_file(
                    paths["validation_report"]
                ),
                "receipt": str(legacy_receipt.resolve()),
                "receipt_sha256": supervisor.sha256_file(legacy_receipt),
                "receipt_v2": str(paths["validation_receipt"]),
                "receipt_v2_sha256": supervisor.sha256_file(
                    paths["validation_receipt"]
                ),
                "strict_passed": strict_passed,
            },
            "quality_vector": copy.deepcopy(payload.get("quality_vector")),
            "backend": backend_evidence.get("backend_selected"),
            "fallback_reason": backend_evidence.get("fallback_reason"),
        }
        if mode == "local_reflow":
            attempt.update({
                **{
                    key: payload.get(key)
                    for key in ("mode", "workflow_iteration",
                                "baseline_sha256", "scope_sha256")
                },
                **{
                    field: _workflow_file_descriptor(paths[name])
                    for name, field in (
                        ("layout_baseline", "layout_baseline"),
                        ("baseline_validation_report",
                         "baseline_validation_report"),
                        ("layout_patch", "layout_patch"),
                        ("preservation", "preservation_evidence"),
                        ("comparison", "comparison_evidence"),
                    )
                },
                "preservation": supervisor.load_json(
                    paths["preservation"]
                ),
                "comparison": supervisor.load_json(paths["comparison"]),
            })
        if (
            receipt_v2.get("attempt_id") != attempt_id
            or request.get("request_id") != attempt_id
        ):
            raise supervisor.SupervisorError(
                f"completed layout attempt {attempt_id} ledger ids differ"
            )
        recovered.append(
            _verify_persisted_layout_attempt(
                run_dir,
                workflow,
                attempt,
                replayed,
            )
        )
        known_attempt_ids.add(attempt_id)
    return recovered


def _strategy_attempt_key(workflow, semantic_plan, adapter_input, strategy):
    strategy_id, strategy_options = strategy
    requested_backend = str(adapter_input.options.get("backend", "auto"))
    backend = "python" if strategy_id == "python-fallback" else requested_backend
    request = layout_model.build_layout_request(
        semantic_plan,
        run_id=workflow["run_id"],
        semantic_plan_sha256=workflow["semantic_plan_v2"]["sha256"],
        mode="create",
        backend=backend,
        strategy_id=strategy_id,
        strategy_options=strategy_options,
        quality_profile_version=workflow.get("quality_profile_version", 2),
        scope=None,
    )
    return "|".join(layout_backend.attempt_key(request))


def _run_generic_create_layouts(
    run_dir,
    workflow,
    semantic_plan,
    adapter_input,
    *,
    timeout,
):
    """Run the bounded strategy schedule over one immutable semantic plan."""
    deadline = _layout_deadline_epoch(workflow, timeout)
    schedule_path = Path(run_dir) / "layout-attempts" / "layout-schedule.json"
    schedule_value = {
        "schema_version": 1,
        "run_id": workflow["run_id"],
        "semantic_plan_sha256": workflow["semantic_plan_v2"]["sha256"],
        "quality_profile_version": workflow.get("quality_profile_version", 2),
        "deadline_epoch": deadline,
        "deadline_at": workflow["layout_deadline_at"],
        "strategies": [
            {"strategy": name, "strategy_options": copy.deepcopy(options)}
            for name, options in LAYOUT_STRATEGIES
        ],
    }
    if schedule_path.is_file():
        if supervisor.load_json(schedule_path) != schedule_value:
            raise supervisor.SupervisorError(
                "persisted layout schedule differs from the resumed request"
            )
    else:
        atomic_write_bytes(
            schedule_path,
            canonical_json_bytes(schedule_value) + b"\n",
        )
    workflow["layout_schedule"] = _workflow_file_descriptor(schedule_path)
    workflow.setdefault("layout_attempts", [])
    workflow.setdefault("layout_attempt_keys", [])
    replayed = lifecycle_v2.require_mutable(run_dir)
    if len(workflow["layout_attempt_keys"]) != len(
        set(workflow["layout_attempt_keys"])
    ):
        raise supervisor.SupervisorError(
            "persisted layout progress contains duplicate attempt keys"
        )
    verified_attempts = [
        _verify_persisted_layout_attempt(
            run_dir,
            workflow,
            attempt,
            replayed,
        )
        for attempt in workflow["layout_attempts"]
        if attempt.get("status") in {"completed", "failed"}
    ]
    completed = [
        attempt for attempt in verified_attempts
        if attempt["status"] == "completed"
    ]
    recovered = _recover_layout_attempts_from_ledger(
        run_dir,
        workflow,
        replayed,
        known_attempt_ids={
            attempt["attempt_id"] for attempt in workflow["layout_attempts"]
        },
    )
    if recovered:
        completed.extend(
            attempt for attempt in recovered
            if attempt["status"] == "completed"
        )
        workflow["layout_attempts"].extend(copy.deepcopy(recovered))
    if len({attempt["attempt_id"] for attempt in completed}) != len(completed):
        raise supervisor.SupervisorError(
            "persisted layout progress contains duplicate completed attempts"
        )
    persisted_selected = workflow.get("selected_layout_attempt")
    if persisted_selected is not None:
        verified_selected = _verify_persisted_layout_attempt(
            run_dir,
            workflow,
            persisted_selected,
            replayed,
        )
        matching = [
            attempt
            for attempt in completed
            if attempt["attempt_id"] == verified_selected["attempt_id"]
        ]
        if (
            len(matching) != 1
            or canonical_json_sha256(matching[0])
            != canonical_json_sha256(verified_selected)
        ):
            raise supervisor.SupervisorError(
                "persisted selected layout attempt differs from completed progress"
            )
        canonical_selected = _canonical_layout_selection(completed)
        expected_exhausted = not canonical_selected["validation"]["strict_passed"]
        if (
            canonical_json_sha256(verified_selected)
            != canonical_json_sha256(canonical_selected)
            or workflow.get("layout_strategy_exhausted")
            is not expected_exhausted
        ):
            raise supervisor.SupervisorError(
                "persisted selected layout attempt is noncanonical"
            )
        return verified_selected
    strict_completed = [
        attempt
        for attempt in completed
        if attempt["validation"]["strict_passed"]
    ]
    if strict_completed:
        selected = _canonical_layout_selection(completed)
        workflow["selected_layout_attempt"] = copy.deepcopy(selected)
        workflow["layout_strategy_exhausted"] = False
        write_workflow(run_dir, workflow)
        return selected
    write_workflow(run_dir, workflow)
    lifecycle_v2.record_tool_attempt(
        run_dir,
        tool="layout-engine",
        attempt_id="layout-schedule",
        status="started",
        artifact_snapshots={
            "layout_schedule": _relative_file(run_dir, schedule_path),
        },
        payload={
            "semantic_plan_sha256": workflow["semantic_plan_v2"]["sha256"],
            "deadline_at": workflow["layout_deadline_at"],
            "strategy_count": len(LAYOUT_STRATEGIES),
        },
    )
    for index, strategy in enumerate(LAYOUT_STRATEGIES):
        if index >= 4 or time.time() >= deadline:
            break
        if (
            _strategy_attempt_key(
                workflow,
                semantic_plan,
                adapter_input,
                strategy,
            )
            in workflow["layout_attempt_keys"]
        ):
            continue
        remaining = max(0.1, deadline - time.time())
        try:
            attempt = execute_layout_attempt(
                workflow,
                semantic_plan,
                run_dir=run_dir,
                adapter_input=adapter_input,
                mode="create",
                scope=None,
                strategy=strategy,
                timeout=remaining,
            )
        except Exception as exc:
            workflow.setdefault("layout_failures", []).append(
                {
                    "strategy": strategy[0],
                    "error": str(exc)[-1000:],
                }
            )
            write_workflow(run_dir, workflow)
            continue
        if attempt.get("status") != "completed":
            continue
        workflow["layout_attempts"].append(attempt)
        completed.append(attempt)
        write_workflow(run_dir, workflow)
        if attempt["validation"]["strict_passed"]:
            break
    if not completed:
        raise supervisor.SupervisorError(
            "all bounded deterministic layout strategies failed before rendering a candidate"
        )
    strict = [
        attempt
        for attempt in completed
        if attempt["validation"]["strict_passed"]
    ]
    selected = _canonical_layout_selection(completed)
    workflow["selected_layout_attempt"] = copy.deepcopy(selected)
    workflow["layout_strategy_exhausted"] = not bool(strict)
    write_workflow(run_dir, workflow)
    return selected


def _adopt_create_layout_attempt(
    run_dir,
    workflow,
    selected,
    *,
    request,
    max_iterations,
):
    accepted = Path(run_dir) / "accepted" / "baseline.drawio"
    atomic_copy(Path(selected["candidate"]["path"]), accepted)
    spec = supervisor.make_spec(
        accepted,
        [source_ref_for_request(workflow["run_id"], request)],
    )
    supervisor.write_json(Path(run_dir) / "diagram-spec.json", spec)
    current_state = supervisor.load_state(run_dir)
    if current_state is None:
        supervisor.transition(
            run_dir,
            "analyzed",
            artifact=accepted,
            max_attempts=max_iterations,
        )
    elif current_state["state"] == "patching":
        supervisor.transition(run_dir, "validating")
    elif current_state["state"] != "analyzed":
        raise supervisor.SupervisorError(
            "create layout adoption requires analyzed or patching state"
        )
    supervisor.run_validation(
        accepted,
        run_dir,
        attempt_id="baseline",
    )
    report_path = Path(run_dir) / "attempts" / "baseline" / "validation-report.json"
    receipt_path = Path(run_dir) / "attempts" / "baseline" / "validation-receipt.json"
    _, receipt_v2_path = lifecycle_v2.mirror_validation_receipt(
        run_dir,
        legacy_receipt_path=receipt_path,
    )
    receipt_verification = lifecycle_v2.verify_v2_receipt(
        run_dir,
        receipt_v2_path,
    )
    if not receipt_verification["valid"]:
        raise supervisor.SupervisorError(
            "adopted create candidate receipt failed: "
            f"{receipt_verification['diagnostics']}"
        )
    if current_state is not None and current_state["state"] == "patching":
        state = supervisor.record_initial_candidate(
            run_dir,
            accepted,
            report_path,
            receipt_path,
        )
    else:
        bind_accepted_validation(run_dir, report_path, receipt_path)
        state = supervisor.load_state(run_dir)
    _set_workflow_accepted(workflow, state)
    baseline_spec_v2 = _record_baseline_v2(
        run_dir,
        workflow,
        accepted,
        spec,
        report_path,
        receipt_path,
    )
    workflow["validation_receipt_v2"] = {
        "path": str(receipt_v2_path.resolve()),
        "sha256": supervisor.sha256_file(receipt_v2_path),
    }
    selected["adopted_validation"] = {
        "report": str(report_path.resolve()),
        "report_sha256": supervisor.sha256_file(report_path),
        "receipt": str(receipt_path.resolve()),
        "receipt_sha256": supervisor.sha256_file(receipt_path),
        "receipt_v2": str(receipt_v2_path.resolve()),
        "receipt_v2_sha256": supervisor.sha256_file(receipt_v2_path),
        "strict_passed": receipt_verification["strict_passed"],
    }
    return accepted, baseline_spec_v2


def validate_plan(plan):
    schema = supervisor.load_json(ROOT / "data" / "semantic-plan.v1.schema.json")
    jsonschema.Draft202012Validator(schema).validate(plan)
    node_ids = [node["id"] for node in plan["result"]["nodes"]]
    edge_ids = [edge["id"] for edge in plan["result"]["edges"]]
    if len(set(node_ids)) != len(node_ids) or len(set(edge_ids)) != len(edge_ids):
        raise supervisor.SupervisorError("semantic plan contains duplicate node or edge ids")
    if set(node_ids) & set(edge_ids):
        raise supervisor.SupervisorError("semantic plan reuses an id for a node and an edge")
    missing = sorted({value for edge in plan["result"]["edges"] for value in (edge["source_id"], edge["target_id"])} - set(node_ids))
    if missing:
        raise supervisor.SupervisorError(f"semantic plan edges reference missing nodes: {missing}")
    return plan


def _levels(nodes, edges):
    ids = [node["id"] for node in nodes]
    incoming = {node_id: 0 for node_id in ids}
    outgoing = {node_id: [] for node_id in ids}
    for edge in edges:
        incoming[edge["target_id"]] += 1
        outgoing[edge["source_id"]].append(edge["target_id"])
    queue = [node_id for node_id in ids if incoming[node_id] == 0]
    level = {node_id: 0 for node_id in queue}
    while queue:
        current = queue.pop(0)
        for target in outgoing[current]:
            level[target] = max(level.get(target, 0), level[current] + 1)
            incoming[target] -= 1
            if incoming[target] == 0:
                queue.append(target)
    for index, node_id in enumerate(ids):
        level.setdefault(node_id, index)
    return level


def _style_with_hint(base, hint, *, protected=()):
    """Apply non-executable draw.io style hints without overriding invariants."""
    if not hint:
        return base
    protected = {str(item).lower() for item in protected}
    safe = []
    for raw in str(hint).split(";"):
        token = raw.strip()
        if not token:
            continue
        key = token.split("=", 1)[0].strip().lower()
        lowered = token.lower()
        if key in protected or any(marker in lowered for marker in ("javascript:", "data:", "file:", "http:", "https:")):
            continue
        safe.append(token)
    return base + (";".join(safe) + ";" if safe else "")


def _v2_node_style(node, *, has_children):
    semantic_type = node["semantic_type"].strip().lower()
    if has_children or semantic_type in {"container", "group", "lane", "swimlane"}:
        base = (
            "swimlane;html=1;rounded=0;collapsible=0;container=1;"
            "recursiveResize=0;whiteSpace=wrap;fillColor=#f5f5f5;strokeColor=#666666;"
        )
    elif semantic_type in {"decision", "gateway", "condition"}:
        base = (
            "rhombus;whiteSpace=wrap;html=1;fillColor=#fff2cc;strokeColor=#d6b656;"
        )
    elif semantic_type in {"start", "end", "event", "terminal"}:
        base = (
            "ellipse;whiteSpace=wrap;html=1;fillColor=#d5e8d4;strokeColor=#82b366;"
        )
    else:
        base = (
            "rounded=1;whiteSpace=wrap;html=1;fillColor=#dae8fc;strokeColor=#6c8ebf;"
        )
    return _style_with_hint(base, node.get("style_hint"))


def _v2_page_layout(page, direction):
    """Return deterministic relative and absolute bounds for one semantic page."""
    nodes = page["nodes"]
    by_id = {node["stable_identity"]["cell_id"]: node for node in nodes}
    children = {node_id: [] for node_id in by_id}
    roots = []
    for node in nodes:
        node_id = node["stable_identity"]["cell_id"]
        parent = node.get("parent")
        if parent is None:
            roots.append(node_id)
        else:
            children[parent["cell_id"]].append(node_id)

    for values in children.values():
        values.sort()
    roots.sort()
    sizes = {}
    relative = {}

    def measure(node_id):
        nested = children[node_id]
        if not nested:
            sizes[node_id] = (160.0, 70.0)
            return sizes[node_id]
        child_sizes = [(child_id, measure(child_id)) for child_id in nested]
        if direction == "LR":
            cursor = 40.0
            for child_id, (width, height) in child_sizes:
                relative[child_id] = (cursor, 60.0)
                cursor += width + 50.0
            width = max(240.0, cursor - 10.0)
            height = max(160.0, max(60.0 + height for _, (_, height) in child_sizes) + 40.0)
        else:
            cursor = 60.0
            for child_id, (width, height) in child_sizes:
                relative[child_id] = (40.0, cursor)
                cursor += height + 50.0
            width = max(240.0, max(40.0 + width for _, (width, _) in child_sizes) + 40.0)
            height = max(160.0, cursor - 10.0)
        sizes[node_id] = (width, height)
        return sizes[node_id]

    for node_id in roots:
        measure(node_id)

    # Cross-field validation guarantees that every non-root parent exists and
    # that parent references are acyclic, so every node is measured above.
    if direction == "LR":
        cursor = 80.0
        for node_id in roots:
            relative[node_id] = (cursor, 80.0)
            cursor += sizes[node_id][0] + 100.0
    else:
        cursor = 80.0
        for node_id in roots:
            relative[node_id] = (80.0, cursor)
            cursor += sizes[node_id][1] + 100.0

    absolute = {}

    def locate(node_id, parent_origin=(0.0, 0.0)):
        x, y = relative[node_id]
        absolute[node_id] = (parent_origin[0] + x, parent_origin[1] + y, *sizes[node_id])
        for child_id in children[node_id]:
            locate(child_id, (absolute[node_id][0], absolute[node_id][1]))

    for node_id in roots:
        locate(node_id)
    return by_id, children, relative, absolute, sizes


def _generated_v2_route(source_box, target_box, *, self_loop):
    sx, sy, sw, sh = source_box
    tx, ty, tw, th = target_box
    if self_loop:
        source_pin, target_pin = (1.0, 0.5), (0.5, 0.0)
        source = (sx + sw, sy + sh / 2.0)
        target = (tx + tw / 2.0, ty)
        margin = 60.0
        points = [
            (source[0] + margin, source[1]),
            (source[0] + margin, target[1] - margin),
            (target[0], target[1] - margin),
        ]
        return source_pin, target_pin, points

    source_center = (sx + sw / 2.0, sy + sh / 2.0)
    target_center = (tx + tw / 2.0, ty + th / 2.0)
    dx = target_center[0] - source_center[0]
    dy = target_center[1] - source_center[1]
    if abs(dx) >= abs(dy):
        forward = dx >= 0
        source_pin = (1.0 if forward else 0.0, 0.5)
        target_pin = (0.0 if forward else 1.0, 0.5)
        source = (sx + source_pin[0] * sw, sy + sh / 2.0)
        target = (tx + target_pin[0] * tw, ty + th / 2.0)
        middle = (source[0] + target[0]) / 2.0
        points = [(middle, source[1]), (middle, target[1])]
    else:
        forward = dy >= 0
        source_pin = (0.5, 1.0 if forward else 0.0)
        target_pin = (0.5, 0.0 if forward else 1.0)
        source = (sx + sw / 2.0, sy + source_pin[1] * sh)
        target = (tx + tw / 2.0, ty + target_pin[1] * th)
        middle = (source[1] + target[1]) / 2.0
        points = [(source[0], middle), (target[0], middle)]
    return source_pin, target_pin, points


def _render_generic_v2(plan, output):
    require_valid_contract(plan, "semantic-plan", 2)
    diagnostics = validate_semantic_plan(plan)
    if diagnostics:
        first = diagnostics[0]
        raise supervisor.SupervisorError(
            f"semantic plan v2 cannot be rendered: {first['code']}: {first['message']}"
        )

    data = plan["result"]
    mxfile = ET.Element("mxfile", {
        "host": "GigaCode", "agent": "drawio-agent-extension", "version": "semantic-plan.v2",
    })
    for page in data["pages"]:
        page_id = page["page_id"]
        diagram = ET.SubElement(mxfile, "diagram", {
            "id": page_id,
            "name": page["name"],
            "data-schema-version": "2",
        })
        model = ET.SubElement(diagram, "mxGraphModel", {
            "dx": "1200", "dy": "800", "grid": "1", "gridSize": "10", "guides": "1",
            "tooltips": "1", "connect": "1", "arrows": "1", "fold": "1", "page": "1",
            "pageScale": "1", "pageWidth": "1169", "pageHeight": "827", "math": "0", "shadow": "0",
        })
        root = ET.SubElement(model, "root")
        ET.SubElement(root, "mxCell", {"id": "0"})
        ET.SubElement(root, "mxCell", {"id": "1", "parent": "0"})

        by_id, children, relative, absolute, sizes = _v2_page_layout(page, data["direction"])

        def depth(node_id):
            value = 0
            parent = by_id[node_id].get("parent")
            while parent is not None:
                value += 1
                parent = by_id[parent["cell_id"]].get("parent")
            return value

        for node_id in sorted(by_id, key=lambda value: (depth(value), value)):
            node = by_id[node_id]
            parent = node.get("parent")
            x, y = relative[node_id]
            width, height = sizes[node_id]
            attributes = {
                "id": node_id,
                "value": node["label"],
                "style": _v2_node_style(node, has_children=bool(children[node_id])),
                "vertex": "1",
                "parent": parent["cell_id"] if parent is not None else "1",
                "data-semantic-type": node["semantic_type"],
                "data-page-id": page_id,
            }
            if node.get("style_hint") is not None:
                attributes["data-style-hint"] = node["style_hint"]
            cell = ET.SubElement(root, "mxCell", attributes)
            ET.SubElement(cell, "mxGeometry", {
                "x": str(round(x, 2)), "y": str(round(y, 2)),
                "width": str(round(width, 2)), "height": str(round(height, 2)),
                "as": "geometry",
            })

        for edge in sorted(page["edges"], key=lambda item: item["stable_identity"]["cell_id"]):
            edge_id = edge["stable_identity"]["cell_id"]
            source_id = edge["source"]["cell_id"]
            target_id = edge["target"]["cell_id"]
            route = edge.get("route")
            if route is None:
                source_pin, target_pin, points = _generated_v2_route(
                    absolute[source_id], absolute[target_id], self_loop=source_id == target_id,
                )
            else:
                source_pin = (route["source_pin"]["x"], route["source_pin"]["y"])
                target_pin = (route["target_pin"]["x"], route["target_pin"]["y"])
                points = [(point["x"], point["y"]) for point in route["waypoints"]]
            base_style = (
                "edgeStyle=orthogonalEdgeStyle;orthogonalLoop=1;jettySize=auto;html=1;rounded=0;"
                f"exitX={source_pin[0]:g};exitY={source_pin[1]:g};"
                f"entryX={target_pin[0]:g};entryY={target_pin[1]:g};"
            )
            attributes = {
                "id": edge_id,
                "value": edge["label"],
                "style": _style_with_hint(
                    base_style,
                    edge.get("style_hint"),
                    protected={"edgeStyle", "orthogonalLoop", "rounded", "exitX", "exitY", "entryX", "entryY"},
                ),
                "edge": "1",
                "parent": edge["parent"]["cell_id"] if edge.get("parent") is not None else "1",
                "source": source_id,
                "target": target_id,
                "data-semantic-type": "edge",
                "data-relationship": edge["relationship"],
                "data-page-id": page_id,
            }
            if edge.get("style_hint") is not None:
                attributes["data-style-hint"] = edge["style_hint"]
            cell = ET.SubElement(root, "mxCell", attributes)
            geometry = ET.SubElement(cell, "mxGeometry", {"relative": "1", "as": "geometry"})
            array = ET.SubElement(geometry, "Array", {"as": "points"})
            for x, y in points:
                ET.SubElement(array, "mxPoint", {"x": str(round(x, 2)), "y": str(round(y, 2))})

    ET.indent(mxfile, space="  ")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = ET.tostring(mxfile, encoding="utf-8", xml_declaration=True) + b"\n"
    with tempfile.NamedTemporaryFile("wb", dir=output.parent, delete=False) as tmp:
        tmp.write(payload)
        tmp.flush()
        os.fsync(tmp.fileno())
        temp_name = tmp.name
    os.replace(temp_name, output)
    return output


def render_generic(plan, output):
    """Render a validated v2 semantic plan, retaining the legacy v1 boundary."""
    if plan.get("schema_version") == 2:
        return _render_generic_v2(plan, output)
    validate_plan(plan)
    data = plan["result"]
    nodes, edges = data["nodes"], data["edges"]
    direction = data["direction"]
    levels = _levels(nodes, edges)
    buckets = {}
    for node in nodes:
        buckets.setdefault(levels[node["id"]], []).append(node["id"])
    positions = {}
    width, height = 160, 70
    for level in sorted(buckets):
        for row, node_id in enumerate(buckets[level]):
            if direction == "LR":
                positions[node_id] = (80 + level * 260, 80 + row * 140)
            else:
                positions[node_id] = (80 + row * 240, 80 + level * 170)

    mxfile = ET.Element("mxfile", {"host": "GigaCode", "agent": "drawio-agent-extension"})
    diagram = ET.SubElement(mxfile, "diagram", {"id": "generated", "name": data["title"][:80]})
    model = ET.SubElement(diagram, "mxGraphModel", {
        "dx": "1200", "dy": "800", "grid": "1", "gridSize": "10", "guides": "1",
        "tooltips": "1", "connect": "1", "arrows": "1", "fold": "1", "page": "1",
        "pageScale": "1", "pageWidth": "1169", "pageHeight": "827", "math": "0", "shadow": "0",
    })
    root = ET.SubElement(model, "root")
    ET.SubElement(root, "mxCell", {"id": "0"})
    ET.SubElement(root, "mxCell", {"id": "1", "parent": "0"})
    for node in nodes:
        x, y = positions[node["id"]]
        cell = ET.SubElement(root, "mxCell", {
            "id": node["id"], "value": node["label"],
            "style": "rounded=1;whiteSpace=wrap;html=1;fillColor=#dae8fc;strokeColor=#6c8ebf;",
            "vertex": "1", "parent": "1", "data-semantic-type": node["semantic_type"],
        })
        ET.SubElement(cell, "mxGeometry", {
            "x": str(x), "y": str(y), "width": str(width), "height": str(height), "as": "geometry",
        })
    for edge in edges:
        sx, sy = positions[edge["source_id"]]
        tx, ty = positions[edge["target_id"]]
        style = "edgeStyle=orthogonalEdgeStyle;orthogonalLoop=1;jettySize=auto;html=1;rounded=0;"
        if direction == "LR":
            style += "exitX=1;exitY=0.5;entryX=0;entryY=0.5;"
            middle = (sx + width + tx) / 2
            points = [(middle, sy + height / 2), (middle, ty + height / 2)]
        else:
            style += "exitX=0.5;exitY=1;entryX=0.5;entryY=0;"
            middle = (sy + height + ty) / 2
            points = [(sx + width / 2, middle), (tx + width / 2, middle)]
        cell = ET.SubElement(root, "mxCell", {
            "id": edge["id"], "value": edge["label"], "style": style, "edge": "1", "parent": "1",
            "source": edge["source_id"], "target": edge["target_id"],
            "data-semantic-type": "edge", "data-relationship": edge["relationship"],
        })
        geometry = ET.SubElement(cell, "mxGeometry", {"relative": "1", "as": "geometry"})
        array = ET.SubElement(geometry, "Array", {"as": "points"})
        for x, y in points:
            ET.SubElement(array, "mxPoint", {"x": str(round(x, 2)), "y": str(round(y, 2))})
    ET.indent(mxfile, space="  ")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = ET.tostring(mxfile, encoding="utf-8", xml_declaration=True) + b"\n"
    with tempfile.NamedTemporaryFile("wb", dir=output.parent, delete=False) as tmp:
        tmp.write(payload)
        tmp.flush()
        os.fsync(tmp.fileno())
        temp_name = tmp.name
    os.replace(temp_name, output)
    return output


def role_call(role, payload, run_dir, workspace, cli, timeout, label):
    role_dir = Path(run_dir) / "roles" / label
    input_path = role_dir / "input.json"
    output_path = role_dir / "output.json"
    supervisor.write_json(input_path, payload)
    runtime = agent_runtime.invoke_role(
        role, input_path, output_path, cli=str(cli), run_dir=run_dir,
        timeout=timeout, cwd=workspace,
    )
    resolution = runtime["resolution"]
    if resolution["fallback_used"] or resolution["resolved_model"] != resolution["requested_model"]:
        policy = supervisor.load_json(agent_runtime.DEFAULT_POLICY)
        allowed = {
            item["model"]
            for item in policy["roles"].get(role, {}).get("runtime_fallbacks", [])
        }
        if (
            role not in {"supervisor", "repair"}
            or resolution["resolved_model"] not in allowed
            or resolution.get("resolution_mode") != "isolated_cli"
        ):
            raise supervisor.SupervisorError(
                f"lifecycle role {role} used an unapproved degraded model"
            )
    return supervisor.load_json(output_path), runtime, input_path, output_path


def consume_supervisor_decision(workflow, decision, *, phase, requested_max_iterations):
    """Apply the typed Supervisor plan or fail closed before invoking sibling roles."""
    result = decision["result"]
    declared_action = result["action"]
    action = declared_action
    # The raw model declaration remains evidence, while executable lifecycle
    # topology is deterministic and owned by the host. Repair is authorized in
    # every lifecycle phase but is invoked only when validation/review findings
    # reach repair_loop.
    declared_roles = set(result["required_roles"])
    if phase == "initial":
        allowed_actions = {"create", "analyze"} if workflow["mode"] == "create" else {"analyze", "repair", "review"}
        host_mandatory_roles = {"supervisor", "semantic_analyst", "repair", "reviewer"}
    else:
        allowed_actions = {"analyze", "repair", "review"}
        if workflow["mode"] == "create" and not workflow.get("accepted_artifact"):
            allowed_actions.add("create")
        host_mandatory_roles = {"supervisor", "repair", "reviewer"}
        if (
            declared_action == "create"
            and workflow["mode"] == "create"
            and workflow.get("accepted_artifact")
        ):
            # A resumed create run already owns immutable baseline bytes.  The
            # model declaration is retained as evidence, but executable host
            # topology must continue from that baseline instead of regenerating.
            action = "repair"
    if action not in allowed_actions:
        raise supervisor.SupervisorError(
            f"Supervisor action {action!r} is not executable during the {phase} phase"
        )
    roles = declared_roles | host_mandatory_roles
    proposed_max = result.get("max_iterations", requested_max_iterations)
    workflow["max_iterations"] = min(int(requested_max_iterations), int(proposed_max))
    workflow["supervisor_declared_roles"] = sorted(declared_roles)
    workflow["host_mandatory_roles"] = sorted(host_mandatory_roles)
    workflow["required_roles"] = sorted(roles)
    workflow["supervisor_declared_action"] = declared_action
    workflow["supervisor_action"] = action
    workflow["supervisor_action_normalized"] = declared_action != action
    workflow["supervisor_decision"] = decision
    return workflow


def source_ref_for_request(run_id, request):
    return {
        "source_id": "user-request",
        "kind": "explicit_user_decision",
        "uri": f"urn:diagram-run:{run_id}:request",
        "revision": None,
        "fragment": None,
        "content_hash": hashlib.sha256(request.encode("utf-8")).hexdigest(),
        "confidence": 1.0,
        "selected": True,
        "notes": request[:1000],
    }


def bind_accepted_validation(run_dir, report_path, receipt_path):
    state = supervisor.load_state(run_dir)
    state["accepted_validation"] = {
        "report": str(Path(report_path).resolve()),
        "report_sha256": supervisor.sha256_file(report_path),
        "receipt": str(Path(receipt_path).resolve()),
        "receipt_sha256": supervisor.sha256_file(receipt_path),
        "strict_passed": supervisor.verify_receipt(receipt_path)["passed"],
    }
    state["updated_at"] = supervisor.utc_now()
    supervisor.commit_state_event(
        run_dir, state, "validation_receipt",
        {"accepted_validation_bound": True, "report_sha256": state["accepted_validation"]["report_sha256"], "receipt_sha256": state["accepted_validation"]["receipt_sha256"]},
        event_state=state["state"],
    )


def _complete_inflight_decision(
    run_dir, workflow, *, outcome, record_v2=True,
):
    """Mark a committed decision complete only after durable progress exists."""
    inflight = workflow.pop("inflight_decision", None)
    if not inflight:
        return
    decision_id = inflight.get("decision_id")
    if decision_id:
        if record_v2:
            lifecycle_v2.mark_decision_processed(
                run_dir, decision_id=decision_id, outcome=outcome,
            )
        workflow.setdefault("processed_decision_ids", []).append(decision_id)
        workflow["processed_decision_ids"] = sorted(
            set(workflow["processed_decision_ids"])
        )


def checkpoint(run_dir, workflow, kind, summary, findings, allowed, *, evidence=None):
    processed_decision_id = (
        (workflow.get("inflight_decision") or {}).get("decision_id")
    )
    value = {
        "schema_version": 1, "run_id": workflow["run_id"], "kind": kind,
        "summary": summary, "findings": findings, "allowed_decisions": allowed,
        "accepted_artifact": workflow.get("accepted_artifact"), "created_at": supervisor.utc_now(),
    }
    if evidence is not None:
        value["evidence"] = evidence
    number = len(workflow.get("checkpoint_history", [])) + 1
    immutable_path = Path(run_dir) / "checkpoints" / f"{number:03d}-{kind}.json"
    supervisor.write_json(immutable_path, value)
    descriptor = {
        **value, "path": str(immutable_path.resolve()),
        "sha256": supervisor.sha256_file(immutable_path),
    }
    supervisor.write_json(Path(run_dir) / CHECKPOINT_FILE, descriptor)
    workflow["status"] = "awaiting_human"
    workflow["checkpoint"] = descriptor
    workflow.setdefault("checkpoint_history", []).append({"path": descriptor["path"], "sha256": descriptor["sha256"], "kind": kind})
    write_workflow(run_dir, workflow)
    supervisor.append_event(
        run_dir, "checkpoint_created",
        {"kind": kind, "checkpoint": descriptor["path"], "checkpoint_sha256": descriptor["sha256"], "allowed_decisions": allowed},
        state=supervisor.load_state(run_dir)["state"],
        actor={"kind": "system", "id": "diagram-orchestrator", "model": None},
    )
    if lifecycle_v2.manifest_path(run_dir).is_file():
        semantic_plan_sha = None
        semantic_delta_sha = None
        baseline_digest = None
        plan_descriptor = workflow.get("semantic_plan_v2") or {}
        if plan_descriptor:
            semantic_plan_sha = plan_descriptor.get("sha256")
            try:
                plan_value = supervisor.load_json(plan_descriptor["path"])
                baseline_digest = plan_value["baseline_semantic_digest"]
                semantic_delta_sha = semantic_delta_sha256(plan_value["result"]["semantic_delta"])
            except (KeyError, OSError, ValueError, json.JSONDecodeError):
                pass
        accepted_descriptor = None
        accepted_path = workflow.get("accepted_artifact", {}).get("path")
        if accepted_path and _inside(accepted_path, run_dir):
            accepted_descriptor = _relative_file(run_dir, accepted_path)
        v2_checkpoint, v2_descriptor = lifecycle_v2.create_checkpoint(
            run_dir,
            checkpoint_type=kind,
            allowed_decisions=allowed,
            context={"summary": summary, "findings": findings, "evidence": evidence},
            baseline_semantic_digest=baseline_digest,
            semantic_plan_sha256=semantic_plan_sha,
            semantic_delta_sha256=semantic_delta_sha,
            accepted_artifact=accepted_descriptor,
            processed_decision_id=processed_decision_id,
        )
        workflow["checkpoint"]["v2_checkpoint_id"] = v2_checkpoint["checkpoint_id"]
        workflow["checkpoint"]["v2_checkpoint_sha256"] = v2_descriptor["canonical_sha256"]
    _complete_inflight_decision(
        run_dir, workflow, outcome=f"checkpoint:{kind}", record_v2=False,
    )
    write_workflow(run_dir, workflow)
    return host_result(run_dir, workflow)


def role_policy_evidence(workflow):
    """Expose model-declared and deterministic host role selection separately."""
    return {
        "supervisor_action": workflow.get("supervisor_action"),
        "supervisor_declared_action": workflow.get("supervisor_declared_action"),
        "supervisor_action_normalized": bool(
            workflow.get("supervisor_action_normalized")
        ),
        "supervisor_declared_roles": workflow.get("supervisor_declared_roles", []),
        "host_mandatory_roles": workflow.get("host_mandatory_roles", []),
        "effective_required_roles": workflow.get("required_roles", []),
    }


def host_result(run_dir, workflow, *, error=None):
    state = supervisor.load_state(run_dir)
    working_artifact = _working_artifact(workflow)
    working_validation = _working_validation(workflow)
    publishable = workflow.get("publishable_candidate") or None
    best_effort = workflow.get("best_effort") or None
    best_effort_candidate = workflow.get("best_effort_candidate") or None
    deliverable = publishable or (
        best_effort_candidate
        if best_effort and best_effort.get("eligible") else None
    )
    role_runs = []
    failed_role_runs = []
    manifest_path = Path(run_dir) / "run-manifest.jsonl"
    if manifest_path.is_file():
        for line in manifest_path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event.get("event_type") == "role_finished":
                role_runs.append({
                    key: event["payload"].get(key)
                    for key in (
                        "role", "requested_model", "resolved_model", "resolution_mode",
                        "fallback_used", "model_proof", "output", "output_sha256",
                        "runtime_capture", "runtime_capture_sha256", "stderr_capture",
                        "stderr_capture_sha256", "isolation_controls",
                        "isolation_proof", "exit_code",
                        "attempted_model", "attempt_id", "output_format",
                        "degradation_reason", "binding_proof",
                    )
                })
            elif event.get("event_type") == "role_failed":
                failed_role_runs.append({
                    key: event["payload"].get(key)
                    for key in (
                        "role", "phase", "failure_kind", "requested_model",
                        "resolved_model", "reported_model", "runtime_version",
                        "exit_code", "diagnostic", "runtime_capture",
                        "runtime_capture_sha256", "stderr_capture",
                        "stderr_capture_sha256", "isolation_controls",
                        "isolation_proof",
                        "attempted_model", "fallback_model", "attempt_id",
                        "output_format", "terminal",
                    )
                })
    result = {
        "schema_version": 1,
        "status": "error" if error else workflow.get("status", state["state"] if state else "unknown"),
        "run_id": workflow["run_id"], "run_dir": str(Path(run_dir).resolve()),
        "mode": workflow["mode"], "state": state["state"] if state else None,
        "working_artifact": working_artifact or None,
        "working_validation": working_validation or None,
        "publishable_candidate": copy.deepcopy(publishable),
        "best_effort": copy.deepcopy(best_effort),
        "best_effort_candidate": copy.deepcopy(best_effort_candidate),
        "strict_passed": bool(working_validation.get("strict_passed")),
        # Do not present a strict-failed compatibility mirror as final accepted.
        "accepted_artifact": (
            copy.deepcopy(deliverable["artifact"]) if deliverable else None
        ),
        "accepted_validation": (
            copy.deepcopy(deliverable["validation"]) if deliverable else None
        ),
        "final_artifact": workflow.get("final_artifact"),
        "published_artifact": workflow.get("published_artifact"),
        "role_runs": role_runs,
        "failed_role_runs": failed_role_runs,
        "model_diversity_degraded": any(
            bool(item.get("fallback_used")) for item in role_runs
        ),
        "role_policy": role_policy_evidence(workflow),
        "checkpoint": workflow.get("checkpoint"),
        "recovered_committed_decision": copy.deepcopy(
            workflow.get("recovered_committed_decision")
        ),
        "evidence": {
            "manifest": str(manifest_path.resolve()),
            "workflow": str((Path(run_dir) / WORKFLOW_FILE).resolve()),
            "diagram_spec": str((Path(run_dir) / "diagram-spec.json").resolve()),
        },
    }
    if error:
        result["error"] = error
    supervisor.write_json(Path(run_dir) / "host-result.json", result)
    return result


def add_command_guidance(result, resolution, *, persist=True):
    result["command_resolution"] = resolution
    run_id = result.get("run_id")
    commands = {}
    checkpoint_value = result.get("checkpoint")
    if run_id and checkpoint_value:
        commands["explicit"] = {}
        short_allowed = False
        try:
            selected, _ = command_ux.select_pending_run(resolution["workspace"])
            short_allowed = Path(selected).resolve() == Path(result["run_dir"]).resolve()
        except command_ux.CommandUXError:
            short_allowed = False
        if short_allowed:
            commands["short"] = {}
        for decision in checkpoint_value.get("allowed_decisions", []):
            short = f"/drawio:resume {decision}"
            if decision == "continue":
                short += " " + command_ux.quote_command_value("необязательные замечания")
            if short_allowed:
                commands["short"][decision] = short
            commands["explicit"][decision] = (
                "/drawio:resume --run "
                f"{command_ux.quote_command_value(run_id)} --decision {decision}"
            )
        result["short_resume_available"] = short_allowed
    if run_id:
        commands["trace"] = "/drawio:trace"
        commands["trace_explicit"] = (
            "/drawio:trace --run " + command_ux.quote_command_value(run_id)
        )
    if commands:
        result["next_commands"] = commands
    if persist and result.get("run_dir"):
        path = Path(result["run_dir"]) / "host-result.json"
        if path.parent.is_dir():
            supervisor.write_json(path, result)
    return result


def _role_document(run_dir, path):
    descriptor = _relative_file(run_dir, path)
    return {
        "path": descriptor["path"],
        "sha256": descriptor["sha256"],
        "content": supervisor.load_json(path),
    }


def _reviewer_input_v2(
    run_dir, workflow, *, review_kind, candidate, report, receipt_v2,
    baseline=None, baseline_report=None, baseline_receipt_v2=None, patch=None,
):
    """Build and validate the actual hash-bound input shown to Reviewer v2."""
    run_dir = Path(run_dir).resolve()
    candidate = Path(candidate).resolve()
    report = Path(report).resolve()
    receipt_v2 = Path(receipt_v2).resolve()
    receipt_check = lifecycle_v2.verify_v2_receipt(run_dir, receipt_v2)
    if not receipt_check["valid"]:
        raise supervisor.SupervisorError(
            f"Reviewer candidate receipt v2 failed before model call: {receipt_check['diagnostics']}"
        )
    candidate_receipt_value = supervisor.load_json(receipt_v2)

    source_bundle, source_descriptor = lifecycle_v2.latest_document(run_dir, "source-bundle")
    source_path = run_dir / source_descriptor["path"]
    evidence_root = run_dir / "evidence" / "reviewer" / supervisor.sha256_file(candidate)[:16]
    candidate_spec_path = evidence_root / "candidate-spec.v2.json"
    candidate_spec = _v1_spec_to_v2(
        run_dir, supervisor.make_spec(candidate), source_descriptor["canonical_sha256"],
    )
    atomic_write_bytes(candidate_spec_path, canonical_json_bytes(candidate_spec) + b"\n")

    baseline_value = None
    baseline_spec_value = None
    comparison = None
    if baseline is not None:
        baseline = Path(baseline).resolve()
        baseline_report = Path(baseline_report).resolve()
        baseline_receipt_v2 = Path(baseline_receipt_v2).resolve()
        baseline_check = lifecycle_v2.verify_v2_receipt(run_dir, baseline_receipt_v2)
        if not baseline_check["valid"]:
            raise supervisor.SupervisorError(
                f"Reviewer baseline receipt v2 failed before model call: {baseline_check['diagnostics']}"
            )
        baseline_receipt_value = supervisor.load_json(baseline_receipt_v2)
        baseline_spec_path = evidence_root / "baseline-spec.v2.json"
        baseline_spec = _v1_spec_to_v2(
            run_dir, supervisor.make_spec(baseline), source_descriptor["canonical_sha256"],
        )
        atomic_write_bytes(baseline_spec_path, canonical_json_bytes(baseline_spec) + b"\n")
        baseline_value = {
            "artifact": _relative_file(run_dir, baseline),
            "report": _role_document(run_dir, baseline_report),
            "receipt": _role_document(run_dir, baseline_receipt_v2),
            "strict_passed": baseline_check["strict_passed"],
        }
        baseline_spec_value = _role_document(run_dir, baseline_spec_path)
        baseline_report_value = supervisor.load_json(baseline_report)
        candidate_report_value = supervisor.load_json(report)
        comparison = {
            "diagram": supervisor.spec_diff(baseline_spec, candidate_spec),
            "quality": supervisor.compare_reports(
                baseline_report_value, candidate_report_value,
                semantic_equal=(baseline_spec["semantic_digest"]["value"]
                                == candidate_spec["semantic_digest"]["value"]),
                untouched_equal=True,
                profile_version=workflow.get("quality_profile_version", 1),
            ),
        }

    plan_value = None
    delta_value = None
    plan_descriptor = workflow.get("semantic_plan_v2") or {}
    plan_path = Path(plan_descriptor.get("path", ""))
    if (
        plan_path.is_file()
        and supervisor.sha256_file(plan_path) == plan_descriptor.get("sha256")
    ):
        plan_value = _role_document(run_dir, plan_path)
        delta = plan_value["content"]["result"]["semantic_delta"]
        delta_value = {"sha256": semantic_delta_sha256(delta), "content": delta}

    resolutions = []
    legacy_manifest = run_dir / "run-manifest.jsonl"
    if legacy_manifest.is_file():
        for line in legacy_manifest.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get("event_type") == "model_resolved":
                resolutions.append(event.get("payload", {}))

    value = {
        "schema_version": 2,
        "run_id": workflow["run_id"],
        "review_kind": review_kind,
        "baseline": baseline_value,
        "candidate": {
            "artifact": _relative_file(run_dir, candidate),
            "report": _role_document(run_dir, report),
            "receipt": _role_document(run_dir, receipt_v2),
            "strict_passed": receipt_check["strict_passed"],
        },
        "baseline_spec": baseline_spec_value,
        "candidate_spec": _role_document(run_dir, candidate_spec_path),
        "patch": _role_document(run_dir, patch) if patch is not None else None,
        "semantic_plan": plan_value,
        "semantic_delta": delta_value,
        "source_bundle": {
            "path": source_descriptor["path"],
            "sha256": source_descriptor["sha256"],
            "content": source_bundle,
        },
        "comparison": comparison,
        "model_resolutions": resolutions,
    }
    require_valid_contract(value, "reviewer-input", 2)
    return value


def _reviewer_gate_binding_error(run_dir, workflow, verdict):
    """Return the first exact Reviewer evidence mismatch, or ``None``."""
    plan_descriptor = workflow.get("semantic_plan_v2") or {}
    plan_path = Path(plan_descriptor.get("path", ""))
    if not plan_path.is_file() or supervisor.sha256_file(plan_path) != plan_descriptor.get("sha256"):
        return "semantic plan evidence is missing or changed"
    plan = supervisor.load_json(plan_path)
    require_valid_contract(plan, "semantic-plan", 2)
    expected_delta = semantic_delta_sha256(plan["result"]["semantic_delta"])
    if any((
        verdict["bindings"].get("semantic_plan_sha256") != plan_descriptor.get("sha256"),
        verdict["bindings"].get("semantic_delta_sha256") != expected_delta,
        plan_descriptor.get("semantic_delta_sha256") != expected_delta,
    )):
        return "Reviewer semantic bindings differ from the active plan"

    replayed = lifecycle_v2.require_mutable(run_dir, workflow["run_id"])
    source_hash = verdict["bindings"].get("source_bundle_sha256")
    source_hashes = {
        snapshot["canonical_sha256"]
        for item in replayed["events"]
        for snapshot in item["event"].get("snapshots", [])
        if snapshot.get("schema_kind") == "source-bundle"
    }
    if source_hash not in source_hashes:
        return "Reviewer source bundle is not an immutable run revision"

    role_input_path = None
    legacy_manifest = Path(run_dir) / "run-manifest.jsonl"
    if legacy_manifest.is_file():
        for line in legacy_manifest.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            payload = event.get("payload", {})
            if (
                event.get("event_type") == "role_started"
                and payload.get("role") == "reviewer"
                and payload.get("input_sha256") == verdict["role_input_sha256"]
            ):
                candidate = Path(payload.get("input", ""))
                if _inside(candidate, run_dir):
                    role_input_path = candidate
                    break
    if (
        role_input_path is None
        or not role_input_path.is_file()
        or supervisor.sha256_file(role_input_path) != verdict["role_input_sha256"]
    ):
        return "Reviewer role input evidence is missing or changed"
    role_input = supervisor.load_json(role_input_path)
    require_valid_contract(role_input, "reviewer-input", 2)
    expected = {
        "candidate_sha256": role_input["candidate"]["artifact"]["sha256"],
        "report_sha256": role_input["candidate"]["report"]["sha256"],
        "receipt_sha256": role_input["candidate"]["receipt"]["sha256"],
        "source_bundle_sha256": canonical_json_sha256(role_input["source_bundle"]["content"]),
        "semantic_plan_sha256": (
            role_input["semantic_plan"]["sha256"]
            if role_input.get("semantic_plan") is not None else None
        ),
        "semantic_delta_sha256": (
            role_input["semantic_delta"]["sha256"]
            if role_input.get("semantic_delta") is not None else None
        ),
    }
    mismatches = [
        key for key, value in expected.items()
        if verdict["bindings"].get(key) != value
    ]
    if mismatches:
        return "Reviewer verdict differs from its exact role input: " + ", ".join(mismatches)
    return None


def _final_approval_eligibility(run_dir, workflow):
    """Compute executable final decisions from hash-bound deterministic evidence."""
    publishable = workflow.get("publishable_candidate") or {}
    if not publishable:
        return {
            "approve": False,
            "approve_with_findings": False,
            "strict_passed": False,
            "unresolved_findings": [],
            "reason": "no strict-pass publishable candidate",
        }
    artifact_descriptor = publishable.get("artifact") or {}
    validation_descriptor = publishable.get("validation") or {}
    receipt_descriptor = publishable.get("validation_receipt_v2") or {}
    verdict_descriptor = publishable.get("reviewer_verdict_v2") or {}
    accepted = Path(artifact_descriptor.get("path", ""))
    report_path = Path(validation_descriptor.get("report", ""))
    receipt_path = Path(receipt_descriptor.get("path", ""))
    verdict_path = Path(verdict_descriptor.get("path", ""))
    receipt_check = lifecycle_v2.verify_v2_receipt(run_dir, receipt_path)
    if not receipt_check["integrity_valid"]:
        return {"approve": False, "approve_with_findings": False, "strict_passed": False, "unresolved_findings": [], "reason": "validation receipt integrity failed"}
    if not receipt_check["strict_passed"]:
        return {
            "approve": False,
            "approve_with_findings": False,
            "strict_passed": False,
            "unresolved_findings": [],
            "reason": "strict validation failed",
        }
    if (
        not accepted.is_file()
        or not report_path.is_file()
        or not verdict_path.is_file()
        or supervisor.sha256_file(verdict_path) != verdict_descriptor.get("sha256")
    ):
        return {"approve": False, "approve_with_findings": False, "strict_passed": receipt_check["strict_passed"], "unresolved_findings": [], "reason": "final evidence is missing or changed"}
    report = supervisor.load_json(report_path)
    verdict = supervisor.load_json(verdict_path)
    require_valid_contract(verdict, "reviewer-verdict", 2)
    if any((
        report.get("artifact_sha256") != supervisor.sha256_file(accepted),
        verdict["run_id"] != workflow["run_id"],
        verdict["bindings"]["candidate_sha256"] != supervisor.sha256_file(accepted),
        verdict["bindings"]["report_sha256"] != supervisor.sha256_file(report_path),
        verdict["bindings"]["receipt_sha256"] != supervisor.sha256_file(receipt_path),
    )):
        return {"approve": False, "approve_with_findings": False, "strict_passed": receipt_check["strict_passed"], "unresolved_findings": [], "reason": "Reviewer bindings differ from accepted evidence"}
    binding_error = _reviewer_gate_binding_error(run_dir, workflow, verdict)
    if binding_error:
        return {
            "approve": False,
            "approve_with_findings": False,
            "strict_passed": receipt_check["strict_passed"],
            "unresolved_findings": [],
            "reason": binding_error,
        }
    report_findings = list(report.get("findings", []))
    reviewer_findings = list(verdict.get("findings", []))
    structural_errors = [
        item for item in report_findings
        if item.get("severity") == "error" and (
            item.get("remediation_class") == "structural"
            or item.get("layer") == "artifact-parse"
            or str(item.get("code", "")).startswith((
                "artifact.structure.", "artifact.id.", "artifact.cell.",
                "artifact.geometry.invalid", "artifact.edge.dangling",
            ))
        )
    ]
    reviewer_blockers = [
        item for item in reviewer_findings
        if item.get("severity") == "error" or item.get("category") == "integrity"
    ]
    unresolved = [
        {"source": "validator", "finding": item} for item in report_findings
    ] + [
        {"source": "reviewer", "finding": item} for item in reviewer_findings
    ]
    error_findings = [
        item for item in report_findings + reviewer_findings
        if item.get("severity") == "error"
    ]
    safe_with_findings = not structural_errors and not reviewer_blockers and not error_findings
    strict_approve = bool(
        receipt_check["strict_passed"]
        and verdict["verdict"] == "approve"
        and not reviewer_blockers
        and not unresolved
    )
    return {
        "approve": strict_approve,
        "approve_with_findings": safe_with_findings and bool(unresolved),
        "strict_passed": receipt_check["strict_passed"],
        "unresolved_findings": unresolved,
        "reason": None if safe_with_findings else "structural or integrity findings remain",
    }


def baseline_review(run_dir, workflow, cli, timeout):
    accepted = Path(workflow["accepted_artifact"]["path"])
    report = Path(workflow["accepted_validation"]["report"])
    receipt = Path(workflow["accepted_validation"]["receipt"])
    receipt_v2 = Path(workflow["validation_receipt_v2"]["path"])
    audit = _reviewer_input_v2(
        run_dir, workflow, review_kind="baseline_audit",
        candidate=accepted, report=report, receipt_v2=receipt_v2,
    )
    analysis, runtime, input_path, output = role_call(
        "reviewer", audit, run_dir, Path(workflow["workspace"]), cli, timeout,
        f"reviewer-baseline-{workflow['iteration']}"
    )
    verdict = analysis
    _verify_reviewer_runtime(verdict, runtime)
    verdict_v2, verdict_v2_path = _bind_reviewer_v2(
        run_dir, workflow, verdict, runtime, input_path, output,
        accepted, report, receipt_v2,
    )
    workflow["reviewer_verdict_v2"] = {
        "path": str(verdict_v2_path.resolve()),
        "sha256": supervisor.sha256_file(verdict_v2_path),
    }
    source_bundle = lifecycle_v2.latest_document(run_dir, "source-bundle")[0]
    evidence = copy.deepcopy(source_bundle["evidence"])
    receipt_v2 = Path(workflow["validation_receipt_v2"]["path"])
    evidence["eligible_review_handoff"] = {
        "artifact": _relative_file(run_dir, accepted),
        "report": _relative_file(run_dir, report),
        "receipt": _relative_file(run_dir, receipt_v2),
        "verdict": _relative_file(run_dir, verdict_v2_path),
        "findings_sha256": canonical_json_sha256(verdict_v2["findings"]),
    }
    lifecycle_v2.revise_sources(
        run_dir, evidence=evidence,
        event_payload={"kind": "review_handoff", "verdict": verdict_v2["verdict"]},
    )
    return verdict, runtime


def _verify_reviewer_runtime(verdict, runtime):
    resolution = runtime["resolution"]
    if verdict.get("schema_version") == 2:
        metadata = runtime.get("runtime_metadata") or {}
        model_proof = metadata.get("model_proof") or {}
        isolation_proof = metadata.get("isolation_proof") or {}
        if not all((
            model_proof.get("verified") is True,
            isolation_proof.get("verified") is True,
            metadata.get("reported_model") == resolution.get("resolved_model"),
            resolution.get("resolution_mode") == "isolated_cli",
        )):
            raise supervisor.SupervisorError(
                "Reviewer v2 runtime proof or isolation evidence is invalid"
            )
    else:
        declared = verdict.get("reviewer") or {}
        expected = {
            "resolved_model": resolution["resolved_model"],
            "provider": resolution["provider"],
            "resolution_mode": resolution["resolution_mode"],
        }
        mismatches = [key for key, value in expected.items() if declared.get(key) != value]
        if mismatches:
            raise supervisor.SupervisorError(
                "Reviewer identity differs from verified runtime proof: " + ", ".join(mismatches)
            )
    if verdict.get("verdict") == "approve" and any(
        finding.get("severity") == "error" for finding in verdict.get("findings", [])
    ):
        raise supervisor.SupervisorError("Reviewer cannot approve while error-level findings remain")


def _bind_reviewer_v2(run_dir, workflow, verdict, runtime, input_path, output_path, candidate, report, receipt):
    resolution = runtime["resolution"]
    if verdict.get("schema_version") == 2:
        analysis = copy.deepcopy(verdict)
        normalized_findings = analysis["findings"]
        analysis_id = analysis["analysis_id"]
    else:
        normalized_findings = []
        for index, finding in enumerate(verdict.get("findings", []), 1):
            reason = finding.get("reason") or finding.get("message") or "Reviewer finding"
            lowered = reason.lower()
            category = "semantic" if "semantic" in lowered else "routing" if any(token in lowered for token in ("route", "waypoint", "cross")) else "layout" if any(token in lowered for token in ("layout", "overlap", "label")) else "validation"
            severity = finding.get("severity", "warning")
            normalized_findings.append({
                "finding_id": finding.get("finding_id") or f"review-finding-{index}",
                "category": category,
                "severity": severity,
                "summary": reason,
                "elements": [],
                "evidence": [{
                    "kind": "validation_report",
                    "path": Path(report).resolve().relative_to(Path(run_dir).resolve()).as_posix(),
                    "sha256": supervisor.sha256_file(report),
                    "pointer": None,
                    "message": reason,
                }],
                "remediation": {
                    "class": "repair" if severity in {"warning", "error"} else "none",
                    "action": reason,
                },
            })
        analysis_id = verdict["verdict_id"]
        analysis = {
            "schema_version": 2,
            "role": "reviewer",
            "status": "needs_human" if verdict["verdict"] == "needs_human" else "ok",
            "analysis_id": analysis_id,
            "verdict": verdict["verdict"],
            "reviewed_at": verdict["reviewed_at"],
            "findings": normalized_findings,
        }
    require_valid_contract(analysis, "reviewer-analysis", 2)
    # Preserve the analytical model output as its own canonical artifact even
    # when the runtime already returned v2.  The host-bound verdict is a
    # separate decision and must not erase analysis_id or structured findings.
    analysis_path = Path(output_path).with_name("analysis.v2.json")
    atomic_write_bytes(analysis_path, canonical_json_bytes(analysis) + b"\n")
    verdict_id = "verdict-" + canonical_json_sha256({
        "run_id": workflow["run_id"],
        "analysis_id": analysis_id,
        "analysis_sha256": supervisor.sha256_file(analysis_path),
        "candidate_sha256": supervisor.sha256_file(candidate),
    })[:24]
    plan_descriptor = workflow.get("semantic_plan_v2") or {}
    source_hash = lifecycle_v2.require_mutable(run_dir)["latest_snapshots"]["source-bundle"]["canonical_sha256"]
    runtime_capture = Path(runtime["runtime_capture"])
    value = {
        "schema_version": 2,
        "verdict_id": verdict_id,
        "analysis_id": analysis_id,
        "run_id": workflow["run_id"],
        "analysis_sha256": supervisor.sha256_file(analysis_path),
        "role_input_sha256": supervisor.sha256_file(input_path),
        "role_output_sha256": supervisor.sha256_file(output_path),
        "bindings": {
            "candidate_sha256": supervisor.sha256_file(candidate),
            "report_sha256": supervisor.sha256_file(report),
            "receipt_sha256": supervisor.sha256_file(receipt),
            "source_bundle_sha256": source_hash,
            "semantic_plan_sha256": plan_descriptor.get("sha256"),
            "semantic_delta_sha256": plan_descriptor.get("semantic_delta_sha256"),
        },
        "runtime_proof": {
            "requested_model": resolution["requested_model"],
            "resolved_model": resolution["resolved_model"],
            "provider": resolution["provider"],
            "resolution_mode": resolution["resolution_mode"],
            "attempt_id": runtime.get("attempt_id") or Path(output_path).parent.name,
            "evidence_sha256": supervisor.sha256_file(runtime_capture),
        },
        "verdict": verdict["verdict"],
        "reviewed_at": verdict["reviewed_at"],
        "findings": normalized_findings,
    }
    require_valid_contract(value, "reviewer-verdict", 2)
    path = Path(output_path).with_name("verdict.v2.json")
    atomic_write_bytes(path, canonical_json_bytes(value) + b"\n")
    return value, path


def repair_input(run_dir, workflow):
    working_artifact = _working_artifact(workflow)
    working_validation = _working_validation(workflow)
    accepted = Path(working_artifact["path"])
    report = supervisor.load_json(working_validation["report"])
    spec = supervisor.make_spec(accepted, [source_ref_for_request(workflow["run_id"], workflow["request"])])
    semantic_authorized = bool(workflow.get("semantic_authorized"))
    approved_semantic_change = (
        workflow.get("approved_semantic_change") if semantic_authorized else None
    )
    source_bundle, source_bundle_descriptor = lifecycle_v2.latest_document(
        run_dir, "source-bundle"
    )
    semantic_plan_descriptor = (
        workflow.get("semantic_plan_v2") or {}
        if semantic_authorized else {}
    )
    semantic_plan_path = Path(semantic_plan_descriptor.get("path", ""))
    semantic_plan = None
    if semantic_plan_path.is_file():
        if supervisor.sha256_file(semantic_plan_path) != semantic_plan_descriptor.get("sha256"):
            raise supervisor.SupervisorError("repair semantic plan changed after review")
        semantic_plan = supervisor.load_json(semantic_plan_path)
        require_valid_contract(semantic_plan, "semantic-plan", 2)
    eligible_handoff = source_bundle.get("evidence", {}).get("eligible_review_handoff") or {}
    handoff_artifact = eligible_handoff.get("artifact") or {}
    if handoff_artifact.get("sha256") != supervisor.sha256_file(accepted):
        # A strict-failed but lexicographically improved candidate may become
        # the next working baseline without being reviewable/publishable.  A
        # Reviewer handoff for the preceding baseline must never be reused.
        eligible_handoff = {}
    verdict_descriptor = eligible_handoff.get("verdict") or {}
    verdict_path = Path(run_dir) / verdict_descriptor.get("path", "")
    reviewer_verdict = None
    if verdict_path.is_file():
        if supervisor.sha256_file(verdict_path) != verdict_descriptor.get("sha256"):
            raise supervisor.SupervisorError("repair Reviewer verdict changed after review")
        reviewer_verdict = supervisor.load_json(verdict_path)
        require_valid_contract(reviewer_verdict, "reviewer-verdict", 2)
        if reviewer_verdict["bindings"]["candidate_sha256"] != supervisor.sha256_file(accepted):
            raise supervisor.SupervisorError("repair review handoff is not bound to the accepted artifact")
    receipt_descriptor = eligible_handoff.get("receipt") or {}
    receipt_v2_path = (
        Path(run_dir) / receipt_descriptor.get("path", "")
        if receipt_descriptor else Path((workflow.get("validation_receipt_v2") or {}).get("path", ""))
    )
    if not receipt_v2_path.is_file():
        raise supervisor.SupervisorError("repair requires the hash-bound v2 validation receipt")
    report_descriptor = eligible_handoff.get("report") or {}
    review_report_path = (
        Path(run_dir) / report_descriptor.get("path", "")
        if report_descriptor else Path(working_validation["report"])
    )
    if any((
        not review_report_path.is_file(),
        report_descriptor and supervisor.sha256_file(review_report_path) != report_descriptor.get("sha256"),
        receipt_descriptor and supervisor.sha256_file(receipt_v2_path) != receipt_descriptor.get("sha256"),
        reviewer_verdict is not None
        and reviewer_verdict["bindings"]["report_sha256"] != supervisor.sha256_file(review_report_path),
        reviewer_verdict is not None
        and reviewer_verdict["bindings"]["receipt_sha256"] != supervisor.sha256_file(receipt_v2_path),
    )):
        raise supervisor.SupervisorError("repair review handoff report/receipt bindings are invalid")
    if semantic_authorized:
        if not approved_semantic_change:
            raise supervisor.SupervisorError("semantic repair has no hash-bound human approval")
        plan_path = Path(approved_semantic_change["semantic_plan"]["path"])
        decision_path = Path(approved_semantic_change["decision"]["path"])
        if (
            not plan_path.is_file()
            or supervisor.sha256_file(plan_path) != approved_semantic_change["semantic_plan"]["sha256"]
            or not decision_path.is_file()
            or supervisor.sha256_file(decision_path) != approved_semantic_change["decision"]["sha256"]
            or canonical_hash(approved_semantic_change["semantic_changes"])
            != approved_semantic_change["semantic_changes_sha256"]
        ):
            raise supervisor.SupervisorError("approved semantic change evidence is missing or hash-mismatched")
        plan_value = supervisor.load_json(plan_path)
        approval_descriptor = approved_semantic_change.get("semantic_approval_v2") or {}
        approval_path = Path(approval_descriptor.get("path", ""))
        if (
            not approval_path.is_file()
            or supervisor.sha256_file(approval_path) != approval_descriptor.get("sha256")
        ):
            raise supervisor.SupervisorError("actual human semantic approval v2 is missing or hash-mismatched")
        approval = supervisor.load_json(approval_path)
        require_valid_contract(plan_value, "semantic-plan", 2)
        require_valid_contract(approval, "semantic-approval", 2)
        delta = plan_value["result"]["semantic_delta"]
        if any((
            approval["decision"] != "approve",
            approval["baseline_semantic_digest"] != plan_value["baseline_semantic_digest"],
            approval["semantic_plan_sha256"] != supervisor.sha256_file(plan_path),
            approval["source_bundle_sha256"] != plan_value["source_bundle_sha256"],
            approval["semantic_delta_sha256"] != semantic_delta_sha256(delta),
        )):
            raise supervisor.SupervisorError("semantic approval v2 bindings differ from the approved plan and delta")
    host_scope = _derive_repair_scope(workflow)
    allowed_finding_ids = set(host_scope.get("finding_ids", []))
    actionable_findings = [
        copy.deepcopy(item) for item in report.get("findings", [])
        if not allowed_finding_ids or item.get("finding_id") in allowed_finding_ids
    ]
    if reviewer_verdict is not None:
        actionable_findings.extend(
            {
                "source": "reviewer",
                **copy.deepcopy(item),
            }
            for item in reviewer_verdict.get("findings", [])
        )
    report_file = _relative_file(run_dir, review_report_path)
    receipt_file = _relative_file(run_dir, receipt_v2_path)
    verdict_file = (
        _relative_file(run_dir, verdict_path)
        if reviewer_verdict is not None else None
    )
    value = {
        "schema_version": 1, "run_id": workflow["run_id"], "mode": workflow["mode"],
        "request": workflow["request"], "iteration": workflow["iteration"],
        "semantic_changes_authorized": semantic_authorized,
        "approved_semantic_change": approved_semantic_change,
        "source_bundle": {
            "schema_version": source_bundle.get("schema_version"),
            "sha256": source_bundle_descriptor["canonical_sha256"],
            "source_count": len(source_bundle.get("sources", [])),
        },
        "semantic_plan_v2": semantic_plan,
        "approved_semantic_delta": (
            semantic_plan["result"]["semantic_delta"]
            if approved_semantic_change and semantic_plan is not None else None
        ),
        "review_evidence": {
            "findings": actionable_findings,
            "report": report_file,
            "receipt": {
                **receipt_file,
                "content": supervisor.load_json(receipt_v2_path),
            },
            "verdict": (
                {**verdict_file, "content": reviewer_verdict}
                if reviewer_verdict is not None else None
            ),
        },
        "baseline": {
            "artifact": {"path": str(accepted), "sha256": supervisor.sha256_file(accepted)},
            "semantic_digest": spec["semantic_digest"]["value"],
            "diagram_spec": spec,
        },
        "requirements": {
            "last_accepted_only": True, "preserve_untouched_regions": True,
            "explicit_waypoints_for_congested_edges": True,
            "allowed_operations": copy.deepcopy(host_scope["allowed_operations"]),
        },
        "host_scope": host_scope,
        "machine_repair_feedback": copy.deepcopy(workflow.get("machine_repair_feedback")),
        "evidence_bindings": {
            "source_bundle_sha256": source_bundle_descriptor["canonical_sha256"],
            "validation_report_sha256": report_file["sha256"],
            "validation_receipt_sha256": receipt_file["sha256"],
            "reviewer_verdict_sha256": (
                verdict_file["sha256"] if verdict_file is not None else None
            ),
        },
    }
    # Legacy/replay workflows retain the patch contract.  New improve runs set
    # this host-owned flag at creation and cannot ask Repair for executable XML
    # operations during a layout-only phase.
    if workflow.get("layout_repair_enabled") and not semantic_authorized:
        layout_scope = _layout_scope_for_intent(spec, host_scope)
        workflow["layout_repair_scope"] = copy.deepcopy(layout_scope)
        value["repair_mode"] = "layout_intent"
        value["layout_scope"] = copy.deepcopy(layout_scope)
        value["requirements"]["allowed_actions"] = copy.deepcopy(
            layout_scope["allowed_actions"]
        )
    return value


ROUTE_FINDING_CODES = {
    "artifact.readability.crossing",
    "artifact.readability.route_through",
    "artifact.layout.terminal_segment",
    "artifact.layout.routing_uncertain",
}
GEOMETRY_FINDING_CODES = {
    "artifact.geometry.invalid",
    "artifact.layout.container_overflow",
    "artifact.layout.container_overlap",
    "artifact.layout.lane_title_collision",
    "artifact.layout.lane_size",
    "artifact.readability.overlap",
    "artifact.readability.text_overflow",
}
ROUTE_OPERATIONS = {"set_edge_route", "set_edge_pins"}
GEOMETRY_OPERATIONS = {
    "set_label_offset", "move_vertex", "resize_vertex", "resize_container",
}
LAYOUT_ROUTE_RE = re.compile(
    r"(?:route|routing|waypoint|arrow|cross(?:ing)?|orthogonal|manhattan|"
    r"маршрут|вейпоинт|стрелк|пересеч|ортогон|манхэттен)",
    re.IGNORECASE,
)
LAYOUT_GEOMETRY_RE = re.compile(
    r"(?:geometry|position|move|resize|size|layout|coordinate|"
    r"геометр|располож|перемест|размер|макет|координат)",
    re.IGNORECASE,
)
SEMANTIC_FEEDBACK_RE = re.compile(
    r"(?:\b(?:add|remove|delete|rename)\s+(?:node|edge|step|role|condition)\b|"
    r"\b(?:label|source|target|relationship)\b|"
    r"(?:добав|удал|переимен).{0,24}(?:узел|ребр|этап|роль|услов|связ)|"
    r"(?:измен|замен).{0,24}(?:этап|роль|услов|смысл|назван|подпис|связ)|"
    r"(?:метк|источник|назначени))",
    re.IGNORECASE,
)
EXPLICIT_CELL_ID_RE = re.compile(r"\b(?:e|n)-[A-Za-z0-9._-]+\b", re.IGNORECASE)


def _layout_feedback_scope(workflow, feedback):
    """Return a deterministic repair scope for unambiguous layout feedback."""
    text = str(feedback or "").strip()
    route_requested = bool(LAYOUT_ROUTE_RE.search(text))
    geometry_requested = bool(LAYOUT_GEOMETRY_RE.search(text))
    if not text or not (route_requested or geometry_requested):
        return None
    if SEMANTIC_FEEDBACK_RE.search(text):
        return None
    derived = _derive_repair_scope(workflow)
    explicit_targets = sorted(set(EXPLICIT_CELL_ID_RE.findall(text)))
    # A model must not expand vague prose such as "fix the route" into every
    # target from the previous validator report.  Bypassing Semantic Analyst
    # is safe only when the user names stable diagram cell IDs explicitly.
    if not explicit_targets:
        return None
    allowed_operations = set()
    if route_requested:
        allowed_operations.update(ROUTE_OPERATIONS)
    if geometry_requested:
        allowed_operations.update(GEOMETRY_OPERATIONS)
    targets = explicit_targets
    # Explicit edge feedback such as "fix only e-2" is deliberately narrow;
    # the obstructing vertex named by Validator evidence is not repair scope.
    return {
        "source": "explicit_layout_feedback",
        "allowed_targets": sorted(set(targets)),
        "allowed_operations": sorted(allowed_operations),
        "finding_ids": list(derived.get("finding_ids", [])),
        "semantic_targets": [],
        "feedback": text,
    }


def _normalized_failure_signature(failure_class, message):
    normalized = str(message).strip().lower()
    normalized = re.sub(r"\b[a-f0-9]{32,64}\b", "<hash>", normalized)
    normalized = re.sub(r"(?:^|\s)/(?:[^\s:]+/?)+", " <path>", normalized)
    normalized = re.sub(r"\b(?:iteration|attempt|repair)-?\d+\b", "<attempt>", normalized)
    normalized = re.sub(r"\b\d+\b", "<n>", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return canonical_hash({"failure_class": failure_class, "message": normalized})


def _record_internal_repair_feedback(
    run_dir, workflow, *, failure_class, message, evidence=None,
):
    """Persist machine feedback and decide whether one bounded retry remains."""
    signature = _normalized_failure_signature(failure_class, message)
    counts = workflow.setdefault("failure_signatures", {})
    count = int(counts.get(signature, 0)) + 1
    counts[signature] = count
    attempt_dir = (
        Path(run_dir) / "attempts" / f"iteration-{workflow.get('iteration', 0)}"
    )
    attempt_dir.mkdir(parents=True, exist_ok=True)
    feedback_path = attempt_dir / f"internal-feedback-{count}.json"
    value = {
        "schema_version": 1,
        "run_id": workflow["run_id"],
        "iteration": workflow.get("iteration", 0),
        "failure_class": failure_class,
        "failure_signature": signature,
        "occurrence": count,
        "message": str(message),
        "repair_scope": _derive_repair_scope(workflow),
        "evidence": copy.deepcopy(evidence or {}),
        "created_at": supervisor.utc_now(),
    }
    supervisor.write_json(feedback_path, value)
    descriptor = {
        "path": str(feedback_path.resolve()),
        "sha256": supervisor.sha256_file(feedback_path),
        "content": value,
    }
    workflow["machine_repair_feedback"] = descriptor
    workflow.setdefault("internal_feedback_history", []).append(descriptor)
    write_workflow(run_dir, workflow)
    supervisor.append_event(
        run_dir,
        "internal_feedback_created",
        {
            "failure_class": failure_class,
            "failure_signature": signature,
            "occurrence": count,
            "feedback": descriptor["path"],
            "feedback_sha256": descriptor["sha256"],
        },
        state=supervisor.load_state(run_dir)["state"],
        actor={"kind": "system", "id": "diagram-orchestrator", "model": None},
    )
    retry = (
        count < MAX_IDENTICAL_FAILURES
        and workflow.get("iteration", 0) < workflow.get("max_iterations", 0)
    )
    if retry:
        supervisor.append_event(
            run_dir,
            "auto_retry_scheduled",
            {
                "failure_class": failure_class,
                "failure_signature": signature,
                "next_iteration": workflow.get("iteration", 0) + 1,
                "max_iterations": workflow.get("max_iterations", 0),
            },
            state=supervisor.load_state(run_dir)["state"],
            actor={"kind": "system", "id": "diagram-orchestrator", "model": None},
        )
    return retry, descriptor


def _finding_targets(finding):
    values = []
    for item in finding.get("elements", []):
        value = item.get("cell_id") if isinstance(item, dict) else item
        if value and value not in values:
            values.append(str(value))
    value = finding.get("element")
    if value and str(value) not in values:
        values.append(str(value))
    message = str(finding.get("message") or finding.get("summary") or "")
    code = str(finding.get("code", ""))
    if code == "artifact.readability.route_through":
        # The obstructing vertex is evidence, but the routed edge is the
        # deterministic repair target.
        matches = re.findall(r"\bedge\s+'([^']+)'", message, flags=re.IGNORECASE)
        primary = str(finding.get("element") or "")
        route_targets = [item for item in [primary, *matches] if item]
        if route_targets:
            return list(dict.fromkeys(route_targets))
        return [value for value in values if str(value).startswith("e-")]
    elif code == "artifact.readability.crossing" or finding.get("category") == "routing":
        matches = re.findall(r"\bedges?\s+'([^']+)'|\band\s+'([^']+)'", message, flags=re.IGNORECASE)
        matches = [left or right for left, right in matches]
    else:
        matches = re.findall(r"'([^']+)'", message)
    for match in matches:
        if match and match not in values:
            values.append(match)
    return values


def _derive_repair_scope(workflow):
    """Derive deterministic allowed targets/operations from typed evidence."""
    explicit = workflow.get("repair_scope")
    if explicit:
        return copy.deepcopy(explicit)
    validation = _working_validation(workflow)
    report_path = Path(validation.get("report", ""))
    report_findings = (
        supervisor.load_json(report_path).get("findings", [])
        if report_path.is_file() else []
    )
    targets, finding_ids, allowed_ops = set(), set(), set()
    for finding in report_findings:
        code = str(finding.get("code", ""))
        if code in ROUTE_FINDING_CODES:
            allowed_ops.update(ROUTE_OPERATIONS)
        elif code in GEOMETRY_FINDING_CODES:
            allowed_ops.update(GEOMETRY_OPERATIONS)
        else:
            continue
        targets.update(_finding_targets(finding))
        if finding.get("finding_id"):
            finding_ids.add(finding["finding_id"])
    for finding in workflow.get("findings", []):
        category = finding.get("category")
        summary = str(finding.get("summary", "")).lower()
        if category == "routing" or any(
            token in summary
            for token in ("route", "waypoint", "cross", "orthogonal", "маршрут", "пересеч")
        ):
            allowed_ops.update(ROUTE_OPERATIONS)
        elif category == "layout":
            allowed_ops.update(GEOMETRY_OPERATIONS)
        else:
            continue
        targets.update(_finding_targets(finding))
        if finding.get("finding_id"):
            finding_ids.add(finding["finding_id"])
    approved = workflow.get("approved_semantic_change") or {}
    semantic_delta = approved.get("semantic_delta") or {}
    semantic_targets = set()
    for operation in semantic_delta.get("operations", []):
        target = operation.get("target") or {}
        if target.get("cell_id"):
            semantic_targets.add(str(target["cell_id"]))
        if operation.get("operation_type") == "add":
            allowed_ops.add("add_semantic_element")
        elif operation.get("operation_type") == "remove":
            allowed_ops.add("remove_semantic_element")
    return {
        "source": "typed_findings",
        "allowed_targets": sorted(targets | semantic_targets),
        "allowed_operations": sorted(allowed_ops),
        "finding_ids": sorted(finding_ids),
        "semantic_targets": sorted(semantic_targets),
    }


LAYOUT_REPAIR_ACTIONS = frozenset(layout_model.SCOPE_EXPANSION_ORDER)
LAYOUT_REPAIR_ACTIONS = LAYOUT_REPAIR_ACTIONS | {"finish_best_effort"}


def _string_ids(value, *, field):
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise supervisor.SupervisorError(f"layout repair {field} must be a list of non-empty ids")
    if len(value) != len(set(value)):
        raise supervisor.SupervisorError(f"layout repair {field} contains duplicate ids")
    return sorted(value)


def _validate_layout_repair_intent(intent, host_scope):
    """Accept only a model intent that is wholly inside a host-owned scope."""
    result = intent.get("result") if isinstance(intent, dict) and isinstance(intent.get("result"), dict) else intent
    if not isinstance(result, dict) or not isinstance(host_scope, dict):
        raise supervisor.SupervisorError("layout repair intent and host scope must be objects")
    action = result.get("action")
    allowed_actions = set(host_scope.get("allowed_actions", LAYOUT_REPAIR_ACTIONS))
    if action not in LAYOUT_REPAIR_ACTIONS or action not in allowed_actions:
        raise supervisor.SupervisorError("layout repair action is outside host scope")
    page_id = result.get("page_id")
    if not isinstance(page_id, str) or page_id != host_scope.get("page_id"):
        raise supervisor.SupervisorError("layout repair page is outside host scope")
    target_edges = _string_ids(result.get("target_edges", result.get("edge_ids", [])), field="target_edges")
    movable_nodes = _string_ids(result.get("movable_nodes", result.get("node_ids", [])), field="movable_nodes")
    locked_nodes = _string_ids(result.get("locked_nodes", []), field="locked_nodes")
    if not set(target_edges).issubset(set(host_scope.get("target_edges", []))):
        raise supervisor.SupervisorError("layout repair target_edges are outside host scope")
    if not set(movable_nodes).issubset(set(host_scope.get("movable_nodes", []))):
        raise supervisor.SupervisorError("layout repair movable_nodes are outside host scope")
    expected_locked = set(host_scope.get("locked_nodes", []))
    if set(locked_nodes) != expected_locked:
        raise supervisor.SupervisorError("layout repair locked_nodes are outside host scope")
    if action == "edge_reroute" and movable_nodes:
        raise supervisor.SupervisorError("edge-only layout repair cannot move nodes")
    if action != "finish_best_effort" and not target_edges:
        raise supervisor.SupervisorError("layout repair requires at least one target edge")
    return {
        "action": action, "page_id": page_id, "target_edges": target_edges,
        "movable_nodes": movable_nodes, "locked_nodes": locked_nodes,
        "locked_edges": sorted(set(host_scope.get("locked_edges", []))),
        "reason": str(result.get("reason", "")),
    }


def _canonical_cell_value(cell):
    return {
        "tag": cell.tag,
        "attributes": sorted(cell.attrib.items()),
        "text": cell.text or "",
        "children": [_canonical_cell_value(child) for child in list(cell)],
    }


def _canonical_cell_hashes(path):
    _, root, _ = supervisor.safe_parse(path)
    values = {}
    for page_id, page in supervisor.page_scopes(root):
        for cell in supervisor.page_cells(page):
            cell_id = cell.get("id")
            if cell_id:
                values[(page_id, cell_id)] = canonical_hash(_canonical_cell_value(cell))
    return values


def _verify_locked_cell_hashes(baseline, candidate, locked_cells):
    """Prove that every immutable cell survived a local candidate unchanged."""
    before, after = _canonical_cell_hashes(baseline), _canonical_cell_hashes(candidate)
    mismatches = []
    for page_id, cell_ids in sorted((locked_cells or {}).items()):
        for cell_id in sorted(cell_ids):
            key = (str(page_id), str(cell_id))
            if before.get(key) != after.get(key):
                mismatches.append({
                    "page_id": key[0], "cell_id": key[1],
                    "baseline_sha256": before.get(key), "candidate_sha256": after.get(key),
                })
    return {
        "valid": not mismatches,
        "reason": None if not mismatches else "preservation_violation",
        "mismatches": mismatches,
    }


def _layout_scope_for_intent(spec, host_scope):
    """Project host findings to a single immutable local-layout scope."""
    pages = {
        str(page.get("id")): {
            str(cell.get("id")): cell for cell in page.get("cells", [])
            if isinstance(cell, dict) and cell.get("id")
        }
        for page in spec.get("pages", []) if isinstance(page, dict) and page.get("id")
    }
    target_ids = set(host_scope.get("allowed_targets", []))
    matches = []
    for page_id, cells in pages.items():
        for cell_id, cell in cells.items():
            if cell_id in target_ids and cell.get("kind") == "edge":
                matches.append((page_id, cell_id, cell))
    if not matches:
        raise supervisor.SupervisorError("layout repair has no host-owned edge targets")
    page_ids = {item[0] for item in matches}
    if len(page_ids) != 1:
        raise supervisor.SupervisorError("layout repair scope spans more than one diagram page")
    page_id = next(iter(page_ids))
    cells = pages[page_id]
    target_edges = sorted(item[1] for item in matches)
    movable_nodes = []
    locked_nodes = sorted(
        cell_id for cell_id, cell in cells.items() if cell.get("kind") != "edge"
    )
    locked_edges = sorted(
        cell_id for cell_id, cell in cells.items()
        if cell.get("kind") == "edge" and cell_id not in target_edges
    )
    return {
        "page_id": page_id,
        "target_edges": target_edges,
        "movable_nodes": movable_nodes,
        "locked_nodes": locked_nodes,
        "locked_edges": locked_edges,
        "allowed_actions": ["edge_reroute"],
        "expansion_count": int(host_scope.get("expansion_count", 0)),
    }


def _next_layout_scope_expansion(workflow, diagram_spec):
    """Persist at most two autonomous local scope expansions, never full reflow."""
    state = workflow.get("layout_repair_scope") or {}
    current = {
        "edge_refs": [
            {"page_id": state["page_id"], "cell_id": cell_id}
            for cell_id in state.get("target_edges", [])
        ],
        "movable_node_refs": [
            {"page_id": state["page_id"], "cell_id": cell_id}
            for cell_id in state.get("movable_nodes", [])
        ],
        "reroutable_edge_refs": [
            {"page_id": state["page_id"], "cell_id": cell_id}
            for cell_id in state.get("target_edges", [])
        ],
    }
    next_scope = layout_model.next_automatic_scope(
        diagram_spec, current, expansion_count=int(state.get("expansion_count", 0)),
    )
    if next_scope is None:
        return None
    scope = next_scope["scope"]
    page_id = scope["page_ids"][0]
    node_ids = {
        str(cell.get("id"))
        for page in diagram_spec.get("pages", [])
        if str(page.get("id")) == page_id
        for cell in page.get("cells", [])
        if isinstance(cell, dict) and cell.get("kind") != "edge" and cell.get("id")
    }
    edge_ids = {
        str(cell.get("id"))
        for page in diagram_spec.get("pages", [])
        if str(page.get("id")) == page_id
        for cell in page.get("cells", [])
        if isinstance(cell, dict) and cell.get("kind") == "edge" and cell.get("id")
    }
    movable_nodes = {
        item["cell_id"] for item in scope["movable_node_refs"]
    }
    target_edges = {
        item["cell_id"] for item in scope["reroutable_edge_refs"]
    }
    updated = {
        **state, "page_id": page_id,
        "target_edges": sorted(target_edges),
        "movable_nodes": sorted(movable_nodes),
        "locked_nodes": sorted(node_ids - movable_nodes),
        "locked_edges": sorted(edge_ids - target_edges),
        "expansion_count": next_scope["expansion_count"],
        "last_action": next_scope["stage"],
    }
    workflow["layout_repair_scope"] = updated
    workflow["active_layout_repair_scope"] = copy.deepcopy(updated)
    workflow.setdefault("layout_scope_expansions", []).append(copy.deepcopy(updated))
    return updated


def _scope_refs_from_layout_state(state):
    return {
        "edge_refs": [
            {"page_id": state["page_id"], "cell_id": cell_id}
            for cell_id in state.get("target_edges", [])
        ],
        "movable_node_refs": [
            {"page_id": state["page_id"], "cell_id": cell_id}
            for cell_id in state.get("movable_nodes", [])
        ],
        "reroutable_edge_refs": [
            {"page_id": state["page_id"], "cell_id": cell_id}
            for cell_id in state.get("target_edges", [])
        ],
    }


def _run_layout_intent_attempts(run_dir, workflow, payload, intent, *, timeout):
    """Run bounded deterministic strategies/scopes for one validated intent."""
    semantic_plan = supervisor.load_json(workflow["semantic_plan_v2"]["path"])
    source_bundle = _source_bundle_bound_to_plan(run_dir, semantic_plan)
    adapter_input = renderer_adapters.select_lifecycle_adapter_input(
        semantic_plan, source_bundle, mode="improve",
    )
    if adapter_input.selection.adapter is not renderer_adapters.GENERIC_ADAPTER:
        raise supervisor.SupervisorError(
            "specialized diagrams must retain their specialized local adapter"
        )
    diagram_spec = payload["baseline"]["diagram_spec"]
    baseline_artifact = Path(_working_artifact(workflow).get("path", ""))
    state = {
        **copy.deepcopy(payload["layout_scope"]),
        "last_action": intent["action"],
        "workflow_iteration": int(workflow.get("iteration", 0)),
        "baseline_sha256": supervisor.sha256_file(baseline_artifact) if baseline_artifact.is_file() else None,
    }
    persisted = workflow.get("active_layout_repair_scope") or {}
    if all(persisted.get(key) == state[key] for key in ("workflow_iteration", "baseline_sha256")):
        state = copy.deepcopy(persisted)
    workflow["active_layout_repair_scope"] = copy.deepcopy(state)
    workflow["layout_repair_scope"] = copy.deepcopy(state)
    scope = _scope_refs_from_layout_state(state)
    expected_local = {"workflow_iteration": state["workflow_iteration"],
                      "baseline_sha256": state["baseline_sha256"],
                      "scope_sha256": canonical_json_sha256(
                          {"page_ids": [state["page_id"]], "node_refs": [], **scope})}
    replayed = lifecycle_v2.require_mutable(run_dir) if baseline_artifact.is_file() \
        else {"events": []}
    completed = []
    for attempt in workflow.setdefault("layout_attempts", []):
        if attempt.get("status") not in {"completed", "failed"}:
            continue
        request_path = _verified_layout_file(
            run_dir, attempt.get("layout_request"), label="layout request")
        if supervisor.load_json(request_path).get("mode") == "local_reflow":
            verified = _verify_persisted_layout_attempt(
                run_dir, workflow, attempt, replayed,
            )
            if verified["status"] == "completed":
                completed.append(verified)
    recovered = _recover_layout_attempts_from_ledger(
        run_dir, workflow, replayed, mode="local_reflow",
        known_attempt_ids={item["attempt_id"] for item in workflow["layout_attempts"]},
    ) if baseline_artifact.is_file() else []
    if recovered:
        workflow["layout_attempts"].extend(copy.deepcopy(recovered))
        completed.extend(item for item in recovered if item["status"] == "completed")
        write_workflow(run_dir, workflow)
    eligible = [
        item for item in completed
        if all(item.get(key) == value for key, value in expected_local.items())
        and item["preservation"]["valid"] and item["comparison"]["accepted"]
    ]
    if eligible:
        order = {name: index for index, (name, _) in enumerate(LAYOUT_STRATEGIES)}
        selected = min(eligible, key=lambda item: order[item["strategy"]])
        return selected
    deadline = _layout_deadline_epoch(workflow, timeout)
    for expansion_count in range(layout_model.MAX_AUTOMATIC_SCOPE_EXPANSIONS + 1):
        scope = _scope_refs_from_layout_state(state)
        for strategy in LAYOUT_STRATEGIES:
            if time.time() >= deadline:
                return None
            try:
                attempt = execute_layout_attempt(
                    workflow, semantic_plan, run_dir=run_dir,
                    adapter_input=adapter_input, mode="local_reflow", scope=scope,
                    strategy=strategy, timeout=max(0.1, deadline - time.time()),
                    baseline=diagram_spec, baseline_artifact=baseline_artifact,
                )
            except Exception as exc:
                workflow.setdefault("layout_failures", []).append({
                    "strategy_id": strategy[0],
                    "scope_expansion": expansion_count,
                    "error": str(exc),
                })
                write_workflow(run_dir, workflow)
                continue
            if attempt.get("status") != "completed":
                continue
            workflow.setdefault("layout_attempts", []).append(copy.deepcopy(attempt))
            write_workflow(run_dir, workflow)
            if (
                attempt.get("preservation", {}).get("valid")
                and attempt.get("comparison", {}).get("accepted")
            ):
                return attempt
        if expansion_count >= layout_model.MAX_AUTOMATIC_SCOPE_EXPANSIONS:
            break
        state = _next_layout_scope_expansion(workflow, diagram_spec)
        if state is None:
            break
        write_workflow(run_dir, workflow)
    return None


def _host_bind_patch(run_dir, workflow, raw_patch, raw_patch_path):
    """Create an executable host-bound patch without changing raw role output."""
    baseline_descriptor = _working_artifact(workflow)
    baseline = Path(baseline_descriptor.get("path", ""))
    if (
        not baseline.is_file()
        or supervisor.sha256_file(baseline) != baseline_descriptor.get("sha256")
    ):
        raise supervisor.SupervisorError(
            "working patch baseline is missing or hash-mismatched"
        )
    _, root, _ = supervisor.safe_parse(baseline)
    page_locations = {
        str(cell.get("id")): page_id
        for page_id, page in supervisor.page_scopes(root)
        for cell in supervisor.page_cells(page)
        if cell.get("id")
    }
    page_ids = {page_id for page_id, _ in supervisor.page_scopes(root)}
    operations = copy.deepcopy(raw_patch["operations"])
    target_ids = {str(operation["target_id"]) for operation in operations}
    existing_target_pages = {
        page_locations[target] for target in target_ids if target in page_locations
    }
    declared_page = raw_patch["affected_region"]["page_id"]
    if len(existing_target_pages) > 1:
        raise supervisor.SupervisorError("patch scope spans more than one diagram page")
    page_id = next(iter(existing_target_pages), declared_page)
    if page_id not in page_ids:
        raise supervisor.SupervisorError(
            "patch affected page is not present in the working baseline"
        )

    scope = _derive_repair_scope(workflow)
    allowed_targets = set(scope.get("allowed_targets", []))
    allowed_operations = set(scope.get("allowed_operations", []))
    semantic_targets = set(scope.get("semantic_targets", []))
    forbidden_targets = target_ids - allowed_targets
    if forbidden_targets:
        raise supervisor.SupervisorError(
            "patch scope violation: targets outside host scope: "
            + ", ".join(sorted(forbidden_targets))
        )
    forbidden_ops = {
        operation["op"] for operation in operations
        if operation["op"] not in allowed_operations
    }
    if forbidden_ops:
        raise supervisor.SupervisorError(
            "patch scope violation: operations outside host scope: "
            + ", ".join(sorted(forbidden_ops))
        )
    for operation in operations:
        if (
            operation.get("semantic_effect") not in {"layout_only", "layout-only"}
            and operation["target_id"] not in semantic_targets
        ):
            raise supervisor.SupervisorError(
                f"patch semantic target {operation['target_id']!r} is outside the approved delta"
            )

    bound = copy.deepcopy(raw_patch)
    bound["baseline"] = {
        "artifact_sha256": supervisor.sha256_file(baseline),
        "semantic_digest": supervisor.document_semantic_digest(root),
    }
    bound["affected_region"] = {
        "page_id": page_id,
        "cell_ids": sorted(target_ids),
    }
    supervisor.validate_patch_contract(bound)
    bound_path = Path(raw_patch_path).with_name("host-bound.patch.json")
    atomic_write_bytes(bound_path, canonical_json_bytes(bound) + b"\n")
    supervisor.append_event(
        run_dir,
        "patch_bound",
        {
            "raw_patch": str(Path(raw_patch_path).resolve()),
            "raw_patch_sha256": supervisor.sha256_file(raw_patch_path),
            "bound_patch": str(bound_path.resolve()),
            "bound_patch_sha256": supervisor.sha256_file(bound_path),
            "baseline_sha256": bound["baseline"]["artifact_sha256"],
            "semantic_digest": bound["baseline"]["semantic_digest"],
            "scope": scope,
        },
        state=supervisor.load_state(run_dir)["state"],
        actor={"kind": "system", "id": "diagram-orchestrator", "model": None},
    )
    return bound, bound_path


def _verify_semantic_patch_authorization(workflow, patch_value):
    semantic_operations = [
        operation for operation in patch_value["operations"]
        if operation.get("semantic_effect", "layout_only") not in {"layout_only", "layout-only"}
    ]
    if not semantic_operations:
        return
    approved = workflow.get("approved_semantic_change") or {}
    delta = approved.get("semantic_delta")
    approval_descriptor = approved.get("semantic_approval_v2") or {}
    if not delta or not approval_descriptor:
        raise supervisor.SupervisorError("semantic patch has no exact v2 human authorization")
    approval_path = Path(approval_descriptor.get("path", ""))
    if not approval_path.is_file() or supervisor.sha256_file(approval_path) != approval_descriptor.get("sha256"):
        raise supervisor.SupervisorError("semantic approval artifact is missing or changed")
    approval = supervisor.load_json(approval_path)
    require_valid_contract(approval, "semantic-approval", 2)
    if approval["decision"] != "approve" or approval["semantic_delta_sha256"] != semantic_delta_sha256(delta):
        raise supervisor.SupervisorError("semantic approval does not authorize the current delta")
    page_id = patch_value["affected_region"]["page_id"]
    approved_targets = {
        (operation["operation_type"], operation["target"]["page_id"], operation["target"]["cell_id"])
        for operation in delta["operations"]
    }
    for operation in semantic_operations:
        patch_type = {
            "semantic_addition": "add",
            "semantic_removal": "remove",
            "semantic_change": "update",
        }[operation["semantic_effect"]]
        target = (page_id, operation["target_id"])
        compatible_types = {patch_type}
        if patch_type == "update":
            compatible_types.update({"relationship", "parent"})
        if not any((kind, *target) in approved_targets for kind in compatible_types):
            raise supervisor.SupervisorError(
                f"semantic patch operation {operation['operation_id']} exceeds the approved typed delta"
            )

    # Type and stable identity are necessary but not sufficient: a Repair
    # response could otherwise keep the approved operation shell and replace
    # its proposed semantic value.  Deterministically replay the patch to a
    # disposable artifact and compare its value-level field/before/after diff
    # with the immutable human-approved v2 delta.  This remains pre-promotion:
    # neither the accepted artifact nor the publication target is mutated.
    baseline_descriptor = workflow.get("accepted_artifact") or {}
    baseline = Path(baseline_descriptor.get("path", ""))
    if (
        not baseline.is_file()
        or supervisor.sha256_file(baseline) != baseline_descriptor.get("sha256")
    ):
        raise supervisor.SupervisorError(
            "semantic authorization baseline is missing or hash-mismatched"
        )
    with tempfile.TemporaryDirectory(prefix="semantic-authorization-") as temporary:
        temporary = Path(temporary)
        patch_path = temporary / "patch.json"
        candidate = temporary / "candidate.drawio"
        supervisor.write_json(patch_path, patch_value)
        supervisor.apply_patch_file(
            baseline, patch_path, candidate, allow_semantic=True,
        )
        semantic_diff = supervisor.semantic_diff_value(baseline, candidate)
    supervisor.load_semantic_approval_v2(
        approval_path,
        workflow["run_id"],
        delta,
        semantic_diff,
    )


def _review_candidate(run_dir, workflow, candidate, report, receipt, patch, cli, timeout, label):
    candidate_receipt_v2 = Path(receipt).with_name("validation-receipt.v2.json")
    baseline_receipt_v2 = Path(workflow["validation_receipt_v2"]["path"])
    payload = _reviewer_input_v2(
        run_dir, workflow, review_kind="candidate_review",
        candidate=candidate, report=report, receipt_v2=candidate_receipt_v2,
        baseline=Path(workflow["accepted_artifact"]["path"]),
        baseline_report=Path(workflow["accepted_validation"]["report"]),
        baseline_receipt_v2=baseline_receipt_v2,
        patch=patch,
    )
    verdict, runtime, input_path, output = role_call(
        "reviewer", payload, run_dir, Path(workflow["workspace"]), cli, timeout, label,
    )
    _verify_reviewer_runtime(verdict, runtime)
    _, verdict_v2_path = _bind_reviewer_v2(
        run_dir, workflow, verdict, runtime, input_path, output,
        candidate, report, candidate_receipt_v2,
    )
    workflow["candidate_reviewer_verdict_v2"] = {
        "path": str(verdict_v2_path.resolve()),
        "sha256": supervisor.sha256_file(verdict_v2_path),
    }
    return verdict, runtime, verdict_v2_path


def _set_workflow_accepted(workflow, state):
    _sync_working_candidate(
        workflow,
        dict(state["accepted_artifact"]),
        dict(state["accepted_validation"]),
    )
    if not state["accepted_validation"].get("strict_passed"):
        _clear_publishable_candidate(workflow)


def repair_loop(run_dir, workflow, cli, timeout, *, already_patching=False):
    while workflow["iteration"] < workflow["max_iterations"]:
        workflow["iteration"] += 1
        write_workflow(run_dir, workflow)
        working_artifact = _working_artifact(workflow)
        working_validation = _working_validation(workflow)
        baseline_path = Path(working_artifact["path"])
        baseline_report = Path(working_validation["report"])
        baseline_receipt = Path(working_validation["receipt"])
        if "repair" not in workflow.get("required_roles", []):
            current = supervisor.load_state(run_dir)["state"]
            if current == "analyzed":
                supervisor.transition(run_dir, "awaiting_decision", artifact=baseline_path)
                current = "awaiting_decision"
            if current != "awaiting_feedback":
                supervisor.transition(run_dir, "awaiting_feedback", decision="pause", reason="Supervisor did not authorize Repair")
            workflow["findings"] = ["Supervisor required_roles did not authorize the Repair role."]
            best_effort = _finish_best_effort(
                run_dir, workflow, cli, timeout,
                reason="Supervisor did not authorize further automatic repair",
            )
            if best_effort is not None:
                return best_effort
            return checkpoint(
                run_dir, workflow, "plateau",
                "Automatic repair was not started because the Supervisor plan did not authorize Repair.",
                workflow["findings"], ["continue", "pause", "stop", "manual_handoff"],
            )
        if not already_patching:
            supervisor.transition(run_dir, "patching", artifact=baseline_path)
        already_patching = False
        layout_attempt = None
        try:
            payload = repair_input(run_dir, workflow)
            raw_patch_value, _, _, raw_patch_path = role_call(
                "repair", payload, run_dir, Path(workflow["workspace"]),
                cli, timeout, f"repair-{workflow['iteration']}"
            )
            if payload.get("repair_mode") == "layout_intent":
                intent = _validate_layout_repair_intent(
                    raw_patch_value, payload["layout_scope"],
                )
                if raw_patch_value.get("run_id") != workflow["run_id"] or raw_patch_value.get("baseline_sha256") != supervisor.sha256_file(baseline_path):
                    raise supervisor.SupervisorError("layout repair intent has mismatched run or baseline binding")
                supervisor.append_event(
                    run_dir, "layout_repair_intent_bound",
                    {"raw_intent": str(Path(raw_patch_path).resolve()), "raw_intent_sha256": supervisor.sha256_file(raw_patch_path), "scope": copy.deepcopy(payload["layout_scope"])},
                    state=supervisor.load_state(run_dir)["state"],
                    actor={"kind": "system", "id": "diagram-orchestrator", "model": None},
                )
                write_workflow(run_dir, workflow)
                if intent["action"] == "finish_best_effort":
                    supervisor.transition(run_dir, "awaiting_feedback", decision="best_effort", reason="layout repair requested safe best effort")
                    best_effort = _finish_best_effort(run_dir, workflow, cli, timeout, reason="Layout Repair requested safe best-effort completion")
                    if best_effort is not None:
                        return best_effort
                supervisor.transition(run_dir, "validating", artifact=baseline_path)
                layout_attempt = _run_layout_intent_attempts(
                    run_dir, workflow, payload, intent, timeout=timeout,
                )
                if layout_attempt is None:
                    supervisor.transition(run_dir, "retrying", artifact=baseline_path)
                    supervisor.transition(run_dir, "awaiting_feedback", decision="best_effort", reason="bounded local layout attempts exhausted")
                    best_effort = _finish_best_effort(run_dir, workflow, cli, timeout, reason="Bounded local layout repair reached no progress")
                    if best_effort is not None:
                        return best_effort
                    return checkpoint(run_dir, workflow, "plateau", "Local layout repair exhausted safely.", workflow.get("findings", []), ["pause", "stop", "manual_handoff"])
                patch_path = Path(layout_attempt["layout_patch"]["path"])
                patch_value = supervisor.load_json(patch_path)
            else:
                workflow.pop("active_layout_repair_scope", None)
                patch_value, patch_path = _host_bind_patch(
                    run_dir, workflow, raw_patch_value, raw_patch_path,
                )
        except supervisor.SupervisorError as exc:
            supervisor.transition(run_dir, "retrying", artifact=baseline_path)
            failure_class = (
                "repair_scope" if "scope" in str(exc).lower()
                else "repair_contract"
            )
            retry, _ = _record_internal_repair_feedback(
                run_dir,
                workflow,
                failure_class=failure_class,
                message=str(exc),
                evidence={"baseline_sha256": supervisor.sha256_file(baseline_path)},
            )
            if retry:
                continue
            supervisor.transition(run_dir, "awaiting_feedback", decision="pause", reason="Repair could not produce a usable typed patch")
            workflow["findings"] = [str(exc)]
            best_effort = _finish_best_effort(
                run_dir, workflow, cli, timeout,
                reason="Repair primary and bounded recovery attempts were exhausted",
            )
            if best_effort is not None:
                return best_effort
            return checkpoint(
                run_dir, workflow, "plateau",
                "Repair could not produce a schema-valid bounded patch; the accepted candidate was preserved.",
                workflow["findings"], ["continue", "pause", "stop", "manual_handoff"],
            )
        semantic_patch = any(
            op["semantic_effect"] not in {"layout_only", "layout-only"}
            for op in patch_value["operations"]
        )
        if semantic_patch and (not workflow.get("semantic_authorized") or not workflow.get("approved_semantic_change")):
            workflow["pending_patch"] = str(patch_path)
            workflow["pending_patch_sha256"] = supervisor.sha256_file(patch_path)
            supervisor.transition(run_dir, "retrying", artifact=baseline_path)
            message = "Repair proposed semantic operations without an approved semantic delta"
            retry, _ = _record_internal_repair_feedback(
                run_dir,
                workflow,
                failure_class="semantic_scope",
                message=message,
                evidence={
                    "raw_patch": str(Path(raw_patch_path).resolve()),
                    "raw_patch_sha256": supervisor.sha256_file(raw_patch_path),
                    "bound_patch": str(Path(patch_path).resolve()),
                    "bound_patch_sha256": supervisor.sha256_file(patch_path),
                },
            )
            if retry:
                continue
            supervisor.transition(run_dir, "awaiting_feedback", decision="pause", reason="semantic patch exceeded the approved scope")
            best_effort = _finish_best_effort(
                run_dir, workflow, cli, timeout,
                reason="Unsafe semantic Repair proposals were rejected; retained baseline selected",
            )
            if best_effort is not None:
                return best_effort
            return checkpoint(
                run_dir, workflow, "plateau",
                "Repair repeatedly proposed semantic changes without a Semantic Analyst plan and human approval.",
                [op["reasons"][0] for op in patch_value["operations"] if op["semantic_effect"] != "layout_only"],
                ["continue", "pause", "stop", "manual_handoff"],
            )
        if semantic_patch:
            try:
                _verify_semantic_patch_authorization(workflow, patch_value)
            except (ContractError, supervisor.SupervisorError, KeyError, ValueError) as exc:
                workflow["pending_patch"] = str(patch_path)
                workflow["pending_patch_sha256"] = supervisor.sha256_file(patch_path)
                supervisor.transition(run_dir, "retrying", artifact=baseline_path)
                retry, _ = _record_internal_repair_feedback(
                    run_dir,
                    workflow,
                    failure_class="semantic_scope",
                    message=str(exc),
                    evidence={
                        "raw_patch": str(Path(raw_patch_path).resolve()),
                        "raw_patch_sha256": supervisor.sha256_file(raw_patch_path),
                        "bound_patch": str(Path(patch_path).resolve()),
                        "bound_patch_sha256": supervisor.sha256_file(patch_path),
                    },
                )
                if retry:
                    continue
                supervisor.transition(run_dir, "awaiting_feedback", decision="pause", reason="Repair repeatedly exceeded semantic authorization")
                best_effort = _finish_best_effort(
                    run_dir, workflow, cli, timeout,
                    reason="Repair exceeded semantic authorization; retained baseline selected",
                )
                if best_effort is not None:
                    return best_effort
                return checkpoint(
                    run_dir, workflow, "plateau",
                    "Repair повторно вышел за подтверждённую семантическую дельту; требуется замечание пользователя или ручная передача.",
                    [str(exc)], ["continue", "pause", "stop", "manual_handoff"],
                    evidence={"patch": str(patch_path), "patch_sha256": supervisor.sha256_file(patch_path)},
                )
        attempt_id = (
            layout_attempt["attempt_id"]
            if layout_attempt is not None else f"iteration-{workflow['iteration']}"
        )
        attempt_dir = (
            Path(layout_attempt["candidate"]["path"]).parent
            if layout_attempt is not None
            else Path(run_dir) / "attempts" / attempt_id
        )
        candidate = (
            Path(layout_attempt["candidate"]["path"])
            if layout_attempt is not None else attempt_dir / "candidate.drawio"
        )
        try:
            if layout_attempt is None:
                tool_step(
                    run_dir, "patch-apply", supervisor.apply_patch_file,
                    baseline_path, patch_path, candidate, allow_semantic=semantic_patch,
                    evidence={"baseline_sha256": supervisor.sha256_file(baseline_path), "patch_sha256": supervisor.sha256_file(patch_path)},
                )
                supervisor.transition(run_dir, "validating", artifact=baseline_path)
                receipt_value = tool_step(
                    run_dir, "strict-validator", supervisor.run_validation,
                    candidate, run_dir, attempt_id=attempt_id,
                    evidence={"candidate_sha256": supervisor.sha256_file(candidate)},
                )
            else:
                receipt_value = supervisor.load_json(
                    layout_attempt["validation"]["receipt"]
                )
        except Exception as exc:
            current = supervisor.load_state(run_dir)["state"]
            if current == "patching":
                supervisor.transition(run_dir, "retrying", artifact=baseline_path)
            elif current == "validating":
                supervisor.transition(run_dir, "retrying", artifact=baseline_path)
            retry, _ = _record_internal_repair_feedback(
                run_dir,
                workflow,
                failure_class="deterministic_tool",
                message=str(exc),
                evidence={
                    "attempt_id": attempt_id,
                    "baseline_sha256": supervisor.sha256_file(baseline_path),
                    "bound_patch": str(Path(patch_path).resolve()),
                    "bound_patch_sha256": supervisor.sha256_file(patch_path),
                },
            )
            if retry:
                continue
            supervisor.transition(run_dir, "awaiting_feedback", decision="pause", reason="deterministic repair tool failed")
            workflow["findings"] = [str(exc)]
            best_effort = _finish_best_effort(
                run_dir, workflow, cli, timeout,
                reason="Deterministic patch processing failed; last safe artifact retained",
            )
            if best_effort is not None:
                return best_effort
            return checkpoint(
                run_dir, workflow, "plateau",
                "Детерминированный patch/validation этап завершился ошибкой; последний принятый артефакт сохранён.",
                workflow["findings"], ["continue", "pause", "stop", "manual_handoff"],
                evidence={"failure_class": "deterministic_tool", "attempt_id": attempt_id},
            )
        report = (
            Path(layout_attempt["validation"]["report"])
            if layout_attempt is not None
            else attempt_dir / "validation-report.json"
        )
        receipt = (
            Path(layout_attempt["validation"]["receipt"])
            if layout_attempt is not None
            else attempt_dir / "validation-receipt.json"
        )
        if layout_attempt is not None:
            preservation = _verify_candidate_preservation(
                workflow, baseline_path, candidate,
                candidate_origin="layout_intent",
            )
            if not preservation["valid"]:
                preservation_path = attempt_dir / "preservation-violation.json"
                atomic_write_bytes(
                    preservation_path,
                    canonical_json_bytes(preservation) + b"\n",
                )
                supervisor.transition(run_dir, "retrying", artifact=baseline_path)
                retry, _ = _record_internal_repair_feedback(
                    run_dir, workflow,
                    failure_class="preservation_violation",
                    message="locked cells changed in local layout candidate",
                    evidence={
                        "candidate": str(candidate.resolve()),
                        "candidate_sha256": supervisor.sha256_file(candidate),
                        "preservation": _workflow_file_descriptor(preservation_path),
                    },
                )
                if retry:
                    continue
                supervisor.transition(run_dir, "awaiting_feedback", decision="best_effort", reason="locked-cell preservation failed")
                best_effort = _finish_best_effort(
                    run_dir, workflow, cli, timeout,
                    reason="Local layout candidate violated locked-cell preservation",
                )
                if best_effort is not None:
                    return best_effort
                return checkpoint(
                    run_dir, workflow, "plateau",
                    "A local candidate changed locked cells; the baseline was retained.",
                    preservation["mismatches"], ["pause", "stop", "manual_handoff"],
                    evidence={"preservation": _workflow_file_descriptor(preservation_path)},
                )
        _, receipt_v2 = lifecycle_v2.mirror_validation_receipt(
            run_dir, legacy_receipt_path=receipt,
        )
        receipt_v2_verification = lifecycle_v2.verify_v2_receipt(run_dir, receipt_v2)
        if not receipt_v2_verification["valid"]:
            supervisor.transition(run_dir, "retrying", artifact=baseline_path)
            supervisor.transition(run_dir, "awaiting_feedback", decision="pause", reason="v2 validation receipt failed")
            workflow["findings"] = [str(receipt_v2_verification["diagnostics"])]
            return checkpoint(
                run_dir, workflow, "plateau",
                "Квитанция валидации v2 не прошла проверку целостности; baseline сохранён.",
                workflow["findings"], ["continue", "pause", "stop", "manual_handoff"],
            )
        workflow.pop("candidate_reviewer_verdict_v2", None)
        verdict = None
        verdict_path = None
        if receipt_v2_verification["strict_passed"]:
            try:
                verdict, _, verdict_path = _review_candidate(
                    run_dir, workflow, candidate, report, receipt, patch_path, cli, timeout,
                    f"reviewer-{workflow['iteration']}",
                )
            except supervisor.SupervisorError as exc:
                supervisor.transition(run_dir, "retrying", artifact=baseline_path)
                retry, _ = _record_internal_repair_feedback(
                    run_dir,
                    workflow,
                    failure_class="reviewer_contract",
                    message=str(exc),
                    evidence={
                        "attempt_id": attempt_id,
                        "candidate_sha256": supervisor.sha256_file(candidate),
                    },
                )
                if retry:
                    continue
                supervisor.transition(run_dir, "awaiting_feedback", decision="pause", reason="independent review failed")
                workflow["findings"] = [str(exc)]
                best_effort = _finish_best_effort(
                    run_dir, workflow, cli, timeout,
                    reason="Independent review failed after bounded attempts; deterministic evidence retained",
                )
                if best_effort is not None:
                    return best_effort
                return checkpoint(
                    run_dir, workflow, "plateau",
                    "The candidate was not promoted because independent review did not produce a usable hash-bound verdict.",
                    workflow["findings"], ["continue", "pause", "stop", "manual_handoff"],
                )
        else:
            workflow["findings"] = supervisor.load_json(report).get("findings", [])
        semantic_approval_path = None
        semantic_approval_v2_path = None
        approved_semantic_delta = None
        if semantic_patch:
            semantic_approval_v2_path = Path(workflow["approved_semantic_change"]["semantic_approval_v2"]["path"])
            approved_semantic_delta = workflow["approved_semantic_change"]["semantic_delta"]
        decision = supervisor.record_candidate(
            run_dir, candidate, baseline_report, report, patch_path,
            baseline_receipt, receipt, reviewer_verdict_path=verdict_path,
            semantic_approval_path=semantic_approval_path,
            semantic_approval_v2_path=semantic_approval_v2_path,
            approved_semantic_delta=approved_semantic_delta,
        )
        state = supervisor.load_state(run_dir)
        workflow["findings"] = (
            verdict.get("findings", [])
            if verdict is not None
            else supervisor.load_json(report).get("findings", [])
        )
        workflow.setdefault("attempts", []).append({
            "iteration": workflow["iteration"], "candidate": str(candidate),
            "candidate_sha256": supervisor.sha256_file(candidate), "decision": decision,
            "validation_result": receipt_value["result"],
            "reviewer_verdict": verdict["verdict"] if verdict is not None else None,
            "review_skipped": verdict is None,
        })
        if decision["accepted"]:
            _set_workflow_accepted(workflow, state)
            workflow["validation_receipt_v2"] = {
                "path": str(receipt_v2.resolve()),
                "sha256": supervisor.sha256_file(receipt_v2),
            }
            verdict_v2_path = (
                Path(workflow["candidate_reviewer_verdict_v2"]["path"])
                if verdict is not None else None
            )
            lifecycle_v2.transition(
                run_dir, "accepted_candidate", iteration=workflow["iteration"],
                accepted_artifact=_relative_file(run_dir, candidate),
                validation_report=_relative_file(run_dir, report),
                validation_receipt=_relative_file(run_dir, receipt_v2),
                reviewer_verdict=(
                    _relative_file(run_dir, verdict_v2_path)
                    if verdict_v2_path is not None else None
                ),
                payload={"comparison": decision.get("reason"), "candidate_sha256": supervisor.sha256_file(candidate)},
            )
            current_source = lifecycle_v2.latest_document(run_dir, "source-bundle")[0]
            source_evidence = copy.deepcopy(current_source["evidence"])
            source_evidence["baseline_validation"] = {
                "artifact": _relative_file(run_dir, candidate),
                "report": _relative_file(run_dir, report),
                "receipt": _relative_file(run_dir, receipt_v2),
            }
            if verdict_v2_path is not None:
                source_evidence["eligible_review_handoff"] = {
                    "artifact": _relative_file(run_dir, candidate),
                    "report": _relative_file(run_dir, report),
                    "receipt": _relative_file(run_dir, receipt_v2),
                    "verdict": _relative_file(run_dir, verdict_v2_path),
                    "findings_sha256": canonical_json_sha256(supervisor.load_json(verdict_v2_path)["findings"]),
                }
            else:
                source_evidence["eligible_review_handoff"] = None
            lifecycle_v2.revise_sources(
                run_dir,
                evidence=source_evidence,
                event_payload={
                    "kind": (
                        "accepted_candidate_review"
                        if verdict_v2_path is not None
                        else "working_candidate_validation"
                    ),
                    "candidate_sha256": supervisor.sha256_file(candidate),
                    "strict_passed": receipt_v2_verification["strict_passed"],
                },
            )
            if verdict_v2_path is not None:
                _set_publishable_candidate(
                    workflow,
                    artifact=_working_artifact(workflow),
                    validation=_working_validation(workflow),
                    receipt_v2=workflow["validation_receipt_v2"],
                    verdict_v2=workflow["candidate_reviewer_verdict_v2"],
                )
            else:
                _clear_publishable_candidate(workflow)
            write_workflow(run_dir, workflow)
            eligibility = _final_approval_eligibility(run_dir, workflow)
            allowed_final = []
            if eligibility["approve"]:
                allowed_final.append("approve")
            if eligibility["approve_with_findings"]:
                allowed_final.append("approve_with_findings")
            if allowed_final:
                workflow["final_approval_eligibility"] = eligibility
                write_workflow(run_dir, workflow)
                supervisor.transition(run_dir, "final_review", artifact=Path(workflow["accepted_artifact"]["path"]))
                return checkpoint(
                    run_dir, workflow, "final_acceptance",
                    (
                        "The best candidate passed strict validation and independent review."
                        if eligibility["approve"]
                        else "The best candidate is structurally safe and evidence-valid, but unresolved findings require explicit approval."
                    ),
                    eligibility["unresolved_findings"],
                    [*allowed_final, "continue", "pause", "stop", "manual_handoff"],
                    evidence={"final_approval_eligibility": eligibility},
                )
            continue
        lifecycle_v2.transition(
            run_dir, state["state"], iteration=workflow["iteration"],
            last_error={
                "code": "candidate.not_accepted",
                "message": decision.get("reason", "candidate was not accepted"),
                "recoverable": state["state"] not in {"manual_handoff", "stopped"},
                "evidence_path": None,
            },
            payload={"candidate_sha256": supervisor.sha256_file(candidate)},
        )
        if decision.get("reason") == "reviewer_needs_human":
            workflow["status"] = "awaiting_human"
            write_workflow(run_dir, workflow)
            return checkpoint(
                run_dir,
                workflow,
                "feedback",
                "Reviewer found a real ambiguity that requires human judgment; the candidate was not promoted and no automatic retry was scheduled.",
                workflow["findings"],
                ["continue", "pause", "stop", "manual_handoff"],
                evidence={
                    "failure_class": "reviewer_needs_human",
                    "candidate_sha256": supervisor.sha256_file(candidate),
                },
            )
        if state["state"] == "retrying":
            retry, _ = _record_internal_repair_feedback(
                run_dir,
                workflow,
                failure_class="candidate_quality",
                message=decision.get("reason", "candidate was not accepted"),
                evidence={
                    "attempt_id": attempt_id,
                    "candidate_sha256": supervisor.sha256_file(candidate),
                    "comparison": copy.deepcopy(decision),
                    "findings": copy.deepcopy(workflow["findings"]),
                },
            )
            if not retry:
                supervisor.transition(
                    run_dir,
                    "awaiting_feedback",
                    reason="the same candidate failure repeated",
                )
                workflow["status"] = "awaiting_human"
                write_workflow(run_dir, workflow)
                best_effort = _finish_best_effort(
                    run_dir, workflow, cli, timeout,
                    reason="The same non-improving candidate failure repeated",
                )
                if best_effort is not None:
                    return best_effort
                return checkpoint(
                    run_dir,
                    workflow,
                    "plateau",
                    "Automatic repair stopped after the same normalized failure repeated.",
                    workflow["findings"],
                    ["continue", "pause", "stop", "manual_handoff"],
                    evidence={
                        "failure_class": "candidate_quality",
                        "failure_signature_limit": MAX_IDENTICAL_FAILURES,
                    },
                )
            write_workflow(run_dir, workflow)
            continue
        workflow["status"] = "awaiting_human"
        write_workflow(run_dir, workflow)
        if state["state"] == "plateau":
            supervisor.transition(run_dir, "awaiting_feedback", reason="automatic improvement plateau")
        best_effort = _finish_best_effort(
            run_dir, workflow, cli, timeout,
            reason="Automatic improvement reached a plateau",
        )
        if best_effort is not None:
            return best_effort
        return checkpoint(
            run_dir, workflow, "plateau",
            "Automatic repair stopped because the candidate repeated or stopped improving.",
            workflow["findings"], ["continue", "pause", "stop", "manual_handoff"],
        )
    state = supervisor.load_state(run_dir)
    if state["state"] not in {"awaiting_feedback", "final_review"}:
        if state["state"] == "retrying":
            supervisor.transition(run_dir, "awaiting_feedback", reason="iteration limit reached")
        elif state["state"] == "accepted_candidate":
            if workflow.get("publishable_candidate"):
                supervisor.transition(
                    run_dir,
                    "final_review",
                    artifact=Path(workflow["publishable_candidate"]["artifact"]["path"]),
                )
            else:
                supervisor.transition(
                    run_dir,
                    "awaiting_feedback",
                    reason="iteration limit reached before strict validation passed",
                )
    workflow["status"] = "awaiting_human"
    write_workflow(run_dir, workflow)
    best_effort = _finish_best_effort(
        run_dir, workflow, cli, timeout,
        reason="Configured automatic iteration limit was reached",
    )
    if best_effort is not None:
        return best_effort
    return checkpoint(
        run_dir, workflow, "plateau", "Configured automatic iteration limit was reached.",
        workflow.get("findings", []), ["continue", "pause", "stop", "manual_handoff"],
    )


def _start_run_impl(
    mode, diagram, request, workspace, cli, *, run_id, timeout=600,
    max_iterations=DEFAULT_MAX_ITERATIONS, review_handoff=None,
    lock_recoveries=(), explicit_documents=(), intake_path=None,
):
    workspace = normalize_workspace(workspace)
    source = normalize_drawio(diagram, workspace, must_exist=(mode == "improve"))
    run_dir = run_dir_for(workspace, run_id)
    if run_dir.exists() and any(path.name != ".run-lock" for path in run_dir.iterdir()):
        raise supervisor.SupervisorError(f"run directory already exists: {run_dir}")
    supervisor.host_preflight(workspace, run_dir, cli)
    workflow = {
        "schema_version": 1, "run_id": supervisor.ensure_run_id(run_dir), "mode": mode,
        "workspace": str(workspace), "target": str(source), "request": request.strip(),
        "status": "running", "iteration": 0, "max_iterations": max_iterations,
        "created_at": supervisor.utc_now(), "attempts": [], "findings": [], "checkpoint": None,
        "quality_profile_version": 2,
        "layout_repair_enabled": mode == "improve",
    }
    if not workflow["request"]:
        raise supervisor.SupervisorError("diagram request must not be empty")
    request_path = run_dir / "inputs" / "request.json"
    supervisor.write_json(request_path, {"schema_version": 1, "mode": mode, "diagram": str(source), "request": request})
    if intake_path is not None:
        intake_path = Path(intake_path).resolve()
        if not intake_path.is_file() or not _inside(intake_path, workspace):
            raise supervisor.SupervisorError(
                "completed intake evidence must be a file inside the workspace"
            )
        intake_value = supervisor.load_json(intake_path)
        layout_contracts.require_diagram_intake(intake_value)
        if (
            intake_value["status"] != "complete"
            or intake_value["mode"] != mode
            or intake_value["request_sha256"] != _intake_request_sha256(request)
        ):
            raise supervisor.SupervisorError(
                "completed intake is not bound to this run request"
            )
        run_intake_path = run_dir / "inputs" / "diagram-intake.json"
        atomic_write_bytes(run_intake_path, intake_path.read_bytes())
        workflow["diagram_intake"] = {
            "intake_id": intake_value["intake_id"],
            **_relative_file(run_dir, run_intake_path),
            "source_path": str(intake_path),
        }
    supervisor.append_event(run_dir, "run_created", {"mode": mode, "request": str(request_path), "request_sha256": supervisor.sha256_file(request_path)})
    write_workflow(run_dir, workflow)
    lifecycle_v2.initialize(
        run_dir=run_dir, workspace=workspace, target=source,
        run_id=workflow["run_id"], mode=mode, request=workflow["request"],
        extension_root=ROOT, explicit_documents=explicit_documents,
    )
    lifecycle_v2.record_lock_recovery(run_dir, lock_recoveries)
    if review_handoff and review_handoff.get("reviewer_verdict_path"):
        handoff_root = run_dir / "inputs" / "review-handoff"
        copied = {}
        for name, source_path, expected_sha in (
            ("artifact", review_handoff["diagram"], review_handoff["artifact_sha256"]),
            ("report", review_handoff["validation_report"], review_handoff["validation_report_sha256"]),
            ("receipt", review_handoff["validation_receipt"], review_handoff["validation_receipt_sha256"]),
            ("verdict", review_handoff["reviewer_verdict_path"], review_handoff["reviewer_verdict_sha256"]),
        ):
            path = Path(source_path).resolve()
            if not path.is_file() or supervisor.sha256_file(path) != expected_sha:
                raise supervisor.SupervisorError(f"review handoff {name} changed before improve started")
            destination = handoff_root / ("artifact.drawio" if name == "artifact" else f"{name}.json")
            atomic_write_bytes(destination, path.read_bytes())
            copied[name] = _relative_file(run_dir, destination)
        lifecycle_v2.revise_sources(
            run_dir,
            new_sources=[],
            evidence={
                "imported_diagramspec": None,
                "baseline_validation": None,
                "eligible_review_handoff": {
                    **copied,
                    "findings_sha256": review_handoff["findings_sha256"],
                },
            },
            event_payload={"kind": "review_handoff_import", "review_run_id": review_handoff["run_id"]},
        )
        workflow["review_handoff"] = copy.deepcopy(review_handoff)
    original = run_dir / "inputs" / "original.drawio"
    accepted = run_dir / "accepted" / "baseline.drawio"
    imported_spec = None
    baseline_spec_v2 = None
    if mode == "improve":
        atomic_copy(source, original)
        workflow["original_artifact"] = {"path": str(original), "sha256": supervisor.sha256_file(original)}
        imported_spec = supervisor.make_spec(original, [source_ref_for_request(workflow["run_id"], request)])
        atomic_copy(original, accepted)
        supervisor.write_json(run_dir / "diagram-spec.json", imported_spec)
        supervisor.transition(run_dir, "analyzed", artifact=accepted, max_attempts=max_iterations)
        receipt = tool_step(run_dir, "strict-validator", supervisor.run_validation, accepted, run_dir, attempt_id="baseline")
        report_path = run_dir / "attempts" / "baseline" / "validation-report.json"
        receipt_path = run_dir / "attempts" / "baseline" / "validation-receipt.json"
        bind_accepted_validation(run_dir, report_path, receipt_path)
        state = supervisor.load_state(run_dir)
        _set_workflow_accepted(workflow, state)
        baseline_spec_v2 = _record_baseline_v2(
            run_dir, workflow, accepted, imported_spec, report_path, receipt_path,
        )
        write_workflow(run_dir, workflow)
    else:
        lifecycle_v2.transition(run_dir, "analyzing", payload={"phase": "semantic_planning"})
    supervisor_payload = {
        "schema_version": 1, "run_id": workflow["run_id"], "mode": mode,
        "request": request, "target": str(source), "existing_diagram_spec": imported_spec,
        "baseline_validation": workflow.get("accepted_validation"),
        "constraints": {"local_only": True, "deterministic_mutations": True, "max_iterations": max_iterations},
    }
    supervisor_result, _, _, _ = role_call("supervisor", supervisor_payload, run_dir, workspace, cli, timeout, "supervisor-initial")
    consume_supervisor_decision(
        workflow, supervisor_result, phase="initial", requested_max_iterations=max_iterations,
    )
    write_workflow(run_dir, workflow)
    semantic_payload = _semantic_input_v2(
        run_dir, workflow, baseline_spec_v2,
    )
    semantic_analysis, _, _, analysis_path = role_call(
        "semantic_analyst", semantic_payload, run_dir, workspace, cli, timeout,
        "semantic-initial",
    )
    analysis_descriptor = {
        "path": str(Path(analysis_path).resolve()),
        "sha256": supervisor.sha256_file(analysis_path),
    }
    workflow["semantic_analysis_v2"] = analysis_descriptor
    # Compatibility alias for old trace/checkpoint readers. New runs never
    # interpret this analysis artifact as a canonical semantic plan.
    workflow["semantic_plan"] = dict(analysis_descriptor)
    semantic_plan_v2, semantic_plan_v2_path = _semantic_analysis_to_v2(
        run_dir, workflow, semantic_analysis, baseline_spec_v2,
    )
    workflow["semantic_plan_v2"] = {
        "path": str(semantic_plan_v2_path.resolve()),
        "sha256": supervisor.sha256_file(semantic_plan_v2_path),
        "semantic_delta_sha256": semantic_delta_sha256(semantic_plan_v2["result"]["semantic_delta"]),
    }
    if mode == "create" and semantic_plan_v2["result"]["requires_human"]:
        supervisor.transition(run_dir, "analyzed", max_attempts=max_iterations)
        supervisor.transition(
            run_dir, "awaiting_decision",
            reason="semantic plan requires clarification before rendering",
        )
        delta = semantic_plan_v2["result"]["semantic_delta"]
        questions = semantic_plan_v2["result"]["human_questions"]
        pending_semantic = {
            "semantic_plan": {
                "path": str(semantic_plan_v2_path.resolve()),
                "sha256": supervisor.sha256_file(semantic_plan_v2_path),
            },
            "baseline_semantic_digest": semantic_plan_v2["baseline_semantic_digest"],
            "source_bundle_sha256": semantic_plan_v2["source_bundle_sha256"],
            "semantic_delta": delta,
            "semantic_delta_sha256": semantic_delta_sha256(delta),
            "semantic_changes": questions,
            "semantic_changes_sha256": canonical_hash(questions),
        }
        workflow["pending_semantic_approval"] = pending_semantic
        write_workflow(run_dir, workflow)
        return checkpoint(
            run_dir, workflow, "semantic_approval",
            "The create plan needs clarification or approval before any diagram bytes are rendered.",
            questions, ["continue", "pause", "stop", "manual_handoff"],
            evidence=pending_semantic,
        )
    if mode == "create":
        accepted.parent.mkdir(parents=True, exist_ok=True)
        source_bundle = lifecycle_v2.latest_document(run_dir, "source-bundle")[0]
        adapter_input = renderer_adapters.select_lifecycle_adapter_input(
            semantic_plan_v2, source_bundle, mode="create",
        )
        selected_adapter = adapter_input.selection.adapter
        lifecycle_v2.transition(
            run_dir,
            "analyzing",
            payload={
                "phase": "rendering",
                "renderer_adapter": adapter_input.record(),
            },
        )
        if selected_adapter is renderer_adapters.GENERIC_ADAPTER:
            selected = _run_generic_create_layouts(
                run_dir,
                workflow,
                semantic_plan_v2,
                adapter_input,
                timeout=timeout,
            )
            accepted, baseline_spec_v2 = _adopt_create_layout_attempt(
                run_dir,
                workflow,
                selected,
                request=request,
                max_iterations=max_iterations,
            )
            workflow["renderer_adapter"] = {
                **adapter_input.record(),
                "options": {
                    **dict(adapter_input.options),
                    "reflow": "full",
                },
                "output_path": str(accepted.resolve()),
                "output_hash": supervisor.sha256_file(accepted),
                "command": ["host:execute_layout_attempt"],
                "layout_request_sha256": selected["request_sha256"],
                "layout_result_sha256": selected["layout_result"]["sha256"],
                "requested_semantic_diagram_type": semantic_analysis["result"]["diagram_type"],
            }
        else:
            renderer_source = semantic_plan_v2
            if adapter_input.source_record is not None:
                source_path = (
                    run_dir / "inputs" / "renderer-sources"
                    / f"{adapter_input.source_record['content_sha256']}.json"
                )
                atomic_write_bytes(
                    source_path,
                    canonical_json_bytes(adapter_input.source_content) + b"\n",
                )
                renderer_source = source_path
            adapter_run = tool_step(
                run_dir, "renderer-adapter",
                renderer_adapters.render_with_adapter,
                selected_adapter.diagram_type, renderer_source, accepted,
                mode="create",
                options=dict(adapter_input.options),
                generic_renderer=render_generic, timeout=timeout,
            )
            workflow["renderer_adapter"] = {
                **adapter_input.record(),
                # Selection evidence owns source binding/fallback provenance;
                # the actual run owns invoked options, command and output.
                **adapter_run.record(),
                "requested_semantic_diagram_type": semantic_analysis["result"]["diagram_type"],
            }
            spec = supervisor.make_spec(
                accepted,
                [source_ref_for_request(workflow["run_id"], request)],
            )
            supervisor.write_json(run_dir / "diagram-spec.json", spec)
            supervisor.transition(
                run_dir,
                "analyzed",
                artifact=accepted,
                max_attempts=max_iterations,
            )
            validation_profile = selected_adapter.validation_profile
            validation_source = (
                adapter_run.source_path
                if validation_profile in {"roadmap", "gitflow"}
                else None
            )
            receipt = tool_step(
                run_dir, "strict-validator", supervisor.run_validation,
                accepted, run_dir,
                profile=None if validation_profile == "structural" else validation_profile,
                source=validation_source,
                attempt_id="baseline",
            )
            report_path = run_dir / "attempts" / "baseline" / "validation-report.json"
            receipt_path = run_dir / "attempts" / "baseline" / "validation-receipt.json"
            bind_accepted_validation(run_dir, report_path, receipt_path)
            state = supervisor.load_state(run_dir)
            _set_workflow_accepted(workflow, state)
            baseline_spec_v2 = _record_baseline_v2(
                run_dir, workflow, accepted, spec, report_path, receipt_path,
            )
    write_workflow(run_dir, workflow)
    delta = semantic_plan_v2["result"]["semantic_delta"]
    changes = semantic_plan_v2["result"]["human_questions"]
    if mode == "improve" and (semantic_plan_v2["result"]["requires_human"] or delta["operations"]):
        supervisor.transition(run_dir, "awaiting_decision", artifact=accepted)
        pending_semantic = {
            "semantic_plan": {"path": str(semantic_plan_v2_path.resolve()), "sha256": supervisor.sha256_file(semantic_plan_v2_path)},
            "baseline_semantic_digest": semantic_plan_v2["baseline_semantic_digest"],
            "source_bundle_sha256": semantic_plan_v2["source_bundle_sha256"],
            "semantic_delta": delta,
            "semantic_delta_sha256": semantic_delta_sha256(delta),
            "semantic_changes": changes,
            "semantic_changes_sha256": canonical_hash(changes),
        }
        workflow["pending_semantic_approval"] = pending_semantic
        return checkpoint(
            run_dir, workflow, "semantic_approval",
            "The supplied process description differs from the existing diagram; these changes will be used in repair after approval.",
            changes, ["continue", "pause", "stop", "manual_handoff"], evidence=pending_semantic,
        )
    if not _working_validation(workflow).get("strict_passed"):
        _clear_publishable_candidate(workflow)
        workflow["findings"] = supervisor.load_json(
            _working_validation(workflow)["report"]
        ).get("findings", [])
        write_workflow(run_dir, workflow)
        if mode == "create" and workflow.get("layout_strategy_exhausted"):
            best_effort = _finish_best_effort(
                run_dir,
                workflow,
                cli,
                timeout,
                reason=(
                    "Bounded deterministic layout strategies completed without "
                    "a strict pass; the safest validated candidate was retained"
                ),
            )
            if best_effort is not None:
                return best_effort
        return repair_loop(run_dir, workflow, cli, timeout)
    try:
        verdict, _ = baseline_review(run_dir, workflow, cli, timeout)
    except supervisor.SupervisorError as exc:
        supervisor.transition(run_dir, "awaiting_decision", artifact=accepted)
        supervisor.transition(run_dir, "awaiting_feedback", reason="independent review failed")
        workflow["findings"] = [str(exc)]
        return checkpoint(
            run_dir, workflow, "plateau",
            "Strict validation evidence was retained, but independent review did not produce a usable verdict.",
            workflow["findings"], ["continue", "pause", "stop", "manual_handoff"],
        )
    workflow["findings"] = verdict.get("findings", [])
    if verdict.get("verdict") == "needs_human":
        supervisor.transition(
            run_dir,
            "final_review",
            artifact=Path(_working_artifact(workflow)["path"]),
        )
        supervisor.transition(
            run_dir,
            "awaiting_feedback",
            reason="Reviewer found an ambiguity in the baseline",
        )
        write_workflow(run_dir, workflow)
        return checkpoint(
            run_dir,
            workflow,
            "feedback",
            "Reviewer found a real ambiguity that requires human judgment; no automatic repair was started.",
            workflow["findings"],
            ["continue", "pause", "stop", "manual_handoff"],
            evidence={"failure_class": "reviewer_needs_human"},
        )
    _set_publishable_candidate(
        workflow,
        artifact=_working_artifact(workflow),
        validation=_working_validation(workflow),
        receipt_v2=workflow["validation_receipt_v2"],
        verdict_v2=workflow["reviewer_verdict_v2"],
    )
    write_workflow(run_dir, workflow)
    eligibility = _final_approval_eligibility(run_dir, workflow)
    allowed_final = []
    if eligibility["approve"]:
        allowed_final.append("approve")
    if eligibility["approve_with_findings"]:
        allowed_final.append("approve_with_findings")
    if allowed_final:
        workflow["final_approval_eligibility"] = eligibility
        write_workflow(run_dir, workflow)
        supervisor.transition(run_dir, "final_review", artifact=accepted)
        return checkpoint(
            run_dir, workflow, "final_acceptance",
            (
                "The candidate passed strict validation and independent review."
                if eligibility["approve"]
                else "The candidate is structurally safe and evidence-valid, but unresolved findings require explicit approval."
            ),
            eligibility["unresolved_findings"],
            [*allowed_final, "continue", "pause", "stop", "manual_handoff"],
            evidence={"final_approval_eligibility": eligibility},
        )
    return repair_loop(run_dir, workflow, cli, timeout)


def start_run(
    mode, diagram, request, workspace, cli, *, run_id=None, timeout=600,
    max_iterations=DEFAULT_MAX_ITERATIONS, review_handoff=None,
    explicit_documents=(), intake_path=None,
):
    normalized_workspace = normalize_workspace(workspace)
    # Reject invalid/existing targets before creating the lock/run hierarchy.
    normalize_drawio(diagram, normalized_workspace, must_exist=(mode == "improve"))
    effective_run_id = run_id or utc_slug(mode)
    run_dir = run_dir_for(normalized_workspace, effective_run_id)
    try:
        with RunLock(
            workspace=normalized_workspace, run_dir=run_dir, run_id=effective_run_id,
        ) as run_lock:
            try:
                return _start_run_impl(
                    mode, diagram, request, normalized_workspace, cli,
                    run_id=effective_run_id, timeout=timeout, max_iterations=max_iterations,
                    review_handoff=review_handoff,
                    lock_recoveries=run_lock.recovery_records,
                    explicit_documents=explicit_documents,
                    intake_path=intake_path,
                )
            except Exception as exc:
                if run_dir.is_dir():
                    if (run_dir / WORKFLOW_FILE).is_file():
                        workflow = load_workflow(run_dir)
                    else:
                        workflow = {
                            "schema_version": 1, "run_id": supervisor.ensure_run_id(run_dir),
                            "mode": mode, "workspace": str(normalized_workspace),
                            "target": str(diagram), "request": request, "status": "error",
                            "created_at": supervisor.utc_now(), "checkpoint": None,
                        }
                    workflow["status"] = "error"
                    workflow["error"] = str(exc)
                    write_workflow(run_dir, workflow)
                    if lifecycle_v2.manifest_path(run_dir).is_file():
                        try:
                            lifecycle_v2.transition(
                                run_dir, "failed",
                                last_error={"code": "run.start_failed", "message": str(exc), "recoverable": True, "evidence_path": None},
                            )
                        except Exception:
                            pass
                    host_result(run_dir, workflow, error=str(exc))
                raise
    except RunAlreadyLocked as exc:
        raise supervisor.SupervisorError(json.dumps(exc.as_result(), ensure_ascii=False)) from exc


def publish(run_dir, workflow, decision):
    publishable = workflow.get("publishable_candidate") or {}
    if not publishable:
        raise supervisor.SupervisorError(
            "publication requires a strict-pass publishable candidate"
        )
    artifact_descriptor = publishable.get("artifact") or {}
    validation_descriptor = publishable.get("validation") or {}
    receipt_descriptor = publishable.get("validation_receipt_v2") or {}
    accepted = Path(artifact_descriptor.get("path", ""))
    target = Path(workflow["target"])
    report = Path(validation_descriptor.get("report", ""))
    receipt_v2 = Path(receipt_descriptor.get("path", ""))
    verdict_descriptor = publishable.get("reviewer_verdict_v2") or {}
    verdict_path = Path(verdict_descriptor.get("path", ""))
    if (
        not verdict_path.is_file()
        or supervisor.sha256_file(verdict_path) != verdict_descriptor.get("sha256")
    ):
        raise supervisor.SupervisorError("publication requires the exact host-bound Reviewer v2 verdict")
    verdict = supervisor.load_json(verdict_path)
    require_valid_contract(verdict, "reviewer-verdict", 2)
    eligibility = _final_approval_eligibility(run_dir, workflow)
    if decision not in {"approve", "approve_with_findings"} or not eligibility.get(decision):
        raise supervisor.SupervisorError(
            f"publication decision {decision!r} is not authorized by current final evidence: "
            f"{eligibility.get('reason') or 'decision gate is false'}"
        )
    if any((
        verdict["run_id"] != workflow["run_id"],
        verdict["bindings"]["candidate_sha256"] != supervisor.sha256_file(accepted),
        verdict["bindings"]["report_sha256"] != supervisor.sha256_file(report),
        verdict["bindings"]["receipt_sha256"] != supervisor.sha256_file(receipt_v2),
    )):
        raise supervisor.SupervisorError("publication Reviewer v2 evidence does not authorize this accepted artifact")
    source_bundle = lifecycle_v2.latest_document(run_dir, "source-bundle")[0]
    handoff = source_bundle.get("evidence", {}).get("eligible_review_handoff") or {}
    if (
        handoff.get("verdict", {}).get("sha256") != supervisor.sha256_file(verdict_path)
        or handoff.get("artifact", {}).get("sha256") != supervisor.sha256_file(accepted)
        or handoff.get("report", {}).get("sha256") != supervisor.sha256_file(report)
        or handoff.get("receipt", {}).get("sha256") != supervisor.sha256_file(receipt_v2)
    ):
        raise supervisor.SupervisorError("publication source bundle lacks the exact eligible review handoff")
    transaction = lifecycle_v2.publish_transaction(
        run_dir,
        accepted_artifact=accepted,
        validation_report=report,
        validation_receipt=receipt_v2,
        reviewer_verdict=verdict_path,
        unresolved_findings=eligibility["unresolved_findings"],
        decision=decision,
    )
    if transaction["status"] != "committed":
        raise supervisor.SupervisorError("publication transaction did not commit")
    supervisor.append_event(
        run_dir, "artifact_published",
        {"decision": decision, "artifact": str(target), "artifact_sha256": supervisor.sha256_file(target), "accepted_sha256": supervisor.sha256_file(accepted), "publication_id": transaction["publication_id"]},
        actor={"kind": "human", "id": "user", "model": None},
    )
    return target


def _best_effort_candidate(run_dir, workflow):
    artifact = _working_artifact(workflow)
    validation = _working_validation(workflow)
    receipt_v2 = workflow.get("validation_receipt_v2") or {}
    reviewer = (
        workflow.get("candidate_reviewer_verdict_v2")
        or workflow.get("reviewer_verdict_v2")
        or {}
    )
    if not artifact or not validation or not receipt_v2:
        return {
            "safe": False,
            "diagnostics": [{"code": "best_effort.evidence_missing"}],
        }
    reviewer_path = Path(reviewer.get("path", ""))
    classification = lifecycle_v2.verify_best_effort_candidate(
        run_dir,
        artifact=Path(artifact["path"]),
        report=Path(validation["report"]),
        receipt=Path(receipt_v2["path"]),
        reviewer_verdict=(reviewer_path if reviewer_path.is_file() else None),
        require_accepted_binding=True,
    )
    return {
        "artifact": copy.deepcopy(artifact),
        "validation": copy.deepcopy(validation),
        "validation_receipt_v2": copy.deepcopy(receipt_v2),
        "reviewer_verdict_v2": (
            copy.deepcopy(reviewer) if reviewer_path.is_file() else None
        ),
        "classification": classification,
        "safe": classification["safe"],
        "diagnostics": classification["diagnostics"],
    }


def _best_effort_publication_target(workflow):
    target = Path(workflow["target"]).resolve()
    if workflow.get("mode") != "create":
        return target
    run_id = str(workflow["run_id"])
    return target.with_name(
        f"{target.stem}.best-effort-{run_id}{target.suffix}"
    )


def _finish_best_effort(run_dir, workflow, cli, timeout, *, reason):
    """Return a safe usable artifact without weakening strict completion."""
    if not workflow.get("reviewer_verdict_v2"):
        try:
            baseline_review(run_dir, workflow, cli, min(timeout, 180))
            workflow["best_effort_review"] = {"status": "completed"}
        except supervisor.SupervisorError as exc:
            workflow["best_effort_review"] = {
                "status": "failed", "reason": str(exc),
            }
    candidate = _best_effort_candidate(run_dir, workflow)
    workflow["best_effort_candidate"] = candidate
    if not candidate.get("safe"):
        workflow["best_effort"] = {
            "eligible": False,
            "reason": reason,
            "diagnostics": copy.deepcopy(candidate.get("diagnostics", [])),
        }
        write_workflow(run_dir, workflow)
        return None
    classification = candidate["classification"]
    artifact = Path(candidate["artifact"]["path"])
    validation = candidate["validation"]
    receipt_v2 = Path(candidate["validation_receipt_v2"]["path"])
    reviewer_descriptor = candidate.get("reviewer_verdict_v2") or {}
    reviewer_path = Path(reviewer_descriptor.get("path", ""))
    unresolved = [
        {"source": "validator", "finding": item}
        for item in classification.get("findings", [])
    ] + [
        {"source": "reviewer", "finding": item}
        for item in classification.get("reviewer_findings", [])
    ]
    supervisor.append_event(
        run_dir,
        "best_effort_selected",
        {
            "artifact": candidate["artifact"],
            "report_sha256": classification.get("report_sha256"),
            "receipt_sha256": classification.get("receipt_sha256"),
            "strict_passed": classification.get("strict_passed"),
            "remaining_finding_ids": [
                item.get("finding", {}).get("finding_id")
                for item in unresolved
                if item.get("finding", {}).get("finding_id")
            ],
            "selection_reason": reason,
        },
        actor={"kind": "system", "id": "diagram-orchestrator", "model": None},
    )
    publication = {"disposition": "run_local", "reason": reason}
    v2_selection_recorded = False
    target = _best_effort_publication_target(workflow)
    if workflow["mode"] == "create":
        workflow["best_effort_target"] = str(target)
    published_descriptor = None
    final_descriptor = copy.deepcopy(candidate["artifact"])
    unchanged_improve = bool(
        workflow["mode"] == "improve"
        and (workflow.get("original_artifact") or {}).get("sha256")
        == candidate["artifact"].get("sha256")
    )
    if unchanged_improve:
        expected = (workflow.get("original_artifact") or {}).get("sha256")
        if target.is_file() and supervisor.sha256_file(target) == expected:
            published_descriptor = {
                "path": str(target.resolve()), "sha256": expected,
            }
            final_descriptor = copy.deepcopy(published_descriptor)
            publication = {
                "disposition": "source_preserved",
                "reason": "no safe monotonic improvement replaced the source",
            }
        else:
            publication = {
                "disposition": "run_local_conflict",
                "reason": "improve target changed; source was not overwritten",
            }
    else:
        try:
            transaction = lifecycle_v2.publish_transaction(
                run_dir,
                accepted_artifact=artifact,
                validation_report=Path(validation["report"]),
                validation_receipt=receipt_v2,
                reviewer_verdict=(reviewer_path if reviewer_path.is_file() else None),
                unresolved_findings=unresolved,
                decision="best_effort",
                target_override=(
                    target
                    if workflow["mode"] == "create"
                    else None
                ),
            )
            if transaction["status"] != "committed":
                raise supervisor.SupervisorError(
                    "best-effort publication transaction did not commit"
                )
            published_descriptor = {
                "path": str(target.resolve()),
                "sha256": supervisor.sha256_file(target),
            }
            final_descriptor = copy.deepcopy(published_descriptor)
            publication = {
                "disposition": "published",
                "publication_id": transaction["publication_id"],
                "reason": reason,
            }
            supervisor.append_event(
                run_dir, "artifact_published",
                {
                    "decision": "best_effort",
                    "artifact": str(target.resolve()),
                    "artifact_sha256": published_descriptor["sha256"],
                    "accepted_sha256": candidate["artifact"]["sha256"],
                    "publication_id": transaction["publication_id"],
                },
                actor={"kind": "system", "id": "diagram-orchestrator", "model": None},
            )
        except ContractError as exc:
            if exc.code != "publication.conflict":
                raise
            publication = {
                "disposition": "run_local_conflict",
                "reason": str(exc),
                "code": exc.code,
            }
            lifecycle_v2.transition(
                run_dir, "best_effort_completed",
                accepted_artifact=_relative_file(run_dir, artifact),
                validation_report=_relative_file(run_dir, validation["report"]),
                validation_receipt=_relative_file(run_dir, receipt_v2),
                reviewer_verdict=(
                    _relative_file(run_dir, reviewer_path)
                    if reviewer_path.is_file() else None
                ),
                payload={
                    "best_effort": True,
                    "classification": classification,
                    "selection_reason": reason,
                    "publication": publication,
                },
            )
            v2_selection_recorded = True
    if unchanged_improve:
        lifecycle_v2.transition(
            run_dir, "best_effort_completed",
            accepted_artifact=_relative_file(run_dir, artifact),
            validation_report=_relative_file(run_dir, validation["report"]),
            validation_receipt=_relative_file(run_dir, receipt_v2),
            reviewer_verdict=(
                _relative_file(run_dir, reviewer_path)
                if reviewer_path.is_file() else None
            ),
            payload={
                "best_effort": True,
                "classification": classification,
                "selection_reason": reason,
                "publication": publication,
            },
        )
        v2_selection_recorded = True
    if not v2_selection_recorded:
        lifecycle_v2.transition(
            run_dir, "best_effort_completed",
            accepted_artifact=_relative_file(run_dir, artifact),
            validation_report=_relative_file(run_dir, validation["report"]),
            validation_receipt=_relative_file(run_dir, receipt_v2),
            reviewer_verdict=(
                _relative_file(run_dir, reviewer_path)
                if reviewer_path.is_file() else None
            ),
            payload={
                "best_effort": True,
                "classification": classification,
                "selection_reason": reason,
                "publication": publication,
            },
        )
    supervisor.transition(
        run_dir, "best_effort_completed",
        artifact=artifact,
        receipt=Path(validation["receipt"]),
        decision="best_effort",
        reason=reason,
    )
    workflow["status"] = "best_effort_completed"
    workflow["best_effort"] = {
        "eligible": True,
        "strict_passed": classification["strict_passed"],
        "reason": reason,
        "remaining_findings": unresolved,
        "classification": classification,
        "publication": publication,
    }
    workflow["findings"] = unresolved
    workflow["published_artifact"] = published_descriptor
    workflow["final_artifact"] = final_descriptor
    _complete_inflight_decision(
        run_dir, workflow, outcome="best_effort_completed",
    )
    workflow["checkpoint"] = None
    (Path(run_dir) / CHECKPOINT_FILE).unlink(missing_ok=True)
    write_workflow(run_dir, workflow)
    return host_result(run_dir, workflow)


def _resume_run_impl(reference, decision, feedback, workspace, cli, *, timeout=600):
    workspace = normalize_workspace(workspace)
    run_dir = resolve_run(reference, workspace)
    workflow = load_workflow(run_dir)
    replayed = lifecycle_v2.require_mutable(run_dir, workflow.get("run_id"))
    if not workflow.get("checkpoint") and replayed["latest_snapshots"].get("publication-transaction"):
        was_terminal = workflow.get("status") in {
            "completed", "approved_with_findings", "best_effort_completed",
        }
        recovered = lifecycle_v2.recover_publication(run_dir)
        if recovered and recovered["status"] == "committed":
            target = Path(workflow["target"])
            workflow["status"] = {
                "approve": "completed",
                "approve_with_findings": "approved_with_findings",
                "best_effort": "best_effort_completed",
            }[recovered["decision"]]
            workflow["published_artifact"] = {
                "path": str(target.resolve()), "sha256": supervisor.sha256_file(target),
            }
            recovered_candidate = (
                workflow.get("publishable_candidate")
                or workflow.get("best_effort_candidate")
                or {}
            )
            workflow["final_artifact"] = copy.deepcopy(
                recovered_candidate.get("artifact")
            )
            _complete_inflight_decision(
                run_dir, workflow, outcome=workflow["status"],
            )
            workflow.pop("pending_publication_decision", None)
            write_workflow(run_dir, workflow)
            result = host_result(run_dir, workflow)
            if was_terminal:
                result["status"] = "already_applied"
                result["decision_replayed"] = True
                result["publication_id"] = recovered["publication_id"]
            return result
    checkpoint_v2, checkpoint_v2_descriptor = lifecycle_v2.latest_document(
        run_dir, "checkpoint", replayed,
    )
    processed_v2_ids = {
        event_record["event"].get("payload", {}).get("decision_id")
        for event_record in replayed["events"]
        if event_record["event"].get("event_type") == "decision_processed"
    } | {
        event_record["event"].get("payload", {}).get("processed_decision_id")
        for event_record in replayed["events"]
        if event_record["event"].get("event_type") == "checkpoint_created"
    }
    recovered_committed_decision = None
    latest_decision_descriptor = replayed["latest_snapshots"].get("decision")
    if latest_decision_descriptor is not None:
        latest_decision, _ = lifecycle_v2.latest_document(
            run_dir, "decision", replayed,
        )
        if (
            latest_decision.get("checkpoint_id")
            == checkpoint_v2.get("checkpoint_id")
            and latest_decision.get("decision_id") not in processed_v2_ids
            and latest_decision.get("decision_id")
            not in set(workflow.get("processed_decision_ids", []))
        ):
            if decision != latest_decision.get("decision"):
                raise supervisor.SupervisorError(
                    "the checkpoint already has an unprocessed committed "
                    f"{latest_decision.get('decision')!r} decision"
                )
            committed_feedback = latest_decision.get("feedback") or ""
            incoming_feedback = feedback or ""
            if (
                incoming_feedback.strip()
                and incoming_feedback != committed_feedback
            ):
                raise supervisor.SupervisorError(
                    "the checkpoint already has an unprocessed committed "
                    "decision whose feedback conflicts with this resume request"
                )
            recovered_committed_decision = copy.deepcopy(latest_decision)
            # A short retry such as `/drawio:resume continue` recovers the
            # durable human input. A new non-empty clarification must never be
            # silently discarded in favor of older committed feedback.
            feedback = committed_feedback
    inflight = workflow.get("inflight_decision") or {}
    inflight_payload_matches = bool(
        inflight.get("decision") == decision
        and inflight.get("feedback", "") == feedback
    )
    decision_id = (
        recovered_committed_decision["decision_id"]
        if recovered_committed_decision is not None
        else inflight["decision_id"]
        if inflight_payload_matches and inflight.get("decision_id")
        else "decision-" + canonical_json_sha256({
            "checkpoint_sha256": checkpoint_v2_descriptor["canonical_sha256"],
            "decision": decision,
            "feedback": feedback,
        })[:24]
    )
    inflight_matches = bool(
        inflight.get("decision_id") == decision_id
        and inflight_payload_matches
    )
    processed_in_v2 = any(
        (
            event_record["event"].get("event_type") == "decision_processed"
            and event_record["event"].get("payload", {}).get("decision_id")
            == decision_id
        )
        or (
            event_record["event"].get("event_type") == "checkpoint_created"
            and event_record["event"].get("payload", {}).get(
                "processed_decision_id"
            ) == decision_id
        )
        for event_record in replayed["events"]
    )
    if inflight_matches and processed_in_v2:
        _complete_inflight_decision(
            run_dir, workflow, outcome="recovered_processed_decision",
            record_v2=False,
        )
        write_workflow(run_dir, workflow)
        result = host_result(run_dir, workflow)
        result["status"] = "already_applied"
        result["decision_id"] = decision_id
        result["decision_replayed"] = True
        return result
    checkpoint_value = workflow.get("checkpoint") or (
        copy.deepcopy(inflight.get("checkpoint")) if inflight_matches else None
    )
    if not checkpoint_value:
        committed = any(
            event["event"].get("event_type") == "decision_committed"
            and event["event"].get("payload", {}).get("decision_id") == decision_id
            for event in replayed["events"]
        )
        if committed and processed_in_v2:
            result = host_result(run_dir, workflow)
            result["status"] = "already_applied"
            result["decision_id"] = decision_id
            result["decision_replayed"] = True
            return result
        if committed and workflow.get("checkpoint_history"):
            legacy_descriptor = workflow["checkpoint_history"][-1]
            legacy_path = Path(legacy_descriptor.get("path", ""))
            if (
                legacy_path.is_file()
                and supervisor.sha256_file(legacy_path)
                == legacy_descriptor.get("sha256")
            ):
                checkpoint_value = {
                    **supervisor.load_json(legacy_path),
                    "path": str(legacy_path.resolve()),
                    "sha256": legacy_descriptor["sha256"],
                }
        if checkpoint_value is None:
            raise supervisor.SupervisorError(
                "committed decision has no recoverable checkpoint evidence"
                if committed else "run has no pending human checkpoint"
            )
    if decision not in checkpoint_value["allowed_decisions"]:
        raise supervisor.SupervisorError(f"decision {decision!r} is not allowed at {checkpoint_value['kind']}")
    checkpoint_path = Path(checkpoint_value["path"])
    if not checkpoint_path.is_file() or supervisor.sha256_file(checkpoint_path) != checkpoint_value["sha256"]:
        raise supervisor.SupervisorError("pending checkpoint evidence is missing or hash-mismatched")
    # Verify every immutable input to the human decision before appending the
    # idempotency event.  Otherwise a stale semantic plan could consume the
    # decision id and leave the still-pending checkpoint impossible to resume.
    approved_proposal = None
    if decision == "continue" and checkpoint_value["kind"] == "semantic_approval":
        approved_proposal = workflow.get("pending_semantic_approval")
        if not approved_proposal or checkpoint_value.get("evidence") != approved_proposal:
            raise supervisor.SupervisorError("semantic checkpoint is not bound to its proposed semantic plan")
        plan_path = Path(approved_proposal["semantic_plan"]["path"])
        if (
            not plan_path.is_file()
            or supervisor.sha256_file(plan_path) != approved_proposal["semantic_plan"]["sha256"]
            or canonical_hash(approved_proposal["semantic_changes"])
            != approved_proposal["semantic_changes_sha256"]
        ):
            raise supervisor.SupervisorError("semantic plan changed after the human checkpoint")
        approved_plan = supervisor.load_json(plan_path)
        require_valid_contract(approved_plan, "semantic-plan", 2)
        if (
            approved_plan["baseline_semantic_digest"] != approved_proposal["baseline_semantic_digest"]
            or approved_plan["source_bundle_sha256"] != approved_proposal["source_bundle_sha256"]
            or approved_plan["result"]["semantic_delta"] != approved_proposal["semantic_delta"]
            or semantic_delta_sha256(approved_plan["result"]["semantic_delta"])
            != approved_proposal["semantic_delta_sha256"]
        ):
            raise supervisor.SupervisorError("semantic checkpoint bindings differ from the approved v2 plan")
    current_state = supervisor.load_state(run_dir)["state"]
    checkpointed_create_recovery = current_state == "patching"
    if checkpointed_create_recovery:
        exact_checkpoint = inflight.get("checkpoint") == checkpoint_value
        if not (
            workflow.get("mode") == "create"
            and decision == "continue"
            and checkpoint_value["kind"] == "semantic_approval"
            and not workflow.get("accepted_artifact")
            and recovered_committed_decision is not None
            and inflight_matches
            and exact_checkpoint
            and workflow.get("semantic_authorized") is True
            and workflow.get("approved_semantic_change")
        ):
            raise supervisor.SupervisorError(
                "patching checkpoint retry does not match the exact inflight "
                "create decision"
            )
    planned_workflow = copy.deepcopy(workflow)
    if (
        decision == "continue"
        and checkpoint_value["kind"] != "publication_conflict"
        and not inflight_matches
    ):
        resume_payload = {
            "schema_version": 1, "run_id": workflow["run_id"], "mode": workflow["mode"],
            "phase": "resume", "request": workflow["request"], "feedback": feedback,
            "checkpoint": checkpoint_value, "accepted_artifact": workflow.get("accepted_artifact"),
            "iteration": workflow["iteration"], "previous_decision": workflow.get("supervisor_decision"),
        }
        try:
            resume_plan, _, _, _ = role_call(
                "supervisor", resume_payload, run_dir, workspace, cli, timeout,
                f"supervisor-resume-{len(workflow.get('decisions', [])) + 1}",
            )
            requested_additional = DEFAULT_MAX_ITERATIONS
            consume_supervisor_decision(
                planned_workflow, resume_plan, phase="resume",
                requested_max_iterations=requested_additional,
            )
        except supervisor.SupervisorError as exc:
            # Supervisor is advisory for an already materialized baseline.  A
            # verified checkpoint plus deterministic host policy can continue
            # repair/review without inventing semantic authorization.
            resume_plan = {
                "schema_version": 1,
                "role": "supervisor",
                "status": "ok",
                "result": {
                    "action": "repair",
                    "reason": "deterministic host resume fallback",
                    "required_roles": ["repair", "reviewer"],
                    "max_iterations": DEFAULT_MAX_ITERATIONS,
                },
            }
            consume_supervisor_decision(
                planned_workflow, resume_plan, phase="resume",
                requested_max_iterations=DEFAULT_MAX_ITERATIONS,
            )
            planned_workflow["supervisor_resume_degraded"] = {
                "reason": str(exc),
                "fallback": "deterministic_host_policy",
            }
            supervisor.append_event(
                run_dir, "state_transition",
                {
                    "phase": "resume_planning",
                    "degraded": True,
                    "reason": str(exc),
                    "fallback": "deterministic_host_policy",
                },
                actor={"kind": "system", "id": "diagram-orchestrator", "model": None},
            )
        planned_workflow["max_iterations"] = (
            workflow["iteration"] + planned_workflow["max_iterations"]
        )
    decision_v2, decision_v2_descriptor, decision_replayed = lifecycle_v2.commit_decision(
        run_dir, decision=decision, feedback=feedback, decision_id=decision_id,
    )
    processed_decisions = set(workflow.get("processed_decision_ids", []))
    if decision_replayed and (
        decision_id in processed_decisions or processed_in_v2
    ):
        result = host_result(run_dir, workflow)
        result["status"] = "already_applied"
        result["decision_id"] = decision_id
        result["decision_replayed"] = True
        return result
    workflow = planned_workflow
    if recovered_committed_decision is not None:
        workflow["recovered_committed_decision"] = {
            "decision_id": recovered_committed_decision["decision_id"],
            "decision": recovered_committed_decision["decision"],
            "feedback": recovered_committed_decision.get("feedback"),
            "decided_at": recovered_committed_decision["decided_at"],
        }
    workflow["inflight_decision"] = {
        "decision_id": decision_id,
        "decision": decision,
        "feedback": feedback,
        "checkpoint": copy.deepcopy(checkpoint_value),
        "committed_at": decision_v2["decided_at"],
    }
    write_workflow(run_dir, workflow)
    semantic_approval_v2 = None
    semantic_approval_v2_path = None
    if checkpoint_value["kind"] == "semantic_approval" and decision == "continue":
        semantic_approval_v2, semantic_approval_v2_path = lifecycle_v2.create_semantic_approval_from_decision(
            run_dir, decision=decision_v2,
        )
        workflow["approved_semantic_change"] = {
            **approved_proposal,
            "semantic_approval_v2": {
                "path": str(semantic_approval_v2_path.resolve()),
                "sha256": supervisor.sha256_file(semantic_approval_v2_path),
                "content": semantic_approval_v2,
            },
        }
        workflow["semantic_authorized"] = True
        write_workflow(run_dir, workflow)
    existing_decision = next((
        item for item in workflow.get("decisions", [])
        if item.get("decision_id") == decision_id
    ), None)
    decision_path = (
        Path(existing_decision["path"])
        if existing_decision
        else run_dir / "decisions" / f"{len(workflow.get('decisions', [])) + 1:03d}.json"
    )
    decision_value = {
        "schema_version": 1, "decision_id": decision_id,
        "decision": decision, "feedback": feedback,
        "checkpoint_kind": checkpoint_value["kind"],
        "checkpoint": {"path": str(checkpoint_path.resolve()), "sha256": checkpoint_value["sha256"]},
        "decided_at": decision_v2["decided_at"],
    }
    if approved_proposal is not None:
        decision_value["approved_semantic_change"] = approved_proposal
        decision_value["semantic_approval_v2"] = {
            "path": str(semantic_approval_v2_path.resolve()),
            "sha256": supervisor.sha256_file(semantic_approval_v2_path),
            "content": semantic_approval_v2,
        }
    if decision_path.is_file():
        if supervisor.load_json(decision_path) != decision_value:
            raise supervisor.SupervisorError(
                "existing decision artifact differs from the committed decision"
            )
    else:
        supervisor.write_json(decision_path, decision_value)
    if not any(
        item.get("decision_id") == decision_id
        for item in workflow.get("decisions", [])
    ):
        workflow.setdefault("decisions", []).append({"path": str(decision_path), "sha256": supervisor.sha256_file(decision_path), **decision_value})
    decision_event_exists = False
    manifest_path = run_dir / "run-manifest.jsonl"
    if manifest_path.is_file():
        decision_event_exists = any(
            event.get("event_type") == "user_decision"
            and event.get("payload", {}).get("decision_id") == decision_id
            for event in (
                json.loads(line)
                for line in manifest_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
        )
    if not decision_event_exists:
        supervisor.append_event(
            run_dir, "user_decision", {"decision_id": decision_id, "decision": decision, "feedback_sha256": hashlib.sha256(feedback.encode("utf-8")).hexdigest(), "decision_path": str(decision_path), "decision_sha256": supervisor.sha256_file(decision_path)},
            actor={"kind": "human", "id": "user", "model": None},
        )
    write_workflow(run_dir, workflow)
    state = supervisor.load_state(run_dir)
    if decision == "stop":
        supervisor.transition(run_dir, "stopped", decision="stop", reason=feedback or "user stopped iterations")
        workflow["status"] = "stopped"
        lifecycle_v2.transition(
            run_dir, "stopped",
            payload={"decision": "stop", "feedback": feedback or None},
        )
        _complete_inflight_decision(run_dir, workflow, outcome="stopped")
        workflow["checkpoint"] = None
        (run_dir / CHECKPOINT_FILE).unlink(missing_ok=True)
        write_workflow(run_dir, workflow)
        return host_result(run_dir, workflow)
    if decision == "manual_handoff":
        supervisor.transition(run_dir, "manual_handoff", decision="manual_handoff", reason=feedback or "user requested manual handoff")
        workflow["status"] = "manual_handoff"
        lifecycle_v2.transition(
            run_dir, "manual_handoff",
            payload={
                "decision": "manual_handoff", "feedback": feedback or None,
            },
        )
        _complete_inflight_decision(
            run_dir, workflow, outcome="manual_handoff",
        )
        workflow["checkpoint"] = None
        (run_dir / CHECKPOINT_FILE).unlink(missing_ok=True)
        write_workflow(run_dir, workflow)
        return host_result(run_dir, workflow)
    if decision == "pause":
        if state["state"] != "awaiting_feedback":
            supervisor.transition(run_dir, "awaiting_feedback", decision="pause", reason=feedback or "user paused")
        workflow["status"] = "awaiting_human"
        write_workflow(run_dir, workflow)
        return checkpoint(run_dir, workflow, checkpoint_value["kind"], "Run remains paused.", checkpoint_value["findings"], checkpoint_value["allowed_decisions"])
    if decision == "continue" and checkpoint_value["kind"] == "publication_conflict":
        publication_decision = workflow.get("pending_publication_decision")
        if publication_decision not in {"approve", "approve_with_findings"}:
            raise supervisor.SupervisorError("publication conflict lost the original final decision")
        try:
            target = publish(run_dir, workflow, publication_decision)
        except ContractError as exc:
            if exc.code != "publication.conflict":
                raise
            workflow["findings"] = [str(exc)]
            return checkpoint(
                run_dir, workflow, "publication_conflict",
                "Целевой файл всё ещё конфликтует с исходным состоянием; перезапись не выполнялась.",
                workflow["findings"], ["continue", "pause", "stop", "manual_handoff"],
                evidence={"code": exc.code, "target": str(Path(workflow["target"]).resolve())},
            )
        final_status = "completed" if publication_decision == "approve" else "approved_with_findings"
        publishable = workflow["publishable_candidate"]
        supervisor.transition(
            run_dir, final_status,
            artifact=Path(publishable["artifact"]["path"]),
            receipt=publishable["validation"]["receipt"],
            decision=publication_decision,
            reason=feedback or "publication conflict resolved",
        )
        workflow["status"] = final_status
        workflow["published_artifact"] = {"path": str(target), "sha256": supervisor.sha256_file(target)}
        workflow["final_artifact"] = copy.deepcopy(
            workflow["publishable_candidate"]["artifact"]
        )
        workflow.pop("pending_publication_decision", None)
        _complete_inflight_decision(
            run_dir, workflow, outcome=final_status,
        )
        workflow["checkpoint"] = None
        (run_dir / CHECKPOINT_FILE).unlink(missing_ok=True)
        write_workflow(run_dir, workflow)
        return host_result(run_dir, workflow)
    if decision in {"approve", "approve_with_findings"}:
        if checkpoint_value["kind"] != "final_acceptance" and decision == "approve":
            raise supervisor.SupervisorError("approve is only valid at final acceptance")
        try:
            target = publish(run_dir, workflow, decision)
        except ContractError as exc:
            if exc.code != "publication.conflict":
                raise
            workflow["status"] = "awaiting_human"
            workflow["findings"] = [str(exc)]
            workflow["pending_publication_decision"] = decision
            write_workflow(run_dir, workflow)
            checkpoint(
                run_dir, workflow, "publication_conflict",
                "Целевой файл изменился после начала запуска; он не был перезаписан. "
                "Устраните конфликт и продолжите либо завершите работу вручную.",
                workflow["findings"],
                ["continue", "pause", "stop", "manual_handoff"],
                evidence={"code": exc.code, "target": str(Path(workflow["target"]).resolve())},
            )
            raise supervisor.SupervisorError(
                "create target appeared after start and will not be overwritten"
                if workflow["mode"] == "create"
                else "improve target changed after start and will not be overwritten"
            ) from exc
        if decision == "approve":
            publishable = workflow["publishable_candidate"]
            supervisor.transition(run_dir, "completed", artifact=Path(publishable["artifact"]["path"]), receipt=publishable["validation"]["receipt"], decision="approve", reason=feedback)
            workflow["status"] = "completed"
        else:
            publishable = workflow["publishable_candidate"]
            supervisor.transition(
                run_dir, "approved_with_findings",
                artifact=Path(publishable["artifact"]["path"]),
                receipt=publishable["validation"]["receipt"],
                decision="approve_with_findings", reason=feedback,
            )
            workflow["status"] = "approved_with_findings"
        _complete_inflight_decision(
            run_dir, workflow, outcome=workflow["status"],
        )
        workflow["checkpoint"] = None
        (run_dir / CHECKPOINT_FILE).unlink(missing_ok=True)
        workflow["published_artifact"] = {"path": str(target), "sha256": supervisor.sha256_file(target)}
        workflow["final_artifact"] = copy.deepcopy(
            workflow["publishable_candidate"]["artifact"]
        )
        write_workflow(run_dir, workflow)
        return host_result(run_dir, workflow)
    if feedback and not checkpointed_create_recovery:
        workflow["request"] += "\n\nUser feedback: " + feedback
        write_workflow(run_dir, workflow)
        reconciliation_result = _reconcile_feedback(
            run_dir, workflow, feedback, decision_id, workspace, cli, timeout,
            approved_proposal=approved_proposal,
        )
        if reconciliation_result is not None:
            _complete_inflight_decision(
                run_dir, workflow,
                outcome=workflow.get("status", "reconciled"),
            )
            write_workflow(run_dir, workflow)
            return reconciliation_result
    if approved_proposal is not None:
        workflow["approved_semantic_change"] = {
            **approved_proposal,
            "decision": {"path": str(decision_path.resolve()), "sha256": supervisor.sha256_file(decision_path)},
            "semantic_approval_v2": decision_value["semantic_approval_v2"],
        }
    workflow["semantic_authorized"] = approved_proposal is not None or workflow.get("semantic_authorized", False)
    workflow["status"] = "running"
    write_workflow(run_dir, workflow)
    if (
        decision == "continue"
        and checkpoint_value["kind"] == "semantic_approval"
        and workflow["mode"] == "create"
        and not workflow.get("accepted_artifact")
    ):
        if approved_proposal is None:
            raise supervisor.SupervisorError(
                "checkpointed create has no hash-bound semantic approval"
            )
        legacy_descriptor = workflow.get("semantic_plan") or {}
        legacy_path = Path(legacy_descriptor.get("path", ""))
        approved_path = Path(approved_proposal["semantic_plan"]["path"])
        if (
            not legacy_path.is_file()
            or supervisor.sha256_file(legacy_path) != legacy_descriptor.get("sha256")
            or not approved_path.is_file()
            or supervisor.sha256_file(approved_path)
            != approved_proposal["semantic_plan"]["sha256"]
        ):
            raise supervisor.SupervisorError(
                "checkpointed create semantic plan evidence is missing or changed"
            )
        result = _finish_checkpointed_create(
            run_dir,
            workflow,
            supervisor.load_json(legacy_path),
            supervisor.load_json(approved_path),
            cli,
            timeout,
            recovering_patching=checkpointed_create_recovery,
        )
        _complete_inflight_decision(
            run_dir, workflow,
            outcome=workflow.get("status", "checkpointed_create"),
        )
        write_workflow(run_dir, workflow)
        return result
    current = supervisor.load_state(run_dir)["state"]
    accepted = Path(workflow["accepted_artifact"]["path"])
    if current == "awaiting_feedback":
        supervisor.transition(run_dir, "patching", artifact=accepted, decision="continue", reason=feedback or "continue requested")
    elif current in {"awaiting_decision", "final_review"}:
        supervisor.transition(run_dir, "patching", artifact=accepted, decision="continue", reason=feedback or "continue requested")
    else:
        raise supervisor.SupervisorError(f"cannot continue run from state {current}")
    supervisor.append_event(run_dir, "run_resumed", {"decision": "continue", "iteration": workflow["iteration"]})
    return repair_loop(run_dir, workflow, cli, timeout, already_patching=True)


def resume_run(reference, decision, feedback, workspace, cli, *, timeout=600):
    normalized_workspace = normalize_workspace(workspace)
    run_dir = resolve_run(reference, normalized_workspace)
    workflow = load_workflow(run_dir)
    lifecycle_v2.require_mutable(run_dir, workflow.get("run_id"))
    try:
        with RunLock(
            workspace=normalized_workspace, run_dir=run_dir,
            run_id=workflow.get("run_id", run_dir.name),
        ) as run_lock:
            lifecycle_v2.record_lock_recovery(run_dir, run_lock.recovery_records)
            return _resume_run_impl(
                str(run_dir), decision, feedback, normalized_workspace, cli,
                timeout=timeout,
            )
    except RunAlreadyLocked as exc:
        return {**exc.as_result(), "run_dir": str(run_dir)}


def trace_run(reference, workspace):
    workspace = normalize_workspace(workspace)
    run_dir = resolve_run(reference, workspace)
    workflow = load_workflow(run_dir)
    state_path = run_dir / "state.json"
    manifest = run_dir / "run-manifest.jsonl"
    checks, events, previous = [], [], None
    schema = supervisor.load_json(ROOT / "data" / "run-event.v1.schema.json")
    for index, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            checks.append({
                "sequence": index, "schema_valid": False, "chain_valid": False,
                "error": f"invalid JSONL event: {exc}",
            })
            previous = None
            continue
        errors = list(jsonschema.Draft202012Validator(schema).iter_errors(event))
        chain_ok = event.get("sequence") == index and event.get("previous_event_sha256") == previous
        checks.append({"sequence": index, "schema_valid": not errors, "chain_valid": chain_ok, "error": errors[0].message if errors else None})
        previous = hashlib.sha256(line.encode("utf-8")).hexdigest()
        events.append(event)
    artifact_checks = []
    role_checks = []
    failed_roles = []
    recovered_failures_by_role = {}
    started_by_role = {}
    policy = supervisor.load_json(agent_runtime.DEFAULT_POLICY)
    expected_isolation_controls = agent_runtime.role_isolation_controls()
    for event in events:
        if event["event_type"] == "role_started":
            path = Path(event["payload"]["input"])
            input_valid = path.is_file() and supervisor.sha256_file(path) == event["payload"]["input_sha256"]
            artifact_checks.append({"kind": "role_input", "path": str(path), "valid": input_valid})
            started_by_role.setdefault(event["payload"]["role"], []).append(event)
        if event["event_type"] == "role_finished":
            payload = event["payload"]
            role = payload.get("role")
            output_path = Path(payload.get("output", ""))
            capture_path = Path(payload.get("runtime_capture", ""))
            stderr_path = Path(payload.get("stderr_capture", ""))
            output_valid = output_path.is_file() and supervisor.sha256_file(output_path) == payload.get("output_sha256")
            capture_valid = capture_path.is_file() and supervisor.sha256_file(capture_path) == payload.get("runtime_capture_sha256")
            stderr_valid = (
                stderr_path.is_file()
                and supervisor.sha256_file(stderr_path) == payload.get("stderr_capture_sha256")
            ) if payload.get("stderr_capture") else True
            artifact_checks.append({"kind": "role_output", "path": str(output_path), "valid": output_valid})
            artifact_checks.append({"kind": "runtime_capture", "path": str(capture_path), "valid": capture_valid})
            if payload.get("stderr_capture"):
                artifact_checks.append({"kind": "runtime_stderr", "path": str(stderr_path), "valid": stderr_valid})
            start = started_by_role.get(role, []).pop(0) if started_by_role.get(role) else None
            proof_valid = False
            diagnostic = None
            binding_proof = None
            binding_proof_valid = False
            try:
                if not start or not output_valid or not capture_valid or role not in policy["roles"]:
                    raise supervisor.SupervisorError("role evidence set is incomplete")
                parsed, metadata = agent_runtime.parse_runtime_output(role, capture_path.read_text(encoding="utf-8"))
                role_input = supervisor.load_json(start["payload"]["input"])
                parsed = agent_runtime.validate_role_output(role, parsed, role_input)
                parsed, binding_proof = agent_runtime.finalize_role_output(
                    role, role_input, parsed
                )
                if parsed != supervisor.load_json(output_path):
                    raise supervisor.SupervisorError("normalized role output differs from runtime capture")
                expected_model = policy["roles"][role]["requested_model"]
                resolved_model = payload.get("resolved_model")
                configured_fallbacks = {
                    item["model"]
                    for item in policy["roles"][role].get("runtime_fallbacks", [])
                }
                configured_fallback_failures = {
                    failure_kind
                    for item in policy["roles"][role].get("runtime_fallbacks", [])
                    if item.get("model") == resolved_model
                    for failure_kind in item.get("on_failure", [])
                }
                fallback_used = bool(payload.get("fallback_used"))
                approved_fallback = (
                    role in {"supervisor", "repair"}
                    and fallback_used
                    and resolved_model in configured_fallbacks
                    and any(
                        failure.get("terminal") is False
                        and failure.get("attempted_model") == expected_model
                        and failure.get("fallback_model") == resolved_model
                        and failure.get("failure_kind")
                        in configured_fallback_failures
                        and failure.get("original_input_sha256")
                        == start["payload"].get("input_sha256")
                        and failure.get("isolation_proof", {}).get("system_models")
                        == [expected_model]
                        and failure.get("isolation_proof", {}).get("assistant_models")
                        in ([], [expected_model])
                        for failure in recovered_failures_by_role.get(role, [])
                    )
                )
                exact_primary = not fallback_used and resolved_model == expected_model
                expected_proof = metadata.get("model_proof")
                expected_isolation = metadata.get("isolation_proof")
                recorded_isolation = payload.get("isolation_proof", expected_isolation)
                recorded_binding_proof = payload.get("binding_proof")
                binding_proof_valid = (
                    recorded_binding_proof is None
                    or recorded_binding_proof == binding_proof
                )
                proof_valid = all((
                    metadata.get("reported_model") == resolved_model,
                    bool(expected_proof and expected_proof.get("verified")),
                    payload.get("requested_model") == expected_model,
                    exact_primary or approved_fallback,
                    payload.get("resolution_mode") == "isolated_cli",
                    payload.get("fallback_used") is (not exact_primary),
                    payload.get("model_proof") == expected_proof,
                    event.get("actor", {}).get("model") == resolved_model,
                    start["payload"].get("requested_model") == expected_model,
                    start["payload"].get("attempted_model", expected_model)
                    == resolved_model,
                    start["payload"].get("fallback_used") is (not exact_primary),
                    start["payload"].get("isolation_controls")
                    == expected_isolation_controls,
                    payload.get("isolation_controls")
                    == expected_isolation_controls,
                    bool(expected_isolation and expected_isolation.get("verified")),
                    recorded_isolation == expected_isolation,
                    binding_proof_valid,
                    stderr_valid,
                ))
                if not proof_valid:
                    raise supervisor.SupervisorError("role model proof does not match the trusted routing policy and raw runtime capture")
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, jsonschema.ValidationError, supervisor.SupervisorError) as exc:
                diagnostic = str(exc)
                proof_valid = False
            role_checks.append({
                "role": role, "valid": proof_valid,
                "requested_model": payload.get("requested_model"),
                "resolved_model": payload.get("resolved_model"),
                "fallback_used": payload.get("fallback_used"),
                "isolation_controls": payload.get("isolation_controls"),
                "isolation_proof": payload.get("isolation_proof"),
                "binding_proof": binding_proof,
                "binding_proof_valid": binding_proof_valid,
                "diagnostic": diagnostic,
            })
        if event["event_type"] == "role_failed":
            role = event["payload"].get("role")
            if started_by_role.get(role):
                started_by_role[role].pop(0)
            payload = event["payload"]
            capture_path = Path(payload.get("runtime_capture", ""))
            stderr_path = Path(payload.get("stderr_capture", ""))
            capture_valid = (
                capture_path.is_file()
                and supervisor.sha256_file(capture_path)
                == payload.get("runtime_capture_sha256")
            )
            stderr_valid = (
                stderr_path.is_file()
                and supervisor.sha256_file(stderr_path)
                == payload.get("stderr_capture_sha256")
            )
            if payload.get("runtime_capture"):
                artifact_checks.append({
                    "kind": "failed_role_runtime_capture",
                    "path": str(capture_path),
                    "valid": capture_valid,
                })
            if payload.get("stderr_capture"):
                artifact_checks.append({
                    "kind": "failed_role_runtime_stderr",
                    "path": str(stderr_path),
                    "valid": stderr_valid,
                })
            computed_isolation = (
                agent_runtime.inspect_runtime_isolation(
                    role, capture_path.read_text(encoding="utf-8")
                )
                if capture_valid else None
            )
            recorded_isolation = payload.get("isolation_proof")
            controls_valid = (
                payload.get("isolation_controls") == expected_isolation_controls
            )
            failed_roles.append({
                "role": role,
                "sequence": event.get("sequence"),
                "phase": payload.get("phase"),
                "failure_kind": payload.get("failure_kind"),
                "terminal": payload.get("terminal", True),
                "attempted_model": payload.get("attempted_model"),
                "fallback_model": payload.get("fallback_model"),
                "attempt_id": payload.get("attempt_id"),
                "output_format": payload.get("output_format"),
                "original_input_sha256": payload.get("original_input_sha256"),
                "exit_code": payload.get("exit_code"),
                "diagnostic": payload.get("diagnostic"),
                "runtime_capture": str(capture_path) if payload.get("runtime_capture") else None,
                "runtime_capture_valid": capture_valid,
                "stderr_capture": str(stderr_path) if payload.get("stderr_capture") else None,
                "stderr_capture_valid": stderr_valid,
                "isolation_proof": computed_isolation or recorded_isolation,
                "isolation_controls": payload.get("isolation_controls"),
                "isolation_controls_valid": controls_valid,
                "isolation_evidence_valid": bool(
                    computed_isolation
                    and recorded_isolation == computed_isolation
                ),
                "evidence_valid": (
                    payload.get("phase") == "capability_detection"
                    or bool(
                        capture_valid
                        and stderr_valid
                        and controls_valid
                        and computed_isolation
                        and recorded_isolation == computed_isolation
                    )
                ),
            })
            recovered_failures_by_role.setdefault(role, []).append(payload)
        if event["event_type"] == "validation_receipt" and event["payload"].get("receipt"):
            path = Path(event["payload"]["receipt"])
            verification = supervisor.verify_receipt(path) if path.is_file() else {"valid": False}
            artifact_checks.append({"path": str(path), "valid": verification["valid"]})
        path_bindings = {
            "run_created": ("request", "request_sha256"),
            "checkpoint_created": ("checkpoint", "checkpoint_sha256"),
            "user_decision": ("decision_path", "decision_sha256"),
            "artifact_published": ("artifact", "artifact_sha256"),
        }
        binding = path_bindings.get(event["event_type"])
        if binding and event["payload"].get(binding[0]) and event["payload"].get(binding[1]):
            path = Path(event["payload"][binding[0]])
            bound_valid = path.is_file() and supervisor.sha256_file(path) == event["payload"][binding[1]]
            if bound_valid and event["event_type"] == "user_decision":
                decision_value = supervisor.load_json(path)
                checkpoint_ref = decision_value.get("checkpoint", {})
                checkpoint_path = Path(checkpoint_ref.get("path", ""))
                bound_valid = (
                    checkpoint_path.is_file()
                    and supervisor.sha256_file(checkpoint_path) == checkpoint_ref.get("sha256")
                )
                approved = decision_value.get("approved_semantic_change")
                if bound_valid and approved:
                    plan_path = Path(approved["semantic_plan"]["path"])
                    bound_valid = (
                        plan_path.is_file()
                        and supervisor.sha256_file(plan_path) == approved["semantic_plan"]["sha256"]
                        and canonical_hash(approved["semantic_changes"]) == approved["semantic_changes_sha256"]
                    )
            artifact_checks.append({"kind": event["event_type"], "path": str(path), "valid": bound_valid})
    accepted = workflow.get("accepted_artifact") or {}
    accepted_path = Path(accepted.get("path", ""))
    accepted_valid = accepted_path.is_file() and supervisor.sha256_file(accepted_path) == accepted.get("sha256")
    accepted_validation = workflow.get("accepted_validation") or {}
    accepted_receipt_path = Path(accepted_validation.get("receipt", ""))
    accepted_receipt_valid = False
    if accepted_valid and accepted_receipt_path.is_file():
        receipt_verification = supervisor.verify_receipt(accepted_receipt_path, accepted_path)
        accepted_receipt_valid = (
            receipt_verification["valid"]
            and supervisor.sha256_file(accepted_receipt_path) == accepted_validation.get("receipt_sha256")
        )
    preflight = supervisor.verify_host_preflight(run_dir)
    v2_trace = {
        "present": lifecycle_v2.manifest_path(run_dir).is_file(),
        "status": "legacy_read_only",
        "valid": True,
        "event_count": 0,
        "diagnostics": [],
        "accepted_artifact_valid": None,
        "accepted_receipt_valid": None,
        "publication_valid": None,
        "implementation_changed": None,
        "implementation_diagnostics": [],
    }
    if v2_trace["present"]:
        replayed_v2 = lifecycle_v2.replay(run_dir, workflow.get("run_id"))
        v2_trace.update({
            "status": replayed_v2["status"],
            "valid": replayed_v2["valid"],
            "event_count": replayed_v2["event_count"],
            "latest_event_sha256": replayed_v2["latest_event_sha256"],
            "diagnostics": replayed_v2["diagnostics"],
            "latest_snapshots": {
                kind: {
                    "path": descriptor["path"],
                    "canonical_sha256": descriptor["canonical_sha256"],
                }
                for kind, descriptor in replayed_v2["latest_snapshots"].items()
            },
        })
        if replayed_v2["valid"]:
            state_v2, _ = lifecycle_v2.latest_document(run_dir, "run-state", replayed_v2)
            accepted_v2 = state_v2.get("accepted_artifact")
            receipt_v2 = state_v2.get("validation_receipt")
            accepted_v2_valid = False
            receipt_v2_valid = False
            if accepted_v2:
                try:
                    accepted_v2_path = Path(run_dir) / accepted_v2["path"]
                    accepted_v2_valid = (
                        accepted_v2_path.is_file()
                        and supervisor.sha256_file(accepted_v2_path) == accepted_v2["sha256"]
                        and accepted_v2_path.stat().st_size == accepted_v2["byte_length"]
                    )
                except OSError:
                    accepted_v2_valid = False
            if receipt_v2:
                try:
                    receipt_v2_path = Path(run_dir) / receipt_v2["path"]
                    receipt_v2_valid = (
                        receipt_v2_path.is_file()
                        and supervisor.sha256_file(receipt_v2_path) == receipt_v2["sha256"]
                        and lifecycle_v2.verify_v2_receipt(run_dir, receipt_v2_path)["valid"]
                    )
                except (OSError, ContractError, KeyError, json.JSONDecodeError):
                    receipt_v2_valid = False
            implementation_v2, _ = lifecycle_v2.latest_document(
                run_dir, "implementation-snapshot", replayed_v2,
            )
            implementation_diagnostics = verify_implementation_snapshot(
                implementation_v2, extension_root=ROOT,
            )
            publication_valid = None
            publication_descriptor = replayed_v2["latest_snapshots"].get("publication-transaction")
            if publication_descriptor:
                publication, _ = lifecycle_v2.latest_document(
                    run_dir, "publication-transaction", replayed_v2,
                )
                target_v2 = Path(workflow["workspace"]) / publication["target_path"]
                publication_valid = (
                    publication["status"] != "committed"
                    or (
                        target_v2.is_file()
                        and supervisor.sha256_file(target_v2) == publication["published_sha256"]
                    )
                )
            v2_trace.update({
                "accepted_artifact_valid": accepted_v2_valid,
                "accepted_receipt_valid": receipt_v2_valid,
                "publication_valid": publication_valid,
                "implementation_changed": bool(implementation_diagnostics),
                "implementation_diagnostics": implementation_diagnostics,
            })
            requires_accepted_evidence = state_v2.get("status") not in {
                "initialized", "analyzing", "failed", "stopped", "manual_handoff",
            }
            v2_trace["valid"] = bool(
                (not requires_accepted_evidence or (accepted_v2_valid and receipt_v2_valid))
                and publication_valid is not False
            )
            if not v2_trace["valid"] and v2_trace["status"] == "verified":
                v2_trace["status"] = "tampered_or_incomplete"
    integrity_valid = (
        bool(events)
        and all(item["schema_valid"] and item["chain_valid"] for item in checks)
        and all(item["valid"] for item in artifact_checks)
        and all(item["valid"] for item in role_checks)
        and all(item["evidence_valid"] for item in failed_roles)
        and preflight["valid"]
        and v2_trace["valid"]
    )
    latest_success_by_role = {}
    for event in events:
        if event["event_type"] == "role_finished":
            role = event.get("payload", {}).get("role")
            if role:
                latest_success_by_role[role] = max(
                    int(event.get("sequence", 0)),
                    latest_success_by_role.get(role, 0),
                )
    for failure in failed_roles:
        failure["recovered_by_later_success"] = bool(
            failure.get("terminal", True)
            and latest_success_by_role.get(failure.get("role"), 0)
            > int(failure.get("sequence") or 0)
        )
    best_effort_verified = bool(
        workflow.get("status") == "best_effort_completed"
        and (workflow.get("best_effort") or {}).get("eligible")
        and (workflow.get("best_effort_candidate") or {}).get("safe")
    )
    for failure in failed_roles:
        failure["accepted_by_best_effort_policy"] = bool(
            best_effort_verified
            and failure.get("role")
            in {"supervisor", "semantic_analyst", "repair", "reviewer"}
        )
    terminal_failed_roles = [
        item for item in failed_roles
        if item.get("terminal", True)
        and not item.get("recovered_by_later_success")
        and not item.get("accepted_by_best_effort_policy")
    ]
    valid = (
        integrity_valid
        and not terminal_failed_roles
        and accepted_valid
        and accepted_receipt_valid
    )
    state = supervisor.load_json(state_path) if state_path.is_file() else None
    roles = [
        {key: event["payload"].get(key) for key in ("role", "requested_model", "resolved_model", "resolution_mode", "fallback_used", "model_proof", "isolation_controls", "isolation_proof", "binding_proof", "output_sha256")}
        for event in events if event["event_type"] == "role_finished"
    ]
    status = (
        "verified_best_effort" if valid and best_effort_verified
        else "verified" if valid
        else "failed_verified" if integrity_valid and terminal_failed_roles
        else "tampered_or_incomplete"
    )
    result = {
        "schema_version": 1, "status": status,
        "valid": valid, "run_id": workflow["run_id"], "run_dir": str(run_dir),
        "state": (state or {}).get("state"),
        "event_count": len(events), "event_checks": checks, "artifact_checks": artifact_checks,
        "accepted_artifact_valid": accepted_valid, "accepted_receipt_valid": accepted_receipt_valid,
        "host_preflight": preflight, "role_checks": role_checks,
        "failed_roles": failed_roles, "integrity_valid": integrity_valid,
        "terminal_failed_roles": terminal_failed_roles,
        "model_diversity_degraded": any(
            bool(role.get("fallback_used")) for role in roles
        ),
        "roles": roles, "terminal_result": workflow.get("status"),
        "strict_passed": bool(
            (workflow.get("accepted_validation") or {}).get("strict_passed")
        ),
        "best_effort": copy.deepcopy(workflow.get("best_effort")),
        "role_policy": role_policy_evidence(workflow),
        "trust_scope": "local runtime capture and configured routing policy; no external cryptographic attestation",
        "control_plane_v2": v2_trace,
    }
    return result


def main():
    parser = argparse.ArgumentParser(description="Deterministic multi-model Draw.io orchestration host")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("create", "improve"):
        cmd = sub.add_parser(name)
        cmd.add_argument("input", nargs="*")
        cmd.add_argument("--diagram")
        cmd.add_argument("--request")
        cmd.add_argument("--workspace", default=str(Path.cwd()))
        cmd.add_argument("--cli", default=str(Path.home() / ".gigacode/bin/gigacode"))
        cmd.add_argument("--run-id")
        cmd.add_argument("--intake-id")
        cmd.add_argument("--intake-answer", action="append", default=[])
        cmd.add_argument("--accept-intake-assumptions", action="store_true")
        cmd.add_argument("--timeout", type=int, default=600)
        cmd.add_argument("--max-iterations", type=int, default=DEFAULT_MAX_ITERATIONS)
        if name == "create":
            cmd.add_argument("--renderer-source")
    resume = sub.add_parser("resume")
    resume.add_argument("input", nargs="*")
    resume.add_argument("--run")
    resume.add_argument("--decision", choices=command_ux.DECISIONS)
    resume.add_argument("--feedback")
    resume.add_argument("--workspace", default=str(Path.cwd()))
    resume.add_argument("--cli", default=str(Path.home() / ".gigacode/bin/gigacode"))
    resume.add_argument("--timeout", type=int, default=600)
    trace = sub.add_parser("trace")
    trace.add_argument("run_positional", nargs="?")
    trace.add_argument("--run")
    trace.add_argument("--workspace", default=str(Path.cwd()))
    try:
        args = parser.parse_args(command_ux.argv_with_qwen_command_args())
        if args.command in {"create", "improve"}:
            intake_replay = None
            if args.intake_id and not args.input and args.request is None:
                intake_replay = load_intake_request(
                    args.workspace, args.intake_id, mode=args.command
                )
                args.request = intake_replay["request"]
                if args.diagram is None:
                    args.diagram = intake_replay["diagram"]
            diagram, request = command_ux.split_diagram_request(
                args.input, diagram=args.diagram, request=args.request,
                request_required=args.command == "create",
            )
            request_was_supplied = request is not None
            if args.command == "create":
                if diagram:
                    resolved_diagram, selection = command_ux.select_diagram(args.workspace, diagram)
                else:
                    resolved_diagram, selection = command_ux.generated_target(args.workspace, request)
                handoff = None
                explicit_documents = []
                if args.renderer_source:
                    renderer_source_path, renderer_document = command_ux.explicit_renderer_document(
                        args.workspace, args.renderer_source,
                    )
                    explicit_documents.append(renderer_document)
            else:
                resolved_diagram, selection, request, handoff = command_ux.resolve_improve_inputs(
                    args.workspace, diagram=diagram, request=request,
                )
                explicit_documents = []
            if intake_replay is not None:
                request_source = "intake_replay"
            elif args.request is not None:
                request_source = "explicit_flag"
            elif request_was_supplied:
                request_source = "conversational_text"
            elif args.command == "improve":
                request_source = "default_review_findings_request"
            else:
                request_source = "conversational_text"
            resolution = {
                "workspace": str(command_ux.workspace_path(args.workspace)),
                "diagram": str(resolved_diagram), "diagram_selection": selection,
                "request": request, "request_source": request_source,
            }
            if handoff:
                resolution["review_handoff"] = handoff
            if explicit_documents:
                resolution["renderer_source"] = str(renderer_source_path)
            intake_answers = command_ux.parse_intake_answers(args.intake_answer)
            intake_state, completed_intake_path, intake_evidence = run_preflight_intake(
                mode=args.command,
                diagram=resolved_diagram,
                request=request,
                workspace=args.workspace,
                cli=args.cli,
                intake_id=args.intake_id,
                answers=intake_answers,
                accept_assumptions=args.accept_intake_assumptions,
                timeout=args.timeout,
            )
            if intake_state["status"] != "complete":
                question = intake_state["questions"][0]
                result = command_ux.intake_awaiting_input(
                    intake_id=intake_state["intake_id"],
                    question=question,
                    command=args.command,
                )
                result.update({
                    "mode": args.command,
                    "classification": intake_state["classification"],
                    "questions": intake_state["questions"],
                    "answers": intake_state["answers"],
                    "assumptions": intake_state["assumptions"],
                    "completeness": intake_state["completeness"],
                    "intake_evidence": intake_evidence,
                    "command_resolution": resolution,
                })
            else:
                result = start_run(
                    args.command, resolved_diagram, request, args.workspace, args.cli,
                    run_id=args.run_id, timeout=args.timeout,
                    max_iterations=args.max_iterations,
                    review_handoff=handoff,
                    explicit_documents=explicit_documents,
                    intake_path=completed_intake_path,
                )
                resolution["intake_id"] = intake_state["intake_id"]
                resolution["intake"] = str(completed_intake_path)
                result = add_command_guidance(result, resolution)
        elif args.command == "resume":
            run, decision, feedback = command_ux.parse_resume(
                args.input, run=args.run, decision=args.decision, feedback=args.feedback,
            )
            resolved_run, selection = command_ux.select_pending_run(args.workspace, run)
            result = resume_run(resolved_run, decision, feedback, args.workspace, args.cli, timeout=args.timeout)
            result = add_command_guidance(result, {
                "workspace": str(command_ux.workspace_path(args.workspace)),
                "run": str(resolved_run), "run_selection": selection,
                "decision": decision, "feedback": feedback,
            })
        else:
            explicit_run = args.run or args.run_positional
            resolved_run, selection = command_ux.select_latest_run(args.workspace, explicit_run)
            result = trace_run(resolved_run, args.workspace)
            result = add_command_guidance(result, {
                "workspace": str(command_ux.workspace_path(args.workspace)),
                "run": str(resolved_run), "run_selection": selection,
            }, persist=False)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    except (OSError, ValueError, json.JSONDecodeError, jsonschema.ValidationError, supervisor.SupervisorError) as exc:
        print(json.dumps(command_ux.error_result(exc), ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
