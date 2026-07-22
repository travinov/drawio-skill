import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import jsonschema


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import agent_runtime
import diagram_supervisor as supervisor
import validate as drawio_validator


def assert_schema(test_case, instance, schema_name):
    schema = json.loads((ROOT / "data" / schema_name).read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker()
    )
    errors = sorted(
        validator.iter_errors(instance),
        key=lambda error: (list(error.absolute_path), error.message),
    )
    rendered = "\n".join(
        f"/{'/'.join(map(str, error.absolute_path))}: {error.message}"
        for error in errors
    )
    test_case.assertFalse(errors, rendered)


def diagram_xml(*, obstacle=False, crossing=False):
    if crossing:
        cells = """
          <mxCell id="a" value="" parent="1" vertex="1"><mxGeometry x="0" y="0" width="20" height="20" as="geometry" /></mxCell>
          <mxCell id="b" value="" parent="1" vertex="1"><mxGeometry x="200" y="200" width="20" height="20" as="geometry" /></mxCell>
          <mxCell id="c" value="" parent="1" vertex="1"><mxGeometry x="0" y="200" width="20" height="20" as="geometry" /></mxCell>
          <mxCell id="d" value="" parent="1" vertex="1"><mxGeometry x="200" y="0" width="20" height="20" as="geometry" /></mxCell>
          <mxCell id="e1" parent="1" source="a" target="b" edge="1"><mxGeometry relative="1" as="geometry" /></mxCell>
          <mxCell id="e2" parent="1" source="c" target="d" edge="1"><mxGeometry relative="1" as="geometry" /></mxCell>
        """
    else:
        middle = """
          <mxCell id="obstacle" value="Obstacle" parent="1" vertex="1">
            <mxGeometry x="140" y="0" width="80" height="60" as="geometry" />
          </mxCell>
        """ if obstacle else ""
        cells = f"""
          <mxCell id="source" value="Source" data-custom="preserve-me" parent="1" vertex="1">
            <mxGeometry x="0" y="0" width="80" height="60" as="geometry" />
          </mxCell>
          {middle}
          <mxCell id="target" value="Target" parent="1" vertex="1">
            <mxGeometry x="300" y="0" width="80" height="60" as="geometry" />
          </mxCell>
          <mxCell id="edge" value="flow" style="html=1;" parent="1" source="source" target="target" edge="1">
            <mxGeometry relative="1" as="geometry" />
          </mxCell>
        """
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<mxfile host="app.diagrams.net" custom-root="preserve-root">
  <diagram id="page-1" name="Page-1" custom-page="preserve-page">
    <mxGraphModel><root>
      <mxCell id="0" />
      <mxCell id="1" parent="0" />
      {cells}
    </root></mxGraphModel>
  </diagram>
</mxfile>
"""


def routed_diagram_xml():
    return diagram_xml().replace(
        'style="html=1;" parent="1" source="source" target="target" edge="1"',
        'style="edgeStyle=orthogonalEdgeStyle;orthogonalLoop=1;rounded=0;exitX=1;exitY=0.5;entryX=0;entryY=0.5;" parent="1" source="source" target="target" edge="1"',
    ).replace(
        '<mxGeometry relative="1" as="geometry" />',
        '<mxGeometry relative="1" as="geometry"><Array as="points"><mxPoint x="190" y="30" /><mxPoint x="190" y="30" /></Array></mxGeometry>',
    )


def clean_diagram_xml():
    return """<?xml version="1.0" encoding="UTF-8"?>
<mxfile host="app.diagrams.net">
  <diagram id="page-1" name="Page-1">
    <mxGraphModel><root>
      <mxCell id="0" />
      <mxCell id="1" parent="0" />
      <mxCell id="node" value="" parent="1" vertex="1">
        <mxGeometry x="100" y="100" width="120" height="60" as="geometry" />
      </mxCell>
    </root></mxGraphModel>
  </diagram>
</mxfile>
"""


def write_text(path, value):
    path.write_text(value, encoding="utf-8")
    return path


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def cell(path, cell_id):
    return next(item for item in ET.parse(path).findall(".//mxCell") if item.get("id") == cell_id)


def report(*findings, route_complexity=0):
    return {
        "report_version": 2,
        "findings": [
            {"layer": layer, "severity": severity, "code": code, "path": "", "message": code}
            for layer, severity, code in findings
        ],
        "metrics": {"route_complexity": route_complexity},
    }


def parsed_cells(source):
    raw, _, cells = supervisor.safe_parse(source)
    return raw, {item.get("id"): item for item in cells if item.get("id")}


def canonical_patch(source, operations, affected_ids=None, created_by="tool"):
    raw, cells = parsed_cells(source)
    semantic_baseline, _ = supervisor.artifact_invariants(source)
    return {
        "schema_version": 1,
        "patch_id": "test-patch",
        "created_at": "2026-07-16T12:00:00+00:00",
        "created_by": created_by,
        "baseline": {
            "artifact_sha256": hashlib.sha256(raw).hexdigest(),
            "semantic_digest": semantic_baseline,
        },
        "affected_region": {
            "page_id": "page-1",
            "cell_ids": affected_ids or sorted({operation["target_id"] for operation in operations}),
        },
        "operations": operations,
    }


def semantic_patch(source, operation):
    return canonical_patch(source, [operation], created_by="human")


def canonical_move_operation(source, target_id, x, y, *, target_hash=None, expected_parent="1", expected_value=None):
    _, cells = parsed_cells(source)
    target = cells[target_id]
    return {
        "operation_id": f"move-{target_id}-{x}-{y}",
        "op": "move_vertex",
        "target_id": target_id,
        "precondition": {
            "target_exists": True,
            "target_hash": target_hash or supervisor.cell_hash(target),
            "expected_parent_id": expected_parent,
            "expected_value": expected_value or {
                "attributes": {"value": target.get("value", "")},
                "geometry": {"x": float(target.find("mxGeometry").get("x", "0"))},
            },
        },
        "proposed_value": {"x": x, "y": y},
        "semantic_effect": "layout_only",
        "reasons": ["focused test move"],
        "finding_ids": [],
        "rollback": {
            "action": "restore_value",
            "value": {"cell_xml": ET.tostring(target, encoding="unicode")},
        },
    }


def canonical_route_operation(source, waypoint):
    _, cells = parsed_cells(source)
    edge = cells["edge"]
    return {
        "operation_id": "route-edge",
        "op": "set_edge_route",
        "target_id": "edge",
        "precondition": {
            "target_exists": True,
            "target_hash": supervisor.cell_hash(edge),
            "expected_parent_id": "1",
            "expected_value": {"attributes": {"source": "source", "target": "target"}},
        },
        "proposed_value": {"waypoints": [waypoint], "orthogonal": True},
        "semantic_effect": "layout_only",
        "reasons": ["focused route test"],
        "finding_ids": [],
        "rollback": {
            "action": "restore_value",
            "value": {"cell_xml": ET.tostring(edge, encoding="unicode")},
        },
    }


def create_move_candidate(temp, run_dir, baseline, attempt, x, *, route_complexity=None):
    operation = canonical_move_operation(baseline, "node", x, 100)
    patch = canonical_patch(baseline, [operation])
    patch_path = write_json(temp / f"patch-{attempt}.json", patch)
    candidate = temp / f"candidate-{attempt}.drawio"
    supervisor.apply_patch_file(baseline, patch_path, candidate)
    supervisor.run_validation(candidate, run_dir, attempt_id=attempt)
    report_path = run_dir / "attempts" / attempt / "validation-report.json"
    receipt_path = run_dir / "attempts" / attempt / "validation-receipt.json"
    if route_complexity is not None:
        report_value = json.loads(report_path.read_text(encoding="utf-8"))
        report_value["metrics"]["route_complexity"] = route_complexity
        supervisor.write_json(report_path, report_value)
        stdout_path = run_dir / "attempts" / attempt / "validator.stdout"
        stdout_path.write_text(json.dumps(report_value, ensure_ascii=False), encoding="utf-8")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["outputs"]["report"]["sha256"] = supervisor.sha256_file(report_path)
        receipt["outputs"]["report"]["byte_length"] = report_path.stat().st_size
        receipt["outputs"]["stdout_sha256"] = supervisor.sha256_file(stdout_path)
        supervisor.write_json(receipt_path, receipt)
    return candidate, patch_path, report_path, receipt_path


def reviewer_verdict(run_dir, candidate, report_path, receipt_path, *, verdict="approve", suffix=""):
    state = supervisor.load_state(run_dir)
    value = {
        "schema_version": 1,
        "verdict_id": f"review-{suffix or 'candidate'}",
        "run_id": state["run_id"],
        "candidate_sha256": supervisor.sha256_file(candidate),
        "report_sha256": supervisor.sha256_file(report_path),
        "receipt_sha256": supervisor.sha256_file(receipt_path),
        "verdict": verdict,
        "reviewed_at": "2026-07-16T12:00:00+00:00",
        "reviewer": {
            "resolved_model": "vllm/DeepSeek-V4-Flash-262k",
            "provider": "vllm",
            "resolution_mode": "isolated_cli",
        },
        "findings": [],
    }
    path = Path(run_dir) / f"reviewer-verdict{('-' + suffix) if suffix else ''}.json"
    return write_json(path, value)


def reviewer_verdict_v2(run_dir, candidate, report_path, receipt_path, *, verdict="approve", suffix=""):
    state = supervisor.load_state(run_dir)
    receipt_v2_path = Path(receipt_path).with_name("validation-receipt.v2.json")
    write_json(receipt_v2_path, {"schema_version": 2, "source": "test-fixture"})
    value = {
        "schema_version": 2,
        "verdict_id": f"review-v2-{suffix or 'candidate'}",
        "analysis_id": f"analysis-v2-{suffix or 'candidate'}",
        "run_id": state["run_id"],
        "analysis_sha256": "1" * 64,
        "role_input_sha256": "2" * 64,
        "role_output_sha256": "3" * 64,
        "bindings": {
            "candidate_sha256": supervisor.sha256_file(candidate),
            "report_sha256": supervisor.sha256_file(report_path),
            "receipt_sha256": supervisor.sha256_file(receipt_v2_path),
            "source_bundle_sha256": "4" * 64,
            "semantic_plan_sha256": None,
            "semantic_delta_sha256": None,
        },
        "runtime_proof": {
            "requested_model": "vllm/DeepSeek-V4-Flash-262k",
            "resolved_model": "vllm/DeepSeek-V4-Flash-262k",
            "provider": "vllm",
            "resolution_mode": "isolated_cli",
            "attempt_id": "contract-attempt-1",
            "evidence_sha256": "5" * 64,
        },
        "verdict": verdict,
        "reviewed_at": "2026-07-22T10:00:00+00:00",
        "findings": [],
    }
    path = Path(run_dir) / f"reviewer-verdict-v2{('-' + suffix) if suffix else ''}.json"
    return write_json(path, value)


def preflight_run(run_dir):
    run_dir = Path(run_dir)
    return supervisor.host_preflight(run_dir.parent, run_dir, sys.executable)


def prepare_routed_candidate(temp):
    temp = Path(temp)
    run_dir = temp / "run"
    baseline = write_text(temp / "baseline.drawio", diagram_xml(obstacle=True))
    preflight_run(run_dir)
    supervisor.transition(run_dir, "analyzed", artifact=baseline)
    supervisor.run_validation(baseline, run_dir, attempt_id="baseline")
    patch = supervisor.route_patch(baseline, "edge", ["route-through"])
    patch_path = write_json(temp / "route.patch.json", patch)
    candidate = temp / "candidate.drawio"
    supervisor.apply_patch_file(baseline, patch_path, candidate)
    supervisor.run_validation(candidate, run_dir, attempt_id="candidate")
    supervisor.transition(run_dir, "patching")
    supervisor.transition(run_dir, "validating")
    return {
        "run_dir": run_dir,
        "baseline": baseline,
        "candidate": candidate,
        "patch": patch_path,
        "baseline_report": run_dir / "attempts/baseline/validation-report.json",
        "baseline_receipt": run_dir / "attempts/baseline/validation-receipt.json",
        "candidate_report": run_dir / "attempts/candidate/validation-report.json",
        "candidate_receipt": run_dir / "attempts/candidate/validation-receipt.json",
    }


def prepare_semantic_candidate(temp):
    temp = Path(temp)
    run_dir = temp / "run"
    baseline = write_text(temp / "baseline.drawio", diagram_xml(obstacle=True))
    preflight_run(run_dir)
    supervisor.transition(run_dir, "analyzed", artifact=baseline)
    supervisor.run_validation(baseline, run_dir, attempt_id="baseline")
    operation = {
        "operation_id": "add-approved-step", "op": "add_semantic_element", "target_id": "approved-step",
        "precondition": {"target_exists": False},
        "proposed_value": {"kind": "vertex", "semantic_type": "process", "label": "Approved step", "parent_id": "1", "geometry": {"x": 500, "y": 100, "width": 100, "height": 40}},
        "semantic_effect": "semantic_addition", "reasons": ["user approved process step"], "finding_ids": [],
        "rollback": {"action": "remove_added_cell", "value": {}},
    }
    patch_path = write_json(temp / "semantic.patch.json", canonical_patch(baseline, [operation], created_by="human"))
    candidate = temp / "semantic-candidate.drawio"
    supervisor.apply_patch_file(baseline, patch_path, candidate, allow_semantic=True)
    supervisor.run_validation(candidate, run_dir, attempt_id="semantic")
    supervisor.transition(run_dir, "patching")
    supervisor.transition(run_dir, "validating")
    candidate_report = run_dir / "attempts/semantic/validation-report.json"
    candidate_receipt = run_dir / "attempts/semantic/validation-receipt.json"
    return {
        "run_dir": run_dir, "baseline": baseline, "candidate": candidate, "patch": patch_path,
        "baseline_report": run_dir / "attempts/baseline/validation-report.json",
        "baseline_receipt": run_dir / "attempts/baseline/validation-receipt.json",
        "candidate_report": candidate_report, "candidate_receipt": candidate_receipt,
        "reviewer": reviewer_verdict(run_dir, candidate, candidate_report, candidate_receipt),
    }


class MainExtensionHostTests(unittest.TestCase):
    def test_host_preflight_creates_auditable_main_host_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "project"
            workspace.mkdir()
            run_dir = workspace / ".diagram-runs" / "preflight-test"

            result = supervisor.host_preflight(workspace, run_dir, sys.executable)

            self.assertEqual(result["execution_owner"], "main_extension_host")
            self.assertFalse(result["native_supervisor_execution"])
            self.assertTrue(result["run_id"])
            self.assertEqual(Path(result["run_dir"]), run_dir.resolve())
            evidence = json.loads((run_dir / "host-preflight.json").read_text(encoding="utf-8"))
            self.assertEqual(evidence, result)
            events = [
                json.loads(line)
                for line in (run_dir / "run-manifest.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(events[0]["event_type"], "host_preflight")
            self.assertEqual(events[0]["actor"]["id"], "main-extension-host")
            self.assertEqual(
                events[0]["payload"]["evidence_sha256"],
                hashlib.sha256((run_dir / "host-preflight.json").read_bytes()).hexdigest(),
            )
            assert_schema(self, events[0], "run-event.v1.schema.json")
            self.assertEqual(
                set(result["required_tools"]),
                {"diagram_supervisor.py", "agent_runtime.py", "validate.py"},
            )
            self.assertTrue(supervisor.verify_host_preflight(run_dir)["valid"])

    def test_state_machine_rejects_analysis_without_host_preflight(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            artifact = write_text(temp / "diagram.drawio", clean_diagram_xml())
            with self.assertRaisesRegex(supervisor.SupervisorError, "main-host preflight"):
                supervisor.transition(temp / "run", "analyzed", artifact=artifact)

    def test_completion_rejects_tampered_host_preflight(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            artifact = write_text(temp / "diagram.drawio", clean_diagram_xml())
            run_dir = temp / "run"
            preflight_run(run_dir)
            supervisor.transition(run_dir, "analyzed", artifact=artifact)
            supervisor.run_validation(artifact, run_dir)
            supervisor.transition(run_dir, "final_review", artifact=artifact)
            evidence_path = run_dir / "host-preflight.json"
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            evidence["execution_owner"] = "native_supervisor"
            supervisor.write_json(evidence_path, evidence)

            with self.assertRaisesRegex(supervisor.SupervisorError, "main-host preflight"):
                supervisor.transition(
                    run_dir, "completed", artifact=artifact,
                    receipt=run_dir / "validation-receipt.json", decision="approve",
                )

    def test_host_preflight_rejects_run_directory_outside_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "project"
            workspace.mkdir()
            outside = root / "outside-run"

            with self.assertRaisesRegex(supervisor.SupervisorError, "inside the workspace"):
                supervisor.host_preflight(workspace, outside, sys.executable)

            self.assertFalse((outside / "host-preflight.json").exists())

    def test_host_preflight_rejects_a_cli_that_cannot_run(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "project"
            workspace.mkdir()
            cli = write_text(root / "broken-cli", "#!/bin/sh\nexit 3\n")
            cli.chmod(0o700)
            run_dir = workspace / ".diagram-runs" / "failed"

            with self.assertRaisesRegex(supervisor.SupervisorError, "version probe exited with 3"):
                supervisor.host_preflight(workspace, run_dir, cli)

            self.assertFalse((run_dir / "host-preflight.json").exists())

    def test_host_preflight_rejects_writes_inside_extension(self):
        with self.assertRaisesRegex(supervisor.SupervisorError, "installed extension"):
            supervisor.host_preflight(ROOT, ROOT / ".diagram-runs" / "forbidden", sys.executable)


class DiagramWorkingModelTests(unittest.TestCase):
    def test_make_spec_conforms_to_diagramspec_schema(self):
        with tempfile.TemporaryDirectory() as temp:
            source = write_text(Path(temp) / "source.drawio", diagram_xml())
            spec = supervisor.make_spec(source)

        assert_schema(self, spec, "diagramspec.v1.schema.json")

    def test_userobject_import_and_patch_preserve_wrapper_shape(self):
        wrapped_xml = """<?xml version="1.0" encoding="UTF-8"?>
