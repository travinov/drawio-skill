#!/usr/bin/env python3
"""Persisted multi-model create/improve/resume/trace host for draw.io.

Models only return typed plans, patches, and verdicts. This host owns XML
rendering, patch application, validation, comparison, state, and publication.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import jsonschema

import agent_runtime
import command_ux
import diagram_host
import diagram_supervisor as supervisor


ROOT = Path(__file__).resolve().parent.parent
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
WORKFLOW_FILE = "workflow.json"
CHECKPOINT_FILE = "pending-checkpoint.json"
DEFAULT_MAX_ITERATIONS = 3


def utc_slug(prefix):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:8]}"


def canonical_hash(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


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


def render_generic(plan, output):
    """Render a graph without Graphviz and include explicit orthogonal waypoints."""
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
            role != "supervisor"
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
    action = result["action"]
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
        host_mandatory_roles = {"supervisor", "repair", "reviewer"}
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
    workflow["supervisor_action"] = action
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


def checkpoint(run_dir, workflow, kind, summary, findings, allowed, *, evidence=None):
    value = {
        "schema_version": 1, "run_id": workflow["run_id"], "kind": kind,
        "summary": summary, "findings": findings, "allowed_decisions": allowed,
        "accepted_artifact": workflow["accepted_artifact"], "created_at": supervisor.utc_now(),
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
    return host_result(run_dir, workflow)


def role_policy_evidence(workflow):
    """Expose model-declared and deterministic host role selection separately."""
    return {
        "supervisor_action": workflow.get("supervisor_action"),
        "supervisor_declared_roles": workflow.get("supervisor_declared_roles", []),
        "host_mandatory_roles": workflow.get("host_mandatory_roles", []),
        "effective_required_roles": workflow.get("required_roles", []),
    }


def host_result(run_dir, workflow, *, error=None):
    state = supervisor.load_state(run_dir)
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
        "accepted_artifact": workflow.get("accepted_artifact"),
        "accepted_validation": workflow.get("accepted_validation"),
        "role_runs": role_runs,
        "failed_role_runs": failed_role_runs,
        "model_diversity_degraded": any(
            bool(item.get("fallback_used")) for item in role_runs
        ),
        "role_policy": role_policy_evidence(workflow),
        "checkpoint": workflow.get("checkpoint"),
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


def baseline_review(run_dir, workflow, cli, timeout):
    accepted = Path(workflow["accepted_artifact"]["path"])
    report = Path(workflow["accepted_validation"]["report"])
    receipt = Path(workflow["accepted_validation"]["receipt"])
    audit = diagram_host.audit_input(run_dir, accepted, Path(run_dir) / "diagram-spec.json", report, receipt)
    verdict, runtime, _, output = role_call(
        "reviewer", audit, run_dir, Path(workflow["workspace"]), cli, timeout, f"reviewer-baseline-{workflow['iteration']}"
    )
    verdict = supervisor.load_reviewer_verdict(output, workflow["run_id"], supervisor.sha256_file(accepted), report, receipt)
    return verdict, runtime


def repair_input(run_dir, workflow):
    accepted = Path(workflow["accepted_artifact"]["path"])
    report = supervisor.load_json(workflow["accepted_validation"]["report"])
    receipt = supervisor.load_json(workflow["accepted_validation"]["receipt"])
    spec = supervisor.make_spec(accepted, [source_ref_for_request(workflow["run_id"], workflow["request"])])
    approved_semantic_change = workflow.get("approved_semantic_change")
    if workflow.get("semantic_authorized"):
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
    return {
        "schema_version": 1, "run_id": workflow["run_id"], "mode": workflow["mode"],
        "request": workflow["request"], "iteration": workflow["iteration"],
        "semantic_changes_authorized": workflow.get("semantic_authorized", False),
        "approved_semantic_change": approved_semantic_change,
        "baseline": {
            "artifact": {"path": str(accepted), "sha256": supervisor.sha256_file(accepted)},
            "semantic_digest": spec["semantic_digest"]["value"],
            "diagram_spec": spec, "validation_report": report, "validation_receipt": receipt,
        },
        "requirements": {
            "last_accepted_only": True, "preserve_untouched_regions": True,
            "explicit_waypoints_for_congested_edges": True,
            "allowed_operations": ["set_edge_route", "set_edge_pins", "set_label_offset", "move_vertex", "resize_vertex", "resize_container", "add_semantic_element", "remove_semantic_element"],
        },
        "previous_findings": workflow.get("findings", []),
    }


def _review_candidate(run_dir, workflow, candidate, report, receipt, patch, cli, timeout, label):
    payload = supervisor.make_reviewer_input(run_dir, candidate, report, receipt, patch)
    verdict, runtime, _, output = role_call("reviewer", payload, run_dir, Path(workflow["workspace"]), cli, timeout, label)
    supervisor.load_reviewer_verdict(output, workflow["run_id"], supervisor.sha256_file(candidate), report, receipt)
    return verdict, runtime, output


def _set_workflow_accepted(workflow, state):
    workflow["accepted_artifact"] = dict(state["accepted_artifact"])
    workflow["accepted_validation"] = dict(state["accepted_validation"])


def repair_loop(run_dir, workflow, cli, timeout, *, already_patching=False):
    while workflow["iteration"] < workflow["max_iterations"]:
        workflow["iteration"] += 1
        write_workflow(run_dir, workflow)
        baseline_path = Path(workflow["accepted_artifact"]["path"])
        baseline_report = Path(workflow["accepted_validation"]["report"])
        baseline_receipt = Path(workflow["accepted_validation"]["receipt"])
        if "repair" not in workflow.get("required_roles", []):
            current = supervisor.load_state(run_dir)["state"]
            if current == "analyzed":
                supervisor.transition(run_dir, "awaiting_decision", artifact=baseline_path)
                current = "awaiting_decision"
            if current != "awaiting_feedback":
                supervisor.transition(run_dir, "awaiting_feedback", decision="pause", reason="Supervisor did not authorize Repair")
            workflow["findings"] = ["Supervisor required_roles did not authorize the Repair role."]
            return checkpoint(
                run_dir, workflow, "plateau",
                "Automatic repair was not started because the Supervisor plan did not authorize Repair.",
                workflow["findings"], ["continue", "pause", "stop", "manual_handoff"],
            )
        if not already_patching:
            supervisor.transition(run_dir, "patching", artifact=baseline_path)
        already_patching = False
        try:
            patch_value, _, _, patch_path = role_call(
                "repair", repair_input(run_dir, workflow), run_dir, Path(workflow["workspace"]),
                cli, timeout, f"repair-{workflow['iteration']}"
            )
        except supervisor.SupervisorError as exc:
            supervisor.transition(run_dir, "retrying", artifact=baseline_path)
            supervisor.transition(run_dir, "awaiting_feedback", decision="pause", reason="Repair could not produce a usable typed patch")
            workflow["findings"] = [str(exc)]
            return checkpoint(
                run_dir, workflow, "plateau",
                "Repair could not produce a schema-valid bounded patch; the accepted candidate was preserved.",
                workflow["findings"], ["continue", "pause", "stop", "manual_handoff"],
            )
        semantic_patch = any(op["semantic_effect"] != "layout_only" for op in patch_value["operations"])
        if semantic_patch and (not workflow.get("semantic_authorized") or not workflow.get("approved_semantic_change")):
            workflow["pending_patch"] = str(patch_path)
            workflow["pending_patch_sha256"] = supervisor.sha256_file(patch_path)
            supervisor.transition(run_dir, "retrying", artifact=baseline_path)
            supervisor.transition(run_dir, "awaiting_feedback", decision="pause", reason="semantic patch requires approval")
            return checkpoint(
                run_dir, workflow, "semantic_approval",
                "Repair proposed semantic changes; approve them before deterministic application.",
                [op["reasons"][0] for op in patch_value["operations"] if op["semantic_effect"] != "layout_only"],
                ["continue", "pause", "stop", "manual_handoff"],
            )
        attempt_id = f"iteration-{workflow['iteration']}"
        attempt_dir = Path(run_dir) / "attempts" / attempt_id
        candidate = attempt_dir / "candidate.drawio"
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
        report = attempt_dir / "validation-report.json"
        receipt = attempt_dir / "validation-receipt.json"
        try:
            verdict, _, verdict_path = _review_candidate(
                run_dir, workflow, candidate, report, receipt, patch_path, cli, timeout,
                f"reviewer-{workflow['iteration']}",
            )
        except supervisor.SupervisorError as exc:
            supervisor.transition(run_dir, "retrying", artifact=baseline_path)
            supervisor.transition(run_dir, "awaiting_feedback", decision="pause", reason="independent review failed")
            workflow["findings"] = [str(exc)]
            return checkpoint(
                run_dir, workflow, "plateau",
                "The candidate was not promoted because independent review did not produce a usable hash-bound verdict.",
                workflow["findings"], ["continue", "pause", "stop", "manual_handoff"],
            )
        semantic_approval_path = None
        if semantic_patch:
            semantic_approval_path = attempt_dir / "semantic-approval.json"
            supervisor.write_json(
                semantic_approval_path,
                supervisor.create_semantic_approval(run_dir, baseline_path, candidate, patch_path, "approve"),
            )
        decision = supervisor.record_candidate(
            run_dir, candidate, baseline_report, report, patch_path,
            baseline_receipt, receipt, reviewer_verdict_path=verdict_path,
            semantic_approval_path=semantic_approval_path,
        )
        state = supervisor.load_state(run_dir)
        workflow["findings"] = verdict.get("findings", [])
        workflow.setdefault("attempts", []).append({
            "iteration": workflow["iteration"], "candidate": str(candidate),
            "candidate_sha256": supervisor.sha256_file(candidate), "decision": decision,
            "validation_result": receipt_value["result"], "reviewer_verdict": verdict["verdict"],
        })
        if decision["accepted"]:
            _set_workflow_accepted(workflow, state)
            write_workflow(run_dir, workflow)
            if receipt_value["result"] == "passed" and verdict["verdict"] == "approve":
                supervisor.transition(run_dir, "final_review", artifact=Path(workflow["accepted_artifact"]["path"]))
                return checkpoint(
                    run_dir, workflow, "final_acceptance",
                    "The best candidate passed strict validation and independent review.",
                    workflow["findings"],
                    ["approve", "approve_with_findings", "continue", "pause", "stop", "manual_handoff"],
                )
            continue
        if state["state"] == "retrying":
            write_workflow(run_dir, workflow)
            continue
        workflow["status"] = "awaiting_human"
        write_workflow(run_dir, workflow)
        if state["state"] == "plateau":
            supervisor.transition(run_dir, "awaiting_feedback", reason="automatic improvement plateau")
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
            supervisor.transition(run_dir, "final_review", artifact=Path(workflow["accepted_artifact"]["path"]))
    return checkpoint(
        run_dir, workflow, "plateau", "Configured automatic iteration limit was reached.",
        workflow.get("findings", []), ["continue", "pause", "stop", "manual_handoff"],
    )


def _start_run_impl(mode, diagram, request, workspace, cli, *, run_id, timeout=600, max_iterations=DEFAULT_MAX_ITERATIONS):
    workspace = normalize_workspace(workspace)
    source = normalize_drawio(diagram, workspace, must_exist=(mode == "improve"))
    run_dir = run_dir_for(workspace, run_id)
    if run_dir.exists():
        raise supervisor.SupervisorError(f"run directory already exists: {run_dir}")
    supervisor.host_preflight(workspace, run_dir, cli)
    workflow = {
        "schema_version": 1, "run_id": supervisor.ensure_run_id(run_dir), "mode": mode,
        "workspace": str(workspace), "target": str(source), "request": request.strip(),
        "status": "running", "iteration": 0, "max_iterations": max_iterations,
        "created_at": supervisor.utc_now(), "attempts": [], "findings": [], "checkpoint": None,
    }
    if not workflow["request"]:
        raise supervisor.SupervisorError("diagram request must not be empty")
    request_path = run_dir / "inputs" / "request.json"
    supervisor.write_json(request_path, {"schema_version": 1, "mode": mode, "diagram": str(source), "request": request})
    supervisor.append_event(run_dir, "run_created", {"mode": mode, "request": str(request_path), "request_sha256": supervisor.sha256_file(request_path)})
    write_workflow(run_dir, workflow)
    original = run_dir / "inputs" / "original.drawio"
    if mode == "improve":
        atomic_copy(source, original)
        workflow["original_artifact"] = {"path": str(original), "sha256": supervisor.sha256_file(original)}
        imported_spec = supervisor.make_spec(original, [source_ref_for_request(workflow["run_id"], request)])
    else:
        imported_spec = None
    supervisor_payload = {
        "schema_version": 1, "run_id": workflow["run_id"], "mode": mode,
        "request": request, "target": str(source), "existing_diagram_spec": imported_spec,
        "constraints": {"local_only": True, "deterministic_mutations": True, "max_iterations": max_iterations},
    }
    supervisor_result, _, _, _ = role_call("supervisor", supervisor_payload, run_dir, workspace, cli, timeout, "supervisor-initial")
    consume_supervisor_decision(
        workflow, supervisor_result, phase="initial", requested_max_iterations=max_iterations,
    )
    write_workflow(run_dir, workflow)
    semantic_payload = {
        "schema_version": 1, "run_id": workflow["run_id"], "mode": mode,
        "request": request, "existing_diagram_spec": imported_spec,
        "source_priority": ["explicit_user_decision", "confirmed_clarification", "openspec", "existing_diagram", "agent_assumption"],
        "requirements": {"compare_request_to_existing": mode == "improve", "return_complete_plan_for_create": mode == "create"},
    }
    semantic_plan, _, _, plan_path = role_call("semantic_analyst", semantic_payload, run_dir, workspace, cli, timeout, "semantic-initial")
    validate_plan(semantic_plan)
    workflow["semantic_plan"] = {"path": str(plan_path), "sha256": supervisor.sha256_file(plan_path)}
    accepted = run_dir / "accepted" / "baseline.drawio"
    if mode == "create":
        tool_step(run_dir, "generic-renderer", render_generic, semantic_plan, accepted)
    else:
        atomic_copy(original, accepted)
    spec = supervisor.make_spec(accepted, [source_ref_for_request(workflow["run_id"], request)])
    supervisor.write_json(run_dir / "diagram-spec.json", spec)
    supervisor.transition(run_dir, "analyzed", artifact=accepted, max_attempts=max_iterations)
    receipt = tool_step(run_dir, "strict-validator", supervisor.run_validation, accepted, run_dir, attempt_id="baseline")
    report_path = run_dir / "attempts" / "baseline" / "validation-report.json"
    receipt_path = run_dir / "attempts" / "baseline" / "validation-receipt.json"
    bind_accepted_validation(run_dir, report_path, receipt_path)
    state = supervisor.load_state(run_dir)
    _set_workflow_accepted(workflow, state)
    write_workflow(run_dir, workflow)
    changes = semantic_plan["result"]["semantic_changes"]
    if mode == "improve" and (semantic_plan["result"]["requires_human"] or changes):
        supervisor.transition(run_dir, "awaiting_decision", artifact=accepted)
        pending_semantic = {
            "semantic_plan": {"path": str(Path(plan_path).resolve()), "sha256": supervisor.sha256_file(plan_path)},
            "semantic_changes": changes,
            "semantic_changes_sha256": canonical_hash(changes),
        }
        workflow["pending_semantic_approval"] = pending_semantic
        return checkpoint(
            run_dir, workflow, "semantic_approval",
            "The supplied process description differs from the existing diagram; these changes will be used in repair after approval.",
            changes, ["continue", "pause", "stop", "manual_handoff"], evidence=pending_semantic,
        )
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
    write_workflow(run_dir, workflow)
    if receipt["result"] == "passed" and verdict["verdict"] == "approve":
        supervisor.transition(run_dir, "final_review", artifact=accepted)
        return checkpoint(
            run_dir, workflow, "final_acceptance",
            "The candidate passed strict validation and independent review.", workflow["findings"],
            ["approve", "approve_with_findings", "continue", "pause", "stop", "manual_handoff"],
        )
    return repair_loop(run_dir, workflow, cli, timeout)


def start_run(mode, diagram, request, workspace, cli, *, run_id=None, timeout=600, max_iterations=DEFAULT_MAX_ITERATIONS):
    normalized_workspace = normalize_workspace(workspace)
    effective_run_id = run_id or utc_slug(mode)
    try:
        return _start_run_impl(
            mode, diagram, request, normalized_workspace, cli,
            run_id=effective_run_id, timeout=timeout, max_iterations=max_iterations,
        )
    except Exception as exc:
        run_dir = run_dir_for(normalized_workspace, effective_run_id)
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
            host_result(run_dir, workflow, error=str(exc))
        raise


def publish(run_dir, workflow, decision):
    accepted = Path(workflow["accepted_artifact"]["path"])
    target = Path(workflow["target"])
    if workflow.get("mode") == "create" and target.exists():
        raise supervisor.SupervisorError(
            f"create target appeared after the run started and will not be overwritten: {target}"
        )
    atomic_copy(accepted, target)
    supervisor.append_event(
        run_dir, "artifact_published",
        {"decision": decision, "artifact": str(target), "artifact_sha256": supervisor.sha256_file(target), "accepted_sha256": supervisor.sha256_file(accepted)},
        actor={"kind": "human", "id": "user", "model": None},
    )
    return target


def resume_run(reference, decision, feedback, workspace, cli, *, timeout=600):
    workspace = normalize_workspace(workspace)
    run_dir = resolve_run(reference, workspace)
    workflow = load_workflow(run_dir)
    checkpoint_value = workflow.get("checkpoint")
    if not checkpoint_value:
        raise supervisor.SupervisorError("run has no pending human checkpoint")
    if decision not in checkpoint_value["allowed_decisions"]:
        raise supervisor.SupervisorError(f"decision {decision!r} is not allowed at {checkpoint_value['kind']}")
    checkpoint_path = Path(checkpoint_value["path"])
    if not checkpoint_path.is_file() or supervisor.sha256_file(checkpoint_path) != checkpoint_value["sha256"]:
        raise supervisor.SupervisorError("pending checkpoint evidence is missing or hash-mismatched")
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
    if decision == "continue":
        resume_payload = {
            "schema_version": 1, "run_id": workflow["run_id"], "mode": workflow["mode"],
            "phase": "resume", "request": workflow["request"], "feedback": feedback,
            "checkpoint": checkpoint_value, "accepted_artifact": workflow["accepted_artifact"],
            "iteration": workflow["iteration"], "previous_decision": workflow.get("supervisor_decision"),
        }
        resume_plan, _, _, _ = role_call(
            "supervisor", resume_payload, run_dir, workspace, cli, timeout,
            f"supervisor-resume-{len(workflow.get('decisions', [])) + 1}",
        )
        requested_additional = DEFAULT_MAX_ITERATIONS
        consume_supervisor_decision(
            workflow, resume_plan, phase="resume", requested_max_iterations=requested_additional,
        )
        workflow["max_iterations"] = workflow["iteration"] + workflow["max_iterations"]
    decision_path = run_dir / "decisions" / f"{len(workflow.get('decisions', [])) + 1:03d}.json"
    decision_value = {
        "schema_version": 1, "decision": decision, "feedback": feedback,
        "checkpoint_kind": checkpoint_value["kind"],
        "checkpoint": {"path": str(checkpoint_path.resolve()), "sha256": checkpoint_value["sha256"]},
        "decided_at": supervisor.utc_now(),
    }
    if approved_proposal is not None:
        decision_value["approved_semantic_change"] = approved_proposal
    supervisor.write_json(decision_path, decision_value)
    workflow.setdefault("decisions", []).append({"path": str(decision_path), "sha256": supervisor.sha256_file(decision_path), **decision_value})
    workflow["checkpoint"] = None
    (run_dir / CHECKPOINT_FILE).unlink(missing_ok=True)
    supervisor.append_event(
        run_dir, "user_decision", {"decision": decision, "feedback_sha256": hashlib.sha256(feedback.encode("utf-8")).hexdigest(), "decision_path": str(decision_path), "decision_sha256": supervisor.sha256_file(decision_path)},
        actor={"kind": "human", "id": "user", "model": None},
    )
    state = supervisor.load_state(run_dir)
    if decision == "stop":
        supervisor.transition(run_dir, "stopped", decision="stop", reason=feedback or "user stopped iterations")
        workflow["status"] = "stopped"
        write_workflow(run_dir, workflow)
        return host_result(run_dir, workflow)
    if decision == "manual_handoff":
        supervisor.transition(run_dir, "manual_handoff", decision="manual_handoff", reason=feedback or "user requested manual handoff")
        workflow["status"] = "manual_handoff"
        write_workflow(run_dir, workflow)
        return host_result(run_dir, workflow)
    if decision == "pause":
        if state["state"] != "awaiting_feedback":
            supervisor.transition(run_dir, "awaiting_feedback", decision="pause", reason=feedback or "user paused")
        workflow["status"] = "awaiting_human"
        write_workflow(run_dir, workflow)
        return checkpoint(run_dir, workflow, checkpoint_value["kind"], "Run remains paused.", checkpoint_value["findings"], checkpoint_value["allowed_decisions"])
    if decision in {"approve", "approve_with_findings"}:
        if checkpoint_value["kind"] != "final_acceptance" and decision == "approve":
            raise supervisor.SupervisorError("approve is only valid at final acceptance")
        target = publish(run_dir, workflow, decision)
        if decision == "approve":
            supervisor.transition(run_dir, "completed", artifact=Path(workflow["accepted_artifact"]["path"]), receipt=workflow["accepted_validation"]["receipt"], decision="approve", reason=feedback)
            workflow["status"] = "completed"
        else:
            supervisor.transition(run_dir, "approved_with_findings", decision="approve_with_findings", reason=feedback)
            workflow["status"] = "approved_with_findings"
        workflow["published_artifact"] = {"path": str(target), "sha256": supervisor.sha256_file(target)}
        write_workflow(run_dir, workflow)
        return host_result(run_dir, workflow)
    if feedback:
        workflow["request"] += "\n\nUser feedback: " + feedback
    if approved_proposal is not None:
        workflow["approved_semantic_change"] = {
            **approved_proposal,
            "decision": {"path": str(decision_path.resolve()), "sha256": supervisor.sha256_file(decision_path)},
        }
    workflow["semantic_authorized"] = approved_proposal is not None or workflow.get("semantic_authorized", False)
    workflow["status"] = "running"
    write_workflow(run_dir, workflow)
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


def trace_run(reference, workspace):
    workspace = normalize_workspace(workspace)
    run_dir = resolve_run(reference, workspace)
    workflow = load_workflow(run_dir)
    manifest = run_dir / "run-manifest.jsonl"
    checks, events, previous = [], [], None
    schema = supervisor.load_json(ROOT / "data" / "run-event.v1.schema.json")
    for index, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        event = json.loads(line)
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
                parsed = agent_runtime.validate_role_output(role, parsed)
                role_input = supervisor.load_json(start["payload"]["input"])
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
                fallback_used = bool(payload.get("fallback_used"))
                approved_fallback = (
                    role == "supervisor"
                    and fallback_used
                    and resolved_model in configured_fallbacks
                    and any(
                        failure.get("terminal") is False
                        and failure.get("attempted_model") == expected_model
                        and failure.get("fallback_model") == resolved_model
                        and failure.get("failure_kind") == "turn_limit"
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
                "phase": payload.get("phase"),
                "failure_kind": payload.get("failure_kind"),
                "terminal": payload.get("terminal", True),
                "attempted_model": payload.get("attempted_model"),
                "fallback_model": payload.get("fallback_model"),
                "attempt_id": payload.get("attempt_id"),
                "output_format": payload.get("output_format"),
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
    integrity_valid = (
        bool(events)
        and all(item["schema_valid"] and item["chain_valid"] for item in checks)
        and all(item["valid"] for item in artifact_checks)
        and all(item["valid"] for item in role_checks)
        and all(item["evidence_valid"] for item in failed_roles)
        and preflight["valid"]
    )
    terminal_failed_roles = [
        item for item in failed_roles if item.get("terminal", True)
    ]
    valid = (
        integrity_valid
        and not terminal_failed_roles
        and accepted_valid
        and accepted_receipt_valid
    )
    roles = [
        {key: event["payload"].get(key) for key in ("role", "requested_model", "resolved_model", "resolution_mode", "fallback_used", "model_proof", "isolation_controls", "isolation_proof", "binding_proof", "output_sha256")}
        for event in events if event["event_type"] == "role_finished"
    ]
    status = (
        "verified" if valid
        else "failed_verified" if integrity_valid and terminal_failed_roles
        else "tampered_or_incomplete"
    )
    result = {
        "schema_version": 1, "status": status,
        "valid": valid, "run_id": workflow["run_id"], "run_dir": str(run_dir),
        "state": (supervisor.load_state(run_dir) or {}).get("state"),
        "event_count": len(events), "event_checks": checks, "artifact_checks": artifact_checks,
        "accepted_artifact_valid": accepted_valid, "accepted_receipt_valid": accepted_receipt_valid,
        "host_preflight": preflight, "role_checks": role_checks,
        "failed_roles": failed_roles, "integrity_valid": integrity_valid,
        "terminal_failed_roles": terminal_failed_roles,
        "model_diversity_degraded": any(
            bool(role.get("fallback_used")) for role in roles
        ),
        "roles": roles, "terminal_result": workflow.get("status"),
        "role_policy": role_policy_evidence(workflow),
        "trust_scope": "local runtime capture and configured routing policy; no external cryptographic attestation",
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
        cmd.add_argument("--timeout", type=int, default=600)
        cmd.add_argument("--max-iterations", type=int, default=DEFAULT_MAX_ITERATIONS)
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
            else:
                resolved_diagram, selection, request, handoff = command_ux.resolve_improve_inputs(
                    args.workspace, diagram=diagram, request=request,
                )
            if args.request is not None:
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
            result = start_run(
                args.command, resolved_diagram, request, args.workspace, args.cli,
                run_id=args.run_id, timeout=args.timeout, max_iterations=args.max_iterations,
            )
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
