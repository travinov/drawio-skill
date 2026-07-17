#!/usr/bin/env python3
"""Build and verify the two independent GigaCode skill archives."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "release" / "skills.json"
DIST = ROOT / "dist"
ZIP_TIME = (2020, 1, 1, 0, 0, 0)


class ReleaseError(RuntimeError):
    pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_config(path: Path = DEFAULT_CONFIG) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("format_version") != 1 or not data.get("skills"):
        raise ReleaseError(f"unsupported or empty release config: {path}")
    return data


def selected_skills(config: dict, requested: str) -> list[tuple[str, dict]]:
    if requested == "all":
        return sorted(config["skills"].items())
    try:
        return [(requested, config["skills"][requested])]
    except KeyError as exc:
        raise ReleaseError(f"unknown skill {requested!r}") from exc


def is_forbidden(path: PurePosixPath, forbidden_parts: set[str]) -> bool:
    return any(part in forbidden_parts for part in path.parts)


def resolve_files(root: Path, patterns: list[str], forbidden: set[str]) -> list[Path]:
    found: dict[str, Path] = {}
    missing = []
    for pattern in patterns:
        matches = sorted(path for path in root.glob(pattern) if path.is_file())
        if not matches:
            missing.append(pattern)
            continue
        for path in matches:
            relative = PurePosixPath(path.relative_to(root).as_posix())
            if is_forbidden(relative, forbidden):
                raise ReleaseError(f"forbidden path matched by allowlist: {relative}")
            found[str(relative)] = path
    if missing:
        raise ReleaseError("allowlist pattern(s) matched nothing: " + ", ".join(missing))
    return [found[key] for key in sorted(found)]


def release_files(source: Path, spec: dict, forbidden: set[str]) -> list[tuple[Path, str]]:
    """Resolve source allowlist plus explicitly mapped repo files."""
    mapped: dict[str, Path] = {
        path.relative_to(source).as_posix(): path
        for path in resolve_files(source, spec["include"], forbidden)
    }
    for item in spec.get("extra_files", []):
        source_path = PurePosixPath(item["source"])
        destination = PurePosixPath(item["destination"])
        if (
            source_path.is_absolute()
            or ".." in source_path.parts
            or "\\" in item["source"]
            or re.match(r"^[A-Za-z]:", item["source"])
        ):
            raise ReleaseError(f"unsafe extra release source: {source_path}")
        path = ROOT.joinpath(*source_path.parts)
        try:
            path.resolve(strict=True).relative_to(ROOT.resolve())
        except (FileNotFoundError, ValueError) as exc:
            raise ReleaseError(f"unsafe extra release source: {source_path}") from exc
        if path.is_symlink():
            raise ReleaseError(f"unsafe extra release source: {source_path}")
        if not path.is_file():
            raise ReleaseError(f"extra release file is missing: {path}")
        if (
            destination.is_absolute()
            or ".." in destination.parts
            or "\\" in item["destination"]
            or re.match(r"^[A-Za-z]:", item["destination"])
            or is_forbidden(destination, forbidden)
        ):
            raise ReleaseError(f"unsafe extra release destination: {destination}")
        key = destination.as_posix()
        if not key or key in mapped:
            raise ReleaseError(f"duplicate extra release destination: {destination}")
        mapped[key] = path
    return [(mapped[key], key) for key in sorted(mapped)]


def nested_json_value(path: Path, key: str) -> str:
    value = json.loads(path.read_text(encoding="utf-8"))
    for part in key.split("."):
        value = value[part]
    return str(value)


def read_versions(source: Path, specs: list[dict]) -> tuple[str, list[dict]]:
    values = []
    for spec in specs:
        path = source / spec["path"]
        if not path.is_file():
            raise ReleaseError(f"version source is missing: {path}")
        if "json_key" in spec:
            value = nested_json_value(path, spec["json_key"])
        else:
            match = re.search(spec["pattern"], path.read_text(encoding="utf-8"))
            if not match:
                raise ReleaseError(f"version pattern did not match: {path}")
            value = match.group(1)
        values.append({"path": spec["path"], "value": value})
    unique = {entry["value"] for entry in values}
    if len(unique) != 1:
        detail = ", ".join(f"{item['path']}={item['value']}" for item in values)
        raise ReleaseError(f"version mismatch: {detail}")
    return next(iter(unique)), values


def run(command: list[str], cwd: Path, *, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True)
    if check and result.returncode:
        output = (result.stdout + result.stderr).strip()
        raise ReleaseError(f"command failed ({result.returncode}): {' '.join(command)}\n{output}")
    return result


def command_version(command: str) -> str | None:
    resolved = shutil.which(command)
    if not resolved and command == "drawio":
        app = Path("/Applications/draw.io.app/Contents/MacOS/draw.io")
        resolved = str(app) if app.is_file() else None
    if not resolved:
        return None
    result = run([resolved, "--version"], ROOT, check=False)
    return (result.stdout + result.stderr).strip() or resolved


def preflight_skill(name: str, spec: dict, *, registry: bool = False) -> dict:
    findings = []
    failed = False
    for package, module in spec.get("python_packages", {}).items():
        try:
            importlib.import_module(module)
            version = importlib.metadata.version(package)
            findings.append({"kind": "python", "name": package, "status": "available", "version": version})
        except (ImportError, importlib.metadata.PackageNotFoundError) as exc:
            failed = True
            findings.append({"kind": "python", "name": package, "status": "missing", "message": str(exc)})
    for command in spec.get("required_commands", []):
        version = command_version(command)
        status = "available" if version else "missing"
        failed |= not bool(version)
        findings.append({"kind": "command", "name": command, "status": status, "version": version})
    for command in spec.get("optional_commands", []):
        version = command_version(command)
        findings.append({"kind": "optional-command", "name": command, "status": "available" if version else "unavailable", "version": version})
    if registry:
        for package in spec.get("registry_packages", []):
            if spec.get("npm_package_dir"):
                result = run(["npm", "view", package, "version"], ROOT / spec["source"], check=False)
                kind = "npm-registry"
            else:
                result = run([sys.executable, "-m", "pip", "index", "versions", package], ROOT, check=False)
                kind = "python-registry"
            status = "available" if result.returncode == 0 else "unavailable"
            failed |= result.returncode != 0
            findings.append({"kind": kind, "name": package, "status": status})
    return {"skill": name, "status": "failed" if failed else "passed", "findings": findings}


def file_records(source: Path, files: list[Path]) -> list[dict]:
    return [
        {
            "path": path.relative_to(source).as_posix(),
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in files
    ]


def release_records(files: list[tuple[Path, str]]) -> list[dict]:
    return [
        {
            "path": destination,
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path, destination in files
    ]


def manifest_text(records: list[dict]) -> str:
    return "".join(f"{record['sha256']}  {record['path']}\n" for record in records)


def zip_info(name: str, executable: bool = False) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, ZIP_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    mode = 0o755 if executable else 0o644
    info.external_attr = (mode & 0xFFFF) << 16
    info.create_system = 3
    return info


def build_skill(name: str, spec: dict, forbidden: set[str], output_dir: Path) -> dict:
    source = ROOT / spec["source"]
    version, version_sources = read_versions(source, spec["version_sources"])
    files = release_files(source, spec, forbidden)
    records = release_records(files)
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / spec["output"]
    archive_root = spec["archive_root"]
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for (path, _), record in zip(files, records):
            relative = record["path"]
            data = path.read_bytes()
            executable = (
                relative.startswith(("scripts/", "install/"))
                and path.suffix in {".py", ".mjs", ".sh"}
            )
            bundle.writestr(zip_info(f"{archive_root}/{relative}", executable), data, compresslevel=9)
        bundle.writestr(zip_info(f"{archive_root}/MANIFEST.sha256"), manifest_text(records).encode("utf-8"), compresslevel=9)
    external_manifest = {
        "format_version": 1,
        "skill": name,
        "version": version,
        "archive": archive.name,
        "archive_root": archive_root,
        "archive_sha256": sha256_file(archive),
        "version_sources": version_sources,
        "files": records,
    }
    manifest_path = output_dir / f"{archive.name}.manifest.json"
    manifest_path.write_text(json.dumps(external_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return external_manifest


def archive_records(bundle: zipfile.ZipFile, archive_root: str, forbidden: set[str]) -> dict[str, str]:
    result = {}
    prefix = f"{archive_root}/"
    for info in bundle.infolist():
        name = PurePosixPath(info.filename)
        if is_forbidden(name, forbidden):
            raise ReleaseError(f"archive contains forbidden path: {name}")
        if not info.filename.startswith(prefix):
            raise ReleaseError(f"archive member is outside root {archive_root}: {name}")
        relative = info.filename[len(prefix):]
        if relative == "MANIFEST.sha256":
            continue
        if not relative or info.is_dir():
            continue
        result[relative] = sha256_bytes(bundle.read(info.filename))
    return result


def verify_skill(name: str, spec: dict, forbidden: set[str], output_dir: Path, *, run_commands: bool = True) -> dict:
    source = ROOT / spec["source"]
    files = release_files(source, spec, forbidden)
    records = release_records(files)
    expected = {record["path"]: record["sha256"] for record in records}
    archive = output_dir / spec["output"]
    if not archive.is_file():
        raise ReleaseError(f"archive is missing: {archive}")
    with zipfile.ZipFile(archive) as bundle:
        bad = bundle.testzip()
        if bad:
            raise ReleaseError(f"corrupt ZIP member: {bad}")
        actual = archive_records(bundle, spec["archive_root"], forbidden)
        if expected != actual:
            missing = sorted(set(expected) - set(actual))
            extra = sorted(set(actual) - set(expected))
            changed = sorted(path for path in set(expected) & set(actual) if expected[path] != actual[path])
            raise ReleaseError(f"archive parity failed: missing={missing}, extra={extra}, changed={changed}")
        internal_manifest_name = f"{spec['archive_root']}/MANIFEST.sha256"
        try:
            internal_manifest = bundle.read(internal_manifest_name).decode("utf-8")
        except KeyError as exc:
            raise ReleaseError(f"archive manifest is missing: {internal_manifest_name}") from exc
        expected_records = [
            {"path": path, "sha256": digest}
            for path, digest in sorted(expected.items())
        ]
        if internal_manifest != manifest_text(expected_records):
            raise ReleaseError("archive MANIFEST.sha256 does not match source files")
        with tempfile.TemporaryDirectory(prefix=f"verify-{name}-") as tmp:
            bundle.extractall(tmp)
            installed = Path(tmp) / spec["archive_root"]
            if run_commands:
                python_executable = None
                if spec.get("python_requirements"):
                    venv = Path(tmp) / "verify-venv"
                    run([sys.executable, "-m", "venv", str(venv)], installed)
                    python_executable = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
                    run(
                        [
                            str(python_executable),
                            "-m",
                            "pip",
                            "install",
                            "--disable-pip-version-check",
                            "--requirement",
                            str(installed / spec["python_requirements"]),
                        ],
                        installed,
                    )
                for command in spec.get("verify_commands", []):
                    resolved_command = [
                        str(python_executable) if part == "{python}" and python_executable else part
                        for part in command
                    ]
                    if "{python}" in resolved_command:
                        raise ReleaseError(f"verify command for {name} requires a configured Python environment")
                    run(resolved_command, installed)
    archive_digest = sha256_file(archive)
    external_manifest_path = output_dir / f"{archive.name}.manifest.json"
    if not external_manifest_path.is_file():
        raise ReleaseError(f"external archive manifest is missing: {external_manifest_path}")
    external_manifest = json.loads(external_manifest_path.read_text(encoding="utf-8"))
    version, version_sources = read_versions(source, spec["version_sources"])
    expected_manifest = {
        "format_version": 1,
        "skill": name,
        "version": version,
        "archive": archive.name,
        "archive_root": spec["archive_root"],
        "archive_sha256": archive_digest,
        "version_sources": version_sources,
        "files": records,
    }
    if external_manifest != expected_manifest:
        changed = sorted(
            key
            for key in set(external_manifest) | set(expected_manifest)
            if external_manifest.get(key) != expected_manifest.get(key)
        )
        raise ReleaseError(f"external archive manifest does not match release inputs: {changed}")
    checksum_path = output_dir / f"{archive.name}.sha256"
    expected_checksum_line = f"{archive_digest}  {archive.name}\n"
    if not checksum_path.is_file() or checksum_path.read_text(encoding="utf-8") != expected_checksum_line:
        raise ReleaseError(f"archive checksum record is missing or invalid: {checksum_path}")
    return {"skill": name, "archive": archive.name, "sha256": archive_digest, "files": len(expected), "status": "passed"}


def write_checksums(config: dict, output_dir: Path) -> None:
    lines = []
    for _, spec in sorted(config["skills"].items()):
        archive = output_dir / spec["output"]
        if archive.is_file():
            checksum = sha256_file(archive)
            lines.append(f"{checksum}  {archive.name}\n")
            (output_dir / f"{archive.name}.sha256").write_text(f"{checksum}  {archive.name}\n", encoding="utf-8")
    (output_dir / "SHA256SUMS.txt").write_text("".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight", "build", "verify", "all"))
    parser.add_argument("--skill", default="all")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=DIST)
    parser.add_argument("--registry", action="store_true", help="also query configured package registries")
    parser.add_argument("--no-smoke", action="store_true", help="verify content without running commands")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        forbidden = set(config["forbidden_parts"])
        reports = []
        skills = selected_skills(config, args.skill)
        if args.action in ("preflight", "all"):
            preflight = [preflight_skill(name, spec, registry=args.registry) for name, spec in skills]
            reports.extend(preflight)
            if any(item["status"] == "failed" for item in preflight):
                raise ReleaseError("dependency preflight failed")
        if args.action in ("build", "all"):
            reports.extend(build_skill(name, spec, forbidden, args.output_dir) for name, spec in skills)
            write_checksums(config, args.output_dir)
        if args.action in ("verify", "all"):
            reports.extend(verify_skill(name, spec, forbidden, args.output_dir, run_commands=not args.no_smoke) for name, spec in skills)
        payload = {"status": "passed", "reports": reports}
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "release-report.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    except (OSError, KeyError, ValueError, ReleaseError, zipfile.BadZipFile) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
