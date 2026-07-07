import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures", "gitflow")


def run_cmd(*args):
    return subprocess.run(
        [sys.executable, *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )


class GitflowValidationTests(unittest.TestCase):
    def test_valid_classic_flow_passes(self):
        proc = run_cmd("scripts/gitflow_validate.py", os.path.join(FIXTURES, "classic.json"))
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)

    def test_valid_hotfix_flow_passes(self):
        proc = run_cmd("scripts/gitflow_validate.py", os.path.join(FIXTURES, "hotfix.json"))
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)

    def test_custom_openspec_flow_passes_without_develop(self):
        proc = run_cmd("scripts/gitflow_validate.py", os.path.join(FIXTURES, "openspec_custom.json"), "--strict")
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)

    def test_unknown_branch_fails(self):
        proc = run_cmd("scripts/gitflow_validate.py", os.path.join(FIXTURES, "invalid_unknown_branch.json"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("unknown branch", proc.stderr + proc.stdout)

    def test_merge_into_self_fails(self):
        proc = run_cmd("scripts/gitflow_validate.py", os.path.join(FIXTURES, "invalid_merge_self.json"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("merge into itself", proc.stderr + proc.stdout)

    def test_gitflow_rule_warnings_fail_in_strict_mode(self):
        path = os.path.join(FIXTURES, "invalid_strict_rules.json")
        relaxed = run_cmd("scripts/gitflow_validate.py", path)
        strict = run_cmd("scripts/gitflow_validate.py", path, "--strict")
        self.assertEqual(relaxed.returncode, 0, relaxed.stderr + relaxed.stdout)
        self.assertIn("warning:", relaxed.stderr + relaxed.stdout)
        self.assertNotEqual(strict.returncode, 0)
        self.assertIn("error:", strict.stderr + strict.stdout)


class GitflowGeneratorTests(unittest.TestCase):
    def test_builtin_route_generates_valid_drawio_with_semantic_coordinates(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "classic.drawio")
            proc = run_cmd("scripts/gitflow.py", os.path.join(FIXTURES, "classic.json"), "-o", out, "--route", "builtin")
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)

            validate = run_cmd("scripts/validate.py", out)
            self.assertEqual(validate.returncode, 0, validate.stderr + validate.stdout)

            tree = ET.parse(out)
            cells = {c.get("id"): c for c in tree.findall(".//mxCell") if c.get("id")}
            c1 = cells["event_c1"].find("mxGeometry")
            b1 = cells["event_b1"].find("mxGeometry")
            main_lane = cells["lane_main"].find("mxGeometry")
            develop_lane = cells["lane_develop"].find("mxGeometry")
            feature_lane = cells["lane_feature_auth"].find("mxGeometry")
            self.assertLess(float(c1.get("x")), float(b1.get("x")))
            self.assertLess(float(main_lane.get("y")), float(develop_lane.get("y")))
            self.assertLess(float(develop_lane.get("y")), float(feature_lane.get("y")))

    def test_auto_falls_back_without_neato(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "hotfix.drawio")
            env = os.environ.copy()
            env["PATH"] = ""
            proc = subprocess.run(
                [
                    sys.executable,
                    "scripts/gitflow.py",
                    os.path.join(FIXTURES, "hotfix.json"),
                    "-o",
                    out,
                    "--route",
                    "auto",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                env=env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertIn("builtin", proc.stderr + proc.stdout)

    def test_custom_openspec_flow_generates_valid_drawio(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "openspec.drawio")
            proc = run_cmd("scripts/gitflow.py", os.path.join(FIXTURES, "openspec_custom.json"), "-o", out, "--route", "builtin")
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            validate = run_cmd("scripts/validate.py", out, "--strict")
            self.assertEqual(validate.returncode, 0, validate.stderr + validate.stdout)

    def test_graphviz_route_requires_neato(self):
        if shutil.which("neato"):
            self.skipTest("neato is installed; absence behavior is not applicable")
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "classic.drawio")
            proc = run_cmd("scripts/gitflow.py", os.path.join(FIXTURES, "classic.json"), "-o", out, "--route", "graphviz")
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("neato", proc.stderr + proc.stdout)


if __name__ == "__main__":
    unittest.main()
