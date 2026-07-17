#!/usr/bin/env python3
"""Shared version dispatch and stable validation-report helpers."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import dataclass, field

import jsonschema


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def pointer(parts):
    if not parts:
        return ""
    return "/" + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in parts)


@dataclass
class ValidationReport:
    schema_version: int | None = None
    report_version: int = 1
    findings: list[dict] = field(default_factory=list)
    details: dict = field(default_factory=dict)

    def add(
        self, layer, severity, code, path="", message="", element=None, *,
        elements=None, geometry=None, remediation_class=None,
        reconstructability=None,
    ):
        item = {
            "layer": str(layer),
            "severity": str(severity),
            "code": str(code),
            "path": str(path),
            "message": str(message),
        }
        if element is not None:
            item["element"] = str(element)
        if self.report_version >= 2:
            related = [str(value) for value in (elements or []) if value is not None]
            if element is not None and str(element) not in related:
                related.insert(0, str(element))
            item["elements"] = related
            identity = {
                "layer": item["layer"],
                "code": item["code"],
                "path": item["path"],
                "elements": related,
            }
            occurrence = sum(
                existing.get("_identity_hash") == canonical
                for existing in self.findings
                for canonical in [hashlib.sha256(
                    json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest()]
            )
            identity["occurrence"] = occurrence
            canonical = hashlib.sha256(
                json.dumps({key: value for key, value in identity.items() if key != "occurrence"}, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
            item["_identity_hash"] = canonical
            item["finding_id"] = "finding-" + hashlib.sha256(
                json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()[:20]
            item["remediation_class"] = remediation_class or "manual-review"
            item["reconstructability"] = reconstructability or "unknown"
            if geometry is not None:
                item["geometry"] = geometry
        self.findings.append(item)

    def extend_schema_errors(self, errors):
        for exc in sorted(errors, key=lambda e: (list(e.absolute_path), e.validator or "", e.message)):
            code = f"schema.{exc.validator or 'invalid'}"
            self.add("schema", "error", code, pointer(exc.absolute_path), exc.message)

    def finish(self, strict=False):
        findings = []
        for item in self.findings:
            rendered = dict(item)
            rendered.pop("_identity_hash", None)
            if strict and rendered["severity"] == "warning":
                rendered["severity"] = "error"
            findings.append(rendered)
        severity_rank = {"error": 0, "warning": 1, "info": 2}
        findings.sort(key=lambda f: (
            severity_rank.get(f["severity"], 9), f["path"], f["code"], f.get("element", ""), f["message"]
        ))
        errors = sum(f["severity"] == "error" for f in findings)
        warnings = sum(f["severity"] == "warning" for f in findings)
        result = {
            "report_version": self.report_version,
            "schema_version": self.schema_version,
            "summary": {
                "status": "failed" if errors else "passed",
                "errors": errors,
                "warnings": warnings,
            },
            "findings": findings,
        }
        result.update(self.details)
        return result


def schema_path(kind, version):
    return os.path.join(ROOT, "data", f"{kind}.v{version}.schema.json")


def load_schema(kind, version):
    path = schema_path(kind, version)
    with open(path, encoding="utf-8") as fh:
        schema = json.load(fh)
    jsonschema.Draft202012Validator.check_schema(schema)
    return schema


def dispatch_version(model, kind, report, supported=(1,)):
    """Return a validation copy with an effective version, or None if blocked."""
    if not isinstance(model, dict):
        report.add("schema", "error", "schema.type", "", "top-level value must be an object")
        return None
    version = model.get("schema_version")
    if version is None:
        version = 1
        report.add(
            "schema", "warning", "contract.version.missing", "/schema_version",
            "schema_version is missing; treating input as v1 for this compatibility release",
        )
    if not isinstance(version, int) or isinstance(version, bool) or version not in supported:
        report.schema_version = version if isinstance(version, int) else None
        report.add(
            "schema", "error", "contract.version.unsupported", "/schema_version",
            f"unsupported {kind} schema version {version!r}; supported: {', '.join(map(str, supported))}",
        )
        return None
    report.schema_version = version
    normalized = copy.deepcopy(model)
    normalized["schema_version"] = version
    return normalized


def validate_schema(model, kind, version, report):
    validator = jsonschema.Draft202012Validator(
        load_schema(kind, version), format_checker=jsonschema.FormatChecker()
    )
    errors = list(validator.iter_errors(model))
    report.extend_schema_errors(errors)
    return not errors


def messages(report):
    errors = [f["message"] for f in report["findings"] if f["severity"] == "error"]
    warnings = [f["message"] for f in report["findings"] if f["severity"] == "warning"]
    return errors, warnings


def print_report(report, as_json=False):
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return
    for finding in report["findings"]:
        path = f" {finding['path']}" if finding["path"] else ""
        print(f"{finding['severity']}: [{finding['code']}]{path}: {finding['message']}")
    summary = report["summary"]
    print(f"{summary['errors']} error(s), {summary['warnings']} warning(s)")
