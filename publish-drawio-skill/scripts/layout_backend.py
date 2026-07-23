#!/usr/bin/env python3
"""Offline router for deterministic ELK and Python layout backends."""
from __future__ import annotations

import json
import os
import re
import selectors
import signal
import shutil
import subprocess
import tempfile
import time
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
DEFAULT_CAPTURE_MAX_BYTES = 4 * 1024 * 1024
MAX_CAPTURE_MAX_BYTES = 64 * 1024 * 1024
PROBE_CAPTURE_MAX_BYTES = 64 * 1024


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


@dataclass(frozen=True)
class _ProcessCapture:
    returncode: int | None
    timed_out: bool
    limit_stream: str | None
    process_group_isolated: bool
    process_reaped: bool
    stdout_bytes_observed: int
    stderr_bytes_observed: int
    stdout_truncated: bool
    stderr_truncated: bool


def effective_options(request: Mapping[str, Any]) -> dict[str, str]:
    """Return the complete pinned ELK option set for one immutable request."""
    constraints = request.get("constraints")
    if not isinstance(constraints, Mapping):
        constraints = {}
    strategy_options = request.get("strategy_options")
    direction = "RIGHT" if request.get("direction") == "LR" else "DOWN"
    options = {
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
    if isinstance(strategy_options, Mapping):
        options["elk.spacing.portPort"] = str(
            10 * float(strategy_options.get("port_separation", 1.0))
        )
        options["elk.spacing.edgeEdge"] = str(
            10 * float(strategy_options.get("shared_penalty", 1.0))
        )
    return options


def attempt_key(request: Mapping[str, Any]) -> tuple[str, str, str]:
    """Return the stable duplicate-prevention key mandated by the host."""
    layout_contracts.require_layout_request(request)
    request_sha256 = canonical_json_sha256(request)
    strategy_id = str(request.get("strategy"))
    options_sha256 = canonical_json_sha256(effective_options(request))
    return request_sha256, strategy_id, options_sha256


def _subprocess_environment(environ: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environ is None else environ
    allowed = (
        "PATH",
        "HOME",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "SYSTEMROOT",
        "COMSPEC",
        "PATHEXT",
    )
    return {
        key: str(source[key])
        for key in allowed
        if key in source and source[key] is not None
    }


def _capture_limit(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("layout_capture_max_bytes must be an integer")
    if value <= 0 or value > MAX_CAPTURE_MAX_BYTES:
        raise ValueError(
            "layout_capture_max_bytes must be between 1 and "
            f"{MAX_CAPTURE_MAX_BYTES}"
        )
    return value


def _kill_and_reap(
    process: subprocess.Popen,
    *,
    process_group_isolated: bool,
) -> bool:
    """Kill the isolated process group when possible, then reap the direct child."""
    if process_group_isolated and os.name == "posix" and hasattr(os, "killpg"):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            try:
                process.kill()
            except OSError:
                pass
    else:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        process.wait()
    return process.returncode is not None


def _run_bounded_process(
    command: list[str],
    *,
    input_bytes: bytes | None,
    timeout_seconds: float,
    capture_max_bytes: int,
    stdout_path: Path,
    stderr_path: Path,
    environ: Mapping[str, str] | None,
) -> _ProcessCapture:
    """Stream captures to bounded files and terminate the whole group on breach."""
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be a positive number")
    capture_max_bytes = _capture_limit(capture_max_bytes)
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_path.write_bytes(b"")
    stderr_path.write_bytes(b"")
    process_group_isolated = os.name == "posix" and hasattr(os, "killpg")
    process: subprocess.Popen | None = None
    streams: dict[str, Any] = {}
    selector: selectors.BaseSelector | None = None
    input_handle = tempfile.TemporaryFile()
    if input_bytes is not None:
        input_handle.write(input_bytes)
        input_handle.seek(0)
    try:
        with stdout_path.open("wb") as stdout_handle, stderr_path.open("wb") as stderr_handle:
            kwargs: dict[str, Any] = {
                "stdin": input_handle if input_bytes is not None else subprocess.DEVNULL,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "env": _subprocess_environment(environ),
                "close_fds": True,
            }
            if process_group_isolated:
                kwargs["start_new_session"] = True
            try:
                process = subprocess.Popen(command, **kwargs)
            except TypeError:
                if not process_group_isolated:
                    raise
                process_group_isolated = False
                kwargs.pop("start_new_session", None)
                process = subprocess.Popen(command, **kwargs)
            if process.stdout is None or process.stderr is None:
                raise RuntimeError("bounded process capture pipes are unavailable")
            selector = selectors.DefaultSelector()
            observed = {"stdout": 0, "stderr": 0}
            written = {"stdout": 0, "stderr": 0}
            handles = {"stdout": stdout_handle, "stderr": stderr_handle}
            streams = {"stdout": process.stdout, "stderr": process.stderr}
            for stream_name, stream in streams.items():
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, stream_name)
            started = time.monotonic()
            timed_out = False
            limit_stream: str | None = None
            process_reaped = False
            while selector.get_map() or process.poll() is None:
                remaining = float(timeout_seconds) - (time.monotonic() - started)
                if remaining <= 0:
                    timed_out = True
                    process_reaped = _kill_and_reap(
                        process, process_group_isolated=process_group_isolated
                    )
                    break
                if selector.get_map():
                    events = selector.select(timeout=min(0.02, remaining))
                else:
                    time.sleep(min(0.02, remaining))
                    events = []
                for key, _ in events:
                    stream_name = key.data
                    try:
                        chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    observed[stream_name] += len(chunk)
                    allowance = max(0, capture_max_bytes - written[stream_name])
                    if allowance:
                        retained = chunk[:allowance]
                        handles[stream_name].write(retained)
                        written[stream_name] += len(retained)
                    if len(chunk) > allowance:
                        limit_stream = stream_name
                        process_reaped = _kill_and_reap(
                            process, process_group_isolated=process_group_isolated
                        )
                        break
                if limit_stream is not None:
                    break
                if process.poll() is not None and not selector.get_map():
                    process.wait()
                    process_reaped = True
                    break
            if (timed_out or limit_stream is not None) and selector.get_map():
                for key in list(selector.get_map().values()):
                    try:
                        selector.unregister(key.fileobj)
                    except (KeyError, ValueError):
                        pass
                    key.fileobj.close()
            selector.close()
            selector = None
            if not process_reaped:
                process.wait()
                process_reaped = True
            stdout_handle.flush()
            stderr_handle.flush()
    finally:
        if process is not None and process.poll() is None:
            _kill_and_reap(
                process, process_group_isolated=process_group_isolated
            )
        if selector is not None:
            selector.close()
        for stream in streams.values():
            if not stream.closed:
                stream.close()
        input_handle.close()

    stdout_observed = observed["stdout"]
    stderr_observed = observed["stderr"]
    stdout_truncated = stdout_observed > stdout_path.stat().st_size
    stderr_truncated = stderr_observed > stderr_path.stat().st_size
    return _ProcessCapture(
        returncode=process.returncode if process is not None else None,
        timed_out=timed_out,
        limit_stream=limit_stream,
        process_group_isolated=process_group_isolated,
        process_reaped=process_reaped,
        stdout_bytes_observed=stdout_observed,
        stderr_bytes_observed=stderr_observed,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
    )


def _capture_text(path: Path, capture_max_bytes: int) -> str:
    with path.open("rb") as handle:
        return handle.read(capture_max_bytes).decode("utf-8", errors="replace")


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
        with tempfile.TemporaryDirectory(prefix="drawio-node-probe-") as temp:
            capture_root = Path(temp)
            version_stdout = capture_root / "version.stdout"
            version_stderr = capture_root / "version.stderr"
            version_run = _run_bounded_process(
                [str(candidate), "--version"],
                input_bytes=None,
                timeout_seconds=timeout_seconds,
                capture_max_bytes=PROBE_CAPTURE_MAX_BYTES,
                stdout_path=version_stdout,
                stderr_path=version_stderr,
                environ=environment,
            )
            version = _capture_text(
                version_stdout, PROBE_CAPTURE_MAX_BYTES
            ).strip()
            version_error = _capture_text(
                version_stderr, PROBE_CAPTURE_MAX_BYTES
            ).strip()
            if (
                version_run.returncode != 0
                or version_run.timed_out
                or version_run.limit_stream is not None
                or version_error
                or not NODE_VERSION_PATTERN.fullmatch(version)
            ):
                return None
            bridge_stdout = capture_root / "bridge.stdout"
            bridge_stderr = capture_root / "bridge.stderr"
            bridge_run = _run_bounded_process(
                [str(candidate), str(ELK_RUNNER), "--probe"],
                input_bytes=None,
                timeout_seconds=timeout_seconds,
                capture_max_bytes=PROBE_CAPTURE_MAX_BYTES,
                stdout_path=bridge_stdout,
                stderr_path=bridge_stderr,
                environ=environment,
            )
            bridge_error = _capture_text(
                bridge_stderr, PROBE_CAPTURE_MAX_BYTES
            ).strip()
            if (
                bridge_run.returncode != 0
                or bridge_run.timed_out
                or bridge_run.limit_stream is not None
                or bridge_error
            ):
                return None
            proof = json.loads(
                _capture_text(bridge_stdout, PROBE_CAPTURE_MAX_BYTES)
            )
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


def _base_evidence(
    request: Mapping[str, Any],
    *,
    node: Path | None,
    timeout_seconds: float,
    capture_max_bytes: int,
) -> dict[str, Any]:
    request_sha256, strategy_id, options_sha256 = attempt_key(request)
    return {
        "backend_requested": "elk",
        "backend_selected": ELK_BACKEND_ID,
        "node_executable": str(node.resolve()) if node is not None else None,
        "node_version": None,
        "elkjs_version": ELKJS_VERSION,
        "timeout_seconds": timeout_seconds,
        "capture_max_bytes": capture_max_bytes,
        "exit_code": None,
        "stdout_path": None,
        "stdout_sha256": None,
        "stderr_path": None,
        "stderr_sha256": None,
        "stdout_bytes_observed": 0,
        "stderr_bytes_observed": 0,
        "stdout_truncated": False,
        "stderr_truncated": False,
        "process_group_isolated": False,
        "process_reaped": False,
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
    capture_max_bytes: int = DEFAULT_CAPTURE_MAX_BYTES,
) -> BackendAttempt:
    """Run the committed bridge once and accept only strict request-bound JSON."""
    layout_contracts.require_layout_request(request)
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be a positive number")
    capture_max_bytes = _capture_limit(capture_max_bytes)
    node = Path(node).expanduser().resolve()
    evidence = _base_evidence(
        request,
        node=node,
        timeout_seconds=float(timeout_seconds),
        capture_max_bytes=capture_max_bytes,
    )
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
    try:
        completed = _run_bounded_process(
            command,
            input_bytes=canonical_json_bytes(bridge_request),
            timeout_seconds=float(timeout_seconds),
            capture_max_bytes=capture_max_bytes,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            environ=os.environ,
        )
    except OSError as exc:
        stdout_path.write_bytes(b"")
        stderr_path.write_text(str(exc), encoding="utf-8")
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

    evidence.update(
        {
            "exit_code": completed.returncode,
            "stdout_path": str(stdout_path),
            "stdout_sha256": file_sha256(stdout_path),
            "stderr_path": str(stderr_path),
            "stderr_sha256": file_sha256(stderr_path),
            "stdout_bytes_observed": completed.stdout_bytes_observed,
            "stderr_bytes_observed": completed.stderr_bytes_observed,
            "stdout_truncated": completed.stdout_truncated,
            "stderr_truncated": completed.stderr_truncated,
            "process_group_isolated": completed.process_group_isolated,
            "process_reaped": completed.process_reaped,
        }
    )
    if completed.limit_stream is not None:
        reason = f"elk_capture_limit_exceeded_{completed.limit_stream}"
        evidence["fallback_reason"] = reason
        raise BackendExecutionError(reason, evidence)
    if completed.timed_out:
        evidence["fallback_reason"] = "elk_timeout"
        raise BackendExecutionError("elk_timeout", evidence)
    if evidence["exit_code"] != 0:
        evidence["fallback_reason"] = "elk_nonzero_exit"
        raise BackendExecutionError("elk_nonzero_exit", evidence)
    stdout_value = _capture_text(stdout_path, capture_max_bytes)
    stderr_value = _capture_text(stderr_path, capture_max_bytes)
    if stderr_value.strip():
        evidence["fallback_reason"] = "elk_stderr_nonempty"
        raise BackendExecutionError("elk_stderr_nonempty", evidence)
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
        "capture_max_bytes": None,
        "exit_code": None,
        "stdout_path": None,
        "stdout_sha256": None,
        "stderr_path": None,
        "stderr_sha256": None,
        "stdout_bytes_observed": 0,
        "stderr_bytes_observed": 0,
        "stdout_truncated": False,
        "stderr_truncated": False,
        "process_group_isolated": False,
        "process_reaped": False,
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
            "capture_max_bytes",
            "exit_code",
            "stdout_path",
            "stdout_sha256",
            "stderr_path",
            "stderr_sha256",
            "stdout_bytes_observed",
            "stderr_bytes_observed",
            "stdout_truncated",
            "stderr_truncated",
            "process_group_isolated",
            "process_reaped",
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
    capture_max_bytes = config.get(
        "layout_capture_max_bytes", DEFAULT_CAPTURE_MAX_BYTES
    )
    try:
        attempt = run_elk(
            request,
            node=node,
            timeout_seconds=timeout_seconds,
            capture_max_bytes=capture_max_bytes,
        )
        evidence = dict(attempt.evidence)
        evidence["backend_requested"] = str(backend)
        return BackendAttempt(result=attempt.result, evidence=evidence)
    except BackendExecutionError as exc:
        if backend == "elk":
            raise
        return _python_attempt(
            request,
            requested_backend=str(backend),
            fallback_reason=exc.reason,
            elk_evidence=exc.evidence,
        )
