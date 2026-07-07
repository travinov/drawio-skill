#!/usr/bin/env python3
"""Validate timeline-aware git-flow JSON for scripts/gitflow.py.

Usage:
  python3 gitflow_validate.py flow.json [--strict]
"""
import argparse
import json
import sys

EVENT_TYPES = {"commit", "branch", "merge", "tag", "release", "hotfix", "cherry-pick", "revert", "note"}
BRANCH_KINDS = {"main", "master", "develop", "feature", "release", "hotfix", "support", "custom"}
SUPPORTED_WORKFLOWS = {"git-flow", "custom"}


def branch_kind(branch):
    kind = str(branch.get("kind", "")).lower()
    label = str(branch.get("label", branch.get("id", ""))).lower()
    bid = str(branch.get("id", "")).lower()
    if kind:
        return kind
    if bid in ("main", "master") or label in ("main", "master"):
        return "main"
    for prefix in ("feature", "release", "hotfix", "support"):
        if bid.startswith(prefix + "_") or label.startswith(prefix + "/"):
            return prefix
    if bid == "develop" or label == "develop":
        return "develop"
    return "custom"


def time_key(event, index, mode):
    if mode == "date":
        return event.get("at")
    if mode == "order":
        return event.get("order", index)
    return None


def _add_ref_error(errors, event, field, branches):
    ref = event.get(field)
    if ref is not None and ref not in branches:
        errors.append(f"event {event.get('id')!r} references unknown branch {ref!r} via {field}")


def validate_spec(spec, strict=False):
    """Return (errors, warnings) for one git-flow input spec."""
    errors, warnings = [], []
    if not isinstance(spec, dict):
        return ["top-level JSON value must be an object"], []

    workflow = spec.get("workflow", "git-flow")
    if workflow not in SUPPORTED_WORKFLOWS:
        errors.append(f"unsupported workflow {workflow!r}; supported: {', '.join(sorted(SUPPORTED_WORKFLOWS))}")

    mode = spec.get("timeMode", "date")
    if mode not in ("date", "order"):
        errors.append("timeMode must be 'date' or 'order'")

    branches = spec.get("branches")
    events = spec.get("events")
    if not isinstance(branches, list) or not branches:
        errors.append("branches must be a non-empty array")
        branches = []
    if not isinstance(events, list):
        errors.append("events must be an array")
        events = []

    by_branch = {}
    for i, branch in enumerate(branches):
        bid = branch.get("id") if isinstance(branch, dict) else None
        if not bid:
            errors.append(f"branch at index {i} is missing id")
            continue
        if bid in by_branch:
            errors.append(f"duplicate branch id {bid!r}")
        by_branch[bid] = branch
        kind = branch_kind(branch)
        if kind not in BRANCH_KINDS:
            errors.append(f"branch {bid!r} has invalid kind {kind!r}")

    main_ids = [bid for bid, b in by_branch.items() if branch_kind(b) in ("main", "master")]
    develop_ids = [bid for bid, b in by_branch.items() if branch_kind(b) == "develop"]
    if workflow == "git-flow":
        if not main_ids:
            errors.append("git-flow requires a main/master production branch")
        if not develop_ids:
            warnings.append("git-flow usually has a develop integration branch; use workflow='custom' for release-based flows without develop")

    event_ids = {}
    branch_created_from = {}
    branch_merged_to = {}
    for i, event in enumerate(events):
        if not isinstance(event, dict):
            errors.append(f"event at index {i} must be an object")
            continue
        eid = event.get("id")
        etype = event.get("type")
        if not eid:
            errors.append(f"event at index {i} is missing id")
        elif eid in event_ids:
            errors.append(f"duplicate event id {eid!r}")
        else:
            event_ids[eid] = event
        if etype not in EVENT_TYPES:
            errors.append(f"event {eid!r} has invalid type {etype!r}")

        t = time_key(event, i, mode)
        if t is None:
            errors.append(f"event {eid!r} is missing {'at' if mode == 'date' else 'order'}")

        if etype in ("commit", "tag", "release", "hotfix", "note", "cherry-pick", "revert"):
            _add_ref_error(errors, event, "branch", by_branch)
        if etype == "branch":
            _add_ref_error(errors, event, "from", by_branch)
            _add_ref_error(errors, event, "to", by_branch)
            if event.get("from") == event.get("to") and event.get("from") is not None:
                errors.append(f"event {eid!r} branches into itself")
            if event.get("to") in by_branch:
                branch_created_from[event["to"]] = event.get("from")
        if etype == "merge":
            _add_ref_error(errors, event, "from", by_branch)
            _add_ref_error(errors, event, "to", by_branch)
            if event.get("from") == event.get("to") and event.get("from") is not None:
                errors.append(f"event {eid!r} cannot merge into itself")
            if event.get("from") in by_branch and event.get("to") in by_branch:
                branch_merged_to.setdefault(event["from"], set()).add(event["to"])

    if workflow == "git-flow":
        main = set(main_ids)
        develop = set(develop_ids)
        release = {bid for bid, b in by_branch.items() if branch_kind(b) == "release"}
        for bid, branch in by_branch.items():
            kind = branch_kind(branch)
            source = branch_created_from.get(bid)
            targets = branch_merged_to.get(bid, set())
            if kind == "feature":
                if source and source not in develop:
                    warnings.append(f"feature branch {bid!r} should branch from develop")
                if targets and not (targets & develop):
                    warnings.append(f"feature branch {bid!r} should merge back into develop")
            elif kind == "release":
                if source and source not in develop:
                    warnings.append(f"release branch {bid!r} should branch from develop")
                if targets and not (targets & main):
                    warnings.append(f"release branch {bid!r} should merge into main/master")
                if targets and not (targets & develop):
                    warnings.append(f"release branch {bid!r} should merge back into develop")
            elif kind == "hotfix":
                if source and source not in main:
                    warnings.append(f"hotfix branch {bid!r} should branch from main/master")
                if targets and not (targets & main):
                    warnings.append(f"hotfix branch {bid!r} should merge into main/master")
                if targets and not ((targets & develop) or (targets & release)):
                    warnings.append(f"hotfix branch {bid!r} should merge back into develop or active release")

    if strict and warnings:
        errors.extend(warnings)
        warnings = []
    return errors, warnings


def load_spec(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Validate git-flow diagram JSON.")
    ap.add_argument("input", help="flow JSON file")
    ap.add_argument("--strict", action="store_true", help="treat git-flow rule warnings as errors")
    args = ap.parse_args(argv)
    try:
        spec = load_spec(args.input)
    except (OSError, json.JSONDecodeError) as exc:
        sys.exit(f"error: cannot read {args.input}: {exc}")
    errors, warnings = validate_spec(spec, strict=args.strict)
    for warning in warnings:
        print(f"warning: {warning}")
    for error in errors:
        print(f"error: {error}")
    print(f"{len(errors)} error(s), {len(warnings)} warning(s)")
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