<mxfile><diagram id="page-1" name="Page-1"><mxGraphModel><root>
  <mxCell id="0"/><mxCell id="1" parent="0"/>
  <UserObject id="wrapped" label="Wrapped label" custom="keep-me">
    <mxCell parent="1" vertex="1" style="rounded=1;">
      <mxGeometry x="10" y="20" width="100" height="40" as="geometry"/>
    </mxCell>
  </UserObject>
</root></mxGraphModel></diagram></mxfile>"""
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            source = write_text(temp / "wrapped.drawio", wrapped_xml)
            spec = supervisor.make_spec(source)
            wrapped = next(
                item for page in spec["pages"] for item in page["cells"]
                if item["id"] == "wrapped"
            )
            self.assertEqual(wrapped["label"], "Wrapped label")

            operation = canonical_move_operation(source, "wrapped", 40, 50)
            patch = canonical_patch(source, [operation])
            assert_schema(self, patch, "diagram-patch.v1.schema.json")
            candidate = temp / "candidate.drawio"
            supervisor.apply_patch_file(
                source, write_json(temp / "patch.json", patch), candidate
            )

            tree = ET.parse(candidate)
            wrapper = tree.find(".//UserObject")
            inner = wrapper.find("mxCell")
            self.assertEqual(wrapper.get("id"), "wrapped")
            self.assertEqual(wrapper.get("label"), "Wrapped label")
            self.assertEqual(wrapper.get("custom"), "keep-me")
            self.assertIsNone(inner.get("id"))
            self.assertIsNone(inner.get("value"))
            self.assertNotIn(supervisor.WRAPPER_MARKER, inner.attrib)
            self.assertEqual(
                (inner.find("mxGeometry").get("x"), inner.find("mxGeometry").get("y")),
                ("40.0", "50.0"),
            )

    def test_full_file_dtd_and_unsafe_links_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            late_dtd = (
                '<?xml version="1.0"?>\n<!--' + ("x" * 9000) + '-->\n'
                '<!DOCTYPE mxfile [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>\n'
                '<mxfile><diagram><mxGraphModel><root><mxCell id="0"/>'
                '<mxCell id="1" parent="0"/></root></mxGraphModel></diagram></mxfile>'
            )
            dtd_path = write_text(temp / "late-dtd.drawio", late_dtd)
            with self.assertRaisesRegex(supervisor.SupervisorError, "DTD and entity"):
                supervisor.safe_parse(dtd_path)

            for attribute, value in (
                ("link", "JaVaScRiPt:alert(1)"),
                ("href", "file:///etc/passwd"),
                ("href", "data:text/html,unsafe-content"),
            ):
                with self.subTest(attribute=attribute, value=value):
                    unsafe = clean_diagram_xml().replace(
                        '<mxCell id="node"', f'<mxCell {attribute}="{value}" id="node"'
                    )
                    path = write_text(temp / f"unsafe-{attribute}.drawio", unsafe)
                    with self.assertRaisesRegex(supervisor.SupervisorError, "unsafe"):
                        supervisor.safe_parse(path)

            embedded_cases = (
                "&lt;a href=&quot;java&amp;#x73;cript:alert(1)&quot;&gt;bad&lt;/a&gt;",
                "&lt;a href=&quot;java&amp;#x09;script:alert(1)&quot;&gt;bad&lt;/a&gt;",
                "&lt;a href=&quot;data&amp;#58;text / html,unsafe&quot;&gt;bad&lt;/a&gt;",
                "&lt;a href=&quot;file:///etc/passwd&quot;&gt;bad&lt;/a&gt;",
            )
            for index, embedded in enumerate(embedded_cases):
                with self.subTest(embedded=index):
                    unsafe = clean_diagram_xml().replace('value=""', f'value="{embedded}"', 1)
                    path = write_text(temp / f"unsafe-embedded-{index}.drawio", unsafe)
                    with self.assertRaisesRegex(supervisor.SupervisorError, "unsafe"):
                        supervisor.safe_parse(path)

            safe_html = clean_diagram_xml().replace(
                'value=""',
                'value="&lt;a href=&quot;https://example.com/docs?q=1&quot;&gt;docs&lt;/a&gt;"',
                1,
            )
            safe_path = write_text(temp / "safe-embedded.drawio", safe_html)
            supervisor.safe_parse(safe_path)
            plain_path = write_text(
                temp / "plain-text.drawio",
                clean_diagram_xml().replace('value=""', 'value="Discuss javascript: URLs safely"', 1),
            )
            supervisor.safe_parse(plain_path)

    def test_source_refs_preserve_existing_provenance_and_priority(self):
        with tempfile.TemporaryDirectory() as temp:
            source = write_text(Path(temp) / "source.drawio", clean_diagram_xml())
            digest = "1" * 64
            refs = [
                {"source_id": "spec", "kind": "openspec", "uri": "openspec/specs/x/spec.md", "revision": "r1", "fragment": "req", "content_hash": digest, "confidence": 0.9},
                {"source_id": "user", "kind": "explicit_user_decision", "uri": "user://decision/1", "revision": None, "fragment": None, "content_hash": digest, "confidence": 1.0},
            ]
            spec = supervisor.make_spec(source, refs)
            self.assertEqual(
                [item["kind"] for item in spec["source_refs"]],
                ["explicit_user_decision", "openspec", "existing_diagram"],
            )
            with self.assertRaisesRegex(supervisor.SupervisorError, "source_refs"):
                supervisor.make_spec(source, [{"kind": "openspec"}])

    def test_multi_page_duplicate_ids_are_page_scoped(self):
        raw = """<?xml version="1.0"?>
<mxfile><diagram id="p1" name="One"><mxGraphModel><root>
<mxCell id="0"/><mxCell id="1" parent="0"/><mxCell id="node" value="one" parent="1" vertex="1"><mxGeometry x="1" y="2" width="10" height="10" as="geometry"/></mxCell>
</root></mxGraphModel></diagram><diagram id="p2" name="Two"><mxGraphModel><root>
<mxCell id="0"/><mxCell id="1" parent="0"/><mxCell id="node" value="two" parent="1" vertex="1"><mxGeometry x="20" y="30" width="10" height="10" as="geometry"/></mxCell>
</root></mxGraphModel></diagram></mxfile>"""
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            source = write_text(temp / "two-page.drawio", raw)
            _, root, _ = supervisor.safe_parse(source)
            p1 = next(page for page_id, page in supervisor.page_scopes(root) if page_id == "p1")
            target = supervisor.page_by_id(p1)["node"]
            operation = {
                "operation_id": "move-p1-node", "op": "move_vertex", "target_id": "node",
                "precondition": {"target_exists": True, "target_hash": supervisor.cell_hash(target), "expected_parent_id": "1"},
                "proposed_value": {"x": 11, "y": 12}, "semantic_effect": "layout_only",
                "reasons": ["page-scoped move"], "finding_ids": [],
                "rollback": {"action": "restore_value", "value": {"cell_xml": ET.tostring(target, encoding="unicode")}},
            }
            patch = canonical_patch(source, [operation], ["node"])
            patch["affected_region"]["page_id"] = "p1"
            candidate = temp / "candidate.drawio"
            supervisor.apply_patch_file(source, write_json(temp / "patch.json", patch), candidate)
            tree = ET.parse(candidate).getroot()
            pages = {page.get("id"): page for page in tree.findall("diagram")}
            p1_node = supervisor.page_by_id(pages["p1"])["node"]
            p2_node = supervisor.page_by_id(pages["p2"])["node"]
            self.assertEqual(p1_node.find("mxGeometry").get("x"), "11.0")
            self.assertEqual(p2_node.find("mxGeometry").get("x"), "20")
            self.assertEqual(supervisor.make_spec(source)["semantic_digest"]["value"], patch["baseline"]["semantic_digest"])

    def test_inspect_is_read_only_and_layout_only_patch_keeps_semantic_digest(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            source = write_text(temp / "source.drawio", diagram_xml())
            original = source.read_bytes()
            before = supervisor.make_spec(source)
            patch = canonical_patch(
                source, [canonical_route_operation(source, {"x": 190, "y": 30})]
            )
            assert_schema(self, patch, "diagram-patch.v1.schema.json")
            patch_path = write_json(temp / "patch.json", patch)
            candidate = temp / "candidate.drawio"

            result = supervisor.apply_patch_file(source, patch_path, candidate)
            after = supervisor.make_spec(candidate)

            self.assertEqual(source.read_bytes(), original, "inspection/patching must not mutate the baseline")
            self.assertEqual(before["semantic_digest"], after["semantic_digest"])
            self.assertEqual(result["semantic_digest_before"], result["semantic_digest_after"])
            self.assertNotEqual(before["artifact"]["sha256"], after["artifact"]["sha256"])
            self.assertEqual(cell(candidate, "source").get("data-custom"), "preserve-me")


class TransactionalPatchTests(unittest.TestCase):
    def test_wrapped_removal_rollback_captures_owner_and_location(self):
        wrapped_xml = """<?xml version="1.0"?><mxfile><diagram id="page-1"><mxGraphModel><root>
<mxCell id="0"/><mxCell id="1" parent="0"/><UserObject id="wrapped" label="Wrapped" custom="keep"><mxCell parent="1" vertex="1"><mxGeometry x="1" y="2" width="10" height="10" as="geometry"/></mxCell></UserObject>
</root></mxGraphModel></diagram></mxfile>"""
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            source = write_text(temp / "source.drawio", wrapped_xml)
            _, root, _ = supervisor.safe_parse(source)
            page = supervisor.page_scopes(root)[0][1]
            target = supervisor.page_by_id(page)["wrapped"]
            wrapper = page.find(".//UserObject")
            operation = {
                "operation_id": "remove-wrapped", "op": "remove_semantic_element", "target_id": "wrapped",
                "precondition": {"target_exists": True, "target_hash": supervisor.cell_hash(target)},
                "proposed_value": {"remove": True}, "semantic_effect": "semantic_removal",
                "reasons": ["approved removal"], "finding_ids": [],
                "rollback": {"action": "restore_removed_cell", "value": {"owner_xml": ET.tostring(wrapper, encoding="unicode")}},
            }
            patch_path = write_json(temp / "patch.json", canonical_patch(source, [operation], created_by="human"))
            candidate = temp / "candidate.drawio"
            result = supervisor.apply_patch_file(source, patch_path, candidate, allow_semantic=True)
            snapshot = result["rollback"][0]
            self.assertTrue(snapshot["value"]["wrapped"])
            self.assertIn("<UserObject", snapshot["value"]["owner_xml"])
            self.assertIsInstance(snapshot["value"]["insertion_index"], int)
            self.assertIsNone(ET.parse(candidate).find(".//UserObject"))

            bad = json.loads(patch_path.read_text(encoding="utf-8"))
            bad["operations"][0]["rollback"]["value"] = {"cell_xml": ET.tostring(target, encoding="unicode")}
            with self.assertRaisesRegex(supervisor.SupervisorError, "rollback removed XML mismatch"):
                supervisor.apply_patch_file(
                    source, write_json(temp / "bad.json", bad), temp / "bad.drawio", allow_semantic=True,
                )

    def test_edge_label_offset_round_trips_position_and_offset_point(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            source = write_text(temp / "source.drawio", diagram_xml())
            before_spec = supervisor.make_spec(source)
            _, cells = parsed_cells(source)
            edge = cells["edge"]
            operation = {
                "operation_id": "offset-edge-label",
                "op": "set_label_offset",
                "target_id": "edge",
                "precondition": {"target_exists": True, "target_hash": supervisor.cell_hash(edge)},
                "proposed_value": {"x": 0.25, "y": -0.5, "offset": {"x": 12, "y": 8}},
                "semantic_effect": "layout_only",
                "reasons": ["move the edge label away from a crossing"],
                "finding_ids": ["label-1"],
                "rollback": {"action": "restore_value", "value": {"cell_xml": ET.tostring(edge, encoding="unicode")}},
            }
            patch = canonical_patch(source, [operation])
            assert_schema(self, patch, "diagram-patch.v1.schema.json")
            candidate = temp / "candidate.drawio"
            supervisor.apply_patch_file(source, write_json(temp / "patch.json", patch), candidate)
            geometry = cell(candidate, "edge").find("mxGeometry")
            self.assertEqual((geometry.get("x"), geometry.get("y"), geometry.get("relative")), ("0.25", "-0.5", "1"))
            offset = geometry.find("mxPoint[@as='offset']")
            self.assertEqual((offset.get("x"), offset.get("y")), ("12.0", "8.0"))
            after_spec = supervisor.make_spec(candidate)
            edge_spec = next(item for page in after_spec["pages"] for item in page["cells"] if item["id"] == "edge")
            self.assertEqual(edge_spec["geometry"]["label_offset"], {"x": 0.25, "y": -0.5, "offset": {"x": 12.0, "y": 8.0}})
            diff = supervisor.spec_diff(before_spec, after_spec)
            self.assertEqual(diff["semantic"]["changed"], [])
            self.assertTrue(any(item["cell_id"] == "edge" and "geometry" in item["changes"] for item in diff["layout"]))

    def test_existing_and_added_groups_round_trip_as_group_kind(self):
        existing_xml = """<?xml version="1.0"?><mxfile><diagram id="page-1"><mxGraphModel><root>
