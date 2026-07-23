#!/usr/bin/env python3
"""Page-scoped DiagramSpec model views and typed semantic deltas."""
from __future__ import annotations

import copy
from typing import Any, Iterable

from lifecycle_contracts import canonical_json_sha256, require_valid_contract


TECHNICAL_KINDS = frozenset({"root", "layer", "wrapper"})
TECHNICAL_ROOT_IDS = frozenset({"0", "1"})
MODEL_ELEMENT_FIELDS = (
    "stable_identity",
    "kind",
    "semantic_type",
    "label",
    "parent",
    "source",
    "target",
    "relationship",
)
SOURCE_PRIORITY = (
    "explicit_user_decision",
    "confirmed_clarification",
    "original_user_request",
    "explicit_user_document",
    "existing_diagram",
    "agent_assumption",
)


def stable_identity(page_id: str, cell_id: str) -> dict[str, str]:
    return {"page_id": page_id, "cell_id": cell_id}


def identity_key(identity: dict[str, str]) -> tuple[str, str]:
    return identity.get("page_id", ""), identity.get("cell_id", "")


def page_scoped_element_key(page_id: str, cell_id: str) -> tuple[str, str]:
    """Return the one stable ordering key shared by DiagramSpec and LayoutIR."""
    return page_id, cell_id


def is_technical_cell(cell: dict[str, Any]) -> bool:
    if cell.get("business_element") is True:
        return False
    return bool(
        cell.get("technical")
        or cell.get("kind") in TECHNICAL_KINDS
        or str(cell.get("id", "")) in TECHNICAL_ROOT_IDS
    )


def build_model_view(diagramspec: dict[str, Any]) -> dict[str, Any]:
    """Project preserved cells to a role-safe model without altering input."""
    model_pages = []
    for page in diagramspec.get("pages", []):
        technical_identities = {
            identity_key(cell.get("stable_identity", {}))
            for cell in page.get("cells", [])
            if is_technical_cell(cell)
        }
        elements = []
        for cell in page.get("cells", []):
            if is_technical_cell(cell):
                continue
            element = {key: copy.deepcopy(cell[key]) for key in MODEL_ELEMENT_FIELDS if key in cell}
            element.setdefault("parent", None)
            element.setdefault("source", None)
            element.setdefault("target", None)
            element.setdefault("relationship", None)
            if element["parent"] is not None and identity_key(element["parent"]) in technical_identities:
                element["parent"] = None
            element["style_hint"] = cell.get("style") or None
            elements.append(element)
        elements.sort(key=lambda item: identity_key(item["stable_identity"]))
        model_pages.append({"id": page["id"], "name": page.get("name", ""), "elements": elements})
    model_pages.sort(key=lambda item: item["id"])
    return {"technical_cells_excluded": True, "pages": model_pages}


def semantic_digest(model_view: dict[str, Any]) -> str:
    return canonical_json_sha256(model_view)


