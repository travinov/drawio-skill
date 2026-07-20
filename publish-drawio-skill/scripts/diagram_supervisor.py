#!/usr/bin/env python3
"""Deterministic tools for the Diagram Supervisor extension.

Models may propose patches and review reports; this module owns XML parsing,
artifact mutation, candidate comparison, validation evidence, and run state.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import html
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import jsonschema


VERSION = "0.1.0"
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_CELLS = 100_000
MAX_XML_DEPTH = 64
MAX_PAGES = 100
DEFAULT_MAX_ATTEMPTS = 12
DEFAULT_MAX_REPAIR_CLASS_ATTEMPTS = 3
WRAPPER_MARKER = "data-diagram-supervisor-normalized"
SEMANTIC_TYPE_ATTR = "data-semantic-type"
RELATIONSHIP_ATTR = "data-relationship"
ATTEMPT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
URL_ATTRIBUTES = {
    "action", "background", "cite", "formaction", "href", "link", "poster", "src", "xlink:href",
}
UNSAFE_URL_SCHEMES = {"javascript", "vbscript", "file"}
QUALITY_KEYS = (
    "semantic_violations",
    "structural_errors",
    "route_through",
    "container_lane",
    "crossings",
    "overlaps",
    "routing_uncertainty",
    "text_overflow",
    "route_complexity",
)
QUALITY_CODE_MAP = {
    "artifact.readability.route_through": "route_through",
    "artifact.layout.container_overflow": "container_lane",
    "artifact.layout.container_overlap": "container_lane",
    "artifact.layout.lane_size": "container_lane",
    "artifact.layout.lane_title_collision": "container_lane",
    "artifact.readability.crossing": "crossings",
    "artifact.readability.overlap": "overlaps",
    "artifact.layout.routing_uncertain": "routing_uncertainty",
    "artifact.layout.terminal_segment": "routing_uncertainty",
    "artifact.readability.text_overflow": "text_overflow",
}
STRUCTURAL_CODE_PREFIXES = (
    "artifact.id.",
    "artifact.cell.",
    "artifact.reference.",
    "artifact.geometry.",
    "artifact.page.",
    "artifact.source.",
    "artifact.structure.",
    "artifact.xml.",
)
TERMINAL_STATES = {"completed", "approved_with_findings", "manual_handoff", "stopped"}
STATES = {
    "analyzed", "awaiting_decision", "patching", "validating", "accepted_candidate",
    "retrying", "plateau", "awaiting_feedback", "final_review", *TERMINAL_STATES,
}
TRANSITIONS = {
    "analyzed": {"awaiting_decision", "patching", "final_review", "stopped"},
    "awaiting_decision": {"patching", "awaiting_feedback", "manual_handoff", "stopped"},
    "patching": {"validating", "retrying", "manual_handoff", "stopped"},
    "validating": {"accepted_candidate", "retrying", "plateau", "manual_handoff", "stopped"},
    "accepted_candidate": {"patching", "validating", "final_review", "stopped"},
    "retrying": {"patching", "plateau", "awaiting_feedback", "manual_handoff", "stopped"},
    "plateau": {"awaiting_feedback", "manual_handoff", "stopped"},
    "final_review": {"completed", "approved_with_findings", "patching", "awaiting_feedback", "manual_handoff", "stopped"},
    "awaiting_feedback": {"awaiting_decision", "patching", "final_review", "manual_handoff", "stopped"},
}


class SupervisorError(RuntimeError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_hash(value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256_bytes(data.encode("utf-8"))


def load_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        tmp.write(payload)
        tmp.flush()
        os.fsync(tmp.fileno())
        temp_name = tmp.name
    os.replace(temp_name, path)
    try:
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        pass


def decode_html_entities(value):
    """Decode nested XML/HTML entities with a small, deterministic bound."""
    decoded = value or ""
    for _ in range(4):
        next_value = html.unescape(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    return decoded


def strip_scheme_obfuscation(value):
    return "".join(
        character for character in value
        if not character.isspace() and unicodedata.category(character) not in {"Cc", "Cf"}
    )


def unsafe_url_scheme(value):
    decoded = decode_html_entities(value).strip()
    colon = decoded.find(":")
    if colon <= 0:
        return None
    normalized_scheme = strip_scheme_obfuscation(decoded[:colon]).lower()
    normalized_url = normalized_scheme + decoded[colon:]
    try:
        scheme = urlsplit(normalized_url).scheme.lower()
    except ValueError:
        return "invalid"
    if scheme in UNSAFE_URL_SCHEMES:
        return scheme
    if scheme == "data":
        payload = normalized_url.split(":", 1)[1]
        media_type = strip_scheme_obfuscation(payload.split(",", 1)[0].split(";", 1)[0]).lower()
        if media_type == "text/html":
            return "data:text/html"
    return None


def local_attribute_name(name):
    return name.rsplit("}", 1)[-1].lower()


class EmbeddedURLScanner(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.unsafe = None

    def handle_starttag(self, _tag, attrs):
        self._scan(attrs)

    def handle_startendtag(self, _tag, attrs):
        self._scan(attrs)

    def _scan(self, attrs):
        for name, value in attrs:
            if local_attribute_name(name) in URL_ATTRIBUTES:
                scheme = unsafe_url_scheme(value or "")
                if scheme:
                    self.unsafe = (name, scheme)
                    return


def assert_safe_embedded_content(value, context):
    decoded = decode_html_entities(value)
    if "<" not in decoded or ">" not in decoded:
        return
    scanner = EmbeddedURLScanner()
    try:
        scanner.feed(decoded)
        scanner.close()
    except (ValueError, TypeError) as exc:
        raise SupervisorError(f"invalid embedded HTML in {context}: {exc}") from exc
    if scanner.unsafe:
        attribute, scheme = scanner.unsafe
        raise SupervisorError(
            f"unsafe {scheme} URL in embedded {attribute} content is not allowed ({context})"
        )


def safe_parse(path):
    path = Path(path)
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise SupervisorError(f"artifact exceeds {MAX_FILE_BYTES} byte safety limit")
    raw = path.read_bytes()
    upper = raw.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise SupervisorError("DTD and entity declarations are not allowed")
    depth = 0
    max_depth = 0
    try:
        for event, _ in ET.iterparse(io.BytesIO(raw), events=("start", "end")):
            depth += 1 if event == "start" else -1
            max_depth = max(max_depth, depth)
            if max_depth > MAX_XML_DEPTH:
                raise SupervisorError(f"XML depth exceeds {MAX_XML_DEPTH}")
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise SupervisorError(f"invalid draw.io XML: {exc}") from exc
    for wrapper in [*root.findall(".//UserObject"), *root.findall(".//object")]:
        inner = wrapper.find("mxCell")
        if inner is None:
            continue
        if WRAPPER_MARKER in inner.attrib:
            raise SupervisorError(f"reserved internal attribute {WRAPPER_MARKER!r} is not allowed")
        flags = []
        if not inner.get("id"):
            inner.set("id", wrapper.get("id", ""))
            flags.append("id")
        if inner.get("value") is None and wrapper.get("label") is not None:
            inner.set("value", wrapper.get("label", ""))
            flags.append("value")
        inner.set(WRAPPER_MARKER, ",".join(flags))
    cells = root.findall(".//mxCell")
    if len(cells) > MAX_CELLS:
        raise SupervisorError(f"cell count exceeds {MAX_CELLS}")
    for element in root.iter():
        for key, value in element.attrib.items():
            attribute = local_attribute_name(key)
            if attribute in URL_ATTRIBUTES:
                scheme = unsafe_url_scheme(value)
                if scheme:
                    raise SupervisorError(f"unsafe {scheme} URL in {key} is not allowed in diagram content")
            if attribute in {"label", "value"}:
                assert_safe_embedded_content(value, f"{element.tag}@{key}")
    diagrams = root.findall("diagram") if root.tag == "mxfile" else []
    if len(diagrams) > MAX_PAGES:
        raise SupervisorError(f"page count exceeds {MAX_PAGES}")
    if diagrams and any(not diagram.findall(".//mxGraphModel") for diagram in diagrams):
        raise SupervisorError("compressed or unsupported diagram page; use manual handoff")
    return raw, root, cells


def denormalize_wrappers(root):
    for wrapper in [*root.findall(".//UserObject"), *root.findall(".//object")]:
        inner = wrapper.find("mxCell")
        if inner is None or WRAPPER_MARKER not in inner.attrib:
            continue
        flags = set(filter(None, inner.attrib.pop(WRAPPER_MARKER, "").split(",")))
        if "id" in flags:
            inner.attrib.pop("id", None)
        if "value" in flags:
            wrapper.set("label", inner.get("value", ""))
            inner.attrib.pop("value", None)


def page_scopes(root):
    if root.tag == "mxfile":
        result = []
        for index, page in enumerate(root.findall("diagram")):
            result.append((page.get("id") or f"page-{index + 1}", page))
        return result
    return [("page-1", root)]


def page_cells(page):
    return page.findall(".//mxCell")


def page_by_id(page):
    result = {}
    for cell in page_cells(page):
        cell_id = cell.get("id")
        if cell_id:
            result[cell_id] = cell
    return result


def validate_patch_contract(patch):
    schema_path = Path(__file__).resolve().parent.parent / "data" / "diagram-patch.v1.schema.json"
    schema = load_json(schema_path)
    errors = sorted(
        jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).iter_errors(patch),
        key=lambda error: (list(error.absolute_path), error.message),
    )
    if errors:
        rendered = "; ".join(
            f"/{'/'.join(map(str, error.absolute_path))}: {error.message}" for error in errors[:5]
        )
        raise SupervisorError(f"patch schema validation failed: {rendered}")


def geometry(cell):
    node = cell.find("mxGeometry")
    if node is None:
        return None
    result = {}
    for key in ("x", "y", "width", "height", "relative"):
        if key in node.attrib:
            value = node.get(key)
            try:
                result[key] = float(value) if key != "relative" else value
            except (TypeError, ValueError):
                result[key] = value
    points = []
    array = node.find("Array[@as='points']")
    if array is not None:
        for point in array.findall("mxPoint"):
            points.append({"x": float(point.get("x", "0")), "y": float(point.get("y", "0"))})
    if points:
        result["points"] = points
    offset = node.find("mxPoint[@as='offset']")
    if offset is not None:
        result["offset"] = {
            "x": float(offset.get("x", "0")),
            "y": float(offset.get("y", "0")),
        }
    return result


def schema_geometry(cell):
    raw = geometry(cell)
    if raw is None:
        return None
    result = {}
    if all(key in raw for key in ("x", "y", "width", "height")):
        result["bounds"] = {key: raw[key] for key in ("x", "y", "width", "height")}
    if raw.get("points"):
        result["waypoints"] = raw["points"]
    if cell.get("edge") == "1" and "x" in raw and "y" in raw:
        result["label_offset"] = {
            "x": raw["x"], "y": raw["y"],
            "offset": raw.get("offset", {"x": 0.0, "y": 0.0}),
        }
    if "relative" in raw:
        result["relative"] = str(raw["relative"]) == "1"
    return result


def inferred_semantic_type(cell):
    explicit = cell.get(SEMANTIC_TYPE_ATTR)
    if explicit:
        return explicit
    if cell.get("edge") == "1":
        return "relationship"
    if cell.get("vertex") == "1":
        style = cell.get("style", "")
        if "swimlane" in style:
            return "container"
        if "rhombus" in style or "shape=mxgraph.flowchart.decision" in style:
            return "decision"
        if cell.get("connectable") == "0" or "group" in style:
            return "group"
        return "process"
    if cell.get("parent") == "0":
        return "layer"
    return "cell"


def inferred_relationship(cell):
    if cell.get("edge") != "1":
        return None
    return cell.get(RELATIONSHIP_ATTR) or cell.get("value") or "flow"


def semantic_record(cell, page_id=None):
    semantic_type = inferred_semantic_type(cell)
    kind = "edge" if cell.get("edge") == "1" else "vertex" if cell.get("vertex") == "1" else "cell"
    if kind == "vertex" and semantic_type == "group":
        kind = "group"
    return {
        "page_id": page_id,
        "id": cell.get("id"),
        "kind": kind,
        "semantic_type": semantic_type,
        "label": cell.get("value", ""),
        "parent": cell.get("parent"),
        "source": cell.get("source"),
        "target": cell.get("target"),
        "relationship": inferred_relationship(cell),
    }


def semantic_digest(cells, page_id=None):
    records = [semantic_record(cell, page_id=page_id) for cell in cells if cell.get("id")]
    return canonical_hash(sorted(records, key=lambda item: (item.get("page_id") or "", item["id"])))


def document_semantic_digest(root):
    records = []
    for page_id, page in page_scopes(root):
        records.extend(
            semantic_record(cell, page_id=page_id)
            for cell in page_cells(page)
            if cell.get("id")
        )
    return canonical_hash(sorted(records, key=lambda item: (item["page_id"], item["id"])))


def document_cell_hashes(root):
    return {
        (page_id, cell.get("id")): cell_hash(cell)
        for page_id, page in page_scopes(root)
        for cell in page_cells(page)
        if cell.get("id")
    }


def validate_source_refs(source_refs):
    schema = load_json(Path(__file__).resolve().parent.parent / "data" / "diagramspec.v1.schema.json")
    validator = jsonschema.Draft202012Validator(
        {"$schema": schema["$schema"], "$ref": "#/$defs/sourceRef", "$defs": schema["$defs"]},
        format_checker=jsonschema.FormatChecker(),
    )
    for index, source_ref in enumerate(source_refs):
        errors = sorted(validator.iter_errors(source_ref), key=lambda error: (list(error.path), error.message))
        if errors:
            rendered = "; ".join(error.message for error in errors[:3])
            raise SupervisorError(f"source_refs[{index}] is invalid: {rendered}")


def ordered_source_refs(existing_ref, source_refs):
    supplied = list(source_refs or [])
    validate_source_refs(supplied)
    refs = [existing_ref, *supplied]
    priority = {
        "explicit_user_decision": 0,
        "confirmed_clarification": 1,
        "openspec": 2,
        "existing_diagram": 3,
        "agent_assumption": 4,
    }
    return sorted(enumerate(refs), key=lambda item: (priority[item[1]["kind"]], item[0]))


def make_spec(path, source_refs=None):
    raw, root, cells = safe_parse(path)
    page_nodes = root.findall("diagram") if root.tag == "mxfile" else []
    digest = document_semantic_digest(root)
    existing_ref = {
        "source_id": "existing-diagram",
        "kind": "existing_diagram",
        "uri": str(Path(path).resolve()),
        "revision": None,
        "fragment": None,
        "content_hash": sha256_bytes(raw),
        "confidence": 1.0,
        "selected": True,
    }
    pages = []
    for page_id, page in page_scopes(root):
        elements = []
        for cell in page_cells(page):
            if not cell.get("id"):
                continue
            semantic = semantic_record(cell, page_id=page_id)
            kind = semantic["kind"]
            if kind == "cell" and cell.get("parent") == "0":
                kind = "layer"
            elif kind == "cell":
                kind = "other"
            item = {
                "id": semantic["id"],
                "kind": kind,
                "semantic_type": semantic["semantic_type"],
                "label": semantic["label"],
                "parent_id": semantic["parent"],
                "source_id": semantic["source"],
                "target_id": semantic["target"],
                "relationship": semantic["relationship"],
                "style": cell.get("style", ""),
                "stable_identity": f"{page_id}:{semantic['id']}",
            }
            parsed_geometry = schema_geometry(cell)
            if parsed_geometry is not None:
                item["geometry"] = parsed_geometry
            elements.append(item)
        pages.append({
            "id": page_id,
            "name": page.get("name", "") if root.tag == "mxfile" else "Page-1",
            "cells": sorted(elements, key=lambda value: value["id"]),
        })
    refs = [item for _, item in ordered_source_refs(existing_ref, source_refs)]
    return {
        "schema_version": 1,
        "diagram_id": page_nodes[0].get("id") if page_nodes and page_nodes[0].get("id") else Path(path).stem,
        "artifact": {
            "uri": str(Path(path).resolve()), "format": "drawio-xml", "sha256": sha256_bytes(raw),
            "byte_length": len(raw), "imported_at": utc_now(), "preservation_policy": "patch-original-xml",
            "source_byte_identical": True,
        },
        "pages": pages,
        "source_refs": refs,
        "source_priority": ["explicit_user_decision", "confirmed_clarification", "openspec", "existing_diagram", "agent_assumption"],
        "assumptions": [],
        "semantic_digest": {"algorithm": "sha256", "value": digest, "canonicalization": "diagramspec-semantic-v1"},
        "limits_applied": {"max_bytes": MAX_FILE_BYTES, "max_pages": MAX_PAGES, "max_cells": MAX_CELLS, "max_xml_depth": MAX_XML_DEPTH},
    }


def cell_hash(cell):
    return sha256_bytes(ET.tostring(cell, encoding="utf-8"))


def cell_snapshot(cell):
    return {
        "attributes": {key: value for key, value in cell.attrib.items() if key != WRAPPER_MARKER},
        "geometry": geometry(cell),
    }


def contains_expected(actual, expected):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(key in actual and contains_expected(actual[key], value) for key, value in expected.items())
    if isinstance(expected, list):
        return actual == expected
    return actual == expected


def style_map(style):
    result = []
    for token in (style or "").split(";"):
        if not token:
            continue
        key, sep, value = token.partition("=")
        result.append([key, value if sep else None])
    return result


def set_style(cell, updates):
    tokens = style_map(cell.get("style", ""))
    keys = {key for key, _ in tokens}
    rendered = []
    for key, value in tokens:
        if key in updates:
            value = updates[key]
        rendered.append(key if value is None else f"{key}={value}")
    for key, value in updates.items():
        if key not in keys:
            rendered.append(key if value is None else f"{key}={value}")
    cell.set("style", ";".join(rendered) + (";" if rendered else ""))


def style_value_local(cell, key):
    for token_key, value in style_map(cell.get("style", "")):
        if token_key == key:
            return value
    return None


def require_geometry(cell):
    node = cell.find("mxGeometry")
    if node is None:
        node = ET.SubElement(cell, "mxGeometry", {"as": "geometry"})
    return node


def check_precondition(cell, precondition):
    if not precondition:
        raise SupervisorError("every patch operation requires a precondition")
    if precondition.get("target_exists") is False:
        raise SupervisorError(f"precondition expected target {cell.get('id')!r} to be absent")
    if not any(key in precondition for key in ("target_hash", "cell_hash", "expected_value", "expected_parent_id", "attributes")):
        raise SupervisorError(f"precondition for {cell.get('id')!r} requires an old value, parent, or target hash")
    expected_hash = precondition.get("target_hash") or precondition.get("cell_hash")
    if expected_hash and cell_hash(cell) != expected_hash:
        raise SupervisorError(f"precondition cell_hash failed for {cell.get('id')}")
    expected = precondition.get("attributes", {})
    for key, value in expected.items():
        if cell.get(key) != value:
            raise SupervisorError(f"precondition attribute {key!r} failed for {cell.get('id')}")
    if "expected_parent_id" in precondition and cell.get("parent") != precondition.get("expected_parent_id"):
        raise SupervisorError(f"precondition expected_parent_id failed for {cell.get('id')}")
    if "expected_value" in precondition:
        expected_value = precondition["expected_value"]
        actual_value = cell_snapshot(cell) if isinstance(expected_value, dict) else cell.get("value")
        if not contains_expected(actual_value, expected_value):
            raise SupervisorError(f"precondition expected_value failed for {cell.get('id')}")


def set_points(cell, points):
    node = require_geometry(cell)
    for old in list(node.findall("Array[@as='points']")):
        node.remove(old)
    array = ET.SubElement(node, "Array", {"as": "points"})
    for point in points:
        ET.SubElement(array, "mxPoint", {"x": str(float(point["x"])), "y": str(float(point["y"]))})


def absolute_rect(cell, by_id):
    geo = geometry(cell)
    if not geo or not all(key in geo for key in ("width", "height")):
        return None
    x = float(geo.get("x", 0))
    y = float(geo.get("y", 0))
    parent_id = cell.get("parent")
    seen = set()
    while parent_id and parent_id not in {"0", "1"} and parent_id not in seen:
        seen.add(parent_id)
        parent = by_id.get(parent_id)
        if parent is None:
            break
        parent_geo = geometry(parent) or {}
        x += float(parent_geo.get("x", 0))
        y += float(parent_geo.get("y", 0))
        parent_id = parent.get("parent")
    return (x, y, float(geo["width"]), float(geo["height"]))


def edge_endpoint(cell, by_id, end):
    vertex = by_id.get(cell.get(end))
    box = absolute_rect(vertex, by_id) if vertex is not None else None
    if not box:
        return None
    x, y, width, height = box
    x_key = "exitX" if end == "source" else "entryX"
    y_key = "exitY" if end == "source" else "entryY"
    fx = float(style_value_local(cell, x_key) or 0.5)
    fy = float(style_value_local(cell, y_key) or 0.5)
    return (x + fx * width, y + fy * height)


def assert_orthogonal_route(cell, by_id):
    raw = geometry(cell) or {}
    waypoints = [(point["x"], point["y"]) for point in raw.get("points", [])]
    source = edge_endpoint(cell, by_id, "source")
    target = edge_endpoint(cell, by_id, "target")
    if not waypoints or source is None or target is None:
        raise SupervisorError(f"edge {cell.get('id')!r} lacks reconstructable route geometry")
    route = [source, *waypoints, target]
    for first, second in zip(route, route[1:]):
        if abs(first[0] - second[0]) > 1e-6 and abs(first[1] - second[1]) > 1e-6:
            raise SupervisorError(f"edge {cell.get('id')!r} route is not orthogonal")


def segment_hits_box(a, b, box, clearance=4.0):
    x, y, width, height = box
    left, right = x - clearance, x + width + clearance
    top, bottom = y - clearance, y + height + clearance
    if a[0] == b[0]:
        return left < a[0] < right and max(min(a[1], b[1]), top) < min(max(a[1], b[1]), bottom)
    if a[1] == b[1]:
        return top < a[1] < bottom and max(min(a[0], b[0]), left) < min(max(a[0], b[0]), right)
    return True


def ancestor_ids(cell, by_id):
    result = set()
    parent_id = cell.get("parent") if cell is not None else None
    while parent_id and parent_id not in result:
        result.add(parent_id)
        parent = by_id.get(parent_id)
        parent_id = parent.get("parent") if parent is not None else None
    return result


def is_routing_annotation(cell):
    tokens = set((cell.get("style") or "").split(";"))
    values = dict((key, value) for key, value in style_map(cell.get("style", "")) if value is not None)
    return "text" in tokens or (values.get("strokeColor") == "none" and values.get("fillColor") == "none")


def route_patch(path, edge_id, finding_ids=None, page_id=None):
    raw, root, cells = safe_parse(path)
    matching_pages = [
        (candidate_page_id, page)
        for candidate_page_id, page in page_scopes(root)
        if edge_id in page_by_id(page)
        and (page_id is None or candidate_page_id == page_id)
    ]
    if not matching_pages:
        raise SupervisorError(f"unknown edge {edge_id!r} in page {page_id!r}")
    if len(matching_pages) != 1:
        raise SupervisorError(f"edge {edge_id!r} is ambiguous across pages; provide page_id")
    page_id, page = matching_pages[0]
    scoped_cells = page_cells(page)
    by_id = page_by_id(page)
    edge = by_id.get(edge_id)
    if edge is None or edge.get("edge") != "1":
        raise SupervisorError(f"unknown edge {edge_id!r}")
    source = by_id.get(edge.get("source"))
    target = by_id.get(edge.get("target"))
    source_box = absolute_rect(source, by_id) if source is not None else None
    target_box = absolute_rect(target, by_id) if target is not None else None
    if not source_box or not target_box:
        raise SupervisorError("edge endpoints require absolute vertex geometry")
    sx, sy, sw, sh = source_box
    tx, ty, tw, th = target_box
    source_center = (sx + sw / 2, sy + sh / 2)
    target_center = (tx + tw / 2, ty + th / 2)
    dx = target_center[0] - source_center[0]
    dy = target_center[1] - source_center[1]
    if abs(dx) >= abs(dy):
        start = (sx + sw if dx >= 0 else sx, source_center[1])
        end = (tx if dx >= 0 else tx + tw, target_center[1])
        pins = {"exitX": 1.0 if dx >= 0 else 0.0, "exitY": 0.5, "entryX": 0.0 if dx >= 0 else 1.0, "entryY": 0.5}
    else:
        start = (source_center[0], sy + sh if dy >= 0 else sy)
        end = (target_center[0], ty if dy >= 0 else ty + th)
        pins = {"exitX": 0.5, "exitY": 1.0 if dy >= 0 else 0.0, "entryX": 0.5, "entryY": 0.0 if dy >= 0 else 1.0}
    obstacles = []
    parent_ids = {cell.get("parent") for cell in scoped_cells if cell.get("parent")}
    endpoint_ancestors = ancestor_ids(source, by_id) | ancestor_ids(target, by_id)
    for cell in scoped_cells:
        if cell.get("vertex") != "1" or cell.get("id") in {source.get("id"), target.get("id")}:
            continue
        if cell.get("id") in parent_ids or cell.get("id") in endpoint_ancestors or is_routing_annotation(cell):
            continue
        box = absolute_rect(cell, by_id)
        if box and box[2] > 0 and box[3] > 0:
            obstacles.append((cell.get("id"), box))
    margin = 24.0
    mid_x = (start[0] + end[0]) / 2
    mid_y = (start[1] + end[1]) / 2
    bounds = [box for _, box in obstacles] + [source_box, target_box]
    left = min(box[0] for box in bounds) - margin
    right = max(box[0] + box[2] for box in bounds) + margin
    top = min(box[1] for box in bounds) - margin
    bottom = max(box[1] + box[3] for box in bounds) + margin
    candidates = [
        [(end[0], start[1])],
        [(start[0], end[1])],
        [(mid_x, start[1]), (mid_x, end[1])],
        [(start[0], mid_y), (end[0], mid_y)],
        [(left, start[1]), (left, end[1])],
        [(right, start[1]), (right, end[1])],
        [(start[0], top), (end[0], top)],
        [(start[0], bottom), (end[0], bottom)],
    ]
    scored = []
    for points in candidates:
        route = [start, *points, end]
        hits = sum(segment_hits_box(a, b, box) for a, b in zip(route, route[1:]) for _, box in obstacles)
        length = sum(abs(a[0] - b[0]) + abs(a[1] - b[1]) for a, b in zip(route, route[1:]))
        scored.append((hits, len(points), length, points))
    hits, _, _, points = min(scored)
    if hits:
        raise SupervisorError("no obstacle-free local orthogonal route found")
    route_operation = {
        "operation_id": f"route-{edge_id}",
        "op": "set_edge_route",
        "target_id": edge_id,
        "precondition": {"target_exists": True, "target_hash": cell_hash(edge)},
        "proposed_value": {"waypoints": [{"x": x, "y": y} for x, y in points], "orthogonal": True},
        "semantic_effect": "layout_only",
        "reasons": ["replace a straight waypoint-free connector with an obstacle-aware orthogonal route"],
        "finding_ids": finding_ids or [],
        "rollback": {"action": "restore_value", "value": {"cell_xml": ET.tostring(edge, encoding="unicode")}},
    }
    routed_edge = copy.deepcopy(edge)
    set_points(routed_edge, route_operation["proposed_value"]["waypoints"])
    set_style(routed_edge, {"edgeStyle": "orthogonalEdgeStyle", "rounded": "0", "orthogonalLoop": "1"})
    pin_operation = {
        "operation_id": f"pins-{edge_id}",
        "op": "set_edge_pins",
        "target_id": edge_id,
        "precondition": {"target_exists": True, "target_hash": cell_hash(routed_edge)},
        "proposed_value": {
            "source": {"x": pins["exitX"], "y": pins["exitY"]},
            "target": {"x": pins["entryX"], "y": pins["entryY"]},
        },
        "semantic_effect": "layout_only",
        "reasons": ["use explicit distinct terminal pins for deterministic routing"],
        "finding_ids": finding_ids or [],
        "rollback": {"action": "restore_value", "value": {"cell_xml": ET.tostring(routed_edge, encoding="unicode")}},
    }
    return {
        "schema_version": 1,
        "patch_id": str(uuid.uuid4()),
        "created_at": utc_now(),
        "created_by": "tool",
        "baseline": {"artifact_sha256": sha256_bytes(raw), "semantic_digest": document_semantic_digest(root)},
        "operations": [route_operation, pin_operation],
        "affected_region": {"page_id": page_id, "cell_ids": [edge_id, source.get("id"), target.get("id")]},
    }


def apply_operation(root, by_id, operation):
    op = operation.get("op")
    target = str(operation.get("target_id", ""))
    cell = by_id.get(target)
    if op == "add_semantic_element":
        if cell is not None:
            raise SupervisorError(f"target {target!r} already exists")
        if operation.get("precondition", {}).get("target_exists") is not False and not operation.get("precondition", {}).get("absent"):
            raise SupervisorError("add_semantic_element requires absent precondition")
        parents = root.findall(".//mxGraphModel/root")
        if len(parents) != 1:
            raise SupervisorError("semantic add currently requires exactly one page")
        value = operation.get("proposed_value", operation.get("value", {}))
        attrs = {str(key): str(val) for key, val in value.get("attributes", {}).items()}
        attrs["id"] = target
        if "label" in value:
            attrs["value"] = str(value["label"])
        if value.get("parent_id") is not None:
            attrs["parent"] = str(value["parent_id"])
        if value.get("source_id") is not None:
            attrs["source"] = str(value["source_id"])
        if value.get("target_id") is not None:
            attrs["target"] = str(value["target_id"])
        attrs[SEMANTIC_TYPE_ATTR] = str(value["semantic_type"])
        if value.get("relationship") is not None:
            attrs[RELATIONSHIP_ATTR] = str(value["relationship"])
        if value.get("kind") == "vertex":
            attrs["vertex"] = "1"
        elif value.get("kind") == "edge":
            attrs["edge"] = "1"
        elif value.get("kind") == "group":
            attrs["vertex"] = "1"
            attrs["connectable"] = "0"
            style = attrs.get("style", "")
            attrs["style"] = style + (";" if style and not style.endswith(";") else "") + "group;"
        cell = ET.SubElement(parents[0], "mxCell", attrs)
        if value.get("geometry") is not None:
            geo = ET.SubElement(cell, "mxGeometry", {"as": "geometry"})
            for key, val in value["geometry"].items():
                if key != "points":
                    geo.set(key, str(val))
        by_id[target] = cell
        return
    if cell is None:
        raise SupervisorError(f"unknown target cell {target!r}")
    if target in {"0", "1"} and op == "remove_semantic_element":
        raise SupervisorError("reserved root/layer cells cannot be removed")
    check_precondition(cell, operation.get("precondition"))
    value = operation.get("proposed_value", operation.get("value", {}))
    if op in {"move_vertex", "resize_vertex", "resize_container"}:
        if cell.get("vertex") != "1":
            raise SupervisorError(f"{op} requires a vertex target")
        if op == "resize_container" and inferred_semantic_type(cell) != "container":
            raise SupervisorError("resize_container requires container semantics")
        node = require_geometry(cell)
        allowed = {
            "move_vertex": ("x", "y"),
            "resize_vertex": ("width", "height"),
            "resize_container": ("width", "height"),
        }[op]
        for key in allowed:
            if key not in value:
                raise SupervisorError(f"{op} requires {key}")
            node.set(key, str(float(value[key])))
    elif op == "set_label_offset":
        if cell.get("edge") != "1":
            raise SupervisorError("set_label_offset requires an edge target")
        if "x" not in value or "y" not in value:
            raise SupervisorError("set_label_offset requires x and y label position")
        node = require_geometry(cell)
        node.set("relative", "1")
        node.set("x", str(float(value["x"])))
        node.set("y", str(float(value["y"])))
        for old in list(node.findall("mxPoint[@as='offset']")):
            node.remove(old)
        offset = value.get("offset", {"x": 0, "y": 0})
        ET.SubElement(node, "mxPoint", {
            "as": "offset", "x": str(float(offset["x"])), "y": str(float(offset["y"])),
        })
    elif op == "set_edge_pins":
        if cell.get("edge") != "1":
            raise SupervisorError(f"{target!r} is not an edge")
        if "source" in value and "target" in value:
            value = {"exitX": value["source"]["x"], "exitY": value["source"]["y"], "entryX": value["target"]["x"], "entryY": value["target"]["y"]}
        required = ("exitX", "exitY", "entryX", "entryY")
        if any(key not in value for key in required):
            raise SupervisorError("set_edge_pins requires source/target or exitX, exitY, entryX, entryY")
        if any(not 0 <= float(value[key]) <= 1 for key in required):
            raise SupervisorError("edge pin coordinates must be normalized to the 0..1 perimeter range")
        set_style(cell, {key: str(float(value[key])) for key in required})
    elif op == "set_edge_route":
        points = value.get("waypoints", value.get("points"))
        if cell.get("edge") != "1" or not isinstance(points, list) or not points:
            raise SupervisorError("set_edge_route requires an edge and non-empty points")
        set_points(cell, points)
        set_style(cell, {"edgeStyle": "orthogonalEdgeStyle", "rounded": "0", "orthogonalLoop": "1"})
        pins = value.get("pins")
        if pins:
            required = ("exitX", "exitY", "entryX", "entryY")
            if any(key not in pins for key in required):
                raise SupervisorError("route pins require exitX, exitY, entryX, entryY")
            set_style(cell, {key: str(float(pins[key])) for key in required})
    elif op == "remove_semantic_element":
        refs = [other.get("id") for other in by_id.values() if target in {other.get("parent"), other.get("source"), other.get("target")}]
        if refs:
            raise SupervisorError(f"cannot remove {target!r}; referenced by {refs}")
        removed = False
        for wrapper in [*root.findall(".//UserObject"), *root.findall(".//object")]:
            if wrapper.find("mxCell") is cell:
                for parent in root.iter():
                    if wrapper in list(parent):
                        parent.remove(wrapper)
                        removed = True
                        break
                break
        if not removed:
            for parent in root.iter():
                if cell in list(parent):
                    parent.remove(cell)
                    break
        del by_id[target]
    else:
        raise SupervisorError(f"unsupported patch operation {op!r}")


def rollback_snapshot(scope_root, cell, operation, page_id):
    op = operation["op"]
    action = (
        "remove_added_cell" if op == "add_semantic_element"
        else "restore_removed_cell" if op == "remove_semantic_element"
        else "restore_value"
    )
    if cell is None:
        return {"action": action, "value": {"page_id": page_id, "target_id": operation["target_id"]}}
    owner = cell
    for wrapper in [*scope_root.findall(".//UserObject"), *scope_root.findall(".//object")]:
        if wrapper.find("mxCell") is cell:
            owner = wrapper
            break
    parent = None
    index = None
    for candidate in scope_root.iter():
        children = list(candidate)
        if owner in children:
            parent = candidate
            index = children.index(owner)
            break
    return {
        "action": action,
        "value": {
            "page_id": page_id,
            "target_id": operation["target_id"],
            "owner_xml": ET.tostring(owner, encoding="unicode"),
            "cell_xml": ET.tostring(cell, encoding="unicode"),
            "wrapped": owner is not cell,
            "parent_tag": parent.tag if parent is not None else None,
            "parent_cell_id": parent.get("id") if parent is not None else None,
            "insertion_index": index,
        },
    }


def validate_rollback_claim(operation, expected):
    claimed = operation.get("rollback") or {}
    if claimed.get("action") != expected["action"]:
        raise SupervisorError(f"rollback action mismatch for {operation['operation_id']!r}")
    claimed_value = claimed.get("value")
    if not isinstance(claimed_value, dict):
        raise SupervisorError(f"rollback value for {operation['operation_id']!r} must be an object")
    if expected["action"] == "restore_value":
        claimed_xml = claimed_value.get("cell_xml")
        if claimed_xml is not None and claimed_xml != expected["value"]["cell_xml"]:
            raise SupervisorError(f"rollback cell_xml mismatch for {operation['operation_id']!r}")
    if expected["action"] == "restore_removed_cell":
        claimed_xml = claimed_value.get("owner_xml", claimed_value.get("cell_xml"))
        if claimed_xml != expected["value"]["owner_xml"]:
            raise SupervisorError(f"rollback removed XML mismatch for {operation['operation_id']!r}")


def apply_patch_file(source, patch_path, output, allow_semantic=False):
    source = Path(source)
    output = Path(output)
    if source.resolve() == output.resolve():
        raise SupervisorError("output must differ from source; promote a validated candidate explicitly")
    raw, root, cells = safe_parse(source)
    patch = load_json(patch_path)
    validate_patch_contract(patch)
    baseline = patch["baseline"]
    expected_artifact = baseline["artifact_sha256"]
    if expected_artifact != sha256_bytes(raw):
        raise SupervisorError("patch base_artifact_sha256 does not match source")
    semantic_effects = {op.get("semantic_effect", "layout_only") for op in patch.get("operations", [])}
    if any(effect not in {"layout-only", "layout_only"} for effect in semantic_effects) and not allow_semantic:
        raise SupervisorError("semantic patch requires explicit --allow-semantic after human approval")
    declared_region = set(patch["affected_region"]["cell_ids"])
    declared_page_id = patch["affected_region"]["page_id"]
    declared_page = next((page for page_id, page in page_scopes(root) if page_id == declared_page_id), None)
    if declared_page is None:
        raise SupervisorError(f"affected_region page {declared_page_id!r} does not exist")
    by_id = page_by_id(declared_page)
    before_hashes = document_cell_hashes(root)
    page_cell_ids = set(by_id)
    if not declared_region <= page_cell_ids | {operation.get("target_id") for operation in patch["operations"] if operation.get("op") == "add_semantic_element"}:
        raise SupervisorError("affected_region contains cells outside its declared page")
    operation_targets = {str(operation["target_id"]) for operation in patch["operations"]}
    if not operation_targets <= declared_region:
        raise SupervisorError(f"patch targets outside affected_region: {sorted(operation_targets - declared_region)}")
    before_semantic = document_semantic_digest(root)
    rollback = []
    for operation in patch.get("operations", []):
        target = operation.get("target_id")
        previous = by_id.get(str(target))
        snapshot = rollback_snapshot(declared_page, previous, operation, declared_page_id)
        validate_rollback_claim(operation, snapshot)
        rollback.append(snapshot)
        apply_operation(declared_page, by_id, operation)
    for operation in patch.get("operations", []):
        if operation.get("op") == "set_edge_route":
            assert_orthogonal_route(by_id[str(operation.get("target_id"))], by_id)
    after_semantic = document_semantic_digest(root)
    after_hashes = document_cell_hashes(root)
    changed_ids = {
        key for key in set(before_hashes) | set(after_hashes)
        if before_hashes.get(key) != after_hashes.get(key)
    }
    declared_keys = {(declared_page_id, cell_id) for cell_id in declared_region}
    if not changed_ids <= declared_keys:
        raise SupervisorError(f"transaction changed cells outside affected_region: {sorted(changed_ids - declared_keys)}")
    expected_semantic = baseline.get("semantic_digest")
    if expected_semantic and expected_semantic != before_semantic:
        raise SupervisorError("patch baseline semantic_digest does not match source")
    layout_only = all(op.get("semantic_effect", "layout_only") in {"layout-only", "layout_only"} for op in patch.get("operations", []))
    if layout_only and before_semantic != after_semantic:
        raise SupervisorError("layout-only transaction changed semantic digest")
    denormalize_wrappers(root)
    payload = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=output.parent, delete=False) as tmp:
        tmp.write(payload)
        temp_name = tmp.name
    validator = Path(__file__).with_name("validate.py").resolve()
    structural_check = subprocess.run(
        [sys.executable, str(validator), temp_name, "--json"],
        text=True, capture_output=True, check=False,
    )
    if structural_check.returncode != 0:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise SupervisorError(
            "candidate failed structural validation before publication: "
            + (structural_check.stdout.strip() or structural_check.stderr.strip())[:1000]
        )
    os.replace(temp_name, output)
    result = {
        "status": "applied",
        "base_artifact_sha256": sha256_bytes(raw),
        "candidate_artifact_sha256": sha256_bytes(payload),
        "semantic_digest_before": before_semantic,
        "semantic_digest_after": after_semantic,
        "affected_elements": [
            {"page_id": page_id, "cell_id": cell_id}
            for page_id, cell_id in sorted(changed_ids)
        ],
        "rollback": rollback,
    }
    return result


def spec_diff(before, after):
    left = {(page["id"], item["id"]): item for page in before["pages"] for item in page["cells"]}
    right = {(page["id"], item["id"]): item for page in after["pages"] for item in page["cells"]}
    semantic = {"added": [], "removed": [], "changed": []}
    layout = []
    for key in sorted(set(left) | set(right)):
        page_id, cell_id = key
        identity = {"page_id": page_id, "cell_id": cell_id}
        if key not in left:
            semantic["added"].append(identity)
            continue
        if key not in right:
            semantic["removed"].append(identity)
            continue
        semantic_keys = ("kind", "semantic_type", "label", "parent_id", "source_id", "target_id", "relationship")
        changes = {field: [left[key].get(field), right[key].get(field)] for field in semantic_keys if left[key].get(field) != right[key].get(field)}
        if changes:
            semantic["changed"].append({**identity, "changes": changes})
        layout_changes = {}
        for field in ("style", "geometry"):
            if left[key].get(field) != right[key].get(field):
                layout_changes[field] = [left[key].get(field), right[key].get(field)]
        if layout_changes:
            layout.append({**identity, "changes": layout_changes})
    return {"semantic": semantic, "layout": layout, "semantic_digest_equal": before["semantic_digest"]["value"] == after["semantic_digest"]["value"]}


def quality_vector(report):
    vector = {key: 0 for key in QUALITY_KEYS}
    for finding in report.get("findings", []):
        code = finding.get("code", "")
        severity = finding.get("severity")
        if "semantic" in code or finding.get("layer") == "round-trip":
            vector["semantic_violations"] += 1
        elif code in QUALITY_CODE_MAP:
            vector[QUALITY_CODE_MAP[code]] += 1
        elif severity == "error" and (
            code.startswith(STRUCTURAL_CODE_PREFIXES)
            or finding.get("layer") == "artifact-parse"
        ):
            vector["structural_errors"] += 1
        elif severity == "error":
            # Fail closed: an unknown validator error is structural until a stable mapping exists.
            vector["structural_errors"] += 1
        elif severity == "warning":
            # Unknown readability warnings remain visible in the uncertainty gate.
            vector["routing_uncertainty"] += 1
    vector["route_complexity"] = int(report.get("metrics", {}).get("route_complexity", 0))
    return vector


def artifact_invariants(path):
    _, root, _ = safe_parse(path)
    return document_semantic_digest(root), document_cell_hashes(root)


def compare_reports(baseline, candidate, semantic_equal=True, untouched_equal=True):
    before = quality_vector(baseline)
    after = quality_vector(candidate)
    if not semantic_equal:
        return {"accepted": False, "reason": "semantic_digest_changed", "baseline": before, "candidate": after}
    if not untouched_equal:
        return {"accepted": False, "reason": "untouched_region_changed", "baseline": before, "candidate": after}
    for key in QUALITY_KEYS:
        if after[key] > before[key]:
            return {"accepted": False, "reason": f"higher_priority_regression:{key}", "baseline": before, "candidate": after}
        if after[key] < before[key]:
            return {"accepted": True, "reason": f"lexicographic_improvement:{key}", "baseline": before, "candidate": after}
    return {"accepted": False, "reason": "no_improvement", "baseline": before, "candidate": after}


def resolve_model(
    policy_path, role, native_available=False, isolated_available=False,
    current_model=None, current_provider=None, run_dir=None,
):
    policy = load_json(policy_path)
    if role not in policy.get("roles", {}):
        raise SupervisorError(f"unknown model role {role!r}")
    config = policy["roles"][role]
    requested = config["requested_model"]
    availability = {
        "native_per_agent": native_available,
        "isolated_cli": isolated_available,
        "inherited_current": bool(current_model),
    }
    selected_index = next(
        (index for index, candidate in enumerate(config["fallback_order"]) if availability[candidate]),
        None,
    )
    if selected_index is None:
        raise SupervisorError("model resolution requires native support, isolated CLI support, or current_model fallback")
    mode = config["fallback_order"][selected_index]
    if mode == "inherited_current":
        resolved, provider = current_model, current_provider or "unknown"
        reason = "runtime has neither a verified isolated explicit-model CLI nor native per-agent model overrides"
    else:
        resolved, provider, reason = requested, config["provider"], None
    result = {
        "role": role,
        "requested_model": requested,
        "resolved_model": resolved,
        "provider": provider,
        "resolution_mode": mode,
        "fallback_used": selected_index > 0 or resolved != requested,
        "degradation_reason": reason,
        "resolved_at": utc_now(),
    }
    if run_dir:
        append_event(run_dir, "model_resolved", result, state=(load_state(run_dir) or {}).get("state"))
    return result


def ensure_run_id(run_dir):
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    state_path = run_dir / "state.json"
    state = load_json(state_path) if state_path.exists() else None
    if state and state.get("run_id"):
        return state["run_id"]
    marker = run_dir / ".run-id"
    if marker.exists():
        return marker.read_text(encoding="utf-8").strip()
    run_id = str(uuid.uuid4())
    marker.write_text(run_id + "\n", encoding="utf-8")
    return run_id


def append_event(run_dir, event_type, payload, state=None, actor=None):
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = run_dir / "run-manifest.jsonl"
    sequence = 1
    previous_event_sha256 = None
    if manifest.exists():
        with open(manifest, encoding="utf-8") as handle:
            lines = [line.rstrip("\n") for line in handle if line.strip()]
        sequence += len(lines)
        if lines:
            previous_event_sha256 = sha256_bytes(lines[-1].encode("utf-8"))
    event = {
        "schema_version": 1, "run_id": ensure_run_id(run_dir), "sequence": sequence,
        "event_id": str(uuid.uuid4()), "timestamp": utc_now(), "event_type": event_type,
        "actor": actor or {"kind": "tool", "id": "diagram-supervisor", "model": None},
        "previous_event_sha256": previous_event_sha256,
        "payload": payload,
    }
    if state is not None:
        event["state"] = state
    with open(manifest, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return event


def _is_within(path, parent):
    path = str(Path(path).resolve())
    parent = str(Path(parent).resolve())
    try:
        return os.path.commonpath((path, parent)) == parent
    except ValueError:
        return False


def _resolve_executable(value):
    expanded = Path(value).expanduser()
    if expanded.is_absolute() or os.sep in value or (os.altsep and os.altsep in value):
        candidate = expanded.resolve()
    else:
        located = shutil.which(value, path=os.environ.get("PATH"))
        if not located:
            raise SupervisorError(f"host CLI executable was not found: {value}")
        candidate = Path(located).resolve()
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise SupervisorError(f"host CLI is not an executable file: {candidate}")
    return candidate


def host_preflight(workspace, run_dir, cli):
    """Prove that the interactive extension host can own a run.

    The check intentionally runs in the parent session. A successful native
    agent call is not equivalent evidence because corporate Qwen agents may
    inherit the parent model and may not be able to execute extension tools.
    """
    extension_root = Path(__file__).resolve().parent.parent
    workspace = Path(workspace).expanduser().resolve()
    run_dir = Path(run_dir).expanduser().resolve()
    if not workspace.is_dir():
        raise SupervisorError(f"host workspace is not a directory: {workspace}")
    if not _is_within(run_dir, workspace):
        raise SupervisorError("host run directory must be inside the workspace")
    if _is_within(run_dir, extension_root):
        raise SupervisorError("host run directory must not be inside the installed extension")

    required = {
        name: extension_root / "scripts" / name
        for name in ("diagram_supervisor.py", "agent_runtime.py", "validate.py")
    }
    missing = [str(path) for path in required.values() if not path.is_file()]
    if missing:
        raise SupervisorError(f"host extension prerequisites are missing: {', '.join(missing)}")
    cli_path = _resolve_executable(cli)
    try:
        cli_probe = subprocess.run(
            [str(cli_path), "--version"],
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
            env={
                key: value for key, value in os.environ.items()
                if key in {"HOME", "LANG", "LC_ALL", "LC_CTYPE", "NO_COLOR", "PATH", "TMPDIR"}
            },
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SupervisorError(f"host CLI version probe failed: {exc}") from exc
    if cli_probe.returncode != 0:
        raise SupervisorError(
            f"host CLI version probe exited with {cli_probe.returncode}: "
            f"{(cli_probe.stderr or cli_probe.stdout).strip()[:512]}"
        )
    cli_version = (cli_probe.stdout or cli_probe.stderr).strip().splitlines()
    run_dir.mkdir(parents=True, exist_ok=True)
    run_id = ensure_run_id(run_dir)

    result = {
        "schema_version": 1,
        "run_id": run_id,
        "checked_at": utc_now(),
        "execution_owner": "main_extension_host",
        "native_supervisor_execution": False,
        "extension_root": str(extension_root),
        "workspace": str(workspace),
        "run_dir": str(run_dir),
        "cli": str(cli_path),
        "cli_version": cli_version[0][:512] if cli_version else "unknown",
        "required_tools": {
            name: {"path": str(path), "sha256": sha256_file(path)}
            for name, path in required.items()
        },
    }
    write_json(run_dir / "host-preflight.json", result)
    evidence_sha256 = sha256_file(run_dir / "host-preflight.json")
    append_event(
        run_dir,
        "host_preflight",
        {
            "execution_owner": result["execution_owner"],
            "native_supervisor_execution": False,
            "evidence": str((run_dir / "host-preflight.json").resolve()),
            "evidence_sha256": evidence_sha256,
            "cli": str(cli_path),
        },
        actor={"kind": "system", "id": "main-extension-host", "model": None},
    )
    return result


def verify_host_preflight(run_dir):
    run_dir = Path(run_dir).expanduser().resolve()
    evidence_path = run_dir / "host-preflight.json"
    manifest_path = run_dir / "run-manifest.jsonl"
    marker_path = run_dir / ".run-id"
    checks = {
        "evidence_exists": evidence_path.is_file(),
        "manifest_exists": manifest_path.is_file(),
        "run_id_exists": marker_path.is_file(),
    }
    if not all(checks.values()):
        return {"valid": False, "checks": checks}
    try:
        evidence = load_json(evidence_path)
        run_id = marker_path.read_text(encoding="utf-8").strip()
        events = [
            json.loads(line)
            for line in manifest_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, json.JSONDecodeError):
        checks["evidence_parse"] = False
        return {"valid": False, "checks": checks}

    extension_root = Path(__file__).resolve().parent.parent
    required = {
        name: extension_root / "scripts" / name
        for name in ("diagram_supervisor.py", "agent_runtime.py", "validate.py")
    }
    evidence_sha256 = sha256_file(evidence_path)
    checks.update({
        "schema_version": evidence.get("schema_version") == 1,
        "execution_owner": evidence.get("execution_owner") == "main_extension_host",
        "native_supervisor_disabled": evidence.get("native_supervisor_execution") is False,
        "run_id_match": bool(run_id) and evidence.get("run_id") == run_id,
        "run_dir_match": evidence.get("run_dir") == str(run_dir),
        "workspace_contains_run": bool(evidence.get("workspace"))
        and _is_within(run_dir, evidence["workspace"]),
        "extension_root_match": evidence.get("extension_root") == str(extension_root),
        "cli_executable": bool(evidence.get("cli"))
        and Path(evidence["cli"]).is_file()
        and os.access(evidence["cli"], os.X_OK),
        "cli_version_recorded": bool(evidence.get("cli_version")),
        "required_tool_set": set(evidence.get("required_tools", {})) == set(required),
    })
    for name, path in required.items():
        descriptor = evidence.get("required_tools", {}).get(name, {})
        checks[f"tool_{name}"] = (
            path.is_file()
            and descriptor.get("path") == str(path)
            and descriptor.get("sha256") == sha256_file(path)
        )
    checks["manifest_event"] = any(
        event.get("run_id") == run_id
        and event.get("event_type") == "host_preflight"
        and event.get("actor", {}).get("id") == "main-extension-host"
        and event.get("payload", {}).get("execution_owner") == "main_extension_host"
        and event.get("payload", {}).get("native_supervisor_execution") is False
        and event.get("payload", {}).get("evidence") == str(evidence_path)
        and event.get("payload", {}).get("evidence_sha256") == evidence_sha256
        for event in events
    )
    return {"valid": all(checks.values()), "checks": checks, "run_id": run_id}


def state_lock_is_live(lock_path):
    try:
        pid = int(Path(lock_path).read_text(encoding="ascii").strip())
        os.kill(pid, 0)
        return True
    except (FileNotFoundError, ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True


def recover_pending_transaction(run_dir):
    run_dir = Path(run_dir)
    pending_path = run_dir / ".state-transaction.json"
    if not pending_path.exists():
        return False
    lock_path = run_dir / ".state.lock"
    if lock_path.exists() and state_lock_is_live(lock_path):
        return False
    if lock_path.exists():
        lock_path.unlink()
    pending = load_json(pending_path)
    transaction_id = pending["transaction_id"]
    write_json(run_dir / "state.json", pending["state"])
    manifest = run_dir / "run-manifest.jsonl"
    recorded = False
    if manifest.exists():
        for line in manifest.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get("payload", {}).get("transaction_id") == transaction_id:
                recorded = True
                break
    if not recorded:
        payload = dict(pending["payload"])
        payload["transaction_id"] = transaction_id
        append_event(
            run_dir, pending["event_type"], payload,
            state=pending.get("event_state"), actor=pending.get("actor"),
        )
    pending_path.unlink()
    return True


def commit_state_event(run_dir, state, event_type, payload, *, event_state=None, actor=None):
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    recover_pending_transaction(run_dir)
    lock_path = run_dir / ".state.lock"
    for attempt in range(2):
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError as exc:
            if attempt == 0 and not state_lock_is_live(lock_path):
                lock_path.unlink(missing_ok=True)
                continue
            raise SupervisorError("another state transaction is in progress") from exc
    try:
        os.write(lock_fd, f"{os.getpid()}\n".encode("ascii"))
        os.fsync(lock_fd)
        transaction_id = str(uuid.uuid4())
        pending = {
            "transaction_id": transaction_id,
            "state": state,
            "event_type": event_type,
            "event_state": event_state,
            "payload": payload,
            "actor": actor,
        }
        pending_path = run_dir / ".state-transaction.json"
        write_json(pending_path, pending)
        write_json(run_dir / "state.json", state)
        event_payload = dict(payload)
        event_payload["transaction_id"] = transaction_id
        append_event(run_dir, event_type, event_payload, state=event_state, actor=actor)
        pending_path.unlink()
    finally:
        os.close(lock_fd)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def validation_output_dir(run_root, attempt_id):
    if attempt_id is None:
        return run_root
    value = str(attempt_id)
    if value in {".", ".."} or not ATTEMPT_ID_RE.fullmatch(value) or "/" in value or "\\" in value:
        raise SupervisorError("attempt_id must be an opaque slug without paths or separators")
    attempts_root = (run_root / "attempts").resolve()
    output_dir = (attempts_root / value).resolve()
    if output_dir.parent != attempts_root or run_root not in output_dir.parents:
        raise SupervisorError("attempt_id resolves outside the run attempts directory")
    return output_dir


def run_validation(artifact, run_dir, profile=None, source=None, attempt_id=None):
    artifact = Path(artifact).resolve()
    run_root = Path(run_dir).resolve()
    run_root.mkdir(parents=True, exist_ok=True)
    output_dir = validation_output_dir(run_root, attempt_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    validator = Path(__file__).with_name("validate.py").resolve()
    command = [sys.executable, str(validator), str(artifact), "--strict", "--json"]
    if profile:
        if not source:
            raise SupervisorError("source-aware validation requires --source")
        command.extend(["--profile", profile, "--source", str(Path(source).resolve())])
    started = utc_now()
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    finished = utc_now()
    stdout_path = output_dir / "validator.stdout"
    stderr_path = output_dir / "validator.stderr"
    report_path = output_dir / "validation-report.json"
    stdout_path.write_text(completed.stdout, encoding="utf-8")
    stderr_path.write_text(completed.stderr, encoding="utf-8")
    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SupervisorError(f"validator did not emit JSON: {exc}") from exc
    write_json(report_path, report)
    run_id = ensure_run_id(run_root)
    receipt = {
        "schema_version": 1,
        "receipt_id": str(uuid.uuid4()),
        "run_id": run_id,
        "attempt_id": attempt_id or output_dir.name,
        "artifact": {"path": str(artifact), "sha256": sha256_file(artifact), "byte_length": artifact.stat().st_size},
        "command": command,
        "exit_code": completed.returncode,
        "strict": True,
        "validator": {
            "name": "publish-drawio-validator", "path": str(validator),
            "file_sha256": sha256_file(validator), "version": report.get("validator", {}).get("version") or "unknown",
        },
        "outputs": {
            "report": {"path": str(report_path), "sha256": sha256_file(report_path), "byte_length": report_path.stat().st_size},
            "stdout_sha256": sha256_file(stdout_path),
            "stderr_sha256": sha256_file(stderr_path),
        },
        "started_at": started,
        "finished_at": finished,
        "platform": {
            "python": platform.python_version(), "system": platform.system() or "unknown",
            "release": platform.release() or "unknown", "machine": platform.machine() or "unknown",
        },
        "tool_versions": {"diagram_supervisor": VERSION, "validator": report.get("validator", {}).get("version") or "unknown"},
        "result": "passed" if completed.returncode == 0 else "failed",
    }
    receipt_path = output_dir / "validation-receipt.json"
    write_json(receipt_path, receipt)
    append_event(run_root, "validation_receipt", {"receipt": str(receipt_path), "artifact_sha256": receipt["artifact"]["sha256"], "exit_code": completed.returncode}, state="validating")
    return receipt


def verify_receipt(receipt_path, artifact=None):
    receipt = load_json(receipt_path)
    artifact_path = Path(artifact or receipt["artifact"]["path"])
    checks = {
        "artifact_hash": artifact_path.exists() and sha256_file(artifact_path) == receipt["artifact"]["sha256"],
        "validator_hash": Path(receipt["validator"]["path"]).exists() and sha256_file(receipt["validator"]["path"]) == receipt["validator"]["file_sha256"],
        "strict": receipt.get("strict") is True,
    }
    report_descriptor = receipt["outputs"]["report"]
    report_path = Path(report_descriptor["path"])
    checks["report_hash"] = report_path.exists() and sha256_file(report_path) == report_descriptor["sha256"]
    report = None
    if checks["report_hash"]:
        try:
            report = load_json(report_path)
        except (OSError, json.JSONDecodeError):
            report = None
    checks["report_artifact_hash"] = bool(report) and report.get("artifact_sha256") == receipt["artifact"]["sha256"]
    checks["report_validator_identity"] = bool(report) and report.get("validator", {}).get("name") == receipt["validator"]["name"] and report.get("validator", {}).get("version") == receipt["validator"]["version"]
    expected_result = "passed" if receipt.get("exit_code") == 0 else "failed"
    checks["result_consistent"] = receipt.get("result") == expected_result and bool(report) and report.get("summary", {}).get("status") == expected_result
    command = receipt.get("command", [])
    checks["command_bound"] = (
        str(artifact_path.resolve()) in command
        and "--strict" in command
        and "--json" in command
        and receipt["validator"]["path"] in command
    )
    receipt_dir = Path(receipt_path).resolve().parent
    stdout_path = receipt_dir / "validator.stdout"
    stderr_path = receipt_dir / "validator.stderr"
    checks["stdout_hash"] = stdout_path.exists() and sha256_file(stdout_path) == receipt["outputs"]["stdout_sha256"]
    checks["stderr_hash"] = stderr_path.exists() and sha256_file(stderr_path) == receipt["outputs"]["stderr_sha256"]
    stdout_report = None
    if checks["stdout_hash"]:
        try:
            stdout_report = json.loads(stdout_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            stdout_report = None
    checks["report_stdout_match"] = bool(report) and stdout_report is not None and report == stdout_report
    return {"valid": all(checks.values()), "passed": receipt.get("exit_code") == 0, "checks": checks}


def load_state(run_dir):
    recover_pending_transaction(run_dir)
    path = Path(run_dir) / "state.json"
    return load_json(path) if path.exists() else None


def transition(run_dir, target, artifact=None, receipt=None, decision=None, reason=None, max_attempts=None):
    if target not in STATES:
        raise SupervisorError(f"unknown state {target!r}")
    current = load_state(run_dir)
    if (current is None and target == "analyzed") or target == "completed":
        preflight = verify_host_preflight(run_dir)
        if not preflight["valid"]:
            raise SupervisorError(f"main-host preflight evidence failed: {preflight['checks']}")
    if current is None:
        if target != "analyzed":
            raise SupervisorError("new run must start in analyzed")
        state = {
            "schema_version": 1, "run_id": ensure_run_id(run_dir), "state": target,
            "created_at": utc_now(), "seen_hashes": [], "seen_vectors": [],
            "attempt_count": 0, "max_attempts": max_attempts or DEFAULT_MAX_ATTEMPTS,
            "repair_class_attempts": {},
        }
    else:
        if current["state"] in TERMINAL_STATES:
            raise SupervisorError("terminal run cannot transition")
        if target not in TRANSITIONS.get(current["state"], set()):
            raise SupervisorError(f"invalid transition {current['state']} -> {target}")
        state = copy.deepcopy(current)
        state["state"] = target
    if artifact:
        digest = sha256_file(artifact)
        if target == "accepted_candidate":
            raise SupervisorError("accepted_candidate can only be entered through the candidate evidence gate")
        if current is None and target == "analyzed":
            if digest not in state["seen_hashes"]:
                state["seen_hashes"].append(digest)
            state["accepted_artifact"] = {"path": str(Path(artifact).resolve()), "sha256": digest}
        elif state.get("accepted_artifact", {}).get("sha256") != digest:
            raise SupervisorError("state transition artifact must equal the last accepted artifact")
    if target == "completed":
        if not artifact or not receipt:
            raise SupervisorError("completion requires artifact and receipt")
        if decision not in {"approve", "approved"}:
            raise SupervisorError("completion requires an explicit approve decision")
        verification = verify_receipt(receipt, artifact)
        if not verification["valid"] or not verification["passed"]:
            raise SupervisorError(f"completion evidence failed: {verification['checks']}")
        receipt_data = load_json(receipt)
        if receipt_data.get("run_id") != state.get("run_id"):
            raise SupervisorError("completion receipt belongs to a different run")
        state["final_receipt"] = str(Path(receipt).resolve())
    if target == "approved_with_findings" and decision != "approve_with_findings":
        raise SupervisorError("approved_with_findings requires decision='approve_with_findings'")
    if target == "awaiting_feedback" and decision == "pause" and not reason:
        raise SupervisorError("pause requires an explicit reason")
    if current and current.get("state") == "awaiting_feedback" and target != "stopped":
        if decision not in {"resume", "continue"} or not reason:
            raise SupervisorError("resume requires decision='resume' or 'continue' and an explicit reason")
    if decision:
        state.setdefault("decisions", []).append({"timestamp": utc_now(), "decision": decision, "reason": reason})
    state["updated_at"] = utc_now()
    commit_state_event(
        run_dir, state,
        "terminal_state" if target in TERMINAL_STATES else "state_transition",
        {"artifact": state.get("accepted_artifact"), "decision": decision, "reason": reason},
        event_state=target,
    )
    return state


def load_reviewer_verdict(path, run_id, candidate_sha256, report_path, receipt_path):
    verdict = load_json(path)
    schema = load_json(Path(__file__).resolve().parent.parent / "data" / "reviewer-verdict.v1.schema.json")
    errors = sorted(
        jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).iter_errors(verdict),
        key=lambda error: (list(error.path), error.message),
    )
    if errors:
        raise SupervisorError(f"reviewer verdict schema validation failed: {errors[0].message}")
    expected = {
        "run_id": run_id,
        "candidate_sha256": candidate_sha256,
        "report_sha256": sha256_file(report_path),
        "receipt_sha256": sha256_file(receipt_path),
    }
    mismatches = [key for key, value in expected.items() if verdict.get(key) != value]
    if mismatches:
        raise SupervisorError(f"reviewer verdict evidence mismatch: {', '.join(mismatches)}")
    return verdict


def make_reviewer_input(run_dir, candidate, report_path, receipt_path, patch_path):
    state = load_state(run_dir)
    if state is None:
        raise SupervisorError("reviewer input requires an initialized run")
    candidate_verification = verify_receipt(receipt_path, candidate)
    if not candidate_verification["valid"]:
        raise SupervisorError(f"reviewer input receipt failed: {candidate_verification['checks']}")
    candidate_receipt = load_json(receipt_path)
    if candidate_receipt.get("run_id") != state.get("run_id"):
        raise SupervisorError("reviewer input receipt belongs to a different run")
    baseline_path = Path(state["accepted_artifact"]["path"])
    accepted_validation = state.get("accepted_validation") or {}
    baseline_report_path = Path(accepted_validation.get("report") or Path(run_dir) / "attempts/baseline/validation-report.json")
    baseline_receipt_path = Path(accepted_validation.get("receipt") or Path(run_dir) / "attempts/baseline/validation-receipt.json")
    baseline_verification = verify_receipt(baseline_receipt_path, baseline_path)
    if not baseline_verification["valid"]:
        raise SupervisorError(f"reviewer baseline receipt failed: {baseline_verification['checks']}")
    baseline_receipt = load_json(baseline_receipt_path)
    if baseline_receipt.get("run_id") != state.get("run_id"):
        raise SupervisorError("reviewer baseline receipt belongs to a different run")
    baseline_spec = make_spec(baseline_path)
    candidate_spec = make_spec(candidate)
    diff = spec_diff(baseline_spec, candidate_spec)
    baseline_report = load_json(baseline_report_path)
    candidate_report = load_json(report_path)
    resolutions = []
    manifest = Path(run_dir) / "run-manifest.jsonl"
    if manifest.exists():
        resolutions = [
            event["payload"] for event in (
                json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines() if line.strip()
            )
            if event.get("event_type") == "model_resolved"
        ]
    result = {
        "schema_version": 1,
        "run_id": state["run_id"],
        "baseline": {
            "artifact": {"path": str(baseline_path.resolve()), "sha256": sha256_file(baseline_path)},
            "report": {"path": str(baseline_report_path.resolve()), "sha256": sha256_file(baseline_report_path), "content": baseline_report},
            "receipt": {"path": str(baseline_receipt_path.resolve()), "sha256": sha256_file(baseline_receipt_path), "content": baseline_receipt},
            "strict_passed": baseline_verification["passed"],
        },
        "candidate": {
            "artifact": {"path": str(Path(candidate).resolve()), "sha256": sha256_file(candidate)},
            "report": {"path": str(Path(report_path).resolve()), "sha256": sha256_file(report_path), "content": candidate_report},
            "receipt": {"path": str(Path(receipt_path).resolve()), "sha256": sha256_file(receipt_path), "content": candidate_receipt},
            "strict_passed": candidate_verification["passed"],
        },
        "patch": {
            "path": str(Path(patch_path).resolve()),
            "sha256": sha256_file(patch_path),
            "content": load_json(patch_path),
        },
        "baseline_spec": baseline_spec,
        "candidate_spec": candidate_spec,
        "diff": diff,
        "quality": {
            "baseline": quality_vector(baseline_report),
            "candidate": quality_vector(candidate_report),
            "comparison": compare_reports(baseline_report, candidate_report, diff["semantic_digest_equal"], True),
        },
        "context": {
            "source_refs": baseline_spec["source_refs"],
            "user_openspec_refs": [
                ref for ref in baseline_spec["source_refs"]
                if ref["kind"] in {"explicit_user_decision", "confirmed_clarification", "openspec"}
            ],
        },
        "model_resolutions": resolutions,
    }
    schema = load_json(Path(__file__).resolve().parent.parent / "data" / "reviewer-input.v1.schema.json")
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(result)
    return result


def semantic_diff_value(baseline, candidate):
    return spec_diff(make_spec(baseline), make_spec(candidate))["semantic"]


def create_semantic_approval(run_dir, baseline, candidate, patch_path, decision, approver="user"):
    if decision not in {"approve", "reject"}:
        raise SupervisorError("semantic approval decision must be approve or reject")
    state = load_state(run_dir)
    if state is None or state.get("accepted_artifact", {}).get("sha256") != sha256_file(baseline):
        raise SupervisorError("semantic approval baseline must be the current accepted artifact")
    semantic_diff = semantic_diff_value(baseline, candidate)
    if not semantic_diff.get("added") and not semantic_diff.get("removed") and not semantic_diff.get("changed"):
        raise SupervisorError("semantic approval requires a non-empty semantic diff")
    return {
        "schema_version": 1,
        "approval_id": str(uuid.uuid4()),
        "run_id": state["run_id"],
        "patch_sha256": sha256_file(patch_path),
        "candidate_sha256": sha256_file(candidate),
        "semantic_diff": semantic_diff,
        "semantic_diff_sha256": canonical_hash(semantic_diff),
        "decision": decision,
        "decided_at": utc_now(),
        "approver": {"kind": "human", "id": approver},
    }


def load_semantic_approval(path, run_id, patch_sha256, candidate_sha256, semantic_diff):
    approval = load_json(path)
    schema = load_json(Path(__file__).resolve().parent.parent / "data" / "semantic-approval.v1.schema.json")
    errors = sorted(
        jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).iter_errors(approval),
        key=lambda error: (list(error.path), error.message),
    )
    if errors:
        raise SupervisorError(f"semantic approval schema validation failed: {errors[0].message}")
    expected = {
        "run_id": run_id,
        "patch_sha256": patch_sha256,
        "candidate_sha256": candidate_sha256,
        "semantic_diff_sha256": canonical_hash(semantic_diff),
    }
    mismatches = [key for key, value in expected.items() if approval.get(key) != value]
    if approval.get("semantic_diff") != semantic_diff:
        mismatches.append("semantic_diff")
    if mismatches:
        raise SupervisorError(f"semantic approval evidence mismatch: {', '.join(mismatches)}")
    return approval


def record_candidate(
    run_dir, artifact, baseline_report_path, candidate_report_path, patch_path,
    baseline_receipt_path, receipt_path,
    semantic_equal=True, untouched_equal=True, repair_class=None,
    reviewer_verdict_path=None, review_exception=None, semantic_approval_path=None,
):
    state = load_state(run_dir)
    if state is None or state.get("state") != "validating":
        raise SupervisorError("candidate decision requires a run in validating state")
    artifact_hash = sha256_file(artifact)
    baseline_report = load_json(baseline_report_path)
    candidate_report = load_json(candidate_report_path)
    accepted_artifact = state.get("accepted_artifact") or {}
    baseline_path = accepted_artifact.get("path")
    baseline_hash = accepted_artifact.get("sha256")
    if not baseline_path or not baseline_hash or sha256_file(baseline_path) != baseline_hash:
        raise SupervisorError("last accepted baseline is missing or changed")
    if baseline_report.get("artifact_sha256") != baseline_hash:
        raise SupervisorError("baseline report is not bound to the last accepted artifact")
    if candidate_report.get("artifact_sha256") != artifact_hash:
        raise SupervisorError("candidate report is not bound to the candidate artifact")
    patch = load_json(patch_path)
    validate_patch_contract(patch)
    patch_sha256 = sha256_file(patch_path)
    if patch["baseline"]["artifact_sha256"] != baseline_hash:
        raise SupervisorError("candidate patch is not based on the last accepted artifact")
    semantic_patch = any(
        operation.get("semantic_effect", "layout_only") not in {"layout-only", "layout_only"}
        for operation in patch["operations"]
    )
    with tempfile.TemporaryDirectory(prefix="candidate-replay-", dir=Path(run_dir)) as replay_dir:
        replay_path = Path(replay_dir) / "candidate.drawio"
        apply_patch_file(
            baseline_path, patch_path, replay_path, allow_semantic=semantic_patch,
        )
        replay_sha256 = sha256_file(replay_path)
    if replay_sha256 != artifact_hash:
        append_event(
            run_dir, "candidate_rejected",
            {
                "reason": "patch_replay_mismatch",
                "patch_sha256": patch_sha256,
                "provided_candidate_sha256": artifact_hash,
                "verified_candidate_sha256": replay_sha256,
            },
            state="validating",
        )
        raise SupervisorError("candidate artifact does not match deterministic patch replay")
    baseline_receipt = load_json(baseline_receipt_path)
    baseline_receipt_verification = verify_receipt(baseline_receipt_path, baseline_path)
    if not baseline_receipt_verification["valid"]:
        raise SupervisorError(f"baseline validation receipt failed: {baseline_receipt_verification['checks']}")
    if baseline_receipt.get("run_id") != state.get("run_id"):
        raise SupervisorError("baseline receipt belongs to a different run")
    if baseline_receipt["outputs"]["report"]["sha256"] != sha256_file(baseline_report_path):
        raise SupervisorError("baseline report hash differs from its receipt")
    accepted_validation = state.get("accepted_validation")
    if accepted_validation and (
        accepted_validation.get("report_sha256") != sha256_file(baseline_report_path)
        or accepted_validation.get("receipt_sha256") != sha256_file(baseline_receipt_path)
    ):
        raise SupervisorError("baseline evidence differs from the last accepted candidate evidence")
    receipt = load_json(receipt_path)
    receipt_verification = verify_receipt(receipt_path, artifact)
    if not receipt_verification["valid"]:
        raise SupervisorError(f"candidate validation receipt failed: {receipt_verification['checks']}")
    if receipt.get("run_id") != state.get("run_id"):
        raise SupervisorError("candidate receipt belongs to a different run")
    if receipt["outputs"]["report"]["sha256"] != sha256_file(candidate_report_path):
        raise SupervisorError("candidate report hash differs from the receipt")
    reviewer_verdict = None
    if reviewer_verdict_path:
        reviewer_verdict = load_reviewer_verdict(
            reviewer_verdict_path, state["run_id"], artifact_hash,
            candidate_report_path, receipt_path,
        )
        append_event(
            run_dir, "review_verdict",
            {
                "verdict": reviewer_verdict["verdict"],
                "verdict_sha256": sha256_file(reviewer_verdict_path),
                "candidate_sha256": artifact_hash,
                "report_sha256": sha256_file(candidate_report_path),
            },
            state="validating",
            actor={"kind": "agent", "id": "reviewer", "model": (reviewer_verdict.get("reviewer") or {}).get("resolved_model")},
        )
    elif review_exception in {"approved_degraded_review", "manual_handoff"}:
        append_event(
            run_dir, "user_decision",
            {"decision": review_exception, "candidate_sha256": artifact_hash, "review_gate_bypassed": True},
            state="validating", actor={"kind": "human", "id": "user", "model": None},
        )
        if review_exception == "manual_handoff":
            state["state"] = "manual_handoff"
            state["updated_at"] = utc_now()
            commit_state_event(
                run_dir, state, "terminal_state",
                {
                    "decision": "manual_handoff",
                    "candidate_sha256": artifact_hash,
                    "candidate_promoted": False,
                    "accepted_artifact": state.get("accepted_artifact"),
                },
                event_state="manual_handoff",
                actor={"kind": "human", "id": "user", "model": None},
            )
            return {
                "state": "manual_handoff", "accepted": False,
                "reason": "manual_handoff", "quality_vector": quality_vector(candidate_report),
            }
    else:
        raise SupervisorError("candidate acceptance requires a hash-bound independent reviewer verdict")
    baseline_semantic, baseline_cells = artifact_invariants(baseline_path)
    candidate_semantic, candidate_cells = artifact_invariants(artifact)
    if patch["baseline"]["semantic_digest"] != baseline_semantic:
        raise SupervisorError("patch semantic baseline differs from the accepted artifact")
    affected = {
        (patch["affected_region"]["page_id"], cell_id)
        for cell_id in patch["affected_region"]["cell_ids"]
    }
    outside_ids = (set(baseline_cells) | set(candidate_cells)) - affected
    computed_untouched_equal = all(baseline_cells.get(cell_id) == candidate_cells.get(cell_id) for cell_id in outside_ids)
    computed_semantic_equal = baseline_semantic == candidate_semantic
    semantic_approval = None
    if semantic_patch:
        semantic_diff = semantic_diff_value(baseline_path, artifact)
        if not semantic_approval_path:
            raise SupervisorError("semantic candidate requires hash-bound human semantic approval")
        semantic_approval = load_semantic_approval(
            semantic_approval_path, state["run_id"], patch_sha256, artifact_hash, semantic_diff,
        )
        append_event(
            run_dir, "user_decision",
            {
                "decision": semantic_approval["decision"],
                "approval_sha256": sha256_file(semantic_approval_path),
                "patch_sha256": patch_sha256,
                "candidate_sha256": artifact_hash,
                "semantic_diff_sha256": semantic_approval["semantic_diff_sha256"],
            },
            state="validating", actor={"kind": "human", "id": semantic_approval["approver"]["id"], "model": None},
        )
        before_vector = quality_vector(baseline_report)
        after_vector = quality_vector(candidate_report)
        regression = next(
            (key for key in QUALITY_KEYS[1:] if after_vector[key] > before_vector[key]),
            None,
        )
        if semantic_approval["decision"] == "reject":
            comparison = {"accepted": False, "reason": "semantic_approval_rejected", "baseline": before_vector, "candidate": after_vector}
        elif regression:
            comparison = {"accepted": False, "reason": f"higher_priority_regression:{regression}", "baseline": before_vector, "candidate": after_vector}
        elif not (computed_untouched_equal and untouched_equal):
            comparison = {"accepted": False, "reason": "untouched_region_changed", "baseline": before_vector, "candidate": after_vector}
        else:
            comparison = {"accepted": True, "reason": "semantic_change_approved", "baseline": before_vector, "candidate": after_vector}
    else:
        comparison = compare_reports(
            baseline_report, candidate_report,
            computed_semantic_equal and semantic_equal,
            computed_untouched_equal and untouched_equal,
        )
    if reviewer_verdict and reviewer_verdict["verdict"] == "reject":
        comparison = {
            "accepted": False,
            "reason": "reviewer_rejected",
            "baseline": comparison["baseline"],
            "candidate": comparison["candidate"],
        }
    vector = quality_vector(candidate_report)
    vector_hash = canonical_hash(vector)
    repeated_hash = artifact_hash in state.get("seen_hashes", [])
    repeated_vector = vector_hash in state.get("seen_vectors", [])
    state["attempt_count"] = int(state.get("attempt_count", 0)) + 1
    class_exhausted = False
    if repair_class:
        attempts = state.setdefault("repair_class_attempts", {})
        attempts[repair_class] = int(attempts.get(repair_class, 0)) + 1
        class_exhausted = attempts[repair_class] >= DEFAULT_MAX_REPAIR_CLASS_ATTEMPTS and not comparison["accepted"]
    iteration_exhausted = state["attempt_count"] >= int(state.get("max_attempts", DEFAULT_MAX_ATTEMPTS)) and not comparison["accepted"]
    state.setdefault("seen_hashes", []).append(artifact_hash) if not repeated_hash else None
    state.setdefault("seen_vectors", []).append(vector_hash) if not repeated_vector else None
    event_payload = {
        "artifact": str(Path(artifact).resolve()),
        "candidate_sha256": artifact_hash,
        "baseline_sha256": baseline_hash,
        "candidate_report_sha256": sha256_file(candidate_report_path),
        "patch_sha256": patch_sha256,
        "verified_candidate_sha256": replay_sha256,
        "reviewer_verdict_sha256": sha256_file(reviewer_verdict_path) if reviewer_verdict_path else None,
        "semantic_approval_sha256": sha256_file(semantic_approval_path) if semantic_approval_path else None,
        "quality_vector": vector,
        "comparison": comparison,
    }
    if repeated_hash or repeated_vector or class_exhausted or iteration_exhausted:
        state["state"] = "plateau"
        if repeated_hash:
            reason = "repeated_artifact_hash"
        elif repeated_vector:
            reason = "repeated_quality_vector"
        elif class_exhausted:
            reason = f"repair_class_exhausted:{repair_class}"
        else:
            reason = "iteration_limit_exhausted"
        event_payload["reason"] = reason
        event_type = "cycle_detected" if repeated_hash else "plateau_detected"
    elif comparison["accepted"]:
        state["state"] = "accepted_candidate"
        state["accepted_artifact"] = {"path": str(Path(artifact).resolve()), "sha256": artifact_hash}
        state["accepted_validation"] = {
            "report": str(Path(candidate_report_path).resolve()),
            "report_sha256": sha256_file(candidate_report_path),
            "receipt": str(Path(receipt_path).resolve()),
            "receipt_sha256": sha256_file(receipt_path),
            "strict_passed": receipt_verification["passed"],
        }
        if semantic_approval:
            state["quality_epoch"] = int(state.get("quality_epoch", 0)) + 1
            state["quality_baseline"] = quality_vector(candidate_report)
            state["semantic_baseline_digest"] = candidate_semantic
        event_type = "candidate_accepted"
    else:
        state["state"] = "retrying"
        event_type = "candidate_rejected"
    state["updated_at"] = utc_now()
    commit_state_event(
        run_dir, state, event_type, event_payload, event_state=state["state"],
    )
    return {"state": state["state"], **comparison, "quality_vector": vector}


def parse_source_refs(values):
    refs = []
    for value in values or []:
        refs.append(load_json(value))
    return refs


def main():
    parser = argparse.ArgumentParser(description="Diagram Supervisor deterministic toolchain")
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="command", required=True)

    inspect_cmd = sub.add_parser("inspect", help="create DiagramSpec without changing the artifact")
    inspect_cmd.add_argument("artifact")
    inspect_cmd.add_argument("--output", required=True)
    inspect_cmd.add_argument("--source-ref", action="append", default=[], help="JSON file containing one source_ref")

    preflight_cmd = sub.add_parser("host-preflight", help="prove that the main extension host can own a run")
    preflight_cmd.add_argument("--workspace", required=True)
    preflight_cmd.add_argument("--run-dir", required=True)
    preflight_cmd.add_argument("--cli", required=True)

    patch_cmd = sub.add_parser("patch", help="apply a transactional patch to a new candidate")
    patch_cmd.add_argument("artifact")
    patch_cmd.add_argument("patch")
    patch_cmd.add_argument("--output", required=True)
    patch_cmd.add_argument("--result")
    patch_cmd.add_argument("--allow-semantic", action="store_true", help="apply an explicitly human-approved semantic patch")

    route_cmd = sub.add_parser("route-edge", help="propose a deterministic local orthogonal edge patch")
    route_cmd.add_argument("artifact")
    route_cmd.add_argument("edge_id")
    route_cmd.add_argument("--page-id")
    route_cmd.add_argument("--finding-id", action="append", default=[])
    route_cmd.add_argument("--output", required=True)

    diff_cmd = sub.add_parser("diff", help="separate semantic and layout changes")
    diff_cmd.add_argument("before")
    diff_cmd.add_argument("after")

    compare_cmd = sub.add_parser("compare", help="compare validation reports lexicographically")
    compare_cmd.add_argument("baseline")
    compare_cmd.add_argument("candidate")
    compare_cmd.add_argument("--semantic-changed", action="store_true")
    compare_cmd.add_argument("--untouched-changed", action="store_true")

    model_cmd = sub.add_parser("resolve-model", help="resolve one role without changing the global model")
    model_cmd.add_argument("role", choices=("supervisor", "reviewer", "repair", "semantic_analyst"))
    model_cmd.add_argument("--policy", default=str(Path(__file__).resolve().parent.parent / "data" / "model-routing.default.json"))
    model_cmd.add_argument("--native-available", action="store_true")
    model_cmd.add_argument("--isolated-available", action="store_true")
    model_cmd.add_argument("--current-model")
    model_cmd.add_argument("--current-provider")
    model_cmd.add_argument("--run-dir")

    validate_cmd = sub.add_parser("validate", help="run strict validator and write a receipt")
    validate_cmd.add_argument("artifact")
    validate_cmd.add_argument("--run-dir", required=True)
    validate_cmd.add_argument("--profile", choices=("roadmap", "gitflow"))
    validate_cmd.add_argument("--source")
    validate_cmd.add_argument("--attempt-id")

    verify_cmd = sub.add_parser("verify-receipt", help="verify validation evidence")
    verify_cmd.add_argument("receipt")
    verify_cmd.add_argument("--artifact")

    review_input_cmd = sub.add_parser("review-input", help="create exact evidence input for the read-only Reviewer")
    review_input_cmd.add_argument("run_dir")
    review_input_cmd.add_argument("candidate")
    review_input_cmd.add_argument("report")
    review_input_cmd.add_argument("receipt")
    review_input_cmd.add_argument("patch")
    review_input_cmd.add_argument("--output", required=True)

    semantic_approval_cmd = sub.add_parser("semantic-approval", help="bind an explicit human decision to one semantic patch and candidate")
    semantic_approval_cmd.add_argument("run_dir")
    semantic_approval_cmd.add_argument("baseline")
    semantic_approval_cmd.add_argument("candidate")
    semantic_approval_cmd.add_argument("patch")
    semantic_approval_cmd.add_argument("--decision", required=True, choices=("approve", "reject"))
    semantic_approval_cmd.add_argument("--approver", default="user")
    semantic_approval_cmd.add_argument("--output", required=True)

    state_cmd = sub.add_parser("state", help="persist a supervisor state transition")
    state_cmd.add_argument("run_dir")
    state_cmd.add_argument("target", choices=sorted(STATES))
    state_cmd.add_argument("--artifact")
    state_cmd.add_argument("--receipt")
    state_cmd.add_argument("--decision")
    state_cmd.add_argument("--reason")
    state_cmd.add_argument("--max-attempts", type=int)

    candidate_cmd = sub.add_parser("candidate", help="record a validated candidate decision and detect cycles/plateaus")
    candidate_cmd.add_argument("run_dir")
    candidate_cmd.add_argument("artifact")
    candidate_cmd.add_argument("baseline_report")
    candidate_cmd.add_argument("candidate_report")
    candidate_cmd.add_argument("patch")
    candidate_cmd.add_argument("baseline_receipt")
    candidate_cmd.add_argument("receipt")
    candidate_cmd.add_argument("--semantic-changed", action="store_true")
    candidate_cmd.add_argument("--untouched-changed", action="store_true")
    candidate_cmd.add_argument("--repair-class")
    candidate_cmd.add_argument("--reviewer-verdict")
    candidate_cmd.add_argument("--semantic-approval")
    candidate_cmd.add_argument("--review-exception", choices=("approved_degraded_review", "manual_handoff"))

    args = parser.parse_args()
    try:
        if args.command == "host-preflight":
            result = host_preflight(args.workspace, args.run_dir, args.cli)
        elif args.command == "inspect":
            result = make_spec(args.artifact, parse_source_refs(args.source_ref))
            write_json(args.output, result)
        elif args.command == "patch":
            result = apply_patch_file(args.artifact, args.patch, args.output, args.allow_semantic)
            if args.result:
                write_json(args.result, result)
        elif args.command == "route-edge":
            result = route_patch(args.artifact, args.edge_id, args.finding_id, args.page_id)
            write_json(args.output, result)
        elif args.command == "diff":
            result = spec_diff(make_spec(args.before), make_spec(args.after))
        elif args.command == "compare":
            result = compare_reports(load_json(args.baseline), load_json(args.candidate), not args.semantic_changed, not args.untouched_changed)
        elif args.command == "resolve-model":
            result = resolve_model(
                args.policy, args.role,
                native_available=args.native_available,
                isolated_available=args.isolated_available,
                current_model=args.current_model,
                current_provider=args.current_provider,
                run_dir=args.run_dir,
            )
        elif args.command == "validate":
            result = run_validation(args.artifact, args.run_dir, args.profile, args.source, args.attempt_id)
        elif args.command == "verify-receipt":
            result = verify_receipt(args.receipt, args.artifact)
        elif args.command == "review-input":
            result = make_reviewer_input(
                args.run_dir, args.candidate, args.report, args.receipt, args.patch,
            )
            write_json(args.output, result)
        elif args.command == "semantic-approval":
            result = create_semantic_approval(
                args.run_dir, args.baseline, args.candidate, args.patch,
                args.decision, args.approver,
            )
            write_json(args.output, result)
        elif args.command == "state":
            result = transition(
                args.run_dir, args.target, args.artifact, args.receipt,
                args.decision, args.reason, args.max_attempts,
            )
        else:
            result = record_candidate(
                args.run_dir, args.artifact, args.baseline_report, args.candidate_report,
                args.patch, args.baseline_receipt, args.receipt,
                not args.semantic_changed, not args.untouched_changed, args.repair_class,
                args.reviewer_verdict, args.review_exception,
                args.semantic_approval,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        if args.command in {"compare", "verify-receipt"} and not result.get("accepted", result.get("valid", True)):
            raise SystemExit(1)
    except (OSError, ValueError, KeyError, SupervisorError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
