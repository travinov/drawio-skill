#!/usr/bin/env python3
"""Offline router for deterministic ELK and Python layout backends."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import layout_builtin
import layout_contracts
from lifecycle_contracts import canonical_json_bytes, canonical_json_sha256, file_sha256


SCRIPT_ROOT = Path(__file__).resolve().parent
ELK_RUNNER = SCRIPT_ROOT / "elk_runner.mjs"
ELK_NOTICE = SCRIPT_ROOT.parent / "vendor" / "elkjs" / "NOTICE.json"
ELKJS_VERSION = "0.11.1"
ELK_BACKEND_ID = f"elk-layered-{ELKJS_VERSION}"
NODE_VERSION_PATTERN = re.compile(r"^v\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
BACKENDS = frozenset({"auto", "elk", "python", "legacy-generic-v2"})


class LayoutBackendError(RuntimeError):
    """Base class for deterministic backend failures."""


class BackendUnavailableError(LayoutBackendError):
    """Raised when an explicitly required backend is not available."""


class DuplicateBackendAttemptError(LayoutBackendError):
    """Raised before repeating the same immutable strategy attempt."""


class BackendExecutionError(LayoutBackendError):
    """Carries bounded subprocess evidence for a rejected ELK result."""

    def __init__(self, reason: str, evidence: Mapping[str, Any]) -> None:
        super().__init__(reason)
        self.reason = reason
        self.evidence = dict(evidence)


@dataclass(frozen=True)
class BackendAttempt:
    result: dict
    evidence: dict


def effective_options(request: Mapping[str, Any]) -> dict[str, str]:
    """Return the complete pinned ELK option set for one immutable request."""
    constraints = request.get("constraints")
    if not isinstance(constraints, Mapping):
        constraints = {}
    direction = "RIGHT" if request.get("direction") == "LR" else "DOWN"
    return {
        "elk.algorithm": "layered",
        "elk.direction": direction,
        "elk.edgeRouting": "ORTHOGONAL",
        "elk.layered.considerModelOrder.strategy": "NODES_AND_EDGES",
        "elk.layered.nodePlacement.strategy": "NETWORK_SIMPLEX",
        "elk.spacing.nodeNode": str(constraints.get("node_separation", 40)),
        "elk.layered.spacing.nodeNodeBetweenLayers": str(
            constraints.get("layer_separation", 80)
        ),
    }


def attempt_key(request: Mapping[str, Any]) -> tuple[str, str, str]:
    """Return the stable duplicate-prevention key mandated by the host."""
    layout_contracts.require_layout_request(request)
    request_sha256 = canonical_json_sha256(request)
    strategy_id = str(request.get("strategy"))
    options_sha256 = canonical_json_sha256(effective_options(request))
    return request_sha256, strategy_id, options_sha256


def _subprocess_environment(environ: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environ is None else environ
    result = {str(key): str(value) for key, value in source.items()}
    result.pop("NODE_OPTIONS", None)
    result.pop("NODE_PATH", None)
    return result


def _probe_node(
    candidate: Path,
    *,
    environ: Mapping[str, str] | None,
    timeout_seconds: float = 5.0,
) -> dict[str, str] | None:
    if not candidate.is_absolute() or not candidate.is_file() or not os.access(candidate, os.X_OK):
        return None
    environment = _subprocess_environment(environ)
    try:
        version_run = subprocess.run(
            [str(candidate), "--version"],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=environment,
            check=False,
        )
        version = version_run.stdout.strip()
        if (
            version_run.returncode != 0
            or version_run.stderr.strip()
            or not NODE_VERSION_PATTERN.fullmatch(version)
        ):
            return None
        bridge_run = subprocess.run(
            [str(candidate), str(ELK_RUNNER), "--probe"],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=environment,
            check=False,
        )
        if bridge_run.returncode != 0 or bridge_run.stderr.strip():
            return None
        proof = json.loads(bridge_run.stdout)
        if (
            not isinstance(proof, dict)
            or proof.get("bridge") != "drawio-elk-runner"
            or proof.get("elkjs_version") != ELKJS_VERSION
        ):
            return None
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, TypeError, ValueError):
        return None
    return {"node_version": version, "elkjs_version": ELKJS_VERSION}


def resolve_node(
    config: Mapping[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
) -> Path | None:
    """Resolve Node only after both executable-version and bundled bridge probes."""
    configured = config.get("node_bin")
    if configured is not None:
        if not isinstance(configured, str) or not configured:
            return None
        candidate = Path(configured).expanduser()
        if not candidate.is_absolute():
            return None
        candidate = candidate.resolve()
        return candidate if _probe_node(candidate, environ=environ) is not None else None

    environment = _subprocess_environment(environ)
    discovered = shutil.which("node", path=environment.get("PATH", ""))
    if discovered is None:
        return None
    candidate = Path(discovered).resolve()
    return candidate if _probe_node(candidate, environ=environment) is not None else None


def _strict_json(text: str) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant {value}")

    return json.loads(text, parse_constant=reject_constant)


def _artifact_directory() -> Path:
    return Path(tempfile.mkdtemp(prefix="drawio-layout-elk-")).resolve()


def _write_capture(path: Path, value: str | bytes | None) -> None:
    if isinstance(value, bytes):
        path.write_bytes(value)
    else:
        path.write_text(value or "", encoding="utf-8")


def _base_evidence(
    request: Mapping[str, Any],
    *,
    node: Path | None,
    timeout_seconds: float,
) -> dict[str, Any]:
    request_sha256, strategy_id, options_sha256 = attempt_key(request)
    return {
        "backend_requested": "elk",
        "backend_selected": ELK_BACKEND_ID,
        "node_executable": str(node.resolve()) if node is not None else None,
        "node_version": None,
        "elkjs_version": ELKJS_VERSION,
        "timeout_seconds": timeout_seconds,
        "exit_code": None,
        "stdout_path": None,
        "stdout_sha256": None,
        "stderr_path": None,
        "stderr_sha256": None,
        "request_sha256": request_sha256,
        "result_sha256": None,
        "schema_valid": False,
        "schema_diagnostics": [],
        "fallback_reason": None,
        "strategy_id": strategy_id,
        "effective_options": effective_options(request),
        "options_sha256": options_sha256,
        "attempt_key": [request_sha256, strategy_id, options_sha256],
    }


def _request_result_diagnostics(
    request: Mapping[str, Any], result: Mapping[str, Any]
) -> list[str]:
    """Check completeness and immutable members not expressible in result schema."""
    diagnostics: list[str] = []
    request_pages = {
        str(page.get("page_id")): page
        for page in request.get("pages", [])
        if isinstance(page, Mapping)
    }
    result_pages = {
        str(page.get("page_id")): page
        for page in result.get("pages", [])
        if isinstance(page, Mapping)
    }
    if set(request_pages) != set(result_pages):
        diagnostics.append("result page ids do not match request page ids")
        return diagnostics
    for page_id in sorted(request_pages):
        request_page = request_pages[page_id]
        result_page = result_pages[page_id]
        request_nodes = {
            str(node.get("node_id")): node
            for node in request_page.get("nodes", [])
            if isinstance(node, Mapping)
        }
        result_nodes = {
            str(node.get("node_id")): node
            for node in result_page.get("nodes", [])
            if isinstance(node, Mapping)
        }
        request_edges = {
            str(edge.get("edge_id")): edge
            for edge in request_page.get("edges", [])
            if isinstance(edge, Mapping)
        }
        result_edges = {
            str(edge.get("edge_id")): edge
            for edge in result_page.get("edges", [])
            if isinstance(edge, Mapping)
        }
        if set(request_nodes) != set(result_nodes):
            diagnostics.append(f"page {page_id!r} result node ids do not match request")
        if set(request_edges) != set(result_edges):
            diagnostics.append(f"page {page_id!r} result edge ids do not match request")
        for node_id in sorted(set(request_nodes) & set(result_nodes)):
            source, candidate = request_nodes[node_id], result_nodes[node_id]
            if bool(source.get("locked")) != bool(candidate.get("locked")):
                diagnostics.append(f"node {page_id}/{node_id} changed locked state")
            if source.get("locked") is True:
                for field in ("x", "y", "width", "height"):
                    if source.get(field) != candidate.get(field):
                        diagnostics.append(f"locked node {page_id}/{node_id} changed {field}")
        for edge_id in sorted(set(request_edges) & set(result_edges)):
            source, candidate = request_edges[edge_id], result_edges[edge_id]
            for field in (
                "source",
                "target",
                "edge_class",
                "source_port",
                "target_port",
            ):
                if source.get(field) != candidate.get(field):
                    diagnostics.append(f"edge {page_id}/{edge_id} changed {field}")
            if source.get("locked") is True and source.get("waypoints") != candidate.get(
                "waypoints"
            ):
                diagnostics.append(f"locked edge {page_id}/{edge_id} changed waypoints")
    return diagnostics


def run_elk(
    request: Mapping[str, Any],
    *,
    node: Path,
    timeout_seconds: float,
) -> BackendAttempt:
    """Run the committed bridge once and accept only strict request-bound JSON."""
    layout_contracts.require_layout_request(request)
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be a positive number")
    node = Path(node).expanduser().resolve()
    evidence = _base_evidence(request, node=node, timeout_seconds=float(timeout_seconds))
    proof = _probe_node(node, environ=os.environ)
    if proof is None:
        evidence["fallback_reason"] = "node_or_bridge_probe_failed"
        raise BackendExecutionError("node_or_bridge_probe_failed", evidence)
    evidence.update(proof)

    capture_dir = _artifact_directory()
    stdout_path = capture_dir / "runtime-output.json"
    stderr_path = capture_dir / "runtime-stderr.txt"
    bridge_request = dict(request)
    bridge_request["__request_sha256"] = evidence["request_sha256"]
    command = [str(node), str(ELK_RUNNER)]
    stdout_value = ""
    stderr_value = ""
    try:
        completed = subprocess.run(
            command,
            input=canonical_json_bytes(bridge_request).decode("utf-8"),
            capture_output=True,
            text=True,
            timeout=float(timeout_seconds),
            env=_subprocess_environment(os.environ),
            check=False,
        )
        stdout_value, stderr_value = completed.stdout, completed.stderr
        evidence["exit_code"] = completed.returncode
    except subprocess.TimeoutExpired as exc:
        stdout_value, stderr_value = exc.stdout or "", exc.stderr or ""
        _write_capture(stdout_path, stdout_value)
        _write_capture(stderr_path, stderr_value)
        evidence.update(
            {
                "stdout_path": str(stdout_path),
                "stdout_sha256": file_sha256(stdout_path),
                "stderr_path": str(stderr_path),
                "stderr_sha256": file_sha256(stderr_path),
                "fallback_reason": "elk_timeout",
            }
        )
        raise BackendExecutionError("elk_timeout", evidence) from exc
    except OSError as exc:
        stderr_value = str(exc)
        _write_capture(stdout_path, "")
        _write_capture(stderr_path, stderr_value)
        evidence.update(
            {
                "stdout_path": str(stdout_path),
                "stdout_sha256": file_sha256(stdout_path),
                "stderr_path": str(stderr_path),
                "stderr_sha256": file_sha256(stderr_path),
                "fallback_reason": "elk_process_error",
            }
        )
        raise BackendExecutionError("elk_process_error", evidence) from exc

    _write_capture(stdout_path, stdout_value)
    _write_capture(stderr_path, stderr_value)
    evidence.update(
        {
            "stdout_path": str(stdout_path),
            "stdout_sha256": file_sha256(stdout_path),
            "stderr_path": str(stderr_path),
            "stderr_sha256": file_sha256(stderr_path),
        }
    )
    if evidence["exit_code"] != 0:
        evidence["fallback_reason"] = "elk_nonzero_exit"
        raise BackendExecutionError("elk_nonzero_exit", evidence)
    try:
        result = _strict_json(stdout_value)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        evidence["fallback_reason"] = "elk_invalid_json"
        raise BackendExecutionError("elk_invalid_json", evidence) from exc
    if not isinstance(result, dict):
        evidence["fallback_reason"] = "elk_invalid_result_type"
        raise BackendExecutionError("elk_invalid_result_type", evidence)

    diagnostics = layout_contracts.validate_layout_result(
        result, expected_request_sha256=evidence["request_sha256"]
    )
    diagnostics.extend(
        {
            "code": "layout.result.request_mismatch",
            "pointer": "/pages",
            "message": message,
        }
        for message in _request_result_diagnostics(request, result)
    )
    evidence["schema_diagnostics"] = diagnostics
    if diagnostics:
        evidence["fallback_reason"] = "elk_contract_rejected"
        raise BackendExecutionError("elk_contract_rejected", evidence)
    evidence["schema_valid"] = True
    evidence["result_sha256"] = canonical_json_sha256(result)
    return BackendAttempt(result=dict(result), evidence=evidence)


def _python_attempt(
    request: Mapping[str, Any],
    *,
    requested_backend: str,
    fallback_reason: str | None,
    elk_evidence: Mapping[str, Any] | None = None,
) -> BackendAttempt:
    request_sha256, strategy_id, options_sha256 = attempt_key(request)
    result = layout_builtin.layout(request)
    layout_contracts.require_layout_result(
        result, expected_request_sha256=request_sha256
    )
    evidence: dict[str, Any] = {
        "backend_requested": requested_backend,
        "backend_selected": "python-layered",
        "node_executable": None,
        "node_version": None,
        "elkjs_version": ELKJS_VERSION,
        "timeout_seconds": None,
        "exit_code": None,
        "stdout_path": None,
        "stdout_sha256": None,
        "stderr_path": None,
        "stderr_sha256": None,
        "request_sha256": request_sha256,
        "result_sha256": canonical_json_sha256(result),
        "schema_valid": True,
        "schema_diagnostics": [],
        "fallback_reason": fallback_reason,
        "fallback_backend": "python-layered" if fallback_reason else None,
        "strategy_id": strategy_id,
        "effective_options": effective_options(request),
        "options_sha256": options_sha256,
        "attempt_key": [request_sha256, strategy_id, options_sha256],
    }
    if elk_evidence is not None:
        evidence["elk_attempt"] = dict(elk_evidence)
        for field in (
            "node_executable",
            "node_version",
            "timeout_seconds",
            "exit_code",
            "stdout_path",
            "stdout_sha256",
            "stderr_path",
            "stderr_sha256",
        ):
            evidence[field] = elk_evidence.get(field)
    return BackendAttempt(result=result, evidence=evidence)


def run_layout(
    request: Mapping[str, Any],
    *,
    config: Mapping[str, Any],
    attempted_keys=frozenset(),
) -> BackendAttempt:
    """Route one immutable request without repeating a strategy attempt."""
    layout_contracts.require_layout_request(request)
    key = attempt_key(request)
    if key in attempted_keys:
        raise DuplicateBackendAttemptError(
            "duplicate layout attempt refused for "
            f"request={key[0]} strategy={key[1]} options={key[2]}"
        )
    backend = config.get("layout_backend", "auto")
    if backend not in BACKENDS:
        raise ValueError(f"unsupported layout_backend {backend!r}")
    if backend == "legacy-generic-v2":
        raise BackendUnavailableError(
            "legacy-generic-v2 is an explicit renderer path, not an automatic layout backend"
        )
    if backend == "python":
        return _python_attempt(
            request, requested_backend="python", fallback_reason=None
        )

    node = resolve_node(config, environ=os.environ)
    if node is None:
        if backend == "elk":
            raise BackendUnavailableError(
                "explicit ELK backend requires a verified Node executable and bundled bridge"
            )
        return _python_attempt(
            request,
            requested_backend="auto",
            fallback_reason="verified_node_unavailable",
        )
    timeout_seconds = config.get("layout_timeout_seconds", 30)
    try:
        attempt = run_elk(request, node=node, timeout_seconds=timeout_seconds)
        evidence = dict(attempt.evidence)
        evidence["backend_requested"] = str(backend)
        return BackendAttempt(result=attempt.result, evidence=evidence)
    except BackendExecutionError as exc:
        return _python_attempt(
            request,
            requested_backend=str(backend),
            fallback_reason=exc.reason,
            elk_evidence=exc.evidence,
        )
