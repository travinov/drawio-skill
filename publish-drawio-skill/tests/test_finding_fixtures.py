import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
FIXTURES = ROOT / "tests" / "fixtures"
ROADMAP = FIXTURES / "roadmap"
GITFLOW = FIXTURES / "gitflow"
ARTIFACT = FIXTURES / "artifact"
LAYOUT = FIXTURES / "layout"
sys.path.insert(0, str(SCRIPTS))

import gitflow_validate
import roadmap_validate


def run_script(name, *args, env=None):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / name), *map(str, args)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def load_script(name):
    spec = importlib.util.spec_from_file_location(f"fixture_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pointer_parts(path):
    if not path:
        return []
    return [part.replace("~1", "/").replace("~0", "~") for part in path.lstrip("/").split("/")]


def parent_for(document, path, create=False):
    parts = pointer_parts(path)
    current = document
    for part in parts[:-1]:
        if isinstance(current, list):
            current = current[int(part)]
        else:
            if create and part not in current:
                current[part] = {}
            current = current[part]
    return current, parts[-1]


def format_template(value, index):
    if isinstance(value, str):
        return value.format(i=index)
    if isinstance(value, list):
        return [format_template(item, index) for item in value]
    if isinstance(value, dict):
        return {key: format_template(item, index) for key, item in value.items()}
    return copy.deepcopy(value)


def apply_ops(document, ops):
    result = copy.deepcopy(document)
    for operation in ops:
        op = operation["op"]
        if op == "replace_document":
            result = copy.deepcopy(operation["value"])
            continue
        parent, key = parent_for(result, operation["path"], create=op in {"set", "repeat"})
        if op == "set":
            if isinstance(parent, list):
                parent[int(key)] = copy.deepcopy(operation["value"])
            else:
                parent[key] = copy.deepcopy(operation["value"])
        elif op == "remove":
            if isinstance(parent, list):
                del parent[int(key)]
            else:
                del parent[key]
        elif op == "append":
            target = parent[int(key)] if isinstance(parent, list) else parent[key]
            target.append(copy.deepcopy(operation["value"]))
        elif op == "repeat":
            values = [format_template(operation["template"], index) for index in range(operation["count"])]
            if isinstance(parent, list):
                parent[int(key)] = values
            else:
                parent[key] = values
        else:
            raise AssertionError(f"unsupported fixture operation: {op}")
    return result


def finding_codes(report):
    return {finding["code"] for finding in report["findings"]}


class RoadmapFindingFixtureTests(unittest.TestCase):
    def test_all_scales_with_and_without_baseline_are_strict_positive(self):
        without_baseline = {
            "week": ROADMAP / "week.yaml",
            "month": ROADMAP / "basic.yaml",
            "quarter": ROADMAP / "quarter.yaml",
            "date": ROADMAP / "date.yaml",
            "order": ROADMAP / "positive" / "order_without_baseline.yaml",
        }
        with_baseline = {
            path.stem.removesuffix("_with_baseline"): path
            for path in sorted((ROADMAP / "positive").glob("*_with_baseline.yaml"))
        }
        self.assertEqual(set(with_baseline), set(without_baseline))
        for baseline_kind, fixtures in (("without", without_baseline), ("with", with_baseline)):
            for scale, source in fixtures.items():
                with self.subTest(baseline=baseline_kind, scale=scale):
                    model = roadmap_validate.load_yaml(source)
                    self.assertEqual(bool(model.get("baseline")), baseline_kind == "with")
                    normalized, report = roadmap_validate.validate_document(model, strict=True)
                    self.assertEqual(report["summary"]["errors"], 0, report)
                    self.assertEqual(normalized["time_scale"], scale)

    def test_baseline_fixture_for_every_scale_generates_and_profiles_strictly(self):
        for source in sorted((ROADMAP / "positive").glob("*_with_baseline.yaml")):
            with self.subTest(source=source.name), tempfile.TemporaryDirectory() as temp:
                artifact = Path(temp) / "roadmap.drawio"
                generated = run_script("roadmap.py", source, "-o", artifact)
                self.assertEqual(generated.returncode, 0, generated.stderr + generated.stdout)
                profile = run_script(
                    "validate.py", artifact, "--profile", "roadmap", "--source", source, "--strict", "--json"
                )
                self.assertEqual(profile.returncode, 0, profile.stderr + profile.stdout)

    def test_negative_fixture_matrix_covers_every_roadmap_finding_code(self):
        matrix = json.loads((ROADMAP / "negative_cases.json").read_text(encoding="utf-8"))
        observed = set()
        for case in matrix["cases"]:
            with self.subTest(case=case["id"]):
                base = roadmap_validate.load_yaml(ROADMAP / case["base"])
                model = apply_ops(base, case.get("ops", []))
                _, report = roadmap_validate.validate_document(model, strict=case.get("strict", False))
                codes = finding_codes(report)
                observed.update(codes)
                self.assertTrue(set(case["expected"]).issubset(codes), report)
                self.assertGreater(report["summary"]["errors"], 0, report)
        expected = set(matrix["expected_codes"])
        self.assertTrue(expected.issubset(observed), sorted(expected - observed))
        # This informational finding is emitted by valid no-lane fixtures, not by a negative contract.
        _, no_lane_report = roadmap_validate.validate_document(roadmap_validate.load_yaml(ROADMAP / "date.yaml"))
        self.assertIn("roadmap.lanes.defaulted", finding_codes(no_lane_report))


class GitflowFindingFixtureTests(unittest.TestCase):
    def test_date_order_and_custom_positive_fixtures_run_full_strict_pipeline(self):
        sources = sorted((GITFLOW / "positive").glob("*.json"))
        self.assertEqual({path.stem for path in sources}, {"date", "order", "custom"})
        for source in sources:
            with self.subTest(source=source.name), tempfile.TemporaryDirectory() as temp:
                model = json.loads(source.read_text(encoding="utf-8"))
                _, report = gitflow_validate.validate_document(model, strict=True)
                self.assertEqual(report["summary"]["errors"], 0, report)
                artifact = Path(temp) / "gitflow.drawio"
                generated = run_script("gitflow.py", source, "-o", artifact, "--route", "builtin")
                self.assertEqual(generated.returncode, 0, generated.stderr + generated.stdout)
                profile = run_script(
                    "validate.py", artifact, "--profile", "gitflow", "--source", source, "--strict", "--json"
                )
                self.assertEqual(profile.returncode, 0, profile.stderr + profile.stdout)

    def test_negative_fixture_matrix_covers_every_gitflow_finding_code(self):
        matrix = json.loads((GITFLOW / "negative_cases.json").read_text(encoding="utf-8"))
        observed = set()
        for case in matrix["cases"]:
            with self.subTest(case=case["id"]):
                base = json.loads((GITFLOW / case["base"]).read_text(encoding="utf-8"))
                model = apply_ops(base, case.get("ops", []))
                _, report = gitflow_validate.validate_document(model, strict=case.get("strict", False))
                codes = finding_codes(report)
                observed.update(codes)
                self.assertTrue(set(case["expected"]).issubset(codes), report)
                self.assertGreater(report["summary"]["errors"], 0, report)
        expected = set(matrix["expected_codes"])
        self.assertTrue(expected.issubset(observed), sorted(expected - observed))


def xml_cell(tree, cell_id):
    for cell in tree.findall(".//mxCell"):
        if cell.get("id") == cell_id:
            return cell
    raise AssertionError(f"fixture mutation cannot find cell {cell_id!r}")


def mutate_artifact(path, mutations):
    tree = ET.parse(path)
    for mutation in mutations:
        cell = xml_cell(tree, mutation["id"])
        if mutation["op"] == "set_cell":
            cell.set(mutation["attribute"], mutation["value"])
        elif mutation["op"] == "set_geometry":
            geometry = cell.find("mxGeometry")
            if geometry is None:
                raise AssertionError(f"cell {mutation['id']!r} has no geometry")
            geometry.set(mutation["attribute"], mutation["value"])
        elif mutation["op"] == "remove_cell":
            parent = next((candidate for candidate in tree.iter() if cell in list(candidate)), None)
            if parent is None:
                raise AssertionError(f"cell {mutation['id']!r} has no parent")
            parent.remove(cell)
        else:
            raise AssertionError(f"unsupported artifact mutation: {mutation['op']}")
    tree.write(path, encoding="utf-8", xml_declaration=True)


class ArtifactFindingFixtureTests(unittest.TestCase):
    def test_layout_geometry_fixtures_cover_v2_codes_and_exemptions(self):
        cases = {
            "shared-trunk.drawio": {
                "artifact.readability.shared_segment",
                "artifact.readability.route_congestion",
                "artifact.readability.port_congestion",
            },
            "label-collisions.drawio": {"artifact.readability.edge_label_collision"},
            "detour-bends.drawio": {
                "artifact.layout.excessive_detour",
                "artifact.layout.excessive_bends",
            },
            "feedback-intrusion.drawio": {"artifact.layout.feedback_intrusion"},
            "extreme-aspect.drawio": {"artifact.layout.aspect_ratio"},
        }
        for filename, expected in cases.items():
            with self.subTest(fixture=filename):
                proc = run_script("validate.py", LAYOUT / filename, "--strict", "--json")
                self.assertNotEqual(proc.returncode, 0, proc.stderr + proc.stdout)
                self.assertTrue(expected.issubset(finding_codes(json.loads(proc.stdout))))

    def test_layout_geometry_exemptions_do_not_emit_shared_segment(self):
        for filename in ("allowed-fanout.drawio", "intentional-bus.drawio"):
            with self.subTest(fixture=filename):
                proc = run_script("validate.py", LAYOUT / filename, "--strict", "--json")
                self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
                self.assertNotIn("artifact.readability.shared_segment", finding_codes(json.loads(proc.stdout)))

    def test_malformed_structural_and_readability_fixtures_cover_stable_codes(self):
        cases = {
            "malformed_xml.drawio": {"artifact.xml.parse"},
            "compressed_page.drawio": {"artifact.page.compressed"},
            "structural_errors.drawio": {
                "artifact.id.missing",
                "artifact.id.duplicate",
                "artifact.cell.invalid_kind",
                "artifact.reference.unresolved",
                "artifact.geometry.invalid",
                "artifact.readability.overlap",
                "artifact.readability.text_overflow",
                "artifact.structure.generic",
            },
            "readability_routes.drawio": {
                "artifact.readability.crossing",
                "artifact.readability.route_through",
            },
            "layout_issues.drawio": {
                "artifact.geometry.invalid",
                "artifact.layout.container_overflow",
                "artifact.layout.lane_title_collision",
                "artifact.layout.lane_size",
                "artifact.layout.container_overlap",
                "artifact.layout.terminal_segment",
                "artifact.layout.routing_uncertain",
                "artifact.readability.route_through",
            },
        }
        observed = set()
        for filename, expected in cases.items():
            with self.subTest(fixture=filename):
                proc = run_script("validate.py", ARTIFACT / filename, "--strict", "--json")
                self.assertNotEqual(proc.returncode, 0, proc.stderr + proc.stdout)
                report = json.loads(proc.stdout)
                codes = finding_codes(report)
                observed.update(codes)
                self.assertTrue(expected.issubset(codes), report)
        self.assertEqual(set().union(*cases.values()), observed)

    def test_valid_layout_accepts_touching_lanes_and_simple_auto_route(self):
        proc = run_script("validate.py", ARTIFACT / "layout_valid.drawio", "--strict", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        report = json.loads(proc.stdout)
        self.assertEqual(report["findings"], [])

    def test_layout_codes_are_identical_when_strict_promotes_severity(self):
        source = ARTIFACT / "layout_issues.drawio"
        relaxed = run_script("validate.py", source, "--json")
        strict = run_script("validate.py", source, "--strict", "--json")
        self.assertEqual(relaxed.returncode, 0, relaxed.stderr + relaxed.stdout)
        self.assertNotEqual(strict.returncode, 0, strict.stderr + strict.stdout)
        relaxed_report = json.loads(relaxed.stdout)
        strict_report = json.loads(strict.stdout)
        relaxed_identity = {
            (item["code"], item["path"], item.get("element"), item["message"])
            for item in relaxed_report["findings"]
        }
        strict_identity = {
            (item["code"], item["path"], item.get("element"), item["message"])
            for item in strict_report["findings"]
        }
        self.assertEqual(relaxed_identity, strict_identity)
        self.assertTrue(all(item["severity"] == "warning" for item in relaxed_report["findings"]))
        self.assertTrue(all(item["severity"] == "error" for item in strict_report["findings"]))

    def test_generic_code_fallbacks_remain_stable(self):
        validator = load_script("validate")
        self.assertEqual(validator._code("unclassified structural failure", "error"), "artifact.structure.generic")
        self.assertEqual(validator._code("unclassified readability warning", "warning"), "artifact.readability.generic")

    def test_semantic_mismatch_fixture_matrix_covers_every_profile_code(self):
        matrix = json.loads((ARTIFACT / "semantic_mismatch_cases.json").read_text(encoding="utf-8"))
        observed = set()
        for case in matrix["cases"]:
            with self.subTest(case=case["id"]), tempfile.TemporaryDirectory() as temp:
                source = FIXTURES / case["source"]
                artifact = Path(temp) / f"{case['profile']}.drawio"
                if case["profile"] == "roadmap":
                    generated = run_script("roadmap.py", source, "-o", artifact)
                else:
                    generated = run_script("gitflow.py", source, "-o", artifact, "--route", "builtin")
                self.assertEqual(generated.returncode, 0, generated.stderr + generated.stdout)
                mutate_artifact(artifact, case.get("mutations", []))
                args = [artifact, "--profile", case["profile"]]
                if not case.get("omit_source"):
                    args += ["--source", FIXTURES / case.get("validation_source", case["source"])]
                args += ["--strict", "--json"]
                proc = run_script("validate.py", *args)
                self.assertNotEqual(proc.returncode, 0, proc.stderr + proc.stdout)
                report = json.loads(proc.stdout)
                codes = finding_codes(report)
                observed.update(codes)
                self.assertTrue(set(case["expected"]).issubset(codes), report)
        expected = set(matrix["expected_codes"])
        self.assertTrue(expected.issubset(observed), sorted(expected - observed))


MINIMAL_DRAWIO = """<?xml version="1.0"?><mxfile><diagram><mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/></root></mxGraphModel></diagram></mxfile>"""


def fake_drawio(directory, payload_hex=None, exit_code=0, write_output=True):
    executable = Path(directory) / "fake-drawio"
    body = [f"#!{sys.executable}", "import pathlib, sys"]
    if write_output:
        body.append(f"payload = bytes.fromhex({(payload_hex or '')!r})")
        body.append("pathlib.Path(sys.argv[sys.argv.index('--output') + 1]).write_bytes(payload)")
    body.append(f"raise SystemExit({exit_code})")
    executable.write_text("\n".join(body) + "\n", encoding="utf-8")
    executable.chmod(0o755)
    return executable


class ExportFindingFixtureTests(unittest.TestCase):
    def test_export_fixture_matrix_covers_every_export_code(self):
        matrix = json.loads((ARTIFACT / "export_cases.json").read_text(encoding="utf-8"))
        observed = set()
        for case in matrix["cases"]:
            with self.subTest(case=case["id"]), tempfile.TemporaryDirectory() as temp:
                source = Path(temp) / "source.drawio"
                source.write_text(MINIMAL_DRAWIO, encoding="utf-8")
                mode = case["mode"]
                if mode == "source_missing":
                    source.unlink()
                    executable = None
                elif mode == "cli_unavailable":
                    executable = Path(temp) / "missing-drawio"
                elif mode == "launch_error":
                    executable = Path(temp) / "broken-drawio"
                    executable.write_text("not an executable format", encoding="utf-8")
                    executable.chmod(0o755)
                else:
                    executable = fake_drawio(
                        temp,
                        payload_hex=case.get("payload_hex"),
                        exit_code=case.get("exit_code", 0),
                        write_output=case.get("write_output", True),
                    )
                args = [source]
                if executable is not None:
                    args += ["--drawio", executable]
                args += ["--json"]
                env = os.environ.copy()
                env["DRAWIO_CLI"] = str(Path(temp) / "configured-missing")
                proc = run_script("export_smoke.py", *args, env=env)
                self.assertNotEqual(proc.returncode, 0, proc.stderr + proc.stdout)
                report = json.loads(proc.stdout)
                codes = finding_codes(report)
                observed.update(codes)
                self.assertTrue(set(case["expected"]).issubset(codes), report)
        expected = set(matrix["expected_codes"])
        self.assertEqual(expected, observed)


if __name__ == "__main__":
    unittest.main()