<mxCell id="0"/><mxCell id="1" parent="0"/><mxCell id="group" parent="1" vertex="1" connectable="0" style="group;"><mxGeometry x="1" y="2" width="100" height="80" as="geometry"/></mxCell>
</root></mxGraphModel></diagram></mxfile>"""
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            source = write_text(temp / "group.drawio", existing_xml)
            spec = supervisor.make_spec(source)
            existing = next(item for page in spec["pages"] for item in page["cells"] if item["id"] == "group")
            self.assertEqual((existing["kind"], existing["semantic_type"]), ("group", "group"))
            operation = {
                "operation_id": "add-group", "op": "add_semantic_element", "target_id": "new-group",
                "precondition": {"target_exists": False},
                "proposed_value": {"kind": "group", "semantic_type": "group", "label": "New group", "parent_id": "1", "geometry": {"x": 120, "y": 2, "width": 100, "height": 80}},
                "semantic_effect": "semantic_addition", "reasons": ["approved group"], "finding_ids": [],
                "rollback": {"action": "remove_added_cell", "value": {}},
            }
            patch = canonical_patch(source, [operation], created_by="human")
            candidate = temp / "candidate.drawio"
            supervisor.apply_patch_file(source, write_json(temp / "patch.json", patch), candidate, allow_semantic=True)
            added_cell = cell(candidate, "new-group")
            self.assertEqual((added_cell.get("vertex"), added_cell.get("connectable")), ("1", "0"))
            self.assertIn("group", added_cell.get("style"))
            added = next(item for page in supervisor.make_spec(candidate)["pages"] for item in page["cells"] if item["id"] == "new-group")
            self.assertEqual((added["kind"], added["semantic_type"]), ("group", "group"))

    def test_resize_container_rejects_plain_vertex(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            source = write_text(temp / "source.drawio", clean_diagram_xml())
            _, cells = parsed_cells(source)
            node = cells["node"]
            operation = {
                "operation_id": "resize-not-container", "op": "resize_container", "target_id": "node",
                "precondition": {"target_exists": True, "target_hash": supervisor.cell_hash(node)},
                "proposed_value": {"width": 200, "height": 100}, "semantic_effect": "layout_only",
                "reasons": ["negative test"], "finding_ids": [],
                "rollback": {"action": "restore_value", "value": {"cell_xml": ET.tostring(node, encoding="unicode")}},
            }
            with self.assertRaisesRegex(supervisor.SupervisorError, "container semantics"):
                supervisor.apply_patch_file(
                    source, write_json(temp / "patch.json", canonical_patch(source, [operation])),
                    temp / "candidate.drawio",
                )

    def test_route_patch_conforms_to_diagram_patch_schema(self):
        with tempfile.TemporaryDirectory() as temp:
            source = write_text(Path(temp) / "source.drawio", diagram_xml(obstacle=True))
            proposal = supervisor.route_patch(source, "edge", ["finding-through"])

        assert_schema(self, proposal, "diagram-patch.v1.schema.json")

    def test_route_shaped_expected_value_applies_and_stale_route_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            source = write_text(temp / "source.drawio", routed_diagram_xml())
            _, cells = parsed_cells(source)
            edge = cells["edge"]
            operation = {
                "operation_id": "route-with-operation-shaped-precondition",
                "op": "set_edge_route",
                "target_id": "edge",
                "precondition": {
                    "target_exists": True,
                    "expected_value": {
                        "orthogonal": True,
                        "waypoints": [{"x": 190.0, "y": 30.0}, {"x": 190.0, "y": 30.0}],
                    },
                },
                "proposed_value": {
                    "orthogonal": True,
                    "waypoints": [{"x": 220.0, "y": 30.0}, {"x": 220.0, "y": 30.0}],
                },
                "semantic_effect": "layout_only",
                "reasons": ["regression for corporate route repair preconditions"],
                "finding_ids": ["route-through"],
                "rollback": {
                    "action": "restore_value",
                    "value": {"cell_xml": ET.tostring(edge, encoding="unicode")},
                },
            }
            patch = canonical_patch(source, [operation])
            assert_schema(self, patch, "diagram-patch.v1.schema.json")
            candidate = temp / "candidate.drawio"
            result = supervisor.apply_patch_file(
                source, write_json(temp / "patch.json", patch), candidate,
            )
            self.assertEqual(result["status"], "applied")
            points = cell(candidate, "edge").findall("mxGeometry/Array[@as='points']/mxPoint")
            self.assertEqual(
                [(point.get("x"), point.get("y")) for point in points],
                [("220.0", "30.0"), ("220.0", "30.0")],
            )

            stale = json.loads(json.dumps(patch))
            stale["operations"][0]["precondition"]["expected_value"]["waypoints"][0]["x"] = 191.0
            blocked = temp / "blocked.drawio"
            with self.assertRaisesRegex(supervisor.SupervisorError, "precondition expected_value failed"):
                supervisor.apply_patch_file(
                    source, write_json(temp / "stale.json", stale), blocked,
                )
            self.assertFalse(blocked.exists())

    def test_operation_shaped_expected_values_cover_pins_labels_move_and_resize(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            routed = write_text(temp / "routed.drawio", routed_diagram_xml())
            _, routed_cells = parsed_cells(routed)
            edge = routed_cells["edge"]
            supervisor.check_precondition(
                edge,
                {
                    "target_exists": True,
                    "expected_value": {
                        "orthogonal": True,
                        "points": [{"x": 190.0, "y": 30.0}, {"x": 190.0, "y": 30.0}],
                    },
                },
                "set_edge_route",
            )
            supervisor.check_precondition(
                edge,
                {
                    "target_exists": True,
                    "expected_value": {
                        "source": {"x": 1.0, "y": 0.5},
                        "target": {"x": 0.0, "y": 0.5},
                    },
                },
                "set_edge_pins",
            )
            supervisor.check_precondition(
                edge,
                {
                    "target_exists": True,
                    "expected_value": {
                        "exitX": 1.0,
                        "exitY": 0.5,
                        "entryX": 0.0,
                        "entryY": 0.5,
                    },
                },
                "set_edge_pins",
            )
            with self.assertRaisesRegex(supervisor.SupervisorError, "precondition expected_value failed"):
                supervisor.check_precondition(
                    edge,
                    {
                        "target_exists": True,
                        "expected_value": {
                            "source": {"x": 0.0, "y": 0.5},
                            "target": {"x": 0.0, "y": 0.5},
                        },
                    },
                    "set_edge_pins",
                )

            edge_geometry = edge.find("mxGeometry")
            edge_geometry.set("x", "0.25")
            edge_geometry.set("y", "-0.5")
            ET.SubElement(edge_geometry, "mxPoint", {"as": "offset", "x": "12", "y": "8"})
            supervisor.check_precondition(
                edge,
                {
                    "target_exists": True,
                    "expected_value": {
                        "x": 0.25,
                        "y": -0.5,
                        "offset": {"x": 12.0, "y": 8.0},
                    },
                },
                "set_label_offset",
            )

            node_source = write_text(temp / "node.drawio", clean_diagram_xml())
            _, node_cells = parsed_cells(node_source)
            node = node_cells["node"]
            supervisor.check_precondition(
                node,
                {"target_exists": True, "expected_value": {"x": 100.0, "y": 100.0}},
                "move_vertex",
            )
            supervisor.check_precondition(
                node,
                {"target_exists": True, "expected_value": {"width": 120.0, "height": 60.0}},
                "resize_vertex",
            )
            supervisor.check_precondition(
                node,
                {
                    "target_exists": True,
                    "expected_value": {
                        "attributes": {"value": ""},
                        "geometry": {"x": 100.0},
                    },
                },
                "move_vertex",
            )

    def test_failed_precondition_does_not_publish_or_replace_output(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            source = write_text(temp / "source.drawio", diagram_xml())
            operation = canonical_move_operation(
                source, "source", 10, 20, target_hash="0" * 64
            )
            patch = canonical_patch(source, [operation])
            assert_schema(self, patch, "diagram-patch.v1.schema.json")
            patch_path = write_json(temp / "patch.json", patch)
            output = write_text(temp / "candidate.drawio", "sentinel")

            with self.assertRaisesRegex(supervisor.SupervisorError, "precondition cell_hash failed"):
                supervisor.apply_patch_file(source, patch_path, output)

            self.assertEqual(output.read_text(encoding="utf-8"), "sentinel")

    def test_diagonal_edge_route_is_rejected_before_publication(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            source = write_text(temp / "source.drawio", diagram_xml())
            patch = canonical_patch(
                source, [canonical_route_operation(source, {"x": 190, "y": 90})]
            )
            assert_schema(self, patch, "diagram-patch.v1.schema.json")
            patch_path = write_json(temp / "diagonal-patch.json", patch)
            output = write_text(temp / "candidate.drawio", "sentinel")

            with self.assertRaisesRegex(supervisor.SupervisorError, "not orthogonal"):
                supervisor.apply_patch_file(source, patch_path, output)

            self.assertEqual(output.read_text(encoding="utf-8"), "sentinel")

    def test_affected_region_and_expected_value_parent_preconditions_are_enforced(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            source = write_text(temp / "source.drawio", diagram_xml())
            valid_operation = canonical_move_operation(source, "source", 10, 20)
            valid_patch = canonical_patch(source, [valid_operation])
            assert_schema(self, valid_patch, "diagram-patch.v1.schema.json")
            valid_path = write_json(temp / "valid.json", valid_patch)
            valid_output = temp / "valid.drawio"
            supervisor.apply_patch_file(source, valid_path, valid_output)
            moved = cell(valid_output, "source").find("mxGeometry")
            self.assertEqual((moved.get("x"), moved.get("y")), ("10.0", "20.0"))

            cases = [
                (
                    "parent",
                    canonical_move_operation(source, "source", 10, 20, expected_parent="wrong"),
                    ["source"],
                    "expected_parent_id",
                ),
                (
                    "value",
                    canonical_move_operation(
                        source, "source", 10, 20,
                        expected_value={"attributes": {"value": "stale"}},
                    ),
                    ["source"],
                    "expected_value",
                ),
                (
                    "region",
                    canonical_move_operation(source, "source", 10, 20),
                    ["target"],
                    "outside affected_region",
                ),
            ]
            for name, operation, affected_ids, message in cases:
                with self.subTest(case=name):
                    patch = canonical_patch(source, [operation], affected_ids=affected_ids)
                    assert_schema(self, patch, "diagram-patch.v1.schema.json")
                    patch_path = write_json(temp / f"{name}.json", patch)
                    output = temp / f"{name}.drawio"
                    with self.assertRaisesRegex(supervisor.SupervisorError, message):
                        supervisor.apply_patch_file(source, patch_path, output)
                    self.assertFalse(output.exists())

    def test_semantic_patch_requires_explicit_opt_in_then_applies_after_approval(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            source = write_text(temp / "source.drawio", diagram_xml())
            operation = {
                "operation_id": "add-approved-node",
                "op": "add_semantic_element",
                "target_id": "approved-node",
                "precondition": {"target_exists": False},
                "proposed_value": {
                    "kind": "vertex",
                    "semantic_type": "process",
                    "label": "Approved",
                    "parent_id": "1",
                    "geometry": {"x": 100, "y": 120, "width": 100, "height": 40},
                },
                "semantic_effect": "semantic_addition",
                "reasons": ["user approved a missing process step"],
                "finding_ids": [],
                "rollback": {"action": "remove_added_cell", "value": {}},
            }
            patch = semantic_patch(source, operation)
            patch_path = write_json(temp / "semantic-patch.json", patch)
            blocked = temp / "blocked.drawio"

            with self.assertRaisesRegex(supervisor.SupervisorError, "--allow-semantic"):
                supervisor.apply_patch_file(source, patch_path, blocked)
            self.assertFalse(blocked.exists())

            approved = temp / "approved.drawio"
            result = supervisor.apply_patch_file(
                source, patch_path, approved, allow_semantic=True
            )
            self.assertTrue(approved.exists())
            self.assertEqual(cell(approved, "approved-node").get("value"), "Approved")
            self.assertEqual(cell(approved, "approved-node").get("data-semantic-type"), "process")
            approved_spec = supervisor.make_spec(approved)
            approved_element = next(
                item for page in approved_spec["pages"] for item in page["cells"]
                if item["id"] == "approved-node"
            )
            self.assertEqual(approved_element["semantic_type"], "process")
            self.assertNotEqual(
                result["semantic_digest_before"], result["semantic_digest_after"]
            )

    def test_mixed_layout_and_semantic_operations_are_rejected_without_explicit_opt_in(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            source = write_text(temp / "source.drawio", diagram_xml())
            layout_operation = canonical_route_operation(source, {"x": 190, "y": 30})
            semantic_operation = {
                "operation_id": "add-semantic-node",
                "op": "add_semantic_element",
                "target_id": "semantic-node",
                "precondition": {"target_exists": False},
                "proposed_value": {
                    "kind": "vertex",
                    "semantic_type": "process",
                    "label": "Semantic",
                    "parent_id": "1",
                    "geometry": {"x": 220, "y": 140, "width": 100, "height": 40},
                },
                "semantic_effect": "semantic_addition",
                "reasons": ["explicit semantic change mixed into a layout patch"],
                "finding_ids": [],
                "rollback": {"action": "remove_added_cell", "value": {}},
            }
            patch = canonical_patch(source, [layout_operation, semantic_operation])
            patch_path = write_json(temp / "mixed-patch.json", patch)

            with self.assertRaisesRegex(supervisor.SupervisorError, "--allow-semantic"):
                supervisor.apply_patch_file(source, patch_path, temp / "blocked.drawio")

    def test_strict_failed_candidate_skips_reviewer_and_records_review_skipped(self):
        with tempfile.TemporaryDirectory() as temp:
            case = prepare_routed_candidate(temp)
            report_path = Path(case["candidate_report"])
            receipt_path = Path(case["candidate_receipt"])
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["summary"]["status"] = "failed"
            report["findings"].append(
                {
                    "layer": "layout",
                    "severity": "warning",
                    "code": "test.warning",
                    "path": "/",
                    "message": "warning only",
                }
            )
            report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
            stdout_path = report_path.with_name("validator.stdout")
            stdout_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["exit_code"] = 1
            receipt["result"] = "failed"
            receipt["outputs"]["report"]["sha256"] = supervisor.sha256_file(report_path)
            receipt["outputs"]["report"]["byte_length"] = report_path.stat().st_size
            receipt["outputs"]["stdout_sha256"] = supervisor.sha256_file(stdout_path)
            receipt_path.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")

            self.assertFalse(supervisor.verify_receipt(receipt_path, case["candidate"])["passed"])
            result = supervisor.record_candidate(
                case["run_dir"], case["candidate"], case["baseline_report"],
                case["candidate_report"], case["patch"], case["baseline_receipt"],
                case["candidate_receipt"],
            )
            self.assertTrue(result["accepted"])

            events = [
                json.loads(line)["event_type"]
                for line in (case["run_dir"] / "run-manifest.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn("review_skipped", events)
            self.assertNotIn("review_verdict", events)

    def test_reserved_root_and_layer_cells_cannot_be_removed_even_with_semantic_opt_in(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            source = write_text(temp / "source.drawio", diagram_xml())
            by_id = {
                item.get("id"): item for item in ET.parse(source).findall(".//mxCell")
            }
            for reserved_id in ("0", "1"):
                with self.subTest(reserved_id=reserved_id):
                    operation = {
                        "operation_id": f"remove-{reserved_id}",
                        "op": "remove_semantic_element",
                        "target_id": reserved_id,
                        "precondition": {
                            "target_exists": True,
                            "target_hash": supervisor.cell_hash(by_id[reserved_id]),
                        },
                        "proposed_value": {"remove": True},
                        "semantic_effect": "semantic_removal",
                        "reasons": ["negative protection test"],
                        "finding_ids": [],
                        "rollback": {
                            "action": "restore_removed_cell",
                            "value": {"cell_xml": ET.tostring(by_id[reserved_id], encoding="unicode")},
                        },
                    }
                    patch_path = write_json(
                        temp / f"remove-{reserved_id}.json",
                        semantic_patch(source, operation),
                    )
                    output = temp / f"removed-{reserved_id}.drawio"
                    with self.assertRaisesRegex(
                        supervisor.SupervisorError, "reserved root/layer"
                    ):
                        supervisor.apply_patch_file(
                            source, patch_path, output, allow_semantic=True
                        )
                    self.assertFalse(output.exists())

    def test_route_edge_proposes_explicit_waypoints_and_terminal_pins(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            source = write_text(temp / "source.drawio", diagram_xml(obstacle=True))
            proposal = supervisor.route_patch(source, "edge", ["finding-through"])
            route_operation = next(op for op in proposal["operations"] if op["op"] == "set_edge_route")
            pin_operation = next(op for op in proposal["operations"] if op["op"] == "set_edge_pins")

            self.assertTrue(route_operation["proposed_value"]["waypoints"])
            self.assertEqual(set(pin_operation["proposed_value"]), {"source", "target"})
            self.assertNotEqual(
                pin_operation["proposed_value"]["source"],
                pin_operation["proposed_value"]["target"],
            )
            self.assertEqual(route_operation["finding_ids"], ["finding-through"])
            self.assertEqual(pin_operation["finding_ids"], ["finding-through"])

            patch_path = write_json(temp / "patch.json", proposal)
            candidate = temp / "candidate.drawio"
            supervisor.apply_patch_file(source, patch_path, candidate)
            routed = cell(candidate, "edge")
            points = routed.findall("mxGeometry/Array[@as='points']/mxPoint")
            self.assertGreaterEqual(len(points), 1)
            style = routed.get("style", "")
            for token in ("edgeStyle=orthogonalEdgeStyle", "exitX=", "exitY=", "entryX=", "entryY="):
                self.assertIn(token, style)


class MonotonicComparisonTests(unittest.TestCase):
    def test_structural_id_errors_dominate_route_improvements(self):
        baseline = report(("layout", "error", "artifact.readability.route_through"))
        candidate = report(("artifact-parse", "error", "artifact.id.duplicate"))
        rejected = supervisor.compare_reports(baseline, candidate)
        improved = supervisor.compare_reports(candidate, report())
        self.assertFalse(rejected["accepted"])
        self.assertEqual(rejected["reason"], "higher_priority_regression:structural_errors")
        self.assertTrue(improved["accepted"])
        self.assertEqual(improved["reason"], "lexicographic_improvement:structural_errors")

    def test_working_baseline_keeps_lower_crossing_count_over_worse_candidate(self):
        baseline = report(*[("layout", "error", "artifact.readability.crossing")] * 20)
        candidate = report(*[("layout", "error", "artifact.readability.crossing")] * 22)
        rejected = supervisor.compare_reports(baseline, candidate)

        self.assertFalse(rejected["accepted"])
        self.assertEqual(rejected["reason"], "higher_priority_regression:crossings")

    def test_all_validator_structural_codes_map_to_structural_errors(self):
        codes = (
            "artifact.id.missing", "artifact.id.duplicate", "artifact.cell.invalid_kind",
            "artifact.reference.unresolved", "artifact.geometry.invalid", "artifact.page.compressed",
            "artifact.source.invalid", "artifact.source.required", "artifact.structure.generic",
            "artifact.xml.parse", "artifact.future.unknown_error",
        )
        for code in codes:
            with self.subTest(code=code):
                value = supervisor.quality_vector(report(("artifact-parse", "error", code)))
                self.assertEqual(value["structural_errors"], 1)

    def test_lexicographic_improvement_can_accept_lower_priority_regression(self):
        baseline = report(
            ("layout", "error", "artifact.readability.route_through"),
            ("layout", "error", "artifact.readability.crossing"),
            route_complexity=4,
        )
        improved = report(("layout", "error", "artifact.readability.crossing"), route_complexity=4)
        lower_priority_regressed = report(
            ("layout", "error", "artifact.readability.crossing"),
            ("layout", "error", "artifact.readability.crossing"),
            route_complexity=4,
        )
        higher_priority_regressed = report(
            ("layout", "error", "artifact.readability.route_through"),
            ("layout", "error", "artifact.readability.route_through"),
            route_complexity=0,
        )

        accepted = supervisor.compare_reports(baseline, improved)
        partial = supervisor.compare_reports(baseline, lower_priority_regressed)
        rejected = supervisor.compare_reports(baseline, higher_priority_regressed)
        semantic_rejected = supervisor.compare_reports(baseline, improved, semantic_equal=False)

        self.assertTrue(accepted["accepted"])
        self.assertEqual(accepted["reason"], "lexicographic_improvement:route_through")
        self.assertTrue(partial["accepted"])
        self.assertEqual(partial["reason"], "lexicographic_improvement:route_through")
        self.assertFalse(rejected["accepted"])
        self.assertEqual(rejected["reason"], "higher_priority_regression:route_through")
        self.assertEqual(semantic_rejected["reason"], "semantic_digest_changed")


class EvidenceAndStateTests(unittest.TestCase):
    def test_reviewer_needs_human_stops_candidate_without_retrying_or_promoting(self):
        with tempfile.TemporaryDirectory() as temp:
            case = prepare_routed_candidate(temp)
            before = supervisor.load_state(case["run_dir"])["accepted_artifact"]

            result = supervisor.record_candidate(
                case["run_dir"],
                case["candidate"],
                case["baseline_report"],
                case["candidate_report"],
                case["patch"],
                case["baseline_receipt"],
                case["candidate_receipt"],
                repair_class="edge-route",
                reviewer_verdict_path=reviewer_verdict_v2(
                    case["run_dir"],
                    case["candidate"],
                    case["candidate_report"],
                    case["candidate_receipt"],
                    verdict="needs_human",
                ),
            )

            state = supervisor.load_state(case["run_dir"])
            self.assertEqual(result["state"], "awaiting_feedback")
            self.assertFalse(result["accepted"])
            self.assertEqual(result["reason"], "reviewer_needs_human")
            self.assertEqual(state["accepted_artifact"], before)
            last_event = json.loads(
                (case["run_dir"] / "run-manifest.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()[-1]
            )
            self.assertEqual(last_event["event_type"], "candidate_rejected")
            self.assertEqual(last_event["state"], "awaiting_feedback")
            self.assertEqual(last_event["payload"]["reason"], "reviewer_needs_human")

    def test_reviewer_input_binds_baseline_candidate_diff_context_and_models(self):
        with tempfile.TemporaryDirectory() as temp:
            case = prepare_routed_candidate(temp)
            value = supervisor.make_reviewer_input(
                case["run_dir"], case["candidate"], case["candidate_report"],
                case["candidate_receipt"], case["patch"],
            )
            assert_schema(self, value, "reviewer-input.v1.schema.json")
            self.assertEqual(value["baseline"]["artifact"]["sha256"], supervisor.sha256_file(case["baseline"]))
            self.assertEqual(value["candidate"]["artifact"]["sha256"], supervisor.sha256_file(case["candidate"]))
            self.assertIn("semantic", value["diff"])
            self.assertIn("layout", value["diff"])
            self.assertIn("comparison", value["quality"])
            self.assertIn("source_refs", value["context"])
            self.assertIsInstance(value["model_resolutions"], list)

            case["baseline_report"].write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(supervisor.SupervisorError, "baseline receipt failed"):
                supervisor.make_reviewer_input(
                    case["run_dir"], case["candidate"], case["candidate_report"],
                    case["candidate_receipt"], case["patch"],
                )

    def test_semantic_candidate_requires_exact_human_approval_and_can_reset_baseline(self):
        with tempfile.TemporaryDirectory() as temp:
            case = prepare_semantic_candidate(temp)
            with self.assertRaisesRegex(supervisor.SupervisorError, "semantic approval"):
                supervisor.record_candidate(
                    case["run_dir"], case["candidate"], case["baseline_report"], case["candidate_report"],
                    case["patch"], case["baseline_receipt"], case["candidate_receipt"],
                    reviewer_verdict_path=case["reviewer"],
                )

        with tempfile.TemporaryDirectory() as temp:
            case = prepare_semantic_candidate(temp)
            approval = supervisor.create_semantic_approval(
                case["run_dir"], case["baseline"], case["candidate"], case["patch"], "approve",
            )
            approval["patch_sha256"] = "0" * 64
            approval_path = write_json(Path(temp) / "approval.json", approval)
            with self.assertRaisesRegex(supervisor.SupervisorError, "evidence mismatch"):
                supervisor.record_candidate(
                    case["run_dir"], case["candidate"], case["baseline_report"], case["candidate_report"],
                    case["patch"], case["baseline_receipt"], case["candidate_receipt"],
                    reviewer_verdict_path=case["reviewer"], semantic_approval_path=approval_path,
                )

        with tempfile.TemporaryDirectory() as temp:
            case = prepare_semantic_candidate(temp)
            approval_path = write_json(
                Path(temp) / "approval.json",
                supervisor.create_semantic_approval(
                    case["run_dir"], case["baseline"], case["candidate"], case["patch"], "reject",
                ),
            )
            result = supervisor.record_candidate(
                case["run_dir"], case["candidate"], case["baseline_report"], case["candidate_report"],
                case["patch"], case["baseline_receipt"], case["candidate_receipt"],
                reviewer_verdict_path=case["reviewer"], semantic_approval_path=approval_path,
            )
            self.assertEqual((result["accepted"], result["reason"]), (False, "semantic_approval_rejected"))

        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            case = prepare_semantic_candidate(temp)
            approval_path = write_json(
                temp / "approval.json",
                supervisor.create_semantic_approval(
                    case["run_dir"], case["baseline"], case["candidate"], case["patch"], "approve",
                ),
            )
            result = supervisor.record_candidate(
                case["run_dir"], case["candidate"], case["baseline_report"], case["candidate_report"],
                case["patch"], case["baseline_receipt"], case["candidate_receipt"],
                reviewer_verdict_path=case["reviewer"], semantic_approval_path=approval_path,
            )
            self.assertEqual((result["accepted"], result["reason"]), (True, "semantic_change_approved"))
            state = supervisor.load_state(case["run_dir"])
            self.assertEqual(state["quality_epoch"], 1)
            new_digest = supervisor.artifact_invariants(case["candidate"])[0]
            self.assertEqual(state["semantic_baseline_digest"], new_digest)

            supervisor.transition(case["run_dir"], "patching")
            route_patch = supervisor.route_patch(case["candidate"], "edge", ["follow-on-route"])
            self.assertEqual(route_patch["baseline"]["semantic_digest"], new_digest)
            route_path = write_json(temp / "route.patch.json", route_patch)
            follow_on = temp / "follow-on.drawio"
            supervisor.apply_patch_file(case["candidate"], route_path, follow_on)
            supervisor.run_validation(follow_on, case["run_dir"], attempt_id="follow-on")
            supervisor.transition(case["run_dir"], "validating")
            follow_report = case["run_dir"] / "attempts/follow-on/validation-report.json"
            follow_receipt = case["run_dir"] / "attempts/follow-on/validation-receipt.json"
            follow_result = supervisor.record_candidate(
                case["run_dir"], follow_on, case["candidate_report"], follow_report,
                route_path, case["candidate_receipt"], follow_receipt,
                reviewer_verdict_path=reviewer_verdict(
                    case["run_dir"], follow_on, follow_report, follow_receipt, suffix="follow-on",
                ),
            )
            self.assertTrue(follow_result["accepted"])

    def test_terminal_and_pause_decisions_are_explicit(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            artifact = write_text(temp / "clean.drawio", clean_diagram_xml())
            run_dir = temp / "run"
            preflight_run(run_dir)
            supervisor.transition(run_dir, "analyzed", artifact=artifact)
            supervisor.transition(run_dir, "final_review", artifact=artifact)
            with self.assertRaisesRegex(supervisor.SupervisorError, "approve_with_findings"):
                supervisor.transition(run_dir, "approved_with_findings", decision="approve")
            with self.assertRaisesRegex(supervisor.SupervisorError, "pause requires"):
                supervisor.transition(run_dir, "awaiting_feedback", decision="pause")

    def test_candidate_requires_exact_reviewer_evidence_and_honors_verdict(self):
        with tempfile.TemporaryDirectory() as temp:
            case = prepare_routed_candidate(temp)
            args = (
                case["run_dir"], case["candidate"], case["baseline_report"],
                case["candidate_report"], case["patch"], case["baseline_receipt"],
                case["candidate_receipt"],
            )
            with self.assertRaisesRegex(supervisor.SupervisorError, "reviewer verdict"):
                supervisor.record_candidate(*args)

        with tempfile.TemporaryDirectory() as temp:
            case = prepare_routed_candidate(temp)
            verdict_path = reviewer_verdict(
                case["run_dir"], case["candidate"], case["candidate_report"], case["candidate_receipt"],
            )
            verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
            verdict["candidate_sha256"] = "0" * 64
            write_json(verdict_path, verdict)
            with self.assertRaisesRegex(supervisor.SupervisorError, "evidence mismatch"):
                supervisor.record_candidate(
                    case["run_dir"], case["candidate"], case["baseline_report"],
                    case["candidate_report"], case["patch"], case["baseline_receipt"],
                    case["candidate_receipt"], reviewer_verdict_path=verdict_path,
                )

        with tempfile.TemporaryDirectory() as temp:
            case = prepare_routed_candidate(temp)
            rejected = supervisor.record_candidate(
                case["run_dir"], case["candidate"], case["baseline_report"],
                case["candidate_report"], case["patch"], case["baseline_receipt"],
                case["candidate_receipt"],
                reviewer_verdict_path=reviewer_verdict(
                    case["run_dir"], case["candidate"], case["candidate_report"],
                    case["candidate_receipt"], verdict="reject",
                ),
            )
            self.assertFalse(rejected["accepted"])
            self.assertEqual(rejected["reason"], "reviewer_rejected")

        with tempfile.TemporaryDirectory() as temp:
            case = prepare_routed_candidate(temp)
            approved = supervisor.record_candidate(
                case["run_dir"], case["candidate"], case["baseline_report"],
                case["candidate_report"], case["patch"], case["baseline_receipt"],
                case["candidate_receipt"],
                reviewer_verdict_path=reviewer_verdict(
                    case["run_dir"], case["candidate"], case["candidate_report"],
                    case["candidate_receipt"], verdict="approve",
                ),
            )
            self.assertTrue(approved["accepted"])
            self.assertEqual(approved["state"], "accepted_candidate")

    def test_candidate_accepts_host_bound_v2_reviewer_verdict_and_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as temp:
            case = prepare_routed_candidate(temp)
            verdict_path = reviewer_verdict_v2(
                case["run_dir"], case["candidate"], case["candidate_report"],
                case["candidate_receipt"],
            )
            approved = supervisor.record_candidate(
                case["run_dir"], case["candidate"], case["baseline_report"],
                case["candidate_report"], case["patch"], case["baseline_receipt"],
                case["candidate_receipt"], reviewer_verdict_path=verdict_path,
            )
            self.assertTrue(approved["accepted"])
            events = [json.loads(line) for line in (case["run_dir"] / "run-manifest.jsonl").read_text().splitlines()]
            review_event = next(event for event in reversed(events) if event["event_type"] == "review_verdict")
            self.assertEqual(review_event["actor"]["model"], "vllm/DeepSeek-V4-Flash-262k")

        with tempfile.TemporaryDirectory() as temp:
            case = prepare_routed_candidate(temp)
            verdict_path = reviewer_verdict_v2(
                case["run_dir"], case["candidate"], case["candidate_report"],
                case["candidate_receipt"], suffix="tampered",
            )
            verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
            verdict["bindings"]["candidate_sha256"] = "0" * 64
            write_json(verdict_path, verdict)
            with self.assertRaisesRegex(supervisor.SupervisorError, "evidence mismatch: candidate_sha256"):
                supervisor.record_candidate(
                    case["run_dir"], case["candidate"], case["baseline_report"],
                    case["candidate_report"], case["patch"], case["baseline_receipt"],
                    case["candidate_receipt"], reviewer_verdict_path=verdict_path,
                )

    def test_candidate_needs_human_v2_verdict_cannot_promote(self):
        with tempfile.TemporaryDirectory() as temp:
            case = prepare_routed_candidate(temp)
            result = supervisor.record_candidate(
                case["run_dir"], case["candidate"], case["baseline_report"],
                case["candidate_report"], case["patch"], case["baseline_receipt"],
                case["candidate_receipt"],
                reviewer_verdict_path=reviewer_verdict_v2(
                    case["run_dir"], case["candidate"], case["candidate_report"],
                    case["candidate_receipt"], verdict="needs_human",
                ),
            )
            self.assertFalse(result["accepted"])
            self.assertEqual(result["reason"], "reviewer_needs_human")
            self.assertNotEqual(
                supervisor.load_state(case["run_dir"])["accepted_artifact"]["sha256"],
                supervisor.sha256_file(case["candidate"]),
            )

    def test_manual_handoff_never_promotes_unreviewed_candidate(self):
        with tempfile.TemporaryDirectory() as temp:
            case = prepare_routed_candidate(temp)
            accepted_before = supervisor.load_state(case["run_dir"])["accepted_artifact"]["sha256"]
            result = supervisor.record_candidate(
                case["run_dir"], case["candidate"], case["baseline_report"],
                case["candidate_report"], case["patch"], case["baseline_receipt"],
                case["candidate_receipt"], review_exception="manual_handoff",
            )
            state = supervisor.load_state(case["run_dir"])
            self.assertEqual(result["state"], "manual_handoff")
            self.assertFalse(result["accepted"])
            self.assertEqual(state["accepted_artifact"]["sha256"], accepted_before)
            self.assertNotEqual(state["accepted_artifact"]["sha256"], supervisor.sha256_file(case["candidate"]))

    def test_candidate_rejects_patch_that_does_not_replay_to_exact_artifact(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            run_dir = temp / "run"
            baseline = write_text(temp / "baseline.drawio", diagram_xml())
            preflight_run(run_dir)
            supervisor.transition(run_dir, "analyzed", artifact=baseline)
            supervisor.run_validation(baseline, run_dir, attempt_id="baseline")
            original = canonical_route_operation(baseline, {"x": 150, "y": 30})
            original["proposed_value"]["waypoints"] = [
                {"x": 150, "y": 30}, {"x": 150, "y": 70}, {"x": 340, "y": 70},
            ]
            patch_path = write_json(temp / "patch.json", canonical_patch(baseline, [original]))
            candidate = temp / "candidate.drawio"
            supervisor.apply_patch_file(baseline, patch_path, candidate)
            supervisor.run_validation(candidate, run_dir, attempt_id="candidate")
            patch = json.loads(patch_path.read_text(encoding="utf-8"))
            patch["operations"][0]["proposed_value"]["waypoints"] = [
                {"x": 180, "y": 30}, {"x": 180, "y": 90}, {"x": 340, "y": 90},
            ]
            write_json(patch_path, patch)
            supervisor.transition(run_dir, "patching")
            supervisor.transition(run_dir, "validating")
            with self.assertRaisesRegex(supervisor.SupervisorError, "patch replay"):
                supervisor.record_candidate(
                    run_dir, candidate,
                    run_dir / "attempts/baseline/validation-report.json",
                    run_dir / "attempts/candidate/validation-report.json",
                    patch_path,
                    run_dir / "attempts/baseline/validation-receipt.json",
                    run_dir / "attempts/candidate/validation-receipt.json",
                )
            event = json.loads((run_dir / "run-manifest.jsonl").read_text().splitlines()[-1])
            self.assertEqual(event["payload"]["reason"], "patch_replay_mismatch")
            self.assertEqual(event["payload"]["provided_candidate_sha256"], supervisor.sha256_file(candidate))
            self.assertIn("patch_sha256", event["payload"])
    def test_attempt_id_rejects_paths_and_escapes(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            artifact = write_text(temp / "clean.drawio", clean_diagram_xml())
            for attempt_id in ("../escape", "/tmp/escape", "nested/name", "nested\\name", ".."):
                with self.subTest(attempt_id=attempt_id):
                    with self.assertRaisesRegex(supervisor.SupervisorError, "opaque slug"):
                        supervisor.run_validation(artifact, temp / "run", attempt_id=attempt_id)

    def test_pending_state_transaction_recovers_state_and_event_once(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"
            run_dir.mkdir()
            run_id = supervisor.ensure_run_id(run_dir)
            state = {
                "schema_version": 1, "run_id": run_id, "state": "analyzed",
                "seen_hashes": [], "seen_vectors": [], "attempt_count": 0,
                "max_attempts": 1, "repair_class_attempts": {}, "updated_at": "now",
            }
            supervisor.write_json(run_dir / ".state-transaction.json", {
                "transaction_id": "recover-me", "state": state,
                "event_type": "state_transition", "event_state": "analyzed",
                "payload": {"recovered": True}, "actor": None,
            })
            write_text(run_dir / ".state.lock", "999999999\n")
            self.assertEqual(supervisor.load_state(run_dir)["state"], "analyzed")
            self.assertFalse((run_dir / ".state-transaction.json").exists())
            self.assertFalse((run_dir / ".state.lock").exists())
            supervisor.recover_pending_transaction(run_dir)
            events = [json.loads(line) for line in (run_dir / "run-manifest.jsonl").read_text().splitlines()]
            self.assertEqual(sum(event["payload"].get("transaction_id") == "recover-me" for event in events), 1)

    def test_receipt_and_every_manifest_line_conform_to_schemas(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            artifact = write_text(temp / "clean.drawio", clean_diagram_xml())
            run_dir = temp / "run"
            receipt = supervisor.run_validation(artifact, run_dir)

            assert_schema(self, receipt, "validation-receipt.v1.schema.json")
            lines = [
                json.loads(line)
                for line in (run_dir / "run-manifest.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertGreaterEqual(len(lines), 1)
            for index, event in enumerate(lines, start=1):
                with self.subTest(sequence=index, event_type=event.get("event_type")):
                    self.assertEqual(event["sequence"], index)
                    assert_schema(self, event, "run-event.v1.schema.json")

    def test_receipt_detects_artifact_and_report_tampering(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            artifact = write_text(temp / "clean.drawio", clean_diagram_xml())
            run_dir = temp / "run"
            receipt = supervisor.run_validation(artifact, run_dir)
            receipt_path = run_dir / "validation-receipt.json"

            self.assertEqual(receipt["exit_code"], 0)
            self.assertTrue(supervisor.verify_receipt(receipt_path)["valid"])

            report_path = Path(receipt["outputs"]["report"]["path"])
            original_report = report_path.read_text(encoding="utf-8")
            report_path.write_text(original_report + " ", encoding="utf-8")
            report_check = supervisor.verify_receipt(receipt_path)
            self.assertFalse(report_check["valid"])
            self.assertFalse(report_check["checks"]["report_hash"])
            report_path.write_text(original_report, encoding="utf-8")

            artifact.write_text(clean_diagram_xml() + "\n", encoding="utf-8")
            artifact_check = supervisor.verify_receipt(receipt_path)
            self.assertFalse(artifact_check["valid"])
            self.assertFalse(artifact_check["checks"]["artifact_hash"])

    def test_receipt_rejects_rehashed_content_binding_and_command_tampering(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            artifact = write_text(temp / "clean.drawio", clean_diagram_xml())
            run_dir = temp / "run"
            supervisor.run_validation(artifact, run_dir)
            receipt_path = run_dir / "validation-receipt.json"
            report_path = run_dir / "validation-report.json"

            report_value = json.loads(report_path.read_text(encoding="utf-8"))
            report_value["artifact_sha256"] = "0" * 64
            supervisor.write_json(report_path, report_value)
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["outputs"]["report"]["sha256"] = supervisor.sha256_file(report_path)
            receipt["outputs"]["report"]["byte_length"] = report_path.stat().st_size
            supervisor.write_json(receipt_path, receipt)

            rebound = supervisor.verify_receipt(receipt_path, artifact)
            self.assertFalse(rebound["valid"])
            self.assertTrue(rebound["checks"]["report_hash"])
            self.assertFalse(rebound["checks"]["report_artifact_hash"])

            report_value["artifact_sha256"] = supervisor.sha256_file(artifact)
            supervisor.write_json(report_path, report_value)
            receipt["outputs"]["report"]["sha256"] = supervisor.sha256_file(report_path)
            receipt["outputs"]["report"]["byte_length"] = report_path.stat().st_size
            receipt["command"] = [part for part in receipt["command"] if part != "--strict"]
            supervisor.write_json(receipt_path, receipt)
            command_check = supervisor.verify_receipt(receipt_path, artifact)
            self.assertFalse(command_check["valid"])
            self.assertFalse(command_check["checks"]["command_bound"])

    def test_receipt_rejects_metric_tampering_even_after_report_rehash(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            artifact = write_text(temp / "clean.drawio", clean_diagram_xml())
            run_dir = temp / "run"
            supervisor.run_validation(artifact, run_dir)
            report_path = run_dir / "validation-report.json"
            receipt_path = run_dir / "validation-receipt.json"
            value = json.loads(report_path.read_text(encoding="utf-8"))
            value["metrics"]["route_complexity"] += 123
            supervisor.write_json(report_path, value)
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["outputs"]["report"]["sha256"] = supervisor.sha256_file(report_path)
            receipt["outputs"]["report"]["byte_length"] = report_path.stat().st_size
            supervisor.write_json(receipt_path, receipt)
            verification = supervisor.verify_receipt(receipt_path, artifact)
            self.assertFalse(verification["valid"])
            self.assertTrue(verification["checks"]["report_hash"])
            self.assertFalse(verification["checks"]["report_stdout_match"])

    def test_completed_state_requires_receipt_for_the_exact_final_artifact(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            artifact = write_text(temp / "clean.drawio", clean_diagram_xml())
            run_dir = temp / "run"
            preflight_run(run_dir)
            supervisor.transition(run_dir, "analyzed", artifact=artifact)
            supervisor.run_validation(artifact, run_dir)
            receipt = run_dir / "validation-receipt.json"
            supervisor.transition(run_dir, "final_review", artifact=artifact)

            with self.assertRaisesRegex(supervisor.SupervisorError, "approve decision"):
                supervisor.transition(
                    run_dir, "completed", artifact=artifact, receipt=receipt
                )
            self.assertEqual(supervisor.load_state(run_dir)["state"], "final_review")

            completed = supervisor.transition(
                run_dir, "completed", artifact=artifact, receipt=receipt,
                decision="approve",
            )
            self.assertEqual(completed["state"], "completed")
            self.assertEqual(completed["accepted_artifact"]["sha256"], supervisor.sha256_file(artifact))

    def test_final_review_can_pause_resume_and_approve(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            artifact = write_text(temp / "clean.drawio", clean_diagram_xml())
            run_dir = temp / "run"
            preflight_run(run_dir)
            supervisor.transition(run_dir, "analyzed", artifact=artifact)
            supervisor.run_validation(artifact, run_dir)
            receipt = run_dir / "validation-receipt.json"
            supervisor.transition(run_dir, "final_review", artifact=artifact)
            paused = supervisor.transition(
                run_dir, "awaiting_feedback", artifact=artifact, decision="pause",
                reason="user requested a checkpoint",
            )
            self.assertEqual(paused["state"], "awaiting_feedback")
            resumed = supervisor.transition(
                run_dir, "final_review", artifact=artifact, decision="continue",
                reason="user supplied clarification",
            )
            self.assertEqual(resumed["state"], "final_review")
            completed = supervisor.transition(
                run_dir, "completed", artifact=artifact, receipt=receipt,
                decision="approve",
            )
            self.assertEqual(completed["state"], "completed")
            self.assertEqual(
                [item["decision"] for item in completed["decisions"]],
                ["pause", "continue", "approve"],
            )

    def test_attempt_limit_moves_rejected_candidate_to_plateau(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            run_dir = temp / "run"
            baseline = write_text(temp / "baseline.drawio", clean_diagram_xml())
            preflight_run(run_dir)
            supervisor.transition(
                run_dir, "analyzed", artifact=baseline, max_attempts=1
            )
            supervisor.run_validation(baseline, run_dir, attempt_id="baseline")
            baseline_report = run_dir / "attempts" / "baseline" / "validation-report.json"
            baseline_receipt = run_dir / "attempts" / "baseline" / "validation-receipt.json"
            artifact, patch_path, candidate_report, candidate_receipt = create_move_candidate(
                temp, run_dir, baseline, "candidate", 110, route_complexity=1
            )
            supervisor.transition(run_dir, "patching")
            supervisor.transition(run_dir, "validating")

            result = supervisor.record_candidate(
                run_dir, artifact, baseline_report, candidate_report, patch_path,
                baseline_receipt, candidate_receipt,
                repair_class="edge-route",
                reviewer_verdict_path=reviewer_verdict(
                    run_dir, artifact, candidate_report, candidate_receipt,
                ),
            )
            state = supervisor.load_state(run_dir)
            last_event = json.loads(
                (run_dir / "run-manifest.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )

            self.assertEqual(result["state"], "plateau")
            self.assertEqual(state["attempt_count"], 1)
            self.assertEqual(state["max_attempts"], 1)
            self.assertEqual(state["repair_class_attempts"], {"edge-route": 1})
            self.assertEqual(last_event["event_type"], "plateau_detected")
            self.assertEqual(last_event["payload"]["reason"], "iteration_limit_exhausted")

    def test_repair_class_limit_plateaus_after_three_distinct_rejections(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            run_dir = temp / "run"
            baseline = write_text(temp / "baseline.drawio", clean_diagram_xml())
            preflight_run(run_dir)
            supervisor.transition(
                run_dir, "analyzed", artifact=baseline, max_attempts=10
            )
            supervisor.run_validation(baseline, run_dir, attempt_id="baseline")
            baseline_report = run_dir / "attempts" / "baseline" / "validation-report.json"
            baseline_receipt = run_dir / "attempts" / "baseline" / "validation-receipt.json"

            for attempt in range(1, supervisor.DEFAULT_MAX_REPAIR_CLASS_ATTEMPTS + 1):
                supervisor.transition(run_dir, "patching")
                supervisor.transition(run_dir, "validating")
                artifact, patch_path, candidate_report, candidate_receipt = create_move_candidate(
                    temp, run_dir, baseline, f"candidate-{attempt}", 100 + attempt,
                    route_complexity=attempt,
                )
                result = supervisor.record_candidate(
                    run_dir, artifact, baseline_report, candidate_report, patch_path,
                    baseline_receipt, candidate_receipt,
                    repair_class="edge-route",
                    reviewer_verdict_path=reviewer_verdict(
                        run_dir, artifact, candidate_report, candidate_receipt,
                        suffix=str(attempt),
                    ),
                )
                expected = (
                    "plateau"
                    if attempt == supervisor.DEFAULT_MAX_REPAIR_CLASS_ATTEMPTS
                    else "retrying"
                )
                self.assertEqual(result["state"], expected)

            state = supervisor.load_state(run_dir)
            last_event = json.loads(
                (run_dir / "run-manifest.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            self.assertEqual(
                state["repair_class_attempts"]["edge-route"],
                supervisor.DEFAULT_MAX_REPAIR_CLASS_ATTEMPTS,
            )
            self.assertEqual(state["attempt_count"], supervisor.DEFAULT_MAX_REPAIR_CLASS_ATTEMPTS)
            self.assertEqual(last_event["payload"]["reason"], "repair_class_exhausted:edge-route")

    def test_candidate_gate_accepts_partial_lexicographic_improvement_with_failed_strict_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            bad_label = """
  <mxCell id="bad-label" value="A very long label that cannot fit" style="rhombus;whiteSpace=wrap;html=1;" parent="1" vertex="1">
    <mxGeometry x="0" y="200" width="24" height="24" as="geometry"/>
  </mxCell>
