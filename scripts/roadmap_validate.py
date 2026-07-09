#!/usr/bin/env python3
"""Validate roadmap YAML and calculate baseline milestone deltas.

Usage:
  python3 roadmap_validate.py roadmap.yaml [--strict] [--json]
"""
import argparse
import datetime as dt
import json
import os
import sys

import jsonschema
import yaml


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA_PATH = os.path.join(ROOT, "data", "roadmap.schema.json")


def load_yaml(path):
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if data is None:
        return {}
    return normalize_scalars(data)


def normalize_scalars(value):
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, list):
        return [normalize_scalars(item) for item in value]
    if isinstance(value, dict):
        return {key: normalize_scalars(item) for key, item in value.items()}
    return value


def load_schema():
    with open(SCHEMA_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def parse_date(value):
    return dt.date.fromisoformat(str(value))


def days_between(a, b):
    return (parse_date(b) - parse_date(a)).days


def _collect(items, kind, errors):
    by_id = {}
    for index, item in enumerate(items or []):
        if not isinstance(item, dict):
            errors.append(f"{kind} at index {index} must be an object")
            continue
        iid = item.get("id")
        if not iid:
            errors.append(f"{kind} at index {index} is missing id")
            continue
        if iid in by_id:
            errors.append(f"duplicate id {iid!r} in {kind}")
        by_id[iid] = item
    return by_id


def calculate_milestone_deltas(model):
    threshold = int(model.get("shift_threshold_days", 0) or 0)
    current = {m["id"]: m for m in model.get("milestones", [])}
    baseline = {m["id"]: m for m in model.get("baseline", {}).get("milestones", [])}
    deltas = []
    for mid in sorted(set(current) | set(baseline)):
        cur = current.get(mid)
        old = baseline.get(mid)
        if cur and not old:
            deltas.append({"id": mid, "state": "added", "current_date": cur["date"]})
        elif old and not cur:
            deltas.append({"id": mid, "state": "removed", "baseline_date": old["date"]})
        else:
            delta = days_between(old["date"], cur["date"])
            if abs(delta) <= threshold:
                state = "unchanged"
            elif delta > 0:
                state = "delayed"
            else:
                state = "accelerated"
            deltas.append({
                "id": mid,
                "state": state,
                "days": delta,
                "baseline_date": old["date"],
                "current_date": cur["date"],
            })
    return deltas


def validate_model(model, strict=False):
    errors, warnings = [], []
    try:
        jsonschema.Draft202012Validator(load_schema()).validate(model)
    except jsonschema.ValidationError as exc:
        path = ".".join(str(p) for p in exc.absolute_path) or "<root>"
        errors.append(f"schema {path}: {exc.message}")

    lanes = _collect(model.get("lanes", []), "lane", errors)
    tasks = _collect(model.get("tasks", []), "task", errors)
    milestones = _collect(model.get("milestones", []), "milestone", errors)
    outcomes = _collect(model.get("outcomes", []), "outcome", errors)
    dependencies = _collect(model.get("dependencies", []), "dependency", errors)
    refs = set(tasks) | set(milestones)

    if not lanes:
        warnings.append("no lanes defined; generator will create a default lane")

    for task in model.get("tasks", []) or []:
        if task.get("lane") and task["lane"] not in lanes:
            errors.append(f"task {task['id']!r} references unknown lane {task['lane']!r}")
        if task.get("start") and task.get("end") and parse_date(task["end"]) < parse_date(task["start"]):
            errors.append(f"task {task['id']!r} ends before it starts")
        for mid in task.get("milestones", []) or []:
            if mid not in milestones:
                errors.append(f"task {task['id']!r} references unknown milestone {mid!r}")
        for oid in task.get("outcomes", []) or []:
            if oid not in outcomes:
                errors.append(f"task {task['id']!r} references unknown outcome {oid!r}")

    for milestone in model.get("milestones", []) or []:
        if milestone.get("lane") and milestone["lane"] not in lanes:
            errors.append(f"milestone {milestone['id']!r} references unknown lane {milestone['lane']!r}")
        for oid in milestone.get("outcomes", []) or []:
            if oid not in outcomes:
                errors.append(f"milestone {milestone['id']!r} references unknown outcome {oid!r}")

    for dep in model.get("dependencies", []) or []:
        if dep.get("from") not in refs:
            errors.append(f"dependency {dep.get('id')!r} references unknown source {dep.get('from')!r}")
        if dep.get("to") not in refs:
            errors.append(f"dependency {dep.get('id')!r} references unknown target {dep.get('to')!r}")
        if dep.get("from") == dep.get("to"):
            errors.append(f"dependency {dep.get('id')!r} cannot target itself")

    baseline = model.get("baseline") or {}
    _collect(baseline.get("tasks", []), "baseline task", errors)
    baseline_milestones = _collect(baseline.get("milestones", []), "baseline milestone", errors)
    _collect(baseline.get("outcomes", []), "baseline outcome", errors)
    _collect(baseline.get("dependencies", []), "baseline dependency", errors)
    for mid, milestone in baseline_milestones.items():
        if milestone.get("lane") and milestone["lane"] not in lanes:
            warnings.append(f"baseline milestone {mid!r} references lane {milestone['lane']!r} not present in current lanes")

    deltas = []
    if baseline.get("milestones"):
        try:
            deltas = calculate_milestone_deltas(model)
        except ValueError as exc:
            errors.append(f"baseline delta calculation failed: {exc}")
        changed = [d for d in deltas if d["state"] in ("delayed", "accelerated", "added", "removed")]
        if len(changed) > 12:
            warnings.append("many milestone shifts; diagram may need filtering or a larger time scale")

    if len(model.get("dependencies", []) or []) > 16:
        warnings.append("many dependencies; diagram may have excessive crossings")
    if len(model.get("milestones", []) or []) > 24:
        warnings.append("many milestones; diagram may be overcrowded")

    if strict and warnings:
        errors.extend(warnings)
        warnings = []
    return errors, warnings, deltas


def main(argv=None):
    ap = argparse.ArgumentParser(description="Validate roadmap YAML.")
    ap.add_argument("input", help="roadmap YAML file")
    ap.add_argument("--strict", action="store_true", help="treat warnings as errors")
    ap.add_argument("--json", action="store_true", help="print machine-readable result")
    args = ap.parse_args(argv)
    try:
        model = load_yaml(args.input)
        errors, warnings, deltas = validate_model(model, strict=args.strict)
    except (OSError, yaml.YAMLError, ValueError, jsonschema.SchemaError) as exc:
        sys.exit(f"error: cannot validate {args.input}: {exc}")

    if args.json:
        print(json.dumps({"errors": errors, "warnings": warnings, "deltas": deltas}, indent=2, sort_keys=True))
    else:
        for warning in warnings:
            print(f"warning: {warning}")
        for error in errors:
            print(f"error: {error}")
        print(f"{len(errors)} error(s), {len(warnings)} warning(s)")
        if deltas:
            counts = {}
            for delta in deltas:
                counts[delta["state"]] = counts.get(delta["state"], 0) + 1
            print("deltas: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
