#!/usr/bin/env python3
"""Capture the exact local implementation and routing policy used by a v2 run."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from lifecycle_contracts import contained_path, file_sha256, require_valid_contract


REQUIRED_COMPONENTS = (
    "orchestration_host",
    "review_host",
    "supervisor_toolchain",
    "role_runtime",
    "validator",
    "role_prompts",
    "output_schemas",
    "routing_policy",
    "extension_manifest",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def hashed_file(extension_root: Path, path: Path | str) -> dict[str, object]:
    resolved = contained_path(extension_root, path)
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    return {
        "path": resolved.relative_to(extension_root.resolve()).as_posix(),
        "sha256": file_sha256(resolved),
        "byte_length": resolved.stat().st_size,
    }


def default_component_paths(extension_root: Path | str) -> dict[str, list[Path]]:
    root = Path(extension_root).resolve()
    return {
        "orchestration_host": [
            root / "scripts" / "diagram_orchestrator.py",
            root / "scripts" / "lifecycle_host_v2.py",
            root / "scripts" / "command_ux.py",
        ],
        "review_host": [root / "scripts" / "diagram_host.py"],
        "supervisor_toolchain": [
            root / "scripts" / "diagram_supervisor.py",
            root / "scripts" / "lifecycle_contracts.py",
            root / "scripts" / "source_bundle_v2.py",
            root / "scripts" / "diagram_model_v2.py",
            root / "scripts" / "run_lock_v2.py",
            root / "scripts" / "evidence_v2.py",
            root / "scripts" / "implementation_snapshot_v2.py",
            root / "scripts" / "renderer_adapters.py",
            root / "scripts" / "layout_contracts.py",
            root / "scripts" / "layout_model.py",
            root / "scripts" / "layout_backend.py",
            root / "scripts" / "layout_builtin.py",
            root / "scripts" / "layout_renderer.py",
            root / "scripts" / "elk_runner.mjs",
            root / "vendor" / "elkjs" / "elk.bundled.js",
        ],
        "role_runtime": [root / "scripts" / "agent_runtime.py"],
        "validator": [root / "scripts" / "validate.py"],
        "role_prompts": sorted((root / "agents").glob("*.md")),
        "output_schemas": sorted((root / "data").glob("*.schema.json")),
        "routing_policy": [root / "data" / "model-routing.default.json"],
        "extension_manifest": [root / "gemini-extension.json"],
    }


def capture_implementation_snapshot(
    *,
    extension_root: Path | str,
    run_id: str,
    snapshot_id: str,
    transaction_id: str,
    component_paths: dict[str, Iterable[Path | str]] | None = None,
    captured_at: str | None = None,
    previous_snapshot_sha256: str | None = None,
) -> dict[str, object]:
    root = Path(extension_root).resolve()
    manifest_path = root / "gemini-extension.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    paths = component_paths or default_component_paths(root)
    missing_groups = sorted(set(REQUIRED_COMPONENTS) - set(paths))
    extra_groups = sorted(set(paths) - set(REQUIRED_COMPONENTS))
    if missing_groups or extra_groups:
        raise ValueError({"missing_component_groups": missing_groups, "unexpected_component_groups": extra_groups})
    components = {}
    for name in REQUIRED_COMPONENTS:
        files = [hashed_file(root, path) for path in paths[name]]
        if not files:
            raise ValueError(f"implementation component {name} has no files")
        components[name] = sorted(files, key=lambda item: str(item["path"]))
    validator_descriptor = components["validator"][0]
    validator_source = (root / str(validator_descriptor["path"])).read_text(encoding="utf-8")
    version_match = re.search(r'^VALIDATOR_VERSION\s*=\s*["\']([^"\']+)["\']', validator_source, re.MULTILINE)
    if version_match is None:
        raise ValueError("trusted validator version constant is missing")
    document = {
        "schema_version": 2,
        "snapshot_id": snapshot_id,
        "run_id": run_id,
        "extension_name": manifest["name"],
        "extension_version": manifest["version"],
        "captured_at": captured_at or utc_now(),
        "components": components,
        "trusted_validator": {
            "name": "publish-drawio-validator",
            "version": version_match.group(1),
            "path": validator_descriptor["path"],
            "file_sha256": validator_descriptor["sha256"],
        },
        "transaction_id": transaction_id,
        "previous_snapshot_sha256": previous_snapshot_sha256,
    }
    require_valid_contract(document, "implementation-snapshot", 2)
    return document


def verify_implementation_snapshot(
    snapshot: dict[str, object],
    *,
    extension_root: Path | str,
) -> list[dict[str, str]]:
    diagnostics: list[dict[str, str]] = []
    try:
        require_valid_contract(snapshot, "implementation-snapshot", 2)
    except Exception as exc:
        details = getattr(exc, "diagnostics", [getattr(exc, "as_dict", lambda: {"code": "snapshot.schema_invalid", "pointer": "", "message": str(exc)})()])
        return list(details)
    root = Path(extension_root).resolve()
    for group, files in snapshot["components"].items():
        for index, descriptor in enumerate(files):
            pointer = f"/components/{group}/{index}"
            try:
                path = contained_path(root, descriptor["path"])
                current_length = path.stat().st_size
                current_hash = file_sha256(path)
            except (OSError, ValueError) as exc:
                diagnostics.append({"code": "implementation.file_unavailable", "pointer": f"{pointer}/path", "message": str(exc)})
                continue
            if current_length != descriptor["byte_length"]:
                diagnostics.append({"code": "implementation.byte_length_changed", "pointer": f"{pointer}/byte_length", "message": "installed file byte length differs from run snapshot"})
            if current_hash != descriptor["sha256"]:
                diagnostics.append({"code": "implementation.hash_changed", "pointer": f"{pointer}/sha256", "message": "installed file hash differs from run snapshot"})
    return diagnostics