"""
            baseline_xml = diagram_xml(obstacle=True).replace(
                "    </root>", bad_label + "    </root>"
            )
            baseline = write_text(temp / "baseline.drawio", baseline_xml)
            run_dir = temp / "run"
            preflight_run(run_dir)
            supervisor.transition(run_dir, "analyzed", artifact=baseline)
            supervisor.run_validation(baseline, run_dir, attempt_id="baseline")
            baseline_report = run_dir / "attempts" / "baseline" / "validation-report.json"
            baseline_receipt = run_dir / "attempts" / "baseline" / "validation-receipt.json"

            patch = supervisor.route_patch(baseline, "edge", ["finding-route-through"])
            assert_schema(self, patch, "diagram-patch.v1.schema.json")
            patch_path = write_json(temp / "route-patch.json", patch)
            candidate = temp / "candidate.drawio"
            supervisor.apply_patch_file(baseline, patch_path, candidate)
            receipt_value = supervisor.run_validation(
                candidate, run_dir, attempt_id="candidate"
            )
            candidate_report = run_dir / "attempts" / "candidate" / "validation-report.json"
            candidate_receipt = run_dir / "attempts" / "candidate" / "validation-receipt.json"
            receipt_check = supervisor.verify_receipt(candidate_receipt, candidate)
            self.assertEqual(receipt_value["result"], "failed")
            self.assertTrue(receipt_check["valid"])
            self.assertFalse(receipt_check["passed"])

            supervisor.transition(run_dir, "patching")
            supervisor.transition(run_dir, "validating")
            result = supervisor.record_candidate(
                run_dir, candidate, baseline_report, candidate_report, patch_path,
                baseline_receipt, candidate_receipt, repair_class="edge-route",
                reviewer_verdict_path=reviewer_verdict(
                    run_dir, candidate, candidate_report, candidate_receipt,
                ),
            )

            self.assertTrue(result["accepted"])
            self.assertEqual(result["state"], "accepted_candidate")
            self.assertEqual(result["reason"], "lexicographic_improvement:route_through")


class ModelRoutingTests(unittest.TestCase):
    def test_approved_role_models_and_isolated_native_inherited_fallback_order(self):
        policy_path = ROOT / "data" / "model-routing.default.json"
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        expected = {
            "supervisor": "GigaChat-3-Ultra",
            "reviewer": "vllm/DeepSeek-V4-Flash-262k",
            "repair": "vllm/MiniMax-M3-113k",
            "semantic_analyst": "vllm/Qwen3.6-35B-262k",
        }
        self.assertEqual(
            {role: config["requested_model"] for role, config in policy["roles"].items()},
            expected,
        )
        self.assertEqual(policy["roles"]["reviewer"]["provider"], "vllm")
        agent_files = {
            "supervisor": "diagram-supervisor.md",
            "reviewer": "diagram-reviewer.md",
            "repair": "diagram-repair.md",
            "semantic_analyst": "diagram-semantic-analyst.md",
        }
        for role, agent_file in agent_files.items():
            agent_definition = (ROOT / "agents" / agent_file).read_text(encoding="utf-8")
            self.assertIn(f"model: {expected[role]}\n", agent_definition)
            self.assertIn("maxTurns:", agent_definition)
            self.assertNotIn("max_turns:", agent_definition)
            self.assertNotIn("kind:", agent_definition)
            self.assertNotIn("temperature:", agent_definition)
        supervisor_definition = (ROOT / "agents" / "diagram-supervisor.md").read_text(encoding="utf-8")
        self.assertNotIn("  - run_shell_command\n", supervisor_definition)
        self.assertIn("owns execution and invokes you as an isolated planning role", supervisor_definition)
        for role in ("reviewer", "repair", "semantic_analyst"):
            agent_definition = (ROOT / "agents" / agent_files[role]).read_text(encoding="utf-8")
            self.assertIn("approvalMode: default\n", agent_definition)
        self.assertEqual(policy["global_interactive_model"], "preserve")
        self.assertEqual(
            {tuple(config["fallback_order"]) for config in policy["roles"].values()},
            {("isolated_cli", "native_per_agent", "inherited_current")},
        )

        results = []
        for role, requested_model in expected.items():
            with self.subTest(role=role, mode="isolated"):
                isolated = supervisor.resolve_model(
                    policy_path, role, native_available=True,
                    isolated_available=True, current_model="interactive-model",
                )
                self.assertEqual(isolated["resolution_mode"], "isolated_cli")
                self.assertEqual(isolated["requested_model"], requested_model)
                self.assertEqual(isolated["resolved_model"], requested_model)
                self.assertFalse(isolated["fallback_used"])
                self.assertIsNone(isolated["degradation_reason"])
                results.append(isolated)

        native = supervisor.resolve_model(
            policy_path, "reviewer", native_available=True,
            current_model="interactive-model",
        )
        self.assertEqual(native["resolution_mode"], "native_per_agent")
        self.assertEqual(native["resolved_model"], expected["reviewer"])
        self.assertTrue(native["fallback_used"])
        self.assertIsNone(native["degradation_reason"])
        results.append(native)

        inherited = supervisor.resolve_model(
            policy_path, "reviewer", current_model="interactive-model"
        )
        self.assertEqual(inherited["resolution_mode"], "inherited_current")
        self.assertEqual(inherited["requested_model"], expected["reviewer"])
        self.assertEqual(inherited["resolved_model"], "interactive-model")
        self.assertEqual(inherited["provider"], "unknown")
        self.assertTrue(inherited["fallback_used"])
        self.assertIn("neither a verified isolated", inherited["degradation_reason"])
        results.append(inherited)

        serialized = json.dumps(results, ensure_ascii=False, sort_keys=True)
        self.assertNotIn("/model", serialized)


class AgentRuntimeTests(unittest.TestCase):
    @staticmethod
    def reviewer_input():
        digest = "1" * 64
        return {
            "run_id": "run-1",
            "candidate": {"sha256": digest},
            "validation_report": {"sha256": digest},
            "validation_receipt": {"sha256": digest},
        }

    @classmethod
    def reviewer_verdict(cls, verdict_id="proof"):
        payload = cls.reviewer_input()
        digest = payload["candidate"]["sha256"]
        return {
            "schema_version": 1,
            "verdict_id": verdict_id,
            "run_id": payload["run_id"],
            "candidate_sha256": digest,
            "report_sha256": digest,
            "receipt_sha256": digest,
            "verdict": "approve",
            "reviewed_at": "2026-07-16T00:00:00Z",
            "findings": [],
        }

    @staticmethod
    def gigacode_events(value, *, model="vllm/DeepSeek-V4-Flash-262k"):
        encoded = json.dumps(value)
        return [
            {
                "type": "system", "subtype": "init", "model": model,
                "qwen_code_version": "0.13.1",
            },
            {
                "type": "assistant",
                "message": {
                    "model": model,
                    "content": [{"type": "text", "text": encoded}],
                },
            },
            {
                "type": "result", "subtype": "success", "is_error": False,
                "result": encoded, "stats": {"models": {model: {}}},
            },
        ]

    @staticmethod
    def gigacode_stream_events(value, *, model="vllm/DeepSeek-V4-Flash-262k", include_stats=True):
        encoded = json.dumps(value)
        events = [
            {
                "type": "system", "subtype": "init", "model": model,
                "qwen_code_version": "0.13.1",
            },
            {
                "type": "assistant",
                "message": {
                    "model": model,
                    "content": [{"type": "text", "text": encoded}],
                },
            },
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": encoded,
            },
        ]
        if include_stats:
            events[-1]["stats"] = {"models": {model: {}}}
        return "\n".join(json.dumps(event, ensure_ascii=False) for event in events)

    def test_gigacode_event_parser_accepts_result_and_last_assistant_payload(self):
        verdict = self.reviewer_verdict()
        events = self.gigacode_events(verdict)
        events.insert(2, {
            "type": "assistant",
            "message": {
                "model": "vllm/DeepSeek-V4-Flash-262k",
                "content": [{"type": "text", "text": json.dumps(verdict)}],
            },
        })
        parsed, metadata = agent_runtime.parse_runtime_output(
            "reviewer", json.dumps(events)
        )
        self.assertEqual(parsed, verdict)
        self.assertTrue(metadata["model_proof"]["verified"])
        self.assertEqual(metadata["runtime_version"], "0.13.1")

        events[-1]["result"] = ""
        parsed_from_assistant, fallback_metadata = agent_runtime.parse_runtime_output(
            "reviewer", json.dumps(events)
        )
        self.assertEqual(parsed_from_assistant, verdict)
        self.assertEqual(
            fallback_metadata["reported_model"], "vllm/DeepSeek-V4-Flash-262k"
        )
        self.assertEqual(metadata["isolation_proof"]["tool_calls"], 0)
        self.assertTrue(metadata["isolation_proof"]["verified"])

    def test_gigacode_stream_json_accepts_missing_result_stats_and_rejects_conflicting_model_proof(self):
        verdict = self.reviewer_verdict("stream-json-proof")
        parsed, metadata = agent_runtime.parse_runtime_output(
            "reviewer",
            self.gigacode_stream_events(verdict, include_stats=False),
        )
        self.assertEqual(parsed, verdict)
        self.assertEqual(metadata["format"], "gigacode_stream_json")
        self.assertTrue(metadata["model_proof"]["verified"])
        self.assertFalse(metadata["model_proof"]["stats_required"])
        self.assertEqual(
            metadata["model_proof"]["sources"],
            ["system.init.model", "assistant.message.model"],
        )

        conflicting = [
            {
                "type": "system",
                "subtype": "init",
                "model": "vllm/DeepSeek-V4-Flash-262k",
                "qwen_code_version": "0.13.1",
            },
            {
                "type": "assistant",
                "message": {
                    "model": "vllm/Qwen3.6-35B-262k",
                    "content": [{"type": "text", "text": json.dumps(verdict)}],
                },
            },
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": json.dumps(verdict),
            },
        ]
        with self.assertRaisesRegex(supervisor.SupervisorError, "model proof mismatch"):
            agent_runtime.parse_runtime_output("reviewer", "\n".join(json.dumps(event) for event in conflicting))

    def test_corporate_recursive_supervisor_fixture_is_rejected_as_isolation_leak(self):
        capture = (
            ROOT / "tests/fixtures/gigacode-recursive-supervisor-runtime.json"
        ).read_text(encoding="utf-8")

        with self.assertRaisesRegex(
            supervisor.SupervisorError, "customization isolation failed"
        ):
            agent_runtime.parse_runtime_output("supervisor", capture)

    def test_gigacode_event_parser_rejects_every_tool_call_without_custom_context(self):
        verdict = self.reviewer_verdict("tool-free-proof")
        events = self.gigacode_events(verdict)
        events[1]["message"]["content"] = [
            {"type": "tool_use", "name": "agent", "input": {}},
            {"type": "text", "text": json.dumps(verdict)},
        ]

        with self.assertRaisesRegex(
            supervisor.SupervisorError, "tool-free role contract: agent"
        ):
            agent_runtime.parse_runtime_output("reviewer", json.dumps(events))

    def test_gigacode_event_parser_accepts_one_markdown_fenced_json_object(self):
        verdict = self.reviewer_verdict("fenced-proof")
        events = self.gigacode_events(verdict)
        events[-1]["result"] = "\n\n```json\n" + json.dumps(verdict) + "\n```\n"

        parsed, metadata = agent_runtime.parse_runtime_output(
            "reviewer", json.dumps(events)
        )

        self.assertEqual(parsed, verdict)
        self.assertTrue(metadata["model_proof"]["verified"])

    def test_gigacode_event_parser_rejects_ambiguous_markdown_payloads(self):
        verdict = self.reviewer_verdict("ambiguous-proof")
        encoded = json.dumps(verdict)
        cases = {
            "prose-before-fence": f"Here is the result:\n```json\n{encoded}\n```",
            "multiple-fences": f"```json\n{encoded}\n```\n```json\n{encoded}\n```",
            "unterminated-fence": f"```json\n{encoded}",
        }
        for name, payload in cases.items():
            events = self.gigacode_events(verdict)
            events[-1]["result"] = payload
            with self.subTest(case=name), self.assertRaisesRegex(
                supervisor.SupervisorError, "ambiguous Markdown JSON fence"
            ):
                agent_runtime.parse_runtime_output("reviewer", json.dumps(events))

    def test_gigacode_event_parser_rejects_incomplete_or_ambiguous_model_proof(self):
        verdict = self.reviewer_verdict()
        base = self.gigacode_events(verdict)

        def without(event_type):
            return [event for event in self.gigacode_events(verdict) if event["type"] != event_type]

        cases = {
            "missing-system": (without("system"), "expected exactly one system init"),
            "missing-assistant": (without("assistant"), "model proof is ambiguous"),
            "missing-result": (without("result"), "has no result event"),
            "empty-stats-models": (
                [*base[:-1], {**base[-1], "stats": {"models": {}}}],
                "model proof mismatch",
            ),
            "missing-stats": (
                [*base[:-1], {key: value for key, value in base[-1].items() if key != "stats"}],
                "model proof mismatch",
            ),
            "ambiguous-system": (
                [
                    base[0],
                    {**base[0], "model": "other-model"},
                    *base[1:],
                ],
                "expected exactly one system init",
            ),
            "ambiguous-assistant": (
                [
                    base[0], base[1],
                    {
                        "type": "assistant",
                        "message": {
                            "model": "other-model",
                            "content": [{"type": "text", "text": json.dumps(verdict)}],
                        },
                    },
                    base[2],
                ],
                "model proof is ambiguous",
            ),
        }
        for name, (events, message) in cases.items():
            with self.subTest(case=name), self.assertRaisesRegex(
                supervisor.SupervisorError, message
            ):
                agent_runtime.parse_runtime_output("reviewer", json.dumps(events))

    def test_gigacode_event_parser_rejects_failed_result_and_bad_payload(self):
        verdict = self.reviewer_verdict()
        base = self.gigacode_events(verdict)
        cases = {
            "is-error": (
                [*base[:-1], {**base[-1], "is_error": True, "result": "denied"}],
                "result reports failure",
            ),
            "failed-subtype": (
                [*base[:-1], {**base[-1], "subtype": "error", "result": "denied"}],
                "result reports failure",
            ),
            "malformed-role-json": (
                [*base[:-1], {**base[-1], "result": "not-json"}],
                "is not role JSON",
            ),
        }
        for name, (events, message) in cases.items():
            with self.subTest(case=name), self.assertRaisesRegex(
                supervisor.SupervisorError, message
            ):
                agent_runtime.parse_runtime_output("reviewer", json.dumps(events))

    def test_baseline_reviewer_prompt_assigns_hash_bindings_to_host(self):
        payload = {
            "schema_version": 1,
            "review_kind": "baseline_audit",
            "run_id": "review-contract-test",
            "artifact": {"path": "diagram.drawio", "sha256": "a" * 64},
            "report": {"path": "report.json", "sha256": "b" * 64, "content": {}},
            "receipt": {"path": "receipt.json", "sha256": "c" * 64, "content": {}},
        }

        contract = agent_runtime.role_output_contract("reviewer", payload)

        self.assertIn("## Required output JSON Schema", contract)
        schema_text = contract.split(
            "Do not omit required properties.\n\n", 1
        )[1].split("\n\n## Host-owned reviewer evidence bindings", 1)[0]
        self.assertEqual(
            json.loads(schema_text),
            json.loads((ROOT / "data/reviewer-analysis.v1.schema.json").read_text()),
        )
        self.assertIn("## Host-owned reviewer evidence bindings", contract)
        self.assertIn("Do not copy run_id", contract)
        self.assertNotIn('"run_id": "review-contract-test"', contract)
        self.assertNotIn('"candidate_sha256": "' + "a" * 64 + '"', contract)

        candidate_contract = agent_runtime.role_output_contract(
            "reviewer", self.reviewer_input()
        )
        self.assertNotIn('"candidate_sha256": "' + "1" * 64 + '"', candidate_contract)

    @staticmethod
    def fake_cli(path, behavior):
        script = (
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "if '--help' in sys.argv:\n"
            "    print('--model --prompt --output-format --approval-mode --extensions --system-prompt --max-session-turns --core-tools --allowed-mcp-server-names --exclude-tools')\n"
            "    raise SystemExit(0)\n"
            "def emit(value):\n"
            "    model=sys.argv[sys.argv.index('--model')+1]\n"
            "    encoded=json.dumps(value)\n"
            "    print(json.dumps([\n"
            "      {'type':'system','subtype':'init','model':model,'qwen_code_version':'0.13.1'},\n"
            "      {'type':'assistant','message':{'model':model,'content':[{'type':'text','text':encoded}]}},\n"
            "      {'type':'result','subtype':'success','is_error':False,'result':encoded,'stats':{'models':{model:{'api':{'totalRequests':1}}}}}\n"
            "    ]))\n"
            + behavior
        )
        write_text(path, script)
        os.chmod(path, 0o755)
        return path

    def test_success_uses_minimal_env_and_publishes_after_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            cli = self.fake_cli(
                temp / "safe-cli",
                "if 'DIAGRAM_TEST_SECRET' in os.environ: raise SystemExit(91)\n"
                "payload=json.loads(sys.stdin.read())\n"
                "d=payload['candidate']['sha256']\n"
                "emit({'schema_version':1,'verdict_id':'v1','run_id':payload['run_id'],'candidate_sha256':d,'report_sha256':payload['validation_report']['sha256'],'receipt_sha256':payload['validation_receipt']['sha256'],'verdict':'approve','reviewed_at':'2026-07-16T00:00:00Z','findings':[]})\n",
            )
            input_path = write_json(temp / "input.json", self.reviewer_input())
            output_path = temp / "verdict.json"
            old = os.environ.get("DIAGRAM_TEST_SECRET")
            os.environ["DIAGRAM_TEST_SECRET"] = "must-not-leak"
            try:
                result = agent_runtime.invoke_role(
                    "reviewer", input_path, output_path, cli=str(cli), run_dir=temp / "run",
                )
            finally:
                if old is None:
                    os.environ.pop("DIAGRAM_TEST_SECRET", None)
                else:
                    os.environ["DIAGRAM_TEST_SECRET"] = old
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(json.loads(output_path.read_text())["verdict"], "approve")
            self.assertEqual(result["runtime_metadata"]["format"], "gigacode_json_events")
            self.assertTrue(result["runtime_metadata"]["model_proof"]["verified"])
            self.assertEqual(result["runtime_metadata"]["runtime_version"], "0.13.1")
            events = [json.loads(line) for line in (temp / "run/run-manifest.jsonl").read_text().splitlines()]
            self.assertEqual(
                [event["event_type"] for event in events],
                ["role_started", "model_resolved", "role_finished", "review_verdict"],
            )
            finished = events[2]["payload"]
            self.assertTrue(finished["isolation_proof"]["verified"])
            self.assertEqual(finished["isolation_proof"]["tool_calls"], 0)
            self.assertTrue(Path(finished["stderr_capture"]).is_file())

    def test_gigacode_model_proof_mismatch_or_missing_proof_is_not_published(self):
        valid = (
            "{'schema_version':1,'verdict_id':'proof','run_id':payload['run_id'],"
            "'candidate_sha256':d,'report_sha256':payload['validation_report']['sha256'],"
            "'receipt_sha256':payload['validation_receipt']['sha256'],'verdict':'approve',"
            "'reviewed_at':'2026-07-16T00:00:00Z','findings':[]}"
        )
        cases = (
            (
                "mismatch",
                "payload=json.loads(sys.stdin.read())\n"
                "d=payload['candidate']['sha256']\n"
                f"value={valid}\n"
                "requested=sys.argv[sys.argv.index('--model')+1]\n"
                "encoded=json.dumps(value)\n"
                "print(json.dumps([{'type':'system','subtype':'init','model':requested},"
                "{'type':'assistant','message':{'model':'wrong-model','content':[{'type':'text','text':encoded}]}},"
                "{'type':'result','subtype':'success','is_error':False,'result':encoded,'stats':{'models':{requested:{}}}}]))\n",
                "model proof mismatch",
            ),
            (
                "missing",
                "payload=json.loads(sys.stdin.read())\n"
                "d=payload['candidate']['sha256']\n"
                f"print(json.dumps({valid}))\n",
                "did not provide verifiable model evidence",
            ),
        )
        for name, behavior, message in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as temp:
                temp = Path(temp)
                cli = self.fake_cli(temp / f"{name}-cli", behavior)
                output = temp / "verdict.json"
                with self.assertRaisesRegex(supervisor.SupervisorError, message):
                    agent_runtime.invoke_role(
                        "reviewer", write_json(temp / "input.json", self.reviewer_input()),
                        output, cli=str(cli), run_dir=temp / "run",
                    )
                self.assertFalse(output.exists())
                manifest = temp / "run/run-manifest.jsonl"
                events = [json.loads(line) for line in manifest.read_text().splitlines()]
                self.assertEqual([event["event_type"] for event in events], ["role_started", "role_failed"])

    def test_realistic_gemini_envelope_extracts_inner_verdict_and_keeps_sanitized_stats(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            cli = self.fake_cli(
                temp / "gemini-envelope-cli",
                "payload=json.loads(sys.stdin.read())\n"
                "d=payload['candidate']['sha256']\n"
                "inner={'schema_version':1,'verdict_id':'vg','run_id':payload['run_id'],'candidate_sha256':d,'report_sha256':payload['validation_report']['sha256'],'receipt_sha256':payload['validation_receipt']['sha256'],'verdict':'approve','reviewed_at':'2026-07-16T00:00:00Z','findings':[]}\n"
                "model=sys.argv[sys.argv.index('--model')+1]\n"
                "print(json.dumps({'response':json.dumps(inner),'model':model,'stats':{'input_tokens':12,'secret':'do-not-record'},'errors':[]}))\n",
            )
            result = agent_runtime.invoke_role(
                "reviewer", write_json(temp / "input.json", self.reviewer_input()),
                temp / "verdict.json", cli=str(cli), run_dir=temp / "run",
            )
            self.assertEqual(json.loads((temp / "verdict.json").read_text())["verdict"], "approve")
            self.assertEqual(result["runtime_metadata"]["format"], "gemini_json_envelope")
            self.assertEqual(result["runtime_metadata"]["stats"]["input_tokens"], 12)
            self.assertEqual(result["runtime_metadata"]["stats"]["secret"], "[REDACTED]")
            event = json.loads((temp / "run/run-manifest.jsonl").read_text().splitlines()[-1])
            self.assertEqual(event["payload"]["runtime_metadata"]["format"], "gemini_json_envelope")

    def test_gemini_error_or_malformed_response_is_not_published(self):
        cases = (
            ("error", "print(json.dumps({'response':'{}','stats':{},'errors':[{'message':'model failed'}]}))\n", "reports errors"),
            ("malformed", "print(json.dumps({'response':'not-json','stats':{},'errors':[]}))\n", "not role JSON"),
        )
        for name, behavior, message in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as temp:
                temp = Path(temp)
                cli = self.fake_cli(temp / "gemini-cli", behavior)
                output = temp / "verdict.json"
                with self.assertRaisesRegex(supervisor.SupervisorError, message):
                    agent_runtime.invoke_role(
                        "reviewer", write_json(temp / "input.json", self.reviewer_input()),
                        output, cli=str(cli), run_dir=temp / "run",
                    )
                self.assertFalse(output.exists())
                events = [json.loads(line) for line in (temp / "run/run-manifest.jsonl").read_text().splitlines()]
                self.assertEqual([event["event_type"] for event in events], ["role_started", "role_failed"])

    def test_nonzero_or_invalid_output_leaves_no_output_or_success_event(self):
        for name, behavior, error in (
            ("exit", "print('nope')\nprint('failed', file=sys.stderr)\nraise SystemExit(42)\n", "exit code 42"),
            ("invalid", "print('{}')\n", "verifiable model evidence"),
        ):
            with self.subTest(case=name), tempfile.TemporaryDirectory() as temp:
                temp = Path(temp)
                cli = self.fake_cli(temp / "fake-cli", behavior)
                input_path = write_json(temp / "input.json", self.reviewer_input())
                output_path = temp / "verdict.json"
                with self.assertRaisesRegex(supervisor.SupervisorError, error):
                    agent_runtime.invoke_role(
                        "reviewer", input_path, output_path, cli=str(cli), run_dir=temp / "run",
                    )
                self.assertFalse(output_path.exists())
                events = [json.loads(line) for line in (temp / "run/run-manifest.jsonl").read_text().splitlines()]
                self.assertEqual([event["event_type"] for event in events], ["role_started", "role_failed"])

    def test_turn_limit_failure_preserves_redacted_runtime_and_isolation_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            cli = self.fake_cli(
                temp / "turn-limited-cli",
                "model=sys.argv[sys.argv.index('--model')+1]\n"
                "print(json.dumps(["
                "{'type':'system','subtype':'init','model':model,'qwen_code_version':'0.13.1','agents':[],'slash_commands':[]},"
                "{'type':'assistant','message':{'model':model,'content':[{'type':'text','text':'still deciding'}]}},"
                "{'type':'result','subtype':'error','is_error':True,'error':'FatalTurnLimitedError'}"
                "]))\n"
                "print('FatalTurnLimitedError API_KEY=must-not-survive', file=sys.stderr)\n"
                "raise SystemExit(2)\n",
            )
            output = temp / "verdict.json"

            with self.assertRaisesRegex(
                supervisor.SupervisorError,
                "exhausted its command-line turn budget; do not change global",
            ):
                agent_runtime.invoke_role(
                    "reviewer",
                    write_json(temp / "input.json", self.reviewer_input()),
                    output,
                    cli=str(cli),
                    run_dir=temp / "run",
                )

            self.assertFalse(output.exists())
            event = json.loads(
                (temp / "run/run-manifest.jsonl").read_text().splitlines()[-1]
            )
            payload = event["payload"]
            self.assertEqual(event["event_type"], "role_failed")
            self.assertEqual(payload["failure_kind"], "turn_limit")
            self.assertEqual(
                payload["isolation_controls"]["approval_mode"], "default"
            )
            self.assertEqual(
                payload["isolation_controls"]["core_tools"],
                [agent_runtime.ROLE_CORE_TOOL_SENTINEL],
            )
            self.assertTrue(payload["isolation_proof"]["verified"])
            self.assertEqual(payload["isolation_proof"]["tool_calls"], 0)
            runtime_capture = Path(payload["runtime_capture"])
            stderr_capture = Path(payload["stderr_capture"])
            self.assertTrue(runtime_capture.is_file())
            self.assertTrue(stderr_capture.is_file())
            self.assertEqual(
                hashlib.sha256(runtime_capture.read_bytes()).hexdigest(),
                payload["runtime_capture_sha256"],
            )
            self.assertIn("FatalTurnLimitedError", stderr_capture.read_text())
            self.assertNotIn("must-not-survive", stderr_capture.read_text())
            self.assertTrue(payload["terminal"])
            self.assertNotIn("fallback_model", payload)

    def test_supervisor_turn_limit_retries_exactly_once_with_deepseek_and_separate_captures(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            cli = write_text(
                temp / "supervisor-fallback-cli",
                f"#!{sys.executable}\n"
                "import json, sys\n"
                "if '--help' in sys.argv:\n"
                "    print('--model --prompt --output-format stream-json --approval-mode --auth-type --extensions --system-prompt --max-session-turns --core-tools --allowed-mcp-server-names --exclude-tools')\n"
                "    raise SystemExit(0)\n"
                "model = sys.argv[sys.argv.index('--model') + 1]\n"
                "payload = json.loads(sys.stdin.read())\n"
                "decision = {'schema_version': 1, 'role': 'supervisor', 'status': 'ok', 'result': {'action': 'create', 'reason': 'fallback approval', 'required_roles': ['supervisor', 'semantic_analyst', 'reviewer'], 'max_iterations': 1}}\n"
                "encoded = json.dumps(decision)\n"
                "if model == 'GigaChat-3-Ultra':\n"
                "    print('\\n'.join([\n"
                "        json.dumps({'type': 'system', 'subtype': 'init', 'model': model, 'qwen_code_version': '0.13.1'}),\n"
                "        json.dumps({'type': 'assistant', 'message': {'model': model, 'content': [{'type': 'text', 'text': 'still deciding'}]}}),\n"
                "        json.dumps({'type': 'result', 'subtype': 'error', 'is_error': True, 'error': 'FatalTurnLimitedError'})\n"
                "    ]))\n"
                "    print('FatalTurnLimitedError', file=sys.stderr)\n"
                "    raise SystemExit(2)\n"
                "print('\\n'.join([\n"
                "    json.dumps({'type': 'system', 'subtype': 'init', 'model': model, 'qwen_code_version': '0.13.1'}),\n"
                "    json.dumps({'type': 'assistant', 'message': {'model': model, 'content': [{'type': 'text', 'text': encoded}]}}),\n"
                "    json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': encoded})\n"
                "]))\n",
            )
            os.chmod(cli, 0o755)
            input_path = write_json(
                temp / "input.json",
                {
                    "run_id": "supervisor-fallback-run",
                    "mode": "create",
                    "request": "Create a fallback test diagram.",
                    "workspace": str(temp),
                    "diagram": str(temp / "diagram.drawio"),
                    "constraints": {"local_only": True, "deterministic_mutations": True, "max_iterations": 1},
                },
            )
            output_path = temp / "decision.json"

            result = agent_runtime.invoke_role(
                "supervisor",
                input_path,
                output_path,
                cli=str(cli),
                run_dir=temp / "run",
            )

            self.assertEqual(result["resolution"]["resolved_model"], "vllm/DeepSeek-V4-Flash-262k")
            self.assertTrue(result["resolution"]["fallback_used"])
            self.assertEqual(result["recovered_from"]["failure_kind"], "turn_limit")
            self.assertEqual(result["recovered_from"]["attempted_model"], "GigaChat-3-Ultra")
            primary_capture = Path(result["recovered_from"]["runtime_capture"])
            fallback_capture = Path(result["runtime_capture"])
            self.assertTrue(primary_capture.is_file())
            self.assertTrue(fallback_capture.is_file())
            self.assertEqual(primary_capture.parent.name, "primary")
            self.assertEqual(fallback_capture.parent.name, "fallback-1")
            self.assertNotEqual(primary_capture, fallback_capture)
            events = [json.loads(line) for line in (temp / "run/run-manifest.jsonl").read_text().splitlines()]
            failed = [event for event in events if event["event_type"] == "role_failed"]
            self.assertEqual(len(failed), 1)
            self.assertFalse(failed[0]["payload"]["terminal"])
            self.assertEqual(failed[0]["payload"]["fallback_model"], "vllm/DeepSeek-V4-Flash-262k")
            started = [event for event in events if event["event_type"] == "role_started"]
            self.assertEqual([event["payload"]["attempt_id"] for event in started], ["primary", "fallback-1"])
            self.assertTrue(output_path.exists())
            self.assertEqual(json.loads(output_path.read_text())["role"], "supervisor")

    def test_global_mcp_servers_are_removed_before_role_discovery(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            cli = self.fake_cli(
                temp / "global-mcp-cli",
                "flag='--allowed-mcp-server-names'\n"
                "if flag not in sys.argv or sys.argv[sys.argv.index(flag)+1] != '':\n"
                "    print(json.dumps([{'type':'assistant','message':{'model':sys.argv[sys.argv.index('--model')+1],'content':[{'type':'tool_use','name':'mcp__AtlassianBitbucket__jira_get_issue','input':{}}]}}]))\n"
                "    print('FatalTurnLimitedError after denied global MCP tool', file=sys.stderr)\n"
                "    raise SystemExit(53)\n"
                "payload=json.loads(sys.stdin.read())\n"
                "d=payload['candidate']['sha256']\n"
                "emit({'schema_version':1,'verdict_id':'mcp-isolated','run_id':payload['run_id'],'candidate_sha256':d,'report_sha256':payload['validation_report']['sha256'],'receipt_sha256':payload['validation_receipt']['sha256'],'verdict':'approve','reviewed_at':'2026-07-21T00:00:00Z','findings':[]})\n",
            )
            result = agent_runtime.invoke_role(
                "reviewer",
                write_json(temp / "input.json", self.reviewer_input()),
                temp / "verdict.json",
                cli=str(cli),
                run_dir=temp / "run",
            )

            flag_index = result["command"].index("--allowed-mcp-server-names")
            self.assertEqual(result["command"][flag_index + 1], "")
            self.assertEqual(result["isolation_controls"]["allowed_mcp_servers"], [])
            self.assertEqual(
                result["runtime_metadata"]["isolation_proof"]["tool_calls"], 0
            )
            self.assertTrue(result["runtime_metadata"]["isolation_proof"]["verified"])
            self.assertEqual(
                json.loads((temp / "verdict.json").read_text())["verdict"],
                "approve",
            )

    def test_supervisor_turn_limit_fallback_failure_is_terminal(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            cli = write_text(
                temp / "supervisor-fallback-failure-cli",
                f"#!{sys.executable}\n"
                "import json, sys\n"
                "if '--help' in sys.argv:\n"
                "    print('--model --prompt --output-format stream-json --approval-mode --auth-type --extensions --system-prompt --max-session-turns --core-tools --allowed-mcp-server-names --exclude-tools')\n"
                "    raise SystemExit(0)\n"
                "model = sys.argv[sys.argv.index('--model') + 1]\n"
                "print('\\n'.join([\n"
                "    json.dumps({'type': 'system', 'subtype': 'init', 'model': model, 'qwen_code_version': '0.13.1'}),\n"
                "    json.dumps({'type': 'assistant', 'message': {'model': model, 'content': [{'type': 'text', 'text': 'still deciding'}]}}),\n"
                "    json.dumps({'type': 'result', 'subtype': 'error', 'is_error': True, 'error': 'FatalTurnLimitedError'})\n"
                "]))\n"
                "print('FatalTurnLimitedError', file=sys.stderr)\n"
                "raise SystemExit(2)\n",
            )
            os.chmod(cli, 0o755)
            input_path = write_json(
                temp / "input.json",
                {
                    "run_id": "supervisor-fallback-failure",
                    "mode": "create",
                    "request": "Create a failing fallback diagram.",
                    "workspace": str(temp),
                    "diagram": str(temp / "diagram.drawio"),
                    "constraints": {"local_only": True, "deterministic_mutations": True, "max_iterations": 1},
                },
            )
            with self.assertRaisesRegex(supervisor.SupervisorError, "exhausted its command-line turn budget"):
                agent_runtime.invoke_role(
                    "supervisor",
                    input_path,
                    temp / "decision.json",
                    cli=str(cli),
                    run_dir=temp / "run",
                )

            events = [json.loads(line) for line in (temp / "run/run-manifest.jsonl").read_text().splitlines()]
            failed = [event for event in events if event["event_type"] == "role_failed"]
            self.assertEqual(len(failed), 2)
            self.assertFalse(failed[0]["payload"]["terminal"])
            self.assertEqual(failed[0]["payload"]["fallback_model"], "vllm/DeepSeek-V4-Flash-262k")
            self.assertTrue(failed[1]["payload"]["terminal"])
            self.assertEqual(failed[1]["payload"]["attempted_model"], "vllm/DeepSeek-V4-Flash-262k")
            self.assertNotIn("fallback_model", failed[1]["payload"])

    def test_supervisor_turn_limit_with_tool_use_does_not_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            cli = write_text(
                temp / "supervisor-tool-use-cli",
                f"#!{sys.executable}\n"
                "import json, sys\n"
                "if '--help' in sys.argv:\n"
                "    print('--model --prompt --output-format stream-json --approval-mode --auth-type --extensions --system-prompt --max-session-turns --core-tools --allowed-mcp-server-names --exclude-tools')\n"
                "    raise SystemExit(0)\n"
                "model = sys.argv[sys.argv.index('--model') + 1]\n"
                "print('\\n'.join([\n"
                "    json.dumps({'type': 'system', 'subtype': 'init', 'model': model, 'qwen_code_version': '0.13.1'}),\n"
                "    json.dumps({'type': 'assistant', 'message': {'model': model, 'content': [{'type': 'tool_use', 'name': 'agent', 'input': {}}]}}),\n"
                "    json.dumps({'type': 'result', 'subtype': 'error', 'is_error': True, 'error': 'FatalTurnLimitedError'})\n"
                "]))\n"
                "print('FatalTurnLimitedError', file=sys.stderr)\n"
                "raise SystemExit(2)\n",
            )
            os.chmod(cli, 0o755)
            input_path = write_json(
                temp / "input.json",
                {
                    "run_id": "supervisor-tool-use",
                    "mode": "create",
                    "request": "Create a tool-use leak diagram.",
                    "workspace": str(temp),
                    "diagram": str(temp / "diagram.drawio"),
                    "constraints": {"local_only": True, "deterministic_mutations": True, "max_iterations": 1},
                },
            )
            with self.assertRaisesRegex(supervisor.SupervisorError, "exhausted its command-line turn budget"):
                agent_runtime.invoke_role(
                    "supervisor",
                    input_path,
                    temp / "decision.json",
                    cli=str(cli),
                    run_dir=temp / "run",
                )

            events = [json.loads(line) for line in (temp / "run/run-manifest.jsonl").read_text().splitlines()]
            self.assertEqual([event["event_type"] for event in events], ["role_started", "role_failed"])
            payload = events[-1]["payload"]
            self.assertTrue(payload["terminal"])
            self.assertEqual(payload["attempt_id"], "primary")
            self.assertNotIn("fallback_model", payload)
            self.assertFalse(payload["isolation_proof"]["verified"])
            self.assertEqual(payload["isolation_proof"]["tool_calls"], 1)

    def test_schema_failure_preserves_proven_model_without_publishing_invalid_json(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            cli = self.fake_cli(temp / "schema-failure-cli", "emit({'schema_version': 1})\n")
            output = temp / "verdict.json"

            with self.assertRaises(agent_runtime.RoleOutputContractError) as raised:
                agent_runtime.invoke_role(
                    "reviewer", write_json(temp / "input.json", self.reviewer_input()),
                    output, cli=str(cli), run_dir=temp / "run",
                )

            error = raised.exception
            self.assertEqual(
                error.resolution["resolved_model"], "vllm/DeepSeek-V4-Flash-262k"
            )
            self.assertTrue(error.runtime_metadata["model_proof"]["verified"])
            self.assertFalse(output.exists())
            self.assertRegex(error.invalid_output_sha256, r"^[a-f0-9]{64}$")
            self.assertFalse(list(temp.glob("*.invalid.json")))
            event = json.loads((temp / "run/run-manifest.jsonl").read_text().splitlines()[-1])
            self.assertEqual(event["event_type"], "role_failed")
            self.assertEqual(
                event["payload"]["resolved_model"], "vllm/DeepSeek-V4-Flash-262k"
            )
            self.assertTrue(event["payload"]["model_proof"]["verified"])
            self.assertEqual(
                event["payload"]["invalid_output_sha256"],
                error.invalid_output_sha256,
            )

    def test_reviewer_hash_binding_mismatch_is_recorded_and_host_normalized(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            verdict = self.reviewer_verdict("wrong-binding")
            verdict["candidate_sha256"] = "f" * 64
            cli = self.fake_cli(temp / "wrong-binding-cli", f"emit({verdict!r})\n")
            output = temp / "verdict.json"

            result = agent_runtime.invoke_role(
                "reviewer", write_json(temp / "input.json", self.reviewer_input()),
                output, cli=str(cli), run_dir=temp / "run",
            )

            self.assertTrue(output.exists())
            normalized = supervisor.load_json(output)
            self.assertEqual(normalized["candidate_sha256"], "1" * 64)
            self.assertEqual(normalized.get("reviewer"), verdict.get("reviewer"))
            proof = result["runtime_metadata"]["binding_proof"]
            self.assertEqual(proof["declared_mismatches"], ["candidate_sha256"])
            self.assertEqual(proof["model_declared"]["candidate_sha256"], "f" * 64)
            self.assertEqual(proof["expected"]["candidate_sha256"], "1" * 64)
            events = [
                json.loads(line)
                for line in (temp / "run/run-manifest.jsonl").read_text().splitlines()
            ]
            self.assertIn("role_finished", [event["event_type"] for event in events])
            finished = next(event for event in events if event["event_type"] == "role_finished")
            self.assertEqual(finished["payload"]["binding_proof"], proof)

    def test_reviewer_may_omit_all_host_owned_bindings(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            verdict = self.reviewer_verdict("analytical-only")
            for key in ("run_id", "candidate_sha256", "report_sha256", "receipt_sha256", "reviewer"):
                verdict.pop(key, None)
            cli = self.fake_cli(temp / "analytical-only-cli", f"emit({verdict!r})\n")
            output = temp / "verdict.json"

            result = agent_runtime.invoke_role(
                "reviewer", write_json(temp / "input.json", self.reviewer_input()),
                output, cli=str(cli), run_dir=temp / "run",
            )

            normalized = supervisor.load_json(output)
            self.assertEqual(normalized["run_id"], "run-1")
            self.assertEqual(normalized["receipt_sha256"], "1" * 64)
            proof = result["runtime_metadata"]["binding_proof"]
            self.assertEqual(proof["model_declared"], {})
            self.assertEqual(proof["declared_mismatches"], [])

    def test_requested_model_unavailable_falls_back_to_inherited_current(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            cli = self.fake_cli(
                temp / "fallback-cli",
                "if '--model' in sys.argv and sys.argv[sys.argv.index('--model')+1] == 'vllm/DeepSeek-V4-Flash-262k':\n"
                "    print('requested model unavailable', file=sys.stderr)\n"
                "    raise SystemExit(3)\n"
                "payload=json.loads(sys.stdin.read())\n"
                "d=payload['candidate']['sha256']\n"
                "emit({'schema_version':1,'verdict_id':'vf','run_id':payload['run_id'],'candidate_sha256':d,'report_sha256':payload['validation_report']['sha256'],'receipt_sha256':payload['validation_receipt']['sha256'],'verdict':'approve','reviewed_at':'2026-07-16T00:00:00Z','findings':[]})\n",
            )
            result = agent_runtime.invoke_role(
                "reviewer", write_json(temp / "input.json", self.reviewer_input()),
                temp / "verdict.json", cli=str(cli), run_dir=temp / "run",
                current_model="interactive-model", current_provider="local-provider",
            )
            self.assertEqual(result["resolution"]["resolution_mode"], "inherited_current")
            self.assertEqual(result["resolution"]["resolved_model"], "interactive-model")
            self.assertEqual(result["resolution"]["provider"], "local-provider")
            self.assertTrue(result["resolution"]["fallback_used"])
            self.assertEqual(
                result["command"][result["command"].index("--model") + 1],
                "interactive-model",
            )
            events = [json.loads(line) for line in (temp / "run/run-manifest.jsonl").read_text().splitlines()]
            self.assertEqual(
                [event["event_type"] for event in events],
                ["role_started", "role_failed", "model_resolved", "role_finished", "review_verdict"],
            )

    def test_cli_without_model_flag_records_proven_runtime_default_as_degradation(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            cli = write_text(
                temp / "inherited-cli",
                f"#!{sys.executable}\n"
                "import json, sys\n"
                "if '--help' in sys.argv:\n"
                "    print('--prompt --output-format --approval-mode --extensions --system-prompt --max-session-turns --core-tools --allowed-mcp-server-names --exclude-tools')\n"
                "    raise SystemExit(0)\n"
                "payload=json.loads(sys.stdin.read())\n"
                "d=payload['candidate']['sha256']\n"
                "value={'schema_version':1,'verdict_id':'inherited','run_id':payload['run_id'],'candidate_sha256':d,'report_sha256':payload['validation_report']['sha256'],'receipt_sha256':payload['validation_receipt']['sha256'],'verdict':'approve','reviewed_at':'2026-07-16T00:00:00Z','findings':[]}\n"
                "encoded=json.dumps(value)\n"
                "model='runtime-default-model'\n"
                "print(json.dumps([{'type':'system','subtype':'init','model':model},"
                "{'type':'assistant','message':{'model':model,'content':[{'type':'text','text':encoded}]}},"
                "{'type':'result','subtype':'success','is_error':False,'result':encoded,'stats':{'models':{model:{}}}}]))\n",
            )
            os.chmod(cli, 0o755)
            result = agent_runtime.invoke_role(
                "reviewer", write_json(temp / "input.json", self.reviewer_input()),
                temp / "verdict.json", cli=str(cli), run_dir=temp / "run",
                current_model="interactive-model", current_provider="parent-provider",
            )
            self.assertEqual(result["resolution"]["resolution_mode"], "inherited_current")
            self.assertEqual(result["resolution"]["resolved_model"], "runtime-default-model")
            self.assertEqual(result["resolution"]["provider"], "unknown")
            self.assertTrue(result["resolution"]["fallback_used"])
            self.assertNotIn("--model", result["command"])

    def test_dry_run_uses_capability_checked_argument_array_without_global_model_command(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            cli = write_text(
                temp / "fake-gemini",
                f"#!{sys.executable}\n"
                "import sys\n"
                "if '--help' in sys.argv:\n"
                "    print('--model --prompt --output-format --approval-mode --extensions --system-prompt --max-session-turns --core-tools --allowed-mcp-server-names --exclude-tools')\n"
                "    raise SystemExit(0)\n"
                "raise SystemExit(99)\n",
            )
            os.chmod(cli, 0o755)
            input_path = write_json(temp / "input.json", {"artifact": "candidate.drawio"})
            output_path = temp / "output.json"

            result = agent_runtime.invoke_role(
                "reviewer", input_path, output_path, cli=str(cli), dry_run=True
            )

            self.assertTrue(result["dry_run"])
            self.assertTrue(result["capabilities"]["available"])
            self.assertEqual(result["capabilities"]["missing_flags"], [])
            self.assertIsInstance(result["command"], list)
            self.assertEqual(result["command"][0], str(cli))
            self.assertEqual(
                result["command"][result["command"].index("--model") + 1],
                "vllm/DeepSeek-V4-Flash-262k",
            )
            self.assertEqual(
                result["command"][result["command"].index("--approval-mode") + 1],
                agent_runtime.ROLE_APPROVAL_MODE,
            )
            self.assertEqual(
                result["command"][result["command"].index("--extensions") + 1],
                "none",
            )
            self.assertEqual(
                result["command"][result["command"].index("--max-session-turns") + 1],
                str(agent_runtime.ROLE_MAX_SESSION_TURNS),
            )
            self.assertEqual(
                result["command"][result["command"].index("--core-tools") + 1],
                agent_runtime.ROLE_CORE_TOOL_SENTINEL,
            )
            self.assertEqual(
                result["command"][
                    result["command"].index("--allowed-mcp-server-names") + 1
                ],
                "",
            )
            excluded = result["command"][
                result["command"].index("--exclude-tools") + 1
            ].split(",")
            self.assertIn("agent", excluded)
            self.assertIn("ask_user_question", excluded)
            self.assertIn("web_search", excluded)
            self.assertIn("mcp__*", excluded)
            system_prompt = result["command"][
                result["command"].index("--system-prompt") + 1
            ]
            self.assertIn("Do not call or delegate to any agent", system_prompt)
            self.assertIn("## Required output JSON Schema", system_prompt)
            self.assertNotIn("sh", result["command"])
            self.assertNotIn("-c", result["command"])
            self.assertNotIn("/model", " ".join(result["command"]))
            self.assertEqual(result["resolution"]["resolution_mode"], "isolated_cli")
            self.assertEqual(result["isolation_controls"]["approval_mode"], "default")
            self.assertEqual(
                result["isolation_controls"]["max_session_turns"],
                agent_runtime.ROLE_MAX_SESSION_TURNS,
            )
            self.assertEqual(
                result["isolation_controls"]["core_tools"],
                [agent_runtime.ROLE_CORE_TOOL_SENTINEL],
            )
            self.assertEqual(result["isolation_controls"]["allowed_mcp_servers"], [])
            self.assertEqual(
                result["isolation_controls"]["excluded_tools"],
                list(agent_runtime.ROLE_EXCLUDED_TOOLS),
            )
            self.assertFalse(output_path.exists(), "dry-run must not execute or publish model output")
            policy = json.loads(
                (ROOT / "data" / "model-routing.default.json").read_text(encoding="utf-8")
            )
            self.assertEqual(policy["global_interactive_model"], "preserve")

    def test_invoke_role_uses_default_approval_mode_and_rejects_plan_mode_cli(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            cli = write_text(
                temp / "approval-mode-cli",
                f"#!{sys.executable}\n"
                "import json, sys\n"
                "if '--help' in sys.argv:\n"
                "    print('--model --prompt --output-format --approval-mode --extensions --system-prompt --max-session-turns --core-tools --allowed-mcp-server-names --exclude-tools')\n"
                "    raise SystemExit(0)\n"
                "if '--version' in sys.argv:\n"
                "    print('26.5.17-test')\n"
                "    raise SystemExit(0)\n"
                "payload=json.loads(sys.stdin.read())\n"
                "model=sys.argv[sys.argv.index('--model')+1]\n"
                "approval=sys.argv[sys.argv.index('--approval-mode')+1]\n"
                "if approval == 'plan':\n"
                "    print('plan mode is not allowed for isolated roles', file=sys.stderr)\n"
                "    raise SystemExit(23)\n"
                "value={'schema_version':1,'verdict_id':'approval-mode','run_id':payload['run_id'],'candidate_sha256':payload['candidate']['sha256'],'report_sha256':payload['validation_report']['sha256'],'receipt_sha256':payload['validation_receipt']['sha256'],'verdict':'approve','reviewed_at':'2026-07-16T00:00:00Z','findings':[]}\n"
                "encoded=json.dumps(value)\n"
                "print(json.dumps([{'type':'system','subtype':'init','model':model,'qwen_code_version':'0.13.1','agents':[],'slash_commands':[]},"
                "{'type':'assistant','message':{'model':model,'content':[{'type':'text','text':encoded}]}},"
                "{'type':'result','subtype':'success','is_error':False,'result':encoded,'stats':{'models':{model:{'api':{'totalRequests':1}}}}}]))\n",
            )
            os.chmod(cli, 0o755)
            input_path = write_json(
                temp / "input.json",
                {
                    "run_id": "approval-mode-run",
                    "candidate": {"sha256": "1" * 64},
                    "validation_report": {"sha256": "2" * 64},
                    "validation_receipt": {"sha256": "3" * 64},
                },
            )
            output_path = temp / "verdict.json"

            result = agent_runtime.invoke_role(
                "reviewer",
                input_path,
                output_path,
                cli=str(cli),
                run_dir=temp / "run",
            )

            self.assertEqual(result["command"][result["command"].index("--approval-mode") + 1], "default")
            self.assertEqual(result["runtime_metadata"]["reported_model"], "vllm/DeepSeek-V4-Flash-262k")
            self.assertTrue(result["runtime_metadata"]["isolation_proof"]["verified"])
            self.assertEqual(result["runtime_metadata"]["isolation_proof"]["tool_calls"], 0)

            events = [
                json.loads(line)
                for line in (temp / "run/run-manifest.jsonl").read_text().splitlines()
            ]
            finished = next(event for event in events if event["event_type"] == "role_finished")
            self.assertEqual(finished["payload"]["isolation_controls"]["approval_mode"], "default")
            self.assertEqual(
                finished["payload"]["isolation_controls"]["max_session_turns"],
                agent_runtime.ROLE_MAX_SESSION_TURNS,
            )
            self.assertEqual(
                finished["payload"]["isolation_controls"]["core_tools"],
                [agent_runtime.ROLE_CORE_TOOL_SENTINEL],
            )
            self.assertEqual(
                finished["payload"]["isolation_controls"]["allowed_mcp_servers"],
                [],
            )
            self.assertEqual(
                finished["payload"]["isolation_controls"]["excluded_tools"],
                list(agent_runtime.ROLE_EXCLUDED_TOOLS),
            )
            self.assertTrue(output_path.exists())
            self.assertEqual(json.loads(output_path.read_text())["verdict"], "approve")

    def test_gigacode_dry_run_pins_corporate_auth_type_when_supported(self):
        for executable, help_text in (
            (
                "gigacode",
                "--model --prompt --output-format --approval-mode --auth-type --extensions --system-prompt --max-session-turns --core-tools --allowed-mcp-server-names --exclude-tools",
            ),
            (
                "corporate-wrapper",
                "GigaCode - CLI --model --prompt --output-format --approval-mode "
                "--auth-type choices: gigacode --extensions --system-prompt "
                "--max-session-turns --core-tools --allowed-mcp-server-names --exclude-tools",
            ),
        ):
            with self.subTest(executable=executable), tempfile.TemporaryDirectory() as temp:
                temp = Path(temp)
                cli = write_text(
                    temp / executable,
                    f"#!{sys.executable}\n"
                    "import sys\n"
                    "if '--help' in sys.argv:\n"
                    f"    print({help_text!r})\n"
                    "    raise SystemExit(0)\n"
                    "raise SystemExit(99)\n",
                )
                os.chmod(cli, 0o755)
                result = agent_runtime.invoke_role(
                    "reviewer", write_json(temp / "input.json", {"artifact": "candidate.drawio"}),
                    temp / "output.json", cli=str(cli), dry_run=True,
                )
                self.assertEqual(result["command"].count("--auth-type"), 1)
                self.assertEqual(
                    result["command"][result["command"].index("--auth-type") + 1],
                    "gigacode",
                )

    def test_default_cli_uses_detected_corporate_gigacode_path(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            cli = write_text(
                temp / "gigacode",
                f"#!{sys.executable}\n"
                "import sys\n"
                "if '--help' in sys.argv:\n"
                "    print('GigaCode --model --prompt --output-format --approval-mode --auth-type gigacode --extensions --system-prompt --max-session-turns --core-tools --allowed-mcp-server-names --exclude-tools')\n"
                "    raise SystemExit(0)\n"
                "raise SystemExit(99)\n",
            )
            os.chmod(cli, 0o755)
            original = agent_runtime.DEFAULT_CLI
            agent_runtime.DEFAULT_CLI = str(cli)
            try:
                result = agent_runtime.invoke_role(
                    "reviewer", write_json(temp / "input.json", {"artifact": "candidate.drawio"}),
                    temp / "output.json", dry_run=True,
                )
            finally:
                agent_runtime.DEFAULT_CLI = original
            self.assertEqual(result["command"][0], str(cli))
            self.assertEqual(
                result["command"][result["command"].index("--auth-type") + 1],
                "gigacode",
            )

    def test_dry_run_refuses_cli_without_required_capabilities(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            cli = write_text(
                temp / "limited-cli",
                f"#!{sys.executable}\nprint('--model only')\n",
            )
            os.chmod(cli, 0o755)
            input_path = write_json(temp / "input.json", {})
            with self.assertRaisesRegex(supervisor.SupervisorError, "lacks isolated-role capabilities"):
                agent_runtime.invoke_role(
                    "reviewer", input_path, temp / "output.json",
                    cli=str(cli), dry_run=True,
                )

    def test_role_does_not_start_without_empty_mcp_allowlist_capability(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            executed = temp / "executed"
            cli = write_text(
                temp / "mcp-limited-cli",
                f"#!{sys.executable}\n"
                "import pathlib, sys\n"
                "if '--help' in sys.argv:\n"
                "    print('--model --prompt --output-format --approval-mode --extensions --system-prompt --max-session-turns --core-tools --exclude-tools')\n"
                "    raise SystemExit(0)\n"
                f"pathlib.Path({str(executed)!r}).write_text('started')\n",
            )
            os.chmod(cli, 0o755)

            with self.assertRaisesRegex(
                supervisor.SupervisorError,
                "--allowed-mcp-server-names",
            ):
                agent_runtime.invoke_role(
                    "reviewer",
                    write_json(temp / "input.json", {}),
                    temp / "output.json",
                    cli=str(cli),
                    dry_run=True,
                )

            self.assertFalse(executed.exists())

    def test_cli_end_to_end_supervisor_sequence_creates_all_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            run_dir = temp / "run"
            source = write_text(temp / "input.drawio", diagram_xml(obstacle=True))
            supervisor_cli = SCRIPTS / "diagram_supervisor.py"
            agent_cli = SCRIPTS / "agent_runtime.py"

            def run(*args):
                return subprocess.run(
                    [sys.executable, *map(str, args)], text=True, capture_output=True,
                    check=True, cwd=ROOT,
                )

            run(
                supervisor_cli, "host-preflight", "--workspace", temp,
                "--run-dir", run_dir, "--cli", sys.executable,
            )
            run(supervisor_cli, "inspect", source, "--output", run_dir / "diagram-spec.json")
            run(supervisor_cli, "state", run_dir, "analyzed", "--artifact", source)
            run(supervisor_cli, "validate", source, "--run-dir", run_dir, "--attempt-id", "baseline")
            run(supervisor_cli, "state", run_dir, "patching", "--artifact", source)
            run(supervisor_cli, "route-edge", source, "edge", "--output", run_dir / "edge.patch.json")
            run(
                supervisor_cli, "patch", source, run_dir / "edge.patch.json",
                "--output", run_dir / "candidate.drawio", "--result", run_dir / "patch-result.json",
            )
            run(supervisor_cli, "state", run_dir, "validating", "--artifact", source)
            run(
                supervisor_cli, "validate", run_dir / "candidate.drawio",
                "--run-dir", run_dir, "--attempt-id", "candidate",
            )
            run(
                supervisor_cli, "review-input", run_dir, run_dir / "candidate.drawio",
                run_dir / "attempts/candidate/validation-report.json",
                run_dir / "attempts/candidate/validation-receipt.json",
                run_dir / "edge.patch.json", "--output", run_dir / "reviewer-input.json",
            )
            fake = self.fake_cli(
                temp / "reviewer-cli",
                "payload=json.loads(sys.stdin.read())\n"
                "emit({'schema_version':1,'verdict_id':'e2e','run_id':payload['run_id'],'candidate_sha256':payload['candidate']['artifact']['sha256'],'report_sha256':payload['candidate']['report']['sha256'],'receipt_sha256':payload['candidate']['receipt']['sha256'],'verdict':'approve','reviewed_at':'2026-07-16T00:00:00Z','findings':[]})\n",
            )
            run(
                agent_cli, "reviewer", run_dir / "reviewer-input.json", "--cli", fake,
                "--run-dir", run_dir, "--output", run_dir / "reviewer-verdict.json",
            )
            run(
                supervisor_cli, "candidate", run_dir, run_dir / "candidate.drawio",
                run_dir / "attempts/baseline/validation-report.json",
                run_dir / "attempts/candidate/validation-report.json",
                run_dir / "edge.patch.json",
                run_dir / "attempts/baseline/validation-receipt.json",
                run_dir / "attempts/candidate/validation-receipt.json",
                "--reviewer-verdict", run_dir / "reviewer-verdict.json",
                "--repair-class", "edge-route",
            )
            expected = (
                "diagram-spec.json", "state.json", "run-manifest.jsonl", "edge.patch.json",
                "patch-result.json", "candidate.drawio", "reviewer-input.json", "reviewer-verdict.json",
                "attempts/baseline/validation-report.json", "attempts/baseline/validation-receipt.json",
                "attempts/candidate/validation-report.json", "attempts/candidate/validation-receipt.json",
            )
            for relative in expected:
                self.assertTrue((run_dir / relative).exists(), relative)
            self.assertEqual(supervisor.load_state(run_dir)["state"], "accepted_candidate")


class ValidationReportV2Tests(unittest.TestCase):
    def test_explicit_route_metrics_include_bends_length_and_complexity(self):
        raw = b"""<?xml version="1.0" encoding="UTF-8"?>
