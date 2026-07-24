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


def layout_pipeline_checks():
    """Exercise the deterministic layout contract without relying on test files.

    Node/ELK is optional for installation, but it is never an omitted pipeline:
    an unavailable verified Node is a successful Python-fallback observation.
    """
    import stat
    import xml.etree.ElementTree as ET

    import diagram_orchestrator
    import diagram_supervisor
    import layout_backend
    import lifecycle_host_v2
    import layout_model
    import validate
    from layout_renderer import render_layout
    from lifecycle_contracts import canonical_json_sha256

    sha = "a" * 64
    def plan(title, *, direction="LR"):
        page_id = "self-check-layout"
        nodes = [
            {"stable_identity": {"page_id": page_id, "cell_id": "source"}, "label": "Source", "semantic_type": "process", "parent": None, "style_hint": None},
            {"stable_identity": {"page_id": page_id, "cell_id": "middle"}, "label": "Middle", "semantic_type": "process", "parent": None, "style_hint": None},
            {"stable_identity": {"page_id": page_id, "cell_id": "target"}, "label": "Target", "semantic_type": "process", "parent": None, "style_hint": None},
        ]
        edges = [
            {"stable_identity": {"page_id": page_id, "cell_id": "source-middle"}, "source": {"page_id": page_id, "cell_id": "source"}, "target": {"page_id": page_id, "cell_id": "middle"}, "label": "", "relationship": "flow", "parent": None, "style_hint": None},
            {"stable_identity": {"page_id": page_id, "cell_id": "middle-target"}, "source": {"page_id": page_id, "cell_id": "middle"}, "target": {"page_id": page_id, "cell_id": "target"}, "label": "", "relationship": "flow", "parent": None, "style_hint": None},
        ]
        return {
            "schema_version": 2, "role": "semantic_analyst", "status": "ok",
            "run_id": "self-check-layout", "source_bundle_sha256": sha,
            "baseline_semantic_digest": sha,
            "result": {
                "mode": "create", "diagram_type": "flowchart", "title": title,
                "direction": direction, "pages": [{"page_id": page_id, "name": title, "nodes": nodes, "edges": edges}],
                "semantic_delta": {"schema_version": 2, "baseline_semantic_digest": sha, "source_bundle_sha256": sha, "operations": []},
                "assumptions": [], "requires_human": False, "human_questions": [],
            },
        }

    def request(value, *, backend="python", baseline=None, mode="create", scope=None):
        return layout_model.build_layout_request(
            value, run_id="self-check-layout", semantic_plan_sha256=canonical_json_sha256(value),
            mode=mode, backend=backend, strategy_id="layered", quality_profile_version=2,
            baseline=baseline, scope=scope,
        )

    def render_and_report(value, attempt, path):
        render_layout(value, attempt.result, path)
        return validate.validate_tree(ET.parse(path))

    records = []
    value = plan("Self-check layout")
    with tempfile.TemporaryDirectory(prefix="drawio-layout-self-check-") as temporary:
        temp = Path(temporary)
        try:
            python_attempt = layout_backend.run_layout(request(value), config={"layout_backend": "python"})
            python_artifact = temp / "python.drawio"
            python_report = render_and_report(value, python_attempt, python_artifact)
            if python_report["summary"]["errors"]:
                raise RuntimeError(f"validator errors: {python_report['findings']}")
            records.append(check_record("layout:python-create", "passed", "selfcheck.layout.python_create.passed", "Python fallback created and validated an artifact", backend=python_attempt.result["backend"]))
        except Exception as exc:
            records.append(check_record("layout:python-create", "failed", "selfcheck.layout.python_create.failed", str(exc)))
            return records

        try:
            node = layout_backend.resolve_node({}, environ=os.environ)
            automatic = layout_backend.run_layout(request(value, backend="auto"), config={"layout_backend": "auto"})
            if node is None:
                if automatic.result["backend"] != "python-layered" or automatic.evidence.get("fallback_reason") != "verified_node_unavailable":
                    raise RuntimeError("Node absence did not take the verified Python fallback")
                records.append(check_record("layout:elk-create", "passed", "selfcheck.layout.elk_unavailable_fallback.passed", "Node/ELK unavailable; verified Python fallback completed", backend=automatic.result["backend"]))
            else:
                elk_artifact = temp / "elk.drawio"
                elk_report = render_and_report(value, automatic, elk_artifact)
                if not automatic.result["backend"].startswith("elk-") or elk_report["summary"]["errors"]:
                    raise RuntimeError("verified Node did not complete a valid ELK layout")
                records.append(check_record("layout:elk-create", "passed", "selfcheck.layout.elk_create.passed", "ELK created and validated an artifact", backend=automatic.result["backend"]))
        except Exception as exc:
            records.append(check_record("layout:elk-create", "failed", "selfcheck.layout.elk_create.failed", str(exc)))

        try:
            fake_node = temp / "failing-node"
            fake_node.write_text(
                "#!" + sys.executable + "\nimport sys\n"
                "if sys.argv[-1:] == ['--version']: print('v22.16.0')\n"
                "elif sys.argv[-1:] == ['--probe']: print('{\\\"bridge\\\":\\\"drawio-elk-runner\\\",\\\"elkjs_version\\\":\\\"0.11.1\\\"}')\n"
                "else: raise SystemExit(2)\n",
                encoding="utf-8",
            )
            fake_node.chmod(fake_node.stat().st_mode | stat.S_IXUSR)
            fallback = layout_backend.run_layout(request(value, backend="auto"), config={"layout_backend": "auto", "node_bin": str(fake_node)})
            if fallback.result["backend"] != "python-layered" or fallback.evidence.get("fallback_reason") != "elk_nonzero_exit":
                raise RuntimeError("forced ELK failure did not select Python fallback")
            records.append(check_record("layout:elk-failure-fallback", "passed", "selfcheck.layout.elk_failure_fallback.passed", "Forced ELK failure selected Python fallback"))
        except Exception as exc:
            records.append(check_record("layout:elk-failure-fallback", "failed", "selfcheck.layout.elk_failure_fallback.failed", str(exc)))

        try:
            baseline = diagram_supervisor.make_spec(python_artifact)
            page_id = "self-check-layout"
            scope = {
                "edge_refs": [{"page_id": page_id, "cell_id": "middle-target"}],
                "reroutable_edge_refs": [{"page_id": page_id, "cell_id": "middle-target"}],
            }
            local_attempt = layout_backend.run_layout(
                request(value, baseline=baseline, mode="local_reflow", scope=scope),
                config={"layout_backend": "python"},
            )
            local_artifact = temp / "local.drawio"
            render_layout(value, local_attempt.result, local_artifact)
            before_digest, _ = diagram_supervisor.artifact_invariants(python_artifact)
            after_digest, _ = diagram_supervisor.artifact_invariants(local_artifact)
            preservation = diagram_orchestrator._verify_locked_cell_hashes(
                python_artifact, local_artifact,
                {page_id: ["source", "middle", "target", "source-middle"]},
            )
            if before_digest != after_digest or not preservation["valid"]:
                raise RuntimeError("local improve changed semantic content or an untouched cell")
            records.append(check_record("layout:local-improve", "passed", "selfcheck.layout.local_improve.passed", "Local improve preserved semantic digest and untouched hashes"))
        except Exception as exc:
            records.append(check_record("layout:local-improve", "failed", "selfcheck.layout.local_improve.failed", str(exc)))

        try:
            workspace = temp / "best-effort-workspace"
            run_id = "self-check-strict-best-effort"
            run_dir = workspace / ".diagram-runs" / run_id
            requested = workspace / "requested.drawio"
            published = workspace / "requested.best-effort.drawio"
            workspace.mkdir()
            lifecycle_host_v2.initialize(
                run_dir=run_dir,
                workspace=workspace,
                target=requested,
                run_id=run_id,
                mode="create",
                request="retain this strict-failing candidate as safe best effort",
                extension_root=ROOT,
            )
            (run_dir / ".run-id").write_text(run_id + "\n", encoding="utf-8")
            candidate = run_dir / "strict-failure.drawio"
            candidate.write_text(
                "<mxfile><diagram id='p' name='routes'><mxGraphModel><root>"
                "<mxCell id='0'/><mxCell id='1' parent='0'/>"
                "<mxCell id='left' parent='1' vertex='1'><mxGeometry x='0' y='100' width='20' height='20' as='geometry'/></mxCell>"
                "<mxCell id='right' parent='1' vertex='1'><mxGeometry x='200' y='100' width='20' height='20' as='geometry'/></mxCell>"
                "<mxCell id='top' parent='1' vertex='1'><mxGeometry x='100' y='0' width='20' height='20' as='geometry'/></mxCell>"
                "<mxCell id='bottom' parent='1' vertex='1'><mxGeometry x='100' y='200' width='20' height='20' as='geometry'/></mxCell>"
                "<mxCell id='middle' parent='1' vertex='1'><mxGeometry x='95' y='90' width='30' height='40' as='geometry'/></mxCell>"
                "<mxCell id='horizontal' parent='1' source='left' target='right' edge='1'><mxGeometry relative='1' as='geometry'><Array as='points'><mxPoint x='100' y='110'/></Array></mxGeometry></mxCell>"
                "<mxCell id='vertical' parent='1' source='top' target='bottom' edge='1'><mxGeometry relative='1' as='geometry'><Array as='points'><mxPoint x='110' y='120'/></Array></mxGeometry></mxCell>"
                "</root></mxGraphModel></diagram></mxfile>", encoding="utf-8",
            )
            legacy = diagram_supervisor.run_validation(
                candidate, run_dir, attempt_id="strict-candidate",
            )
            report_path = (
                run_dir / "attempts" / "strict-candidate"
                / "validation-report.json"
            )
            legacy_receipt_path = (
                run_dir / "attempts" / "strict-candidate"
                / "validation-receipt.json"
            )
            receipt, receipt_path = lifecycle_host_v2.mirror_validation_receipt(
                run_dir, legacy_receipt_path=legacy_receipt_path,
            )
            lifecycle_host_v2.transition(
                run_dir,
                "final_review",
                accepted_artifact=lifecycle_host_v2.make_file_descriptor(
                    candidate, root=run_dir,
                ),
                validation_report=lifecycle_host_v2.make_file_descriptor(
                    report_path, root=run_dir,
                ),
                validation_receipt=lifecycle_host_v2.make_file_descriptor(
                    receipt_path, root=run_dir,
                ),
            )
            classification = lifecycle_host_v2.verify_best_effort_candidate(
                run_dir,
                artifact=candidate,
                report=report_path,
                receipt=receipt_path,
                require_accepted_binding=True,
            )
            publication = lifecycle_host_v2.publish_transaction(
                run_dir,
                accepted_artifact=candidate,
                validation_report=report_path,
                validation_receipt=receipt_path,
                unresolved_findings=[
                    {"source": "validator", "finding": finding}
                    for finding in classification["findings"]
                ],
                decision="best_effort",
                target_override=published,
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))
            candidate_sha256 = diagram_supervisor.sha256_file(candidate)
            bound_hashes = {
                candidate_sha256,
                report["artifact_sha256"],
                receipt["bindings"]["candidate_sha256"],
                diagram_supervisor.sha256_file(published),
            }
            if (
                legacy["result"] != "failed"
                or classification["strict_passed"]
                or not classification["safe"]
                or publication["status"] != "committed"
                or len(bound_hashes) != 1
                or requested.exists()
            ):
                raise RuntimeError(
                    "strict-failed candidate was not safely hash-bound to "
                    "its separate best-effort publication"
                )
            records.append(check_record(
                "layout:strict-best-effort",
                "passed",
                "selfcheck.layout.strict_best_effort.passed",
                "The same strict-failed candidate was safely published as hash-bound best effort",
                candidate_sha256=candidate_sha256,
                publication_status=publication["status"],
                strict_passed=False,
            ))
        except Exception as exc:
            records.append(check_record("layout:strict-best-effort", "failed", "selfcheck.layout.strict_best_effort.failed", str(exc)))
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
            checks.extend(layout_pipeline_checks())
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
