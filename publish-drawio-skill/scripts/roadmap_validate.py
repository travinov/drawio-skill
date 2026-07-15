#!/usr/bin/env python3
"""Validate versioned roadmap YAML and calculate deterministic roadmap deltas."""
from __future__ import annotations

import argparse
import datetime as dt
import sys

import yaml

from validation_common import (
    ValidationReport, dispatch_version, messages, print_report, validate_schema,
)


COLLECTIONS = ("lanes", "outcomes", "tasks", "milestones", "dependencies")


def normalize_scalars(value):
    if isinstance(value, (dt.date, dt.datetime)):
        return value.date().isoformat() if isinstance(value, dt.datetime) else value.isoformat()
    if isinstance(value, list):
        return [normalize_scalars(item) for item in value]
    if isinstance(value, dict):
        return {key: normalize_scalars(item) for key, item in value.items()}
    return value


def load_yaml(path):
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return normalize_scalars(data if data is not None else {})


def parse_date(value):
    return dt.date.fromisoformat(str(value))


def coordinate(item, mode, start=False):
    if mode == "order":
        key = "start_order" if start else "end_order"
        if "order" in item:
            key = "order"
        return item.get(key)
    key = "start" if start else "end"
    if "date" in item:
        key = "date"
    value = item.get(key)
    return parse_date(value) if value is not None else None


def delta_amount(old, current, mode):
    if mode == "order":
        return int(current) - int(old)
    return (current - old).days


def _entity_deltas(kind, current_items, baseline_items, mode, threshold=0):
    current = {item["id"]: item for item in current_items or [] if isinstance(item, dict) and item.get("id")}
    baseline = {item["id"]: item for item in baseline_items or [] if isinstance(item, dict) and item.get("id")}
    deltas = []
    for iid in sorted(set(current) | set(baseline)):
        cur, old = current.get(iid), baseline.get(iid)
        if cur is None:
            deltas.append({"entity": kind, "id": iid, "state": "removed", "baseline": old})
            continue
        if old is None:
            item = {"entity": kind, "id": iid, "state": "added", "current": cur}
            if kind == "milestone":
                item["current_date" if mode != "order" else "current_order"] = (
                    cur.get("date") if mode != "order" else cur.get("order")
                )
            deltas.append(item)
            continue
        if kind == "milestone":
            old_pos = coordinate(old, mode)
            cur_pos = coordinate(cur, mode)
            amount = delta_amount(old_pos, cur_pos, mode)
            state = "unchanged" if abs(amount) <= threshold else ("delayed" if amount > 0 else "accelerated")
            item = {"entity": kind, "id": iid, "state": state, "delta": amount, "days": amount}
            if mode == "order":
                item.update({"baseline_order": old_pos, "current_order": cur_pos})
            else:
                item.update({"baseline_date": old["date"], "current_date": cur["date"]})
            deltas.append(item)
        elif kind == "task":
            old_range = [coordinate(old, mode, True), coordinate(old, mode, False)]
            cur_range = [coordinate(cur, mode, True), coordinate(cur, mode, False)]
            if old_range == cur_range and old == cur:
                state = "unchanged"
            elif old_range != cur_range:
                state = "schedule_changed"
            else:
                state = "changed"
            deltas.append({"entity": kind, "id": iid, "state": state, "baseline": old, "current": cur})
        else:
            state = "unchanged" if old == cur else "changed"
            deltas.append({"entity": kind, "id": iid, "state": state, "baseline": old, "current": cur})
    return deltas


def calculate_deltas(model):
    baseline = model.get("baseline") or {}
    if not baseline:
        return []
    mode = model.get("time_scale", "month")
    threshold = int(model.get("shift_threshold_days", 0) or 0)
    result = []
    result += _entity_deltas("task", model.get("tasks", []), baseline.get("tasks", []), mode)
    result += _entity_deltas("milestone", model.get("milestones", []), baseline.get("milestones", []), mode, threshold)
    result += _entity_deltas("dependency", model.get("dependencies", []), baseline.get("dependencies", []), mode)
    result += _entity_deltas("outcome", model.get("outcomes", []), baseline.get("outcomes", []), mode)
    return sorted(result, key=lambda d: (d["entity"], d["id"]))


