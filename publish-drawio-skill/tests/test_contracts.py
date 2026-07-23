import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import jsonschema
import yaml


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
ROADMAP = ROOT / "tests" / "fixtures" / "roadmap"
GITFLOW = ROOT / "tests" / "fixtures" / "gitflow"
sys.path.insert(0, str(SCRIPTS))

import gitflow_validate
import roadmap_validate


def run(script, *args, env=None):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *map(str, args)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=env,
    )


def cell_map(path):
    tree = ET.parse(path)
    return {cell.get("id"): cell for cell in tree.findall(".//mxCell") if cell.get("id")}


class SchemaCompilationTests(unittest.TestCase):
    def test_all_bundled_schemas_compile_as_draft_2020_12(self):
        for path in sorted((ROOT / "data").glob("*.schema.json")):
            with self.subTest(path=path.name):
                jsonschema.Draft202012Validator.check_schema(json.loads(path.read_text(encoding="utf-8")))

    def test_layout_schema_kinds_are_bundled(self):
        import lifecycle_contracts

        for kind in (
            "diagram-intake",
            "diagram-intake-analysis",
            "layout-request",
            "layout-result",
            "layout-repair-intent",
        ):
            with self.subTest(kind=kind):
                self.assertEqual(lifecycle_contracts.load_schema(kind, 1)["$schema"], "https://json-schema.org/draft/2020-12/schema")


