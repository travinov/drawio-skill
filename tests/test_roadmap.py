import json
import os
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures", "roadmap")


def run_cmd(*args):
    return subprocess.run(
        [sys.executable, *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )


def read_text(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


class RoadmapDocumentationTests(unittest.TestCase):
    def test_skill_routes_roadmap_to_reference_and_scripts(self):
        skill = read_text("SKILL.md")
        self.assertIn("roadmap diagrams", skill)
        self.assertIn("references/roadmap.md", skill)
        self.assertIn("scripts/roadmap_validate.py", skill)
        self.assertIn("scripts/roadmap.py", skill)

    def test_intake_reference_has_roadmap_prompts(self):
        intake = read_text("references", "diagram-intake.md")
        self.assertIn("roadmap", intake)
        self.assertIn("baseline", intake)
        self.assertIn("shift", intake)


class RoadmapValidationTests(unittest.TestCase):
    def test_basic_roadmap_validates(self):
        proc = run_cmd("scripts/roadmap_validate.py", os.path.join(FIXTURES, "basic.yaml"))
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)

    def test_unknown_dependency_ref_fails(self):
        proc = run_cmd("scripts/roadmap_validate.py", os.path.join(FIXTURES, "invalid_unknown_ref.yaml"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("unknown target", proc.stderr + proc.stdout)

    def test_baseline_delta_states_are_reported(self):
        proc = run_cmd("scripts/roadmap_validate.py", os.path.join(FIXTURES, "baseline_shift.yaml"), "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        result = json.loads(proc.stdout)
        states = {delta["id"]: delta["state"] for delta in result["deltas"]}
        self.assertEqual(states["m-wallet-pilot"], "delayed")
        self.assertEqual(states["m-billing-api"], "accelerated")
        self.assertEqual(states["m-analytics-beta"], "added")
        self.assertEqual(states["m-legacy-report"], "removed")


class RoadmapGeneratorTests(unittest.TestCase):
    def test_baseline_roadmap_generates_valid_drawio_with_shift_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "roadmap.drawio")
            proc = run_cmd("scripts/roadmap.py", os.path.join(FIXTURES, "baseline_shift.yaml"), "-o", out)
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)

            validate = run_cmd("scripts/validate.py", out)
            self.assertEqual(validate.returncode, 0, validate.stderr + validate.stdout)

            tree = ET.parse(out)
            ids = {cell.get("id") for cell in tree.findall(".//mxCell")}
            self.assertIn("baseline_m-wallet-pilot", ids)
            self.assertIn("milestone_m-wallet-pilot", ids)
            self.assertIn("shift_m-wallet-pilot", ids)
            self.assertIn("dep_dep-billing-wallet", ids)

    def test_dense_roadmap_generates_without_overlap_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "dense.drawio")
            proc = run_cmd("scripts/roadmap.py", os.path.join(FIXTURES, "dense_overlap.yaml"), "-o", out)
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)

            validate = run_cmd("scripts/validate.py", out, "--strict")
            self.assertEqual(validate.returncode, 0, validate.stderr + validate.stdout)


if __name__ == "__main__":
    unittest.main()