def calculate_milestone_deltas(model):
    return [d for d in calculate_deltas(model) if d["entity"] == "milestone"]


def milestone_revisions(milestone, mode):
    """Return v2 history plus current revision in deterministic order."""
    if not isinstance(milestone, dict):
        return []
    coordinate_key = "order" if mode == "order" else "date"
    history = [dict(item) for item in milestone.get("history", []) or [] if isinstance(item, dict)]
    current = {
        "revision_id": milestone.get("revision_id"),
        "revision_order": milestone.get("revision_order"),
        "plan_version": milestone.get("plan_version"),
        coordinate_key: milestone.get(coordinate_key),
        "recorded_at": milestone.get("recorded_at"),
        "reason": milestone.get("reason", ""),
        "is_current": True,
    }
    for item in history:
        item["is_current"] = False
    return sorted(history + [current], key=lambda item: (item.get("revision_order", 0), item.get("revision_id", "")))


def calculate_history_deltas(model):
    """Calculate every consecutive v2 milestone shift; never trust table formulas."""
    if model.get("schema_version") != 2:
        return []
    mode = model.get("time_scale", "month")
    threshold = int(model.get("shift_threshold_days", 0) or 0)
    coordinate_key = "order" if mode == "order" else "date"
    result = []
    for milestone in sorted(model.get("milestones", []) or [], key=lambda item: item.get("id", "")):
        revisions = milestone_revisions(milestone, mode)
        if not revisions:
            continue
        initial = coordinate({coordinate_key: revisions[0].get(coordinate_key)}, mode)
        cumulative = 0
        for previous, current in zip(revisions, revisions[1:]):
            old_value = coordinate({coordinate_key: previous.get(coordinate_key)}, mode)
            new_value = coordinate({coordinate_key: current.get(coordinate_key)}, mode)
            amount = delta_amount(old_value, new_value, mode)
            cumulative = delta_amount(initial, new_value, mode)
            state = "unchanged" if abs(amount) <= threshold else ("delayed" if amount > 0 else "accelerated")
            result.append({
                "entity": "milestone_history",
                "id": milestone["id"],
                "from_revision_id": previous["revision_id"],
                "to_revision_id": current["revision_id"],
                "state": state,
                "delta": amount,
                "days": amount,
                "cumulative_delta": cumulative,
            })
    return result


def _collect(items, kind, base_path, report, global_ids=None):
    by_id = {}
    if not isinstance(items, list):
        return by_id
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        iid = item.get("id")
        if not iid:
            continue
        path = f"{base_path}/{index}/id"
        if iid in by_id:
            report.add("references", "error", "reference.id.duplicate", path, f"duplicate {kind} id {iid!r}", iid)
        if global_ids is not None and iid in global_ids:
            report.add("references", "error", "reference.id.global_duplicate", path, f"id {iid!r} is already used by {global_ids[iid]}", iid)
        by_id[iid] = item
        if global_ids is not None:
            global_ids[iid] = kind
    return by_id


def _reference(report, known, value, path, code, owner):
    if value is not None and value not in known:
        noun = "source" if code.endswith("source_unknown") else "target" if code.endswith("target_unknown") else "id"
        report.add("references", "error", code, path, f"{owner} references unknown {noun} {value!r}", owner)