class RoadmapContractTests(unittest.TestCase):
    def test_every_scale_validates_generates_profiles_and_is_deterministic(self):
        fixtures = {
            "week": "week.yaml",
            "month": "basic.yaml",
            "quarter": "quarter.yaml",
            "date": "date.yaml",
            "order": "order.yaml",
        }
        for scale, filename in fixtures.items():
            with self.subTest(scale=scale), tempfile.TemporaryDirectory() as temp:
                source = ROADMAP / filename
                artifact = Path(temp) / f"{scale}.drawio"
                validation = run("roadmap_validate.py", source, "--strict", "--json")
                self.assertEqual(validation.returncode, 0, validation.stderr + validation.stdout)
                generation = run("roadmap.py", source, "-o", artifact)
                self.assertEqual(generation.returncode, 0, generation.stderr + generation.stdout)
                profile = run("validate.py", artifact, "--profile", "roadmap", "--source", source, "--strict", "--json")
                self.assertEqual(profile.returncode, 0, profile.stderr + profile.stdout)
                deterministic = run("verify_determinism.py", "roadmap", source, "--json")
                self.assertEqual(deterministic.returncode, 0, deterministic.stderr + deterministic.stdout)
                labels = [cell.get("value") for cid, cell in cell_map(artifact).items() if cid.startswith("period_")]
                self.assertTrue(labels)
                if scale == "quarter":
                    self.assertTrue(all("-Q" in label for label in labels))
                elif scale == "week":
                    self.assertTrue(all("-W" in label for label in labels))
                elif scale == "date":
                    self.assertTrue(all(len(label) == 10 for label in labels))
                elif scale == "order":
                    self.assertEqual(labels, ["5", "10", "20", "30"])

    def test_unversioned_compatibility_does_not_mutate_source(self):
        model = yaml.safe_load((ROADMAP / "basic.yaml").read_text(encoding="utf-8"))
        del model["schema_version"]
        original = copy.deepcopy(model)
        normalized, report = roadmap_validate.validate_document(model)
        self.assertEqual(model, original)
        self.assertEqual(normalized["schema_version"], 1)
        self.assertIn("contract.version.missing", {finding["code"] for finding in report["findings"]})

    def test_unsupported_unknown_mixed_invalid_duplicate_and_baseline_refs_fail(self):
        base = yaml.safe_load((ROADMAP / "basic.yaml").read_text(encoding="utf-8"))
        cases = []
        unsupported = copy.deepcopy(base); unsupported["schema_version"] = 999
        cases.append((unsupported, "contract.version.unsupported"))
        unknown = copy.deepcopy(base); unknown["unknown"] = True
        cases.append((unknown, "schema.additionalProperties"))
        mixed = copy.deepcopy(base); mixed["time_scale"] = "order"
        cases.append((mixed, "schema.required"))
        invalid_date = copy.deepcopy(base); invalid_date["milestones"][0]["date"] = "2026-02-31"
        cases.append((invalid_date, "schema.format"))
        duplicate = copy.deepcopy(base); duplicate["tasks"][1]["id"] = duplicate["tasks"][0]["id"]
        cases.append((duplicate, "reference.id"))
        baseline_ref = copy.deepcopy(base)
        baseline_ref["baseline"] = {"tasks": [{"id": "old", "title": "Old", "start": "2026-01-01", "end": "2026-01-02", "outcomes": ["missing"]}]}
        cases.append((baseline_ref, "reference.baseline.outcome_unknown"))
        for model, prefix in cases:
            with self.subTest(prefix=prefix):
                _, report = roadmap_validate.validate_document(model)
                codes = {finding["code"] for finding in report["findings"]}
                self.assertTrue(any(code.startswith(prefix) for code in codes), codes)
                self.assertGreater(report["summary"]["errors"], 0)

    def test_full_delta_order_is_stable(self):
        model = yaml.safe_load((ROADMAP / "order.yaml").read_text(encoding="utf-8"))
        _, first = roadmap_validate.validate_document(model)
        _, second = roadmap_validate.validate_document(model)
        self.assertEqual(first["deltas"], second["deltas"])
        self.assertEqual(first["deltas"], sorted(first["deltas"], key=lambda item: (item["entity"], item["id"])))
        states = {(item["entity"], item["id"]): item["state"] for item in first["deltas"]}
        self.assertEqual(states[("task", "ordered-task")], "schedule_changed")
        self.assertEqual(states[("milestone", "ordered-ready")], "delayed")

    def test_special_text_milestone_outcomes_status_and_risk_are_lossless(self):
        with tempfile.TemporaryDirectory() as temp:
            artifact = Path(temp) / "order.drawio"
            proc = run("roadmap.py", ROADMAP / "order.yaml", "-o", artifact)
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            cells = cell_map(artifact)
            self.assertEqual(cells["task_ordered-task"].get("data-title"), "A & B <задача>")
            self.assertIn("A & B <задача>", cells["task_ordered-task"].get("value"))
            self.assertEqual(cells["milestone_ordered-ready"].get("data-risk"), "launch window")
            self.assertEqual(cells["milestone_ordered-ready"].get("data-status"), "on_track")
            self.assertIn("outcome_edge_outcome-quality_ordered-task", cells)
            self.assertIn("outcome_edge_outcome-quality_ordered-ready", cells)


