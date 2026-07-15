#!/usr/bin/env python3
"""Copy the bundled roadmap intake template without ever editing the asset."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
ASSET_DIR = ROOT / "assets" / "roadmap"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def destination_path(value: str | None, format_name: str) -> Path:
    name = f"roadmap-template.{format_name}"
    if value is None:
        return (Path.cwd() / name).resolve()
    candidate = Path(value).expanduser()
    if candidate.exists() and candidate.is_dir():
        candidate = candidate / name
    elif not candidate.suffix:
        candidate = candidate / name
    elif candidate.suffix.lower() in (".xlsx", ".csv") and candidate.suffix.lower() != f".{format_name}":
        raise ValueError(f"destination suffix {candidate.suffix!r} does not match --format {format_name!r}")
    return candidate.resolve()


def copy_template(destination: Path, format_name: str, force: bool = False) -> dict:
    source = (ASSET_DIR / f"roadmap-template.{format_name}").resolve()
    if not source.is_file():
        raise FileNotFoundError(f"bundled template is missing: {source}")
    if destination == source:
        raise ValueError("destination cannot be the bundled template")
    if destination.exists() and not force:
        raise FileExistsError(f"destination already exists: {destination}; use --force to replace it")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    os.close(fd)
    temporary_path = Path(temporary)
    try:
        shutil.copyfile(source, temporary_path)
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)
    return {
        "status": "copied",
        "format": format_name,
        "path": str(destination),
        "source": str(source),
        "sha256": sha256(destination),
        "bytes": destination.stat().st_size,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Copy a bundled roadmap XLSX/CSV template into a working directory.")
    parser.add_argument("destination", nargs="?", help="output directory or file path; default is the current directory")
    parser.add_argument("--format", choices=("xlsx", "csv"), default="xlsx", help="template format; default: xlsx")
    parser.add_argument("--force", action="store_true", help="replace an existing working copy")
    parser.add_argument("--json", action="store_true", help="print a machine-readable result")
    args = parser.parse_args(argv)
    try:
        destination = destination_path(args.destination, args.format)
        result = copy_template(destination, args.format, force=args.force)
    except (OSError, ValueError) as exc:
        if args.json:
            print(json.dumps({"status": "failed", "code": "roadmap.template.copy_failed", "message": str(exc)}, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"copied {result['format']} roadmap template to {result['path']}")
        print(f"sha256: {result['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