def _validate_semantics(model, report):
    global_ids = {}
    lanes = _collect(model.get("lanes", []), "lane", "/lanes", report, global_ids)
    outcomes = _collect(model.get("outcomes", []), "outcome", "/outcomes", report, global_ids)
    tasks = _collect(model.get("tasks", []), "task", "/tasks", report, global_ids)
    milestones = _collect(model.get("milestones", []), "milestone", "/milestones", report, global_ids)
    dependencies = _collect(model.get("dependencies", []), "dependency", "/dependencies", report, global_ids)
    if not lanes:
        report.add("semantics", "info", "roadmap.lanes.defaulted", "/lanes", "no lanes defined; generator will create the deterministic default lane")

    mode = model.get("time_scale", "month")
    for i, task in enumerate(model.get("tasks", []) or []):
        if not isinstance(task, dict):
            continue
        owner = task.get("id", f"task[{i}]")
        _reference(report, lanes, task.get("lane"), f"/tasks/{i}/lane", "reference.lane.unknown", owner)
        for j, mid in enumerate(task.get("milestones", []) or []):
            _reference(report, milestones, mid, f"/tasks/{i}/milestones/{j}", "reference.milestone.unknown", owner)
        for j, oid in enumerate(task.get("outcomes", []) or []):
            _reference(report, outcomes, oid, f"/tasks/{i}/outcomes/{j}", "reference.outcome.unknown", owner)
        try:
            start, end = coordinate(task, mode, True), coordinate(task, mode, False)
            if start is not None and end is not None and end < start:
                report.add("semantics", "error", "roadmap.task.range", f"/tasks/{i}", f"task {owner!r} ends before it starts", owner)
        except (TypeError, ValueError):
            pass

    for i, milestone in enumerate(model.get("milestones", []) or []):
        if not isinstance(milestone, dict):
            continue
        owner = milestone.get("id", f"milestone[{i}]")
        _reference(report, lanes, milestone.get("lane"), f"/milestones/{i}/lane", "reference.lane.unknown", owner)
        for j, oid in enumerate(milestone.get("outcomes", []) or []):
            _reference(report, outcomes, oid, f"/milestones/{i}/outcomes/{j}", "reference.outcome.unknown", owner)
        if model.get("schema_version") == 2:
            revisions = milestone_revisions(milestone, mode)
            if revisions and not revisions[-1].get("is_current"):
                report.add("semantics", "error", "roadmap.history.current.not_latest", f"/milestones/{i}/revision_order", "current milestone must have the greatest revision_order", owner)
            seen_ids, seen_orders = set(), set()
            previous_order = None
            previous_recorded = None
            for j, revision in enumerate(revisions):
                path = f"/milestones/{i}/" + (f"history/{j}" if not revision.get("is_current") else "revision_id")
                rid, order = revision.get("revision_id"), revision.get("revision_order")
                if rid in seen_ids:
                    report.add("semantics", "error", "roadmap.history.revision_id.duplicate", path, f"duplicate revision_id {rid!r}", owner)
                if order in seen_orders:
                    report.add("semantics", "error", "roadmap.history.revision_order.duplicate", path, f"duplicate revision_order {order!r}", owner)
                if previous_order is not None and order is not None and order <= previous_order:
                    report.add("semantics", "error", "roadmap.history.revision_order.invalid", path, "revision_order must increase strictly", owner)
                recorded = revision.get("recorded_at")
                try:
                    recorded_date = parse_date(recorded) if recorded else None
                    if previous_recorded and recorded_date and recorded_date < previous_recorded:
                        report.add("semantics", "error", "roadmap.history.recorded_at.order", path, "recorded_at must not move backwards", owner)
                    previous_recorded = recorded_date or previous_recorded
                except (TypeError, ValueError):
                    pass
                seen_ids.add(rid)
                seen_orders.add(order)
                previous_order = order

    refs = set(tasks) | set(milestones)
    for i, dep in enumerate(model.get("dependencies", []) or []):
        if not isinstance(dep, dict):
            continue
        owner = dep.get("id", f"dependency[{i}]")
        _reference(report, refs, dep.get("from"), f"/dependencies/{i}/from", "reference.dependency.source_unknown", owner)
        _reference(report, refs, dep.get("to"), f"/dependencies/{i}/to", "reference.dependency.target_unknown", owner)
        if dep.get("from") is not None and dep.get("from") == dep.get("to"):
            report.add("semantics", "error", "roadmap.dependency.self", f"/dependencies/{i}/to", f"dependency {owner!r} cannot target itself", owner)

    baseline = model.get("baseline") or {}
    b_global = {}
    b_outcomes = _collect(baseline.get("outcomes", []), "baseline outcome", "/baseline/outcomes", report, b_global)
    b_tasks = _collect(baseline.get("tasks", []), "baseline task", "/baseline/tasks", report, b_global)
    b_milestones = _collect(baseline.get("milestones", []), "baseline milestone", "/baseline/milestones", report, b_global)
    _collect(baseline.get("dependencies", []), "baseline dependency", "/baseline/dependencies", report, b_global)
    shared_outcomes = set(outcomes) | set(b_outcomes)
    shared_refs = set(tasks) | set(milestones) | set(b_tasks) | set(b_milestones)
    for kind, items in (("tasks", baseline.get("tasks", [])), ("milestones", baseline.get("milestones", []))):
        for i, item in enumerate(items or []):
            if not isinstance(item, dict):
                continue
            owner = item.get("id", f"baseline {kind}[{i}]")
            _reference(report, lanes, item.get("lane"), f"/baseline/{kind}/{i}/lane", "reference.baseline.lane_unknown", owner)
            for j, oid in enumerate(item.get("outcomes", []) or []):
                _reference(report, shared_outcomes, oid, f"/baseline/{kind}/{i}/outcomes/{j}", "reference.baseline.outcome_unknown", owner)
    for i, task in enumerate(baseline.get("tasks", []) or []):
        for j, mid in enumerate(task.get("milestones", []) or []):
            _reference(report, shared_refs, mid, f"/baseline/tasks/{i}/milestones/{j}", "reference.baseline.milestone_unknown", task.get("id", i))
    for i, dep in enumerate(baseline.get("dependencies", []) or []):
        owner = dep.get("id", f"baseline dependency[{i}]")
        _reference(report, shared_refs, dep.get("from"), f"/baseline/dependencies/{i}/from", "reference.baseline.source_unknown", owner)
        _reference(report, shared_refs, dep.get("to"), f"/baseline/dependencies/{i}/to", "reference.baseline.target_unknown", owner)

    if len(model.get("dependencies", []) or []) > 16:
        report.add("semantics", "warning", "roadmap.density.dependencies", "/dependencies", "many dependencies; diagram may have excessive crossings")
    if len(model.get("milestones", []) or []) > 24:
        report.add("semantics", "warning", "roadmap.density.milestones", "/milestones", "many milestones; diagram may be overcrowded")