class GitflowContractTests(unittest.TestCase):
    def test_missing_branch_is_rejected_by_schema(self):
        model = json.loads((GITFLOW / "classic.json").read_text(encoding="utf-8"))
        del model["events"][0]["branch"]
        _, report = gitflow_validate.validate_document(model)
        self.assertGreater(report["summary"]["errors"], 0)
        self.assertIn("schema.oneOf", {finding["code"] for finding in report["findings"]})

    def test_out_of_order_and_ties_use_normalized_chronology(self):
        source = GITFLOW / "out_of_order.json"
        model = json.loads(source.read_text(encoding="utf-8"))
        self.assertEqual([event["id"] for event in gitflow_validate.normalize_events(model)], ["early", "tie-a", "tie-b", "late"])
        with tempfile.TemporaryDirectory() as temp:
            artifact = Path(temp) / "ordered.drawio"
            generation = run("gitflow.py", source, "-o", artifact, "--route", "builtin")
            self.assertEqual(generation.returncode, 0, generation.stderr + generation.stdout)
            cells = cell_map(artifact)
            centers = []
            for eid in ("early", "tie-a", "tie-b", "late"):
                box = cells[f"event_{eid}"].find("mxGeometry")
                centers.append(float(box.get("x")) + float(box.get("width")) / 2)
            self.assertEqual(centers, sorted(centers))
            profile = run("validate.py", artifact, "--profile", "gitflow", "--source", source, "--strict", "--json")
            self.assertEqual(profile.returncode, 0, profile.stderr + profile.stdout)

    def test_lifecycle_and_strict_policy_have_stable_codes(self):
        model = json.loads((GITFLOW / "classic.json").read_text(encoding="utf-8"))
        duplicate_creation = copy.deepcopy(model)
        duplicate_creation["events"].append({"id": "b4", "type": "branch", "from": "main", "to": "develop", "at": "2026-07-08"})
        _, report = gitflow_validate.validate_document(duplicate_creation)
        self.assertIn("gitflow.branch.duplicate_creation", {finding["code"] for finding in report["findings"]})
        strict_model = json.loads((GITFLOW / "invalid_strict_rules.json").read_text(encoding="utf-8"))
        _, relaxed = gitflow_validate.validate_document(strict_model)
        _, strict = gitflow_validate.validate_document(strict_model, strict=True)
        relaxed_codes = {finding["code"] for finding in relaxed["findings"]}
        strict_codes = {finding["code"] for finding in strict["findings"]}
        self.assertEqual(relaxed_codes, strict_codes)
        self.assertEqual(relaxed["summary"]["errors"], 0)
        self.assertGreater(strict["summary"]["errors"], 0)

    def test_auto_falls_back_to_builtin_without_graphviz(self):
        with tempfile.TemporaryDirectory() as temp:
            artifact = Path(temp) / "flow.drawio"
            env = os.environ.copy()
            env["PATH"] = ""
            proc = run("gitflow.py", GITFLOW / "classic.json", "-o", artifact, "--route", "auto", env=env)
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertIn("builtin", proc.stderr)


class ArtifactProfileRegressionTests(unittest.TestCase):
    def test_profile_detects_wrong_coordinate_and_double_escaped_text(self):
        with tempfile.TemporaryDirectory() as temp:
            artifact = Path(temp) / "order.drawio"
            run("roadmap.py", ROADMAP / "order.yaml", "-o", artifact)
            tree = ET.parse(artifact)
            task = next(cell for cell in tree.findall(".//mxCell") if cell.get("id") == "task_ordered-task")
            task.set("data-title", "A &amp; B <задача>")
            task.set("value", "A &amp; B <задача>")
            task.find("mxGeometry").set("x", "999")
            tree.write(artifact, encoding="utf-8", xml_declaration=True)
            proc = run("validate.py", artifact, "--profile", "roadmap", "--source", ROADMAP / "order.yaml", "--json")
            self.assertNotEqual(proc.returncode, 0)
            codes = {finding["code"] for finding in json.loads(proc.stdout)["findings"]}
            self.assertIn("artifact.text.entity", codes)
            self.assertIn("artifact.coordinate.timeline", codes)

    def test_structural_findings_have_stable_codes(self):
        xml = """<mxfile><diagram name='x'><mxGraphModel><root><mxCell id='0'/><mxCell id='1' parent='0'/><mxCell id='a' parent='1' vertex='1'><mxGeometry x='0' y='0' width='10' height='10' as='geometry'/></mxCell><mxCell id='a' parent='missing' vertex='1'><mxGeometry x='20' y='0' width='10' height='10' as='geometry'/></mxCell></root></mxGraphModel></diagram></mxfile>"""
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bad.drawio"
            path.write_text(xml, encoding="utf-8")
            proc = run("validate.py", path, "--json")
            self.assertNotEqual(proc.returncode, 0)
            codes = {finding["code"] for finding in json.loads(proc.stdout)["findings"]}
            self.assertIn("artifact.id.duplicate", codes)
            self.assertIn("artifact.reference.unresolved", codes)


if __name__ == "__main__":
    unittest.main()
