#!/usr/bin/env python3
"""Run a local installation and minimal generation self-check.

Registry resolution is opt-in because it may contact the package source already
configured for pip. The command never adds or changes an index URL.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = ROOT / "requirements.txt"
DEPENDENCIES = {
    "PyYAML": {
        "requirement": "PyYAML>=6.0,<7",
        "supported": lambda parts: bool(parts) and parts[0] == 6,
    },
    "jsonschema": {
        "requirement": "jsonschema>=4.18,<5",
        "supported": lambda parts: len(parts) >= 2 and parts[0] == 4 and parts[1] >= 18,
    },
    "openpyxl": {
        "requirement": "openpyxl>=3.1,<4",
        "supported": lambda parts: len(parts) >= 2 and parts[0] == 3 and parts[1] >= 1,
    },
}


def version_parts(value: str):
    match = re.match(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?", value)
    return tuple(int(part or 0) for part in match.groups(default="0")) if match else ()


def remediation():
    return f"{sys.executable} -m pip install -r {REQUIREMENTS}"


def check_record(name: str, status: str, code: str, message: str, **details):
    record = {"name": name, "status": status, "code": code, "message": message}
    record.update(details)
    return record


def registry_checks():
    records = []
    with tempfile.TemporaryDirectory(prefix="drawio-registry-check-") as temp:
        for name, config in DEPENDENCIES.items():
            command = [
                sys.executable,
                "-m",
                "pip",
                "download",
                "--no-deps",
                "--disable-pip-version-check",
                "--dest",
                temp,
                config["requirement"],
            ]
            proc = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
            if proc.returncode:
                diagnostic = (proc.stderr or proc.stdout or "pip returned no diagnostic output").strip()
                records.append(check_record(
                    f"registry:{name}",
                    "failed",
                    "dependency.registry.unavailable",
                    f"configured pip source cannot resolve {config['requirement']}: {diagnostic[:1200]}",
                    command=command,
                    remediation=remediation(),
                ))
            else:
                records.append(check_record(
                    f"registry:{name}",
                    "passed",
                    "dependency.registry.available",
                    f"configured pip source resolves {config['requirement']}",
                    command=command,
                ))
    return records


def installed_dependency_checks():
    records = []
    all_supported = True
    for name, config in DEPENDENCIES.items():
        try:
            version = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            all_supported = False
            records.append(check_record(
                f"installed:{name}",
                "failed",
                "dependency.installed.missing",
                f"{name} is not installed",
                remediation=remediation(),
            ))
            continue
        if not config["supported"](version_parts(version)):
            all_supported = False
            records.append(check_record(
                f"installed:{name}",
                "failed",
                "dependency.installed.unsupported",
                f"installed {name} {version} is outside {config['requirement']}",
                version=version,
                remediation=remediation(),
            ))
            continue
        records.append(check_record(
            f"installed:{name}",
            "passed",
            "dependency.installed.supported",
            f"installed {name} {version} satisfies {config['requirement']}",
            version=version,
        ))
    return records, all_supported


def schema_checks():
    import jsonschema

    records = []
    paths = sorted((ROOT / "data").glob("*.schema.json"))
    if not paths:
        return [check_record(
            "schemas",
            "failed",
            "schema.bundle.empty",
            f"no bundled schemas found under {ROOT / 'data'}",
        )]
    for path in paths:
        try:
            with path.open(encoding="utf-8") as handle:
                schema = json.load(handle)
            jsonschema.Draft202012Validator.check_schema(schema)
        except (OSError, json.JSONDecodeError, jsonschema.SchemaError) as exc:
            records.append(check_record(
                f"schema:{path.name}",
                "failed",
                "schema.compile.failed",
                f"cannot compile {path.name} as Draft 2020-12: {exc}",
            ))
        else:
            records.append(check_record(
                f"schema:{path.name}",
                "passed",
                "schema.compile.passed",
                f"compiled {path.name} as Draft 2020-12",
            ))
    return records


def run_command(name: str, command: list[str], code: str):
    proc = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
    if proc.returncode:
        diagnostic = (proc.stderr or proc.stdout or "command returned no diagnostic output").strip()
        return check_record(
            name,
            "failed",
            code,
            f"command exited with {proc.returncode}: {diagnostic[:1600]}",
            command=command,
        )
    return check_record(name, "passed", code.replace(".failed", ".passed"), "command completed", command=command)


def minimal_pipeline_checks():
    import yaml

    roadmap = {
        "schema_version": 1,
        "title": "Self-check roadmap",
        "time_scale": "month",
        "lanes": [{"id": "delivery", "title": "Delivery"}],
        "tasks": [{
            "id": "build",
            "title": "Build",
            "lane": "delivery",
            "start": "2026-01-01",
            "end": "2026-01-31",
            "status": "planned",
        }],
        "milestones": [{
            "id": "ready",
            "title": "Ready",
            "lane": "delivery",
            "date": "2026-01-31",
            "status": "planned",
        }],
    }
    gitflow = {
        "schema_version": 1,
        "title": "Self-check git-flow",
        "workflow": "custom",
        "timeMode": "order",
        "branches": [{"id": "main", "label": "main", "kind": "main"}],
        "events": [{"id": "initial", "type": "commit", "branch": "main", "order": 1, "label": "Initial"}],
    }
    records = []
    with tempfile.TemporaryDirectory(prefix="drawio-self-check-") as temp:
        temp_path = Path(temp)
        template_copy = temp_path / "roadmap-template.xlsx"
        template_yaml = temp_path / "roadmap-template.yaml"
        records.append(run_command(
            "template:copy",
            [sys.executable, str(ROOT / "scripts" / "roadmap_template.py"), str(template_copy), "--format", "xlsx"],
            "selfcheck.template_copy.failed",
        ))
        if records[-1]["status"] == "passed":
            records.append(run_command(
                "template:import",
                [sys.executable, str(ROOT / "scripts" / "roadmap_table.py"), str(template_copy), "-o", str(template_yaml), "--strict"],
                "selfcheck.template_import.failed",
            ))
        roadmap_source = temp_path / "roadmap.yaml"
        gitflow_source = temp_path / "gitflow.json"
        roadmap_source.write_text(yaml.safe_dump(roadmap, allow_unicode=True, sort_keys=False), encoding="utf-8")
        gitflow_source.write_text(json.dumps(gitflow, ensure_ascii=False, indent=2), encoding="utf-8")
        profiles = (
            (
                "roadmap",
                roadmap_source,
                ROOT / "scripts" / "roadmap_validate.py",
                ROOT / "scripts" / "roadmap.py",
                [],
            ),
            (
                "gitflow",
                gitflow_source,
                ROOT / "scripts" / "gitflow_validate.py",
                ROOT / "scripts" / "gitflow.py",
                ["--route", "builtin"],
            ),
        )
        for profile, source, validator, generator, generator_options in profiles:
            validation = run_command(
                f"source:{profile}",
                [sys.executable, str(validator), str(source), "--strict", "--json"],
                "selfcheck.source.failed",
            )
            records.append(validation)
            if validation["status"] != "passed":
                continue
            artifact = temp_path / f"{profile}.drawio"
            generation = run_command(
                f"generation:{profile}",
                [sys.executable, str(generator), str(source), "-o", str(artifact), *generator_options],
                "selfcheck.generation.failed",
            )
            records.append(generation)
            if generation["status"] != "passed":
                continue
            records.append(run_command(
                f"artifact:{profile}",
                [sys.executable, str(ROOT / "scripts" / "validate.py"), str(artifact)],
                "selfcheck.artifact.failed",
            ))
    return records


def build_report(check_registry: bool):
    checks = []
    if check_registry:
        checks.extend(registry_checks())
    else:
        checks.append(check_record(
            "registry",
            "skipped",
            "dependency.registry.skipped",
            "registry resolution was not requested; rerun with --check-registry to query configured pip sources",
        ))
    installed, supported = installed_dependency_checks()
    checks.extend(installed)
    if supported:
        checks.extend(schema_checks())
        if not any(item["status"] == "failed" for item in checks):
            checks.extend(minimal_pipeline_checks())
    errors = sum(item["status"] == "failed" for item in checks)
    return {
        "report_version": 1,
        "summary": {
            "status": "failed" if errors else "passed",
            "errors": errors,
            "passed": sum(item["status"] == "passed" for item in checks),
            "skipped": sum(item["status"] == "skipped" for item in checks),
        },
        "checks": checks,
    }


def print_report(report: dict, as_json: bool):
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return
    for item in report["checks"]:
        print(f"{item['status']}: [{item['code']}] {item['message']}")
        if item.get("remediation"):
            print(f"  remediation: {item['remediation']}")
    summary = report["summary"]
    print(f"self-check {summary['status']}: {summary['passed']} passed, {summary['errors']} failed, {summary['skipped']} skipped")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Verify draw.io skill dependencies, schemas, and minimal pipelines.")
    parser.add_argument(
        "--check-registry",
        action="store_true",
        help="query only pip's already-configured package sources via a temporary download; never installs packages",
    )
    parser.add_argument("--json", action="store_true", help="print a machine-readable report")
    args = parser.parse_args(argv)
    report = build_report(args.check_registry)
    print_report(report, args.json)
    return 1 if report["summary"]["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