def validate_document(model, strict=False):
    report = ValidationReport()
    model = normalize_scalars(model)
    normalized = dispatch_version(model, "roadmap", report, supported=(1, 2))
    deltas = []
    if normalized is not None:
        schema_ok = validate_schema(normalized, "roadmap", report.schema_version, report)
        if schema_ok:
            _validate_semantics(normalized, report)
            if not any(f["severity"] == "error" for f in report.findings):
                deltas = calculate_deltas(normalized)
                history_deltas = calculate_history_deltas(normalized)
                if normalized.get("schema_version") == 2:
                    report.details["history_deltas"] = history_deltas
                changed = [d for d in deltas if d["state"] not in ("unchanged",)]
                changed += [d for d in history_deltas if d["state"] not in ("unchanged",)]
                if len(changed) > 20:
                    report.add("semantics", "warning", "roadmap.density.deltas", "/baseline", "many baseline changes; diagram may need filtering")
    report.details["deltas"] = deltas
    return normalized, report.finish(strict=strict)


def validate_model(model, strict=False):
    _, report = validate_document(model, strict=strict)
    errors, warnings = messages(report)
    return errors, warnings, report.get("deltas", [])


def main(argv=None):
    ap = argparse.ArgumentParser(description="Validate versioned roadmap YAML.")
    ap.add_argument("input", help="roadmap YAML file")
    ap.add_argument("--strict", action="store_true", help="treat warnings as errors")
    ap.add_argument("--json", action="store_true", help="print the stable machine-readable report")
    args = ap.parse_args(argv)
    try:
        model = load_yaml(args.input)
        _, report = validate_document(model, strict=args.strict)
    except (OSError, yaml.YAMLError) as exc:
        sys.exit(f"error: cannot validate {args.input}: {exc}")
    print_report(report, as_json=args.json)
    if report["summary"]["errors"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