<mxfile><diagram id="page-1" name="Page-1"><mxGraphModel><root>
  <mxCell id="0"/><mxCell id="1" parent="0"/>
  <mxCell id="a" parent="1" vertex="1"><mxGeometry x="0" y="0" width="20" height="20" as="geometry"/></mxCell>
  <mxCell id="b" parent="1" vertex="1"><mxGeometry x="100" y="100" width="20" height="20" as="geometry"/></mxCell>
  <mxCell id="e" parent="1" source="a" target="b" edge="1">
    <mxGeometry relative="1" as="geometry"><Array as="points"><mxPoint x="10" y="110"/></Array></mxGeometry>
  </mxCell>
</root></mxGraphModel></diagram></mxfile>"""
        report_value = drawio_validator.validate_tree(
            ET.ElementTree(ET.fromstring(raw)),
            artifact_sha256=hashlib.sha256(raw).hexdigest(),
        )
        metrics = report_value["metrics"]

        self.assertEqual(metrics["bend_count"], 1)
        self.assertEqual(metrics["route_length"], 200.0)
        self.assertEqual(metrics["route_complexity"], 1_000_200)
        self.assertEqual(
            metrics["route_complexity_encoding"],
            "bend_count*1000000+rounded_route_length",
        )

    def test_finding_id_does_not_depend_on_human_readable_message(self):
        def finding(message):
            report_value = drawio_validator.ValidationReport(report_version=2)
            report_value.add(
                "layout", "warning", "artifact.readability.crossing", "/pages/0",
                message, "e1", elements=["e1", "e2"],
                remediation_class="edge-route", reconstructability="deterministic",
            )
            return report_value.finish()["findings"][0]

        original = finding("edges 'e1' and 'e2' cross")
        reworded = finding("connectors 'e1' and 'e2' intersect")

        self.assertNotEqual(original["message"], reworded["message"])
        self.assertEqual(original["finding_id"], reworded["finding_id"])

    def test_multi_element_geometry_hash_and_finding_identity_survive_strict_promotion(self):
        raw = diagram_xml(crossing=True).encode("utf-8")
        artifact_hash = hashlib.sha256(raw).hexdigest()

        def validate(strict):
            tree = ET.ElementTree(ET.fromstring(raw))
            return drawio_validator.validate_tree(
                tree, strict=strict, artifact_sha256=artifact_hash
            )

        relaxed = validate(False)
        strict = validate(True)
        relaxed_crossing = next(
            finding for finding in relaxed["findings"]
            if finding["code"] == "artifact.readability.crossing"
        )
        strict_crossing = next(
            finding for finding in strict["findings"]
            if finding["code"] == "artifact.readability.crossing"
        )

        self.assertEqual(relaxed["report_version"], 2)
        self.assertEqual(relaxed["artifact_sha256"], artifact_hash)
        self.assertEqual(relaxed["validator"]["name"], "publish-drawio-validator")
        self.assertEqual(relaxed_crossing["element"], "e1")
        self.assertEqual(relaxed_crossing["elements"], ["e1", "e2"])
        self.assertEqual(set(relaxed_crossing["geometry"]["elements"]), {"e1", "e2"})
        self.assertEqual(relaxed_crossing["remediation_class"], "edge-route")
        self.assertEqual(relaxed_crossing["reconstructability"], "deterministic")
        self.assertRegex(relaxed_crossing["finding_id"], r"^finding-[0-9a-f]{20}$")
        self.assertEqual(relaxed_crossing["severity"], "warning")
        self.assertEqual(strict_crossing["severity"], "error")
        for stable_field in ("finding_id", "code", "path", "element", "elements"):
            self.assertEqual(relaxed_crossing[stable_field], strict_crossing[stable_field])
        self.assertEqual(
            json.dumps(relaxed_crossing["geometry"], sort_keys=True),
            json.dumps(strict_crossing["geometry"], sort_keys=True),
        )
        serialized = json.dumps(relaxed, ensure_ascii=False, allow_nan=False)
        self.assertNotIn("NaN", serialized)

    def test_duplicate_findings_receive_stable_distinct_occurrence_ids(self):
        def render():
            report = drawio_validator.ValidationReport(report_version=2)
            for _ in range(2):
                report.add(
                    "layout", "warning", "artifact.readability.crossing", "/pages/0",
                    "edges 'e1' and 'e2' cross", "e1", elements=["e1", "e2"],
                    geometry={"elements": {"e1": {}, "e2": {}}},
                    remediation_class="edge-route", reconstructability="deterministic",
                )
            return report.finish(strict=True)["findings"]

        first = render()
        second = render()
        self.assertEqual([item["finding_id"] for item in first], [item["finding_id"] for item in second])
        self.assertEqual(len({item["finding_id"] for item in first}), 2)


if __name__ == "__main__":
    unittest.main()