def with_model_view(diagramspec: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(diagramspec)
    result["model_view"] = build_model_view(result)
    result["semantic_digest"] = {
        "algorithm": "sha256",
        "canonicalization": "diagramspec-model-view-v2",
        "value": semantic_digest(result["model_view"]),
    }
    return result


def _diagnostic(code: str, pointer: str, message: str) -> dict[str, str]:
    return {"code": code, "pointer": pointer, "message": message}


def _validate_identity_reference(
    reference: dict[str, str] | None,
    *,
    owner_page: str,
    known: set[tuple[str, str]],
    pointer: str,
    role: str,
) -> list[dict[str, str]]:
    if reference is None:
        return []
    key = identity_key(reference)
    diagnostics = []
    if key not in known:
        diagnostics.append(_diagnostic(f"diagram.reference.{role}_missing", pointer, f"{role} reference {key!r} does not exist"))
    if key[0] != owner_page:
        diagnostics.append(_diagnostic(f"diagram.reference.{role}_cross_page", pointer, f"{role} reference must remain on page {owner_page!r}"))
    return diagnostics


def validate_diagramspec_cross_fields(diagramspec: dict[str, Any]) -> list[dict[str, str]]:
    diagnostics: list[dict[str, str]] = []
    page_ids: set[str] = set()
    known: set[tuple[str, str]] = set()
    cell_locations: dict[tuple[str, str], str] = {}
    cells: list[tuple[str, int, dict[str, Any]]] = []
    for page_index, page in enumerate(diagramspec.get("pages", [])):
        page_id = page.get("id", "")
        if page_id in page_ids:
            diagnostics.append(_diagnostic("diagram.page.duplicate", f"/pages/{page_index}/id", f"duplicate page id {page_id!r}"))
        page_ids.add(page_id)
        for cell_index, cell in enumerate(page.get("cells", [])):
            pointer = f"/pages/{page_index}/cells/{cell_index}"
            key = (page_id, str(cell.get("id", "")))
            if key in known:
                diagnostics.append(_diagnostic("diagram.identity.duplicate", f"{pointer}/id", f"duplicate page-scoped cell identity {key!r}"))
            known.add(key)
            cell_locations[key] = pointer
            cells.append((page_id, cell_index, cell))
            if identity_key(cell.get("stable_identity", {})) != key:
                diagnostics.append(_diagnostic("diagram.identity.mismatch", f"{pointer}/stable_identity", "stable identity must equal the containing page id and cell id"))
    parents: dict[tuple[str, str], tuple[str, str]] = {}
    for page_id, _, cell in cells:
        key = (page_id, str(cell.get("id", "")))
        pointer = cell_locations[key]
        for role in ("parent", "source", "target"):
            diagnostics.extend(_validate_identity_reference(cell.get(role), owner_page=page_id, known=known, pointer=f"{pointer}/{role}", role=role))
        if cell.get("parent") is not None:
            parents[key] = identity_key(cell["parent"])
        if cell.get("kind") == "edge":
            if cell.get("source") is None:
                diagnostics.append(_diagnostic("diagram.edge.source_required", f"{pointer}/source", "edge source is required"))
            if cell.get("target") is None:
                diagnostics.append(_diagnostic("diagram.edge.target_required", f"{pointer}/target", "edge target is required"))
    for start in sorted(parents):
        chain: list[tuple[str, str]] = []
        current = start
        while current in parents:
            if current in chain:
                cycle = chain[chain.index(current):] + [current]
                diagnostics.append(_diagnostic("diagram.parent.cycle", f"{cell_locations[start]}/parent", "parent cycle: " + " -> ".join(f"{page}/{cell}" for page, cell in cycle)))
                break
            chain.append(current)
            current = parents[current]
    expected_view = build_model_view(diagramspec)
    if diagramspec.get("model_view") != expected_view:
        diagnostics.append(_diagnostic("diagram.model_view.mismatch", "/model_view", "model view is not the deterministic projection of preserved cells"))
    expected_digest = semantic_digest(expected_view)
    actual_digest = diagramspec.get("semantic_digest", {}).get("value")
    if actual_digest != expected_digest:
        diagnostics.append(_diagnostic("diagram.semantic_digest.mismatch", "/semantic_digest/value", "semantic digest does not match model view"))
    return diagnostics


def validate_diagramspec(diagramspec: dict[str, Any]) -> list[dict[str, str]]:
    diagnostics: list[dict[str, str]] = []
    try:
        require_valid_contract(diagramspec, "diagramspec", 2)
    except Exception as exc:
        diagnostics.extend(getattr(exc, "diagnostics", [getattr(exc, "as_dict", lambda: _diagnostic("diagram.schema_invalid", "", str(exc)))()]))
        return diagnostics
    diagnostics.extend(validate_diagramspec_cross_fields(diagramspec))
    return diagnostics


def validate_semantic_analysis_input(payload: dict[str, Any]) -> list[dict[str, str]]:
    """Validate the host-owned v2 input before an isolated semantic role runs."""
    diagnostics: list[dict[str, str]] = []
    if not isinstance(payload, dict):
        return [_diagnostic("semantic.input.type", "", "semantic input must be an object")]
    expected_keys = {
        "schema_version", "run_id", "mode", "request", "feedback",
        "source_bundle", "baseline", "source_priority", "requirements",
    }
    missing = sorted(expected_keys - set(payload))
    extra = sorted(set(payload) - expected_keys)
    for key in missing:
        diagnostics.append(_diagnostic("semantic.input.required", f"/{key}", f"required input field {key!r} is missing"))
    for key in extra:
        diagnostics.append(_diagnostic("semantic.input.additional_property", f"/{key}", f"unexpected input field {key!r}"))
    if missing:
        return diagnostics
    if payload.get("schema_version") != 2:
        diagnostics.append(_diagnostic("semantic.input.version", "/schema_version", "semantic input schema_version must equal 2"))
    if not isinstance(payload.get("run_id"), str) or not payload["run_id"]:
        diagnostics.append(_diagnostic("semantic.input.run_id", "/run_id", "run_id must be a non-empty string"))
    if payload.get("mode") not in {"create", "improve"}:
        diagnostics.append(_diagnostic("semantic.input.mode", "/mode", "mode must be create or improve"))
    if not isinstance(payload.get("request"), str) or not payload["request"].strip():
        diagnostics.append(_diagnostic("semantic.input.request", "/request", "request must be a non-empty string"))
    if payload.get("feedback") is not None and not isinstance(payload.get("feedback"), str):
        diagnostics.append(_diagnostic("semantic.input.feedback", "/feedback", "feedback must be a string or null"))
    if payload.get("source_priority") != list(SOURCE_PRIORITY):
        diagnostics.append(_diagnostic("semantic.input.source_priority", "/source_priority", "source priority differs from the fixed lifecycle order"))

    source_binding = payload.get("source_bundle")
    source_bundle = source_binding.get("content") if isinstance(source_binding, dict) else None
    if not isinstance(source_binding, dict) or set(source_binding) != {"sha256", "content"}:
        diagnostics.append(_diagnostic("semantic.input.source_binding", "/source_bundle", "source_bundle must contain exactly sha256 and content"))
    elif not isinstance(source_bundle, dict):
        diagnostics.append(_diagnostic("semantic.input.source_content", "/source_bundle/content", "source bundle content must be an object"))
    else:
        try:
            require_valid_contract(source_bundle, "source-bundle", 2)
        except Exception as exc:
            nested = getattr(exc, "diagnostics", ())
            if nested:
                diagnostics.extend(
                    _diagnostic(
                        item.get("code", "semantic.input.source_schema"),
                        "/source_bundle/content" + item.get("pointer", ""),
                        item.get("message", str(exc)),
                    )
                    for item in nested
                )
            else:
                diagnostics.append(_diagnostic("semantic.input.source_schema", "/source_bundle/content", str(exc)))
        if source_binding.get("sha256") != canonical_json_sha256(source_bundle):
            diagnostics.append(_diagnostic("semantic.input.source_hash", "/source_bundle/sha256", "source bundle hash does not match canonical content"))
        if source_bundle.get("run_id") != payload.get("run_id"):
            diagnostics.append(_diagnostic("semantic.input.source_run", "/source_bundle/content/run_id", "source bundle belongs to another run"))
        if source_bundle.get("source_priority") != payload.get("source_priority"):
            diagnostics.append(_diagnostic("semantic.input.source_priority_mismatch", "/source_bundle/content/source_priority", "source bundle priority differs from the role input"))

    baseline = payload.get("baseline")
    if not isinstance(baseline, dict) or set(baseline) != {"semantic_digest", "model_view", "evidence"}:
        diagnostics.append(_diagnostic("semantic.input.baseline", "/baseline", "baseline must contain exactly semantic_digest, model_view, and evidence"))
    else:
        model_view = baseline.get("model_view")
        if not isinstance(model_view, dict):
            diagnostics.append(_diagnostic("semantic.input.model_view", "/baseline/model_view", "baseline model_view must be an object"))
        elif baseline.get("semantic_digest") != semantic_digest(model_view):
            diagnostics.append(_diagnostic("semantic.input.baseline_digest", "/baseline/semantic_digest", "baseline semantic digest does not match the supplied model view"))
        if isinstance(source_bundle, dict) and baseline.get("evidence") != source_bundle.get("evidence"):
            diagnostics.append(_diagnostic("semantic.input.evidence_binding", "/baseline/evidence", "baseline evidence must equal the active source-bundle evidence"))

    requirements = payload.get("requirements")
    required_flags = {
        "complete_desired_graph", "compare_request_to_existing",
        "return_complete_plan_for_create", "preserve_page_scoped_ids",
    }
    if not isinstance(requirements, dict) or set(requirements) != required_flags:
        diagnostics.append(_diagnostic("semantic.input.requirements", "/requirements", "semantic requirements contain an unexpected or missing field"))
    elif any(type(requirements[name]) is not bool for name in required_flags):
        diagnostics.append(_diagnostic("semantic.input.requirement_type", "/requirements", "semantic requirement flags must be booleans"))
    return sorted(diagnostics, key=lambda item: (item["pointer"], item["code"], item["message"]))


def validate_semantic_analysis_cross_fields(analysis: dict[str, Any]) -> list[dict[str, str]]:
    """Validate page scope and relationships in model-owned analysis output."""
    diagnostics: list[dict[str, str]] = []
    result = analysis.get("result", {})
    all_known: set[tuple[str, str]] = set()
    node_known: set[tuple[str, str]] = set()
    locations: dict[tuple[str, str], str] = {}
    parents: dict[tuple[str, str], tuple[str, str]] = {}
    elements: list[tuple[str, str, dict[str, Any], str]] = []
    page_ids: set[str] = set()
    for page_index, page in enumerate(result.get("pages", [])):
        page_id = page.get("page_id", "")
        if page_id in page_ids:
            diagnostics.append(_diagnostic("semantic.page.duplicate", f"/result/pages/{page_index}/page_id", f"duplicate page id {page_id!r}"))
        page_ids.add(page_id)
        for collection in ("nodes", "edges"):
            for element_index, element in enumerate(page.get(collection, [])):
                pointer = f"/result/pages/{page_index}/{collection}/{element_index}"
                key = identity_key(element.get("stable_identity", {}))
                if key[0] != page_id:
                    diagnostics.append(_diagnostic("semantic.identity.page_mismatch", f"{pointer}/stable_identity/page_id", "stable identity page must equal its containing page"))
                if key[1] in TECHNICAL_ROOT_IDS:
                    diagnostics.append(_diagnostic("semantic.identity.technical", f"{pointer}/stable_identity/cell_id", "technical root/layer ids are forbidden in model semantics"))
                if key in all_known:
                    diagnostics.append(_diagnostic("semantic.identity.duplicate", f"{pointer}/stable_identity", f"duplicate page-scoped identity {key!r}"))
                all_known.add(key)
                if collection == "nodes":
                    node_known.add(key)
                locations[key] = pointer
                elements.append((page_id, collection, element, pointer))
    for page_id, collection, element, pointer in elements:
        key = identity_key(element.get("stable_identity", {}))
        parent = element.get("parent")
        diagnostics.extend(_validate_identity_reference(parent, owner_page=page_id, known=node_known, pointer=f"{pointer}/parent", role="parent"))
        if collection == "nodes" and parent is not None:
            parents[key] = identity_key(parent)
        if collection == "edges":
            for role in ("source", "target"):
                diagnostics.extend(_validate_identity_reference(element.get(role), owner_page=page_id, known=node_known, pointer=f"{pointer}/{role}", role=role))
            route = element.get("route")
            if route is not None:
                if route.get("source_pin") == route.get("target_pin"):
                    diagnostics.append(_diagnostic("semantic.route.pins_not_distinct", f"{pointer}/route", "source_pin and target_pin must be distinct"))
                points = route.get("waypoints", [])
                for segment_index, (start, end) in enumerate(zip(points, points[1:])):
                    if start.get("x") != end.get("x") and start.get("y") != end.get("y"):
                        diagnostics.append(_diagnostic("semantic.route.non_orthogonal_segment", f"{pointer}/route/waypoints/{segment_index + 1}", "consecutive waypoints must share x or y"))
    reported_cycles: set[tuple[tuple[str, str], ...]] = set()
    for start in sorted(parents):
        chain: list[tuple[str, str]] = []
        current = start
        while current in parents:
            if current in chain:
                raw_cycle = chain[chain.index(current):]
                rotation = min(tuple(raw_cycle[index:] + raw_cycle[:index]) for index in range(len(raw_cycle)))
                if rotation not in reported_cycles:
                    reported_cycles.add(rotation)
                    diagnostics.append(_diagnostic("semantic.parent.cycle", f"{locations[start]}/parent", "parent cycle: " + " -> ".join(f"{page}/{cell}" for page, cell in [*raw_cycle, current])))
                break
            chain.append(current)
            current = parents[current]
    needs_human = analysis.get("status") == "needs_human"
    requires_human = result.get("requires_human") is True
    if needs_human != requires_human:
        diagnostics.append(_diagnostic("semantic.human_status_mismatch", "/result/requires_human", "status and requires_human must describe the same human-input requirement"))
    if requires_human and not result.get("human_questions"):
        diagnostics.append(_diagnostic("semantic.human_questions_required", "/result/human_questions", "human questions are required when input is unresolved"))
    return sorted(diagnostics, key=lambda item: (item["pointer"], item["code"], item["message"]))


def validate_semantic_analysis(analysis: dict[str, Any]) -> list[dict[str, str]]:
    diagnostics: list[dict[str, str]] = []
    try:
        require_valid_contract(analysis, "semantic-analysis", 2)
    except Exception as exc:
        diagnostics.extend(getattr(exc, "diagnostics", [getattr(exc, "as_dict", lambda: _diagnostic("semantic.analysis_schema_invalid", "", str(exc)))()]))
        return diagnostics
    diagnostics.extend(validate_semantic_analysis_cross_fields(analysis))
    return diagnostics


def _normalized_plan_pages(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    pages = []
    for supplied_page in analysis["result"]["pages"]:
        nodes = []
        for supplied in supplied_page["nodes"]:
            nodes.append({
                "stable_identity": copy.deepcopy(supplied["stable_identity"]),
                "label": supplied["label"],
                "semantic_type": supplied["semantic_type"],
                "parent": copy.deepcopy(supplied.get("parent")),
                "style_hint": supplied.get("style_hint"),
            })
        edges = []
        for supplied in supplied_page["edges"]:
            edge = {
                "stable_identity": copy.deepcopy(supplied["stable_identity"]),
                "source": copy.deepcopy(supplied["source"]),
                "target": copy.deepcopy(supplied["target"]),
                "label": supplied["label"],
                "relationship": supplied["relationship"],
                "parent": copy.deepcopy(supplied.get("parent")),
                "style_hint": supplied.get("style_hint"),
            }
            if supplied.get("route") is not None:
                edge["route"] = copy.deepcopy(supplied["route"])
            edges.append(edge)
        nodes.sort(key=lambda item: identity_key(item["stable_identity"]))
        edges.sort(key=lambda item: identity_key(item["stable_identity"]))
        pages.append({
            "page_id": supplied_page["page_id"],
            "name": supplied_page["name"],
            "nodes": nodes,
            "edges": edges,
        })
    pages.sort(key=lambda item: item["page_id"])
    return pages


def _semantic_element_kind(collection: str, element: dict[str, Any]) -> str:
    if collection == "edges" or element.get("kind") == "edge":
        return "edge"
    if element.get("semantic_type", "").strip().lower() in {"container", "group", "lane", "swimlane"}:
        return "group"
    return "node"


def _delta_element(collection: str, element: dict[str, Any]) -> dict[str, Any]:
    """Project an element to semantic fields; route remains layout-only."""
    fields = (
        ("stable_identity", "label", "semantic_type", "parent")
        if collection == "nodes"
        else ("stable_identity", "source", "target", "label", "relationship", "parent")
    )
    return {field: copy.deepcopy(element.get(field)) for field in fields}


def _analysis_delta_operations(
    baseline_model_view: dict[str, Any], desired_pages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    baseline: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
    for page in baseline_model_view.get("pages", []):
        for element in page.get("elements", []):
            collection = "edges" if element.get("kind") == "edge" else "nodes"
            baseline[identity_key(element["stable_identity"])] = (collection, element)
    desired: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
    for page in desired_pages:
        for collection in ("nodes", "edges"):
            for element in page[collection]:
                desired[identity_key(element["stable_identity"])] = (collection, element)

    operations: list[dict[str, Any]] = []
    for key in sorted(set(baseline) | set(desired)):
        old_entry = baseline.get(key)
        new_entry = desired.get(key)
        if old_entry is None:
            collection, element = new_entry
            operations.append({
                "operation_type": "add",
                "element_kind": _semantic_element_kind(collection, element),
                "target": copy.deepcopy(element["stable_identity"]),
                "changes": [{
                    "field": "element", "before": None,
                    "after": _delta_element(collection, element),
                }],
            })
            continue
        if new_entry is None:
            collection, element = old_entry
            operations.append({
                "operation_type": "remove",
                "element_kind": _semantic_element_kind(collection, element),
                "target": copy.deepcopy(element["stable_identity"]),
                "changes": [{
                    "field": "element",
                    "before": _delta_element(collection, element), "after": None,
                }],
            })
            continue
        old_collection, old = old_entry
        new_collection, new = new_entry
        element_kind = _semantic_element_kind(new_collection, new)
        if old_collection != new_collection:
            changes = [{
                "field": "element",
                "before": _delta_element(old_collection, old),
                "after": _delta_element(new_collection, new),
            }]
        else:
            fields = (
                ("label", "semantic_type", "parent", "style_hint")
                if new_collection == "nodes"
                else ("label", "source", "target", "relationship", "parent", "style_hint")
            )
            changes = [
                {
                    "field": field,
                    "before": copy.deepcopy(old.get(field)),
                    "after": copy.deepcopy(new.get(field)),
                }
                for field in fields
                if old.get(field) != new.get(field)
            ]
        if not changes:
            continue
        change_fields = {item["field"] for item in changes}
        operation_type = (
            "parent" if change_fields == {"parent"}
            else "relationship"
            if change_fields and change_fields <= {"source", "target", "relationship"}
            else "update"
        )
        operations.append({
            "operation_type": operation_type,
            "element_kind": element_kind,
            "target": copy.deepcopy(new["stable_identity"]),
            "changes": changes,
        })
    return operations


def semantic_analysis_to_plan(
    analysis: dict[str, Any], *, run_id: str, mode: str,
    source_bundle_sha256: str, baseline_semantic_digest: str,
    baseline_model_view: dict[str, Any], assumption_source_ids: Iterable[str],
) -> dict[str, Any]:
    """Bind model analysis to host evidence and derive the canonical v2 plan."""
    diagnostics = validate_semantic_analysis(analysis)
    if diagnostics:
        raise ValueError(diagnostics)
    if analysis["result"]["mode"] != mode:
        raise ValueError([_diagnostic("semantic.analysis.mode_mismatch", "/result/mode", "analysis mode differs from the host run")])
    if semantic_digest(baseline_model_view) != baseline_semantic_digest:
        raise ValueError([_diagnostic("semantic.analysis.baseline_mismatch", "/baseline_semantic_digest", "host baseline digest does not match its model view")])
    desired_pages = _normalized_plan_pages(analysis)
    delta = normalize_semantic_delta(
        baseline_semantic_digest=baseline_semantic_digest,
        source_bundle_sha256=source_bundle_sha256,
        operations=_analysis_delta_operations(baseline_model_view, desired_pages),
    )
    assumption_texts = list(analysis["result"]["assumptions"])
    source_ids = list(assumption_source_ids)
    if len(assumption_texts) != len(source_ids) or len(set(source_ids)) != len(source_ids):
        raise ValueError([_diagnostic("semantic.analysis.assumption_binding", "/result/assumptions", "each assumption must have one distinct host-owned source id")])
    requires_human = bool(
        analysis["result"]["requires_human"]
        or analysis["status"] == "needs_human"
        or (mode == "improve" and delta["operations"])
    )
    questions = list(analysis["result"]["human_questions"])
    if requires_human and not questions:
        questions = [
            "Подтвердите предложенную семантическую дельту перед изменением диаграммы."
        ]
    plan = {
        "schema_version": 2,
        "role": "semantic_analyst",
        "status": "needs_human" if requires_human else "ok",
        "run_id": run_id,
        "source_bundle_sha256": source_bundle_sha256,
        "baseline_semantic_digest": baseline_semantic_digest,
        "result": {
            "mode": mode,
            "diagram_type": analysis["result"]["diagram_type"],
            "title": analysis["result"]["title"],
            "direction": analysis["result"]["direction"],
            "pages": desired_pages,
            "semantic_delta": delta,
            "assumptions": [
                {
                    "assumption_id": "assumption-" + canonical_json_sha256({"text": text, "source_id": source_id})[:20],
                    "text": text,
                    "source_id": source_id,
                }
                for text, source_id in zip(assumption_texts, source_ids)
            ],
            "requires_human": requires_human,
            "human_questions": questions,
        },
    }
    plan_diagnostics = validate_semantic_plan(plan)
    if plan_diagnostics:
        raise ValueError(plan_diagnostics)
    return plan


def validate_semantic_plan_cross_fields(plan: dict[str, Any]) -> list[dict[str, str]]:
    diagnostics: list[dict[str, str]] = []
    result = plan.get("result", {})
    known: set[tuple[str, str]] = set()
    locations: dict[tuple[str, str], str] = {}
    parents: dict[tuple[str, str], tuple[str, str]] = {}
    elements: list[tuple[str, dict[str, Any], str]] = []
    page_ids: set[str] = set()
    for page_index, page in enumerate(result.get("pages", [])):
        page_id = page.get("page_id", "")
        if page_id in page_ids:
            diagnostics.append(_diagnostic("semantic.page.duplicate", f"/result/pages/{page_index}/page_id", f"duplicate page id {page_id!r}"))
        page_ids.add(page_id)
        for collection in ("nodes", "edges"):
            for element_index, element in enumerate(page.get(collection, [])):
                pointer = f"/result/pages/{page_index}/{collection}/{element_index}"
                key = identity_key(element.get("stable_identity", {}))
                if key[0] != page_id:
                    diagnostics.append(_diagnostic("semantic.identity.page_mismatch", f"{pointer}/stable_identity/page_id", "stable identity page must equal its containing page"))
                if key[1] in TECHNICAL_ROOT_IDS:
                    diagnostics.append(_diagnostic("semantic.identity.technical", f"{pointer}/stable_identity/cell_id", "technical root/layer ids are forbidden in model semantics"))
                if key in known:
                    diagnostics.append(_diagnostic("semantic.identity.duplicate", f"{pointer}/stable_identity", f"duplicate page-scoped identity {key!r}"))
                known.add(key)
                locations[key] = pointer
                elements.append((page_id, element, pointer))
    for page_id, element, pointer in elements:
        for role in ("parent", "source", "target"):
            if role not in element:
                continue
            diagnostics.extend(_validate_identity_reference(element.get(role), owner_page=page_id, known=known, pointer=f"{pointer}/{role}", role=role))
        key = identity_key(element.get("stable_identity", {}))
        if element.get("parent") is not None:
            parents[key] = identity_key(element["parent"])
        route = element.get("route")
        if route is not None:
            if route.get("source_pin") == route.get("target_pin"):
                diagnostics.append(_diagnostic("semantic.route.pins_not_distinct", f"{pointer}/route", "source_pin and target_pin must be distinct"))
            points = route.get("waypoints", [])
            for segment_index, (start, end) in enumerate(zip(points, points[1:])):
                if start.get("x") != end.get("x") and start.get("y") != end.get("y"):
                    diagnostics.append(_diagnostic("semantic.route.non_orthogonal_segment", f"{pointer}/route/waypoints/{segment_index + 1}", "consecutive waypoints must share x or y"))
    reported_cycles: set[tuple[tuple[str, str], ...]] = set()
    for start in sorted(parents):
        chain: list[tuple[str, str]] = []
        current = start
        while current in parents:
            if current in chain:
                raw_cycle = chain[chain.index(current):]
                rotation = min(tuple(raw_cycle[index:] + raw_cycle[:index]) for index in range(len(raw_cycle)))
                if rotation not in reported_cycles:
                    reported_cycles.add(rotation)
                    diagnostics.append(_diagnostic("semantic.parent.cycle", f"{locations[start]}/parent", "parent cycle: " + " -> ".join(f"{page}/{cell}" for page, cell in [*raw_cycle, current])))
                break
            chain.append(current)
            current = parents[current]
    delta = result.get("semantic_delta", {})
    if delta.get("baseline_semantic_digest") != plan.get("baseline_semantic_digest"):
        diagnostics.append(_diagnostic("semantic.delta.baseline_mismatch", "/result/semantic_delta/baseline_semantic_digest", "semantic delta baseline differs from plan baseline"))
    if delta.get("source_bundle_sha256") != plan.get("source_bundle_sha256"):
        diagnostics.append(_diagnostic("semantic.delta.source_mismatch", "/result/semantic_delta/source_bundle_sha256", "semantic delta source bundle differs from plan source bundle"))
    diagnostics.extend(
        {**item, "pointer": "/result/semantic_delta" + item["pointer"]}
        for item in validate_semantic_delta(delta)
    )
    if plan.get("status") == "needs_human" and not result.get("requires_human"):
        diagnostics.append(_diagnostic("semantic.human_status_mismatch", "/result/requires_human", "needs_human status requires requires_human true"))
    return diagnostics


def validate_semantic_plan(plan: dict[str, Any]) -> list[dict[str, str]]:
    diagnostics: list[dict[str, str]] = []
    try:
        require_valid_contract(plan, "semantic-plan", 2)
    except Exception as exc:
        diagnostics.extend(getattr(exc, "diagnostics", [getattr(exc, "as_dict", lambda: _diagnostic("semantic.schema_invalid", "", str(exc)))()]))
        return diagnostics
    diagnostics.extend(validate_semantic_plan_cross_fields(plan))
    return diagnostics


def _canonical_operation(operation: dict[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(operation)
    normalized.pop("operation_id", None)
    normalized["changes"] = sorted(
        normalized.get("changes", []),
        key=lambda item: (item.get("field", ""), canonical_json_sha256(item)),
    )
    return normalized


def semantic_operation_id(operation: dict[str, Any]) -> str:
    return "op-" + canonical_json_sha256(_canonical_operation(operation))[:20]


def normalize_semantic_delta(
    *,
    baseline_semantic_digest: str,
    source_bundle_sha256: str,
    operations: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    normalized_operations = []
    for supplied in operations:
        operation = _canonical_operation(supplied)
        operation["operation_id"] = semantic_operation_id(operation)
        normalized_operations.append(operation)
    normalized_operations.sort(key=lambda item: item["operation_id"])
    delta = {
        "schema_version": 2,
        "baseline_semantic_digest": baseline_semantic_digest,
        "source_bundle_sha256": source_bundle_sha256,
        "operations": normalized_operations,
    }
    require_valid_contract(delta, "semantic-delta", 2)
    diagnostics = validate_semantic_delta(delta)
    if diagnostics:
        raise ValueError(diagnostics)
    return delta


def validate_semantic_delta(delta: dict[str, Any]) -> list[dict[str, str]]:
    diagnostics: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, operation in enumerate(delta.get("operations", [])):
        pointer = f"/operations/{index}"
        operation_id = operation.get("operation_id")
        if operation_id in seen:
            diagnostics.append(_diagnostic("semantic.operation.duplicate", f"{pointer}/operation_id", f"duplicate operation id {operation_id!r}"))
        seen.add(operation_id)
        expected = semantic_operation_id(operation)
        if operation_id != expected:
            diagnostics.append(_diagnostic("semantic.operation.identity_mismatch", f"{pointer}/operation_id", f"expected deterministic operation id {expected}"))
        target = operation.get("target", {})
        if target.get("cell_id") in TECHNICAL_ROOT_IDS:
            diagnostics.append(_diagnostic("semantic.operation.technical_target", f"{pointer}/target/cell_id", "technical root/layer ids cannot be semantic operation targets"))
        fields = [change.get("field") for change in operation.get("changes", [])]
        operation_type = operation.get("operation_type")
        if operation_type == "parent" and any(field != "parent" for field in fields):
            diagnostics.append(_diagnostic("semantic.operation.parent_fields", f"{pointer}/changes", "parent operations may change only parent"))
        if operation_type == "relationship" and any(field not in {"source", "target", "relationship"} for field in fields):
            diagnostics.append(_diagnostic("semantic.operation.relationship_fields", f"{pointer}/changes", "relationship operations may change only source, target, or relationship"))
    return diagnostics


def semantic_delta_sha256(delta: dict[str, Any]) -> str:
    require_valid_contract(delta, "semantic-delta", 2)
    diagnostics = validate_semantic_delta(delta)
    if diagnostics:
        raise ValueError(diagnostics)
    return canonical_json_sha256(delta)
