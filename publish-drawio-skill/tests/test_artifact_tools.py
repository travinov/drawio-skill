import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
FIXTURES = ROOT / "tests" / "fixtures"
MINIMAL_DRAWIO = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <mxfile host="drawio">
      <diagram id="page" name="Page">
        <mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/></root></mxGraphModel>
      </diagram>
    </mxfile>
""")
PNG_HEX = "89504e470d0a1a0a0000000049454e44ae426082"


def load_script(name):
    spec = importlib.util.spec_from_file_location(f"test_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_script(name, *args, env=None):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / name), *map(str, args)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def make_fake_drawio(directory, name="fake-drawio", payload_hex=PNG_HEX, exit_code=0):
    path = Path(directory) / name
    path.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, sys\n"
        f"payload = bytes.fromhex({payload_hex!r})\n"
        "if '--output' in sys.argv:\n"
        "    pathlib.Path(sys.argv[sys.argv.index('--output') + 1]).write_bytes(payload)\n"
        f"raise SystemExit({exit_code})\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


class ExportSmokeTests(unittest.TestCase):
    def test_explicit_binary_exports_valid_png_and_overrides_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.drawio"
            output = Path(temp) / "output.png"
            source.write_text(MINIMAL_DRAWIO, encoding="utf-8")
            executable = make_fake_drawio(temp)
            env = os.environ.copy()
            env["DRAWIO_CLI"] = str(Path(temp) / "missing")
            proc = run_script(
                "export_smoke.py",
                source,
                "--drawio",
                executable,
                "--output",
                output,
                "--json",
                env=env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            report = json.loads(proc.stdout)
            self.assertEqual(report["summary"]["status"], "passed")
            self.assertEqual(report["discovery"], "argument")
            self.assertEqual(output.read_bytes().hex(), PNG_HEX)

    def test_environment_binary_is_supported(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.drawio"
            source.write_text(MINIMAL_DRAWIO, encoding="utf-8")
            executable = make_fake_drawio(temp)
            env = os.environ.copy()
            env["DRAWIO_CLI"] = str(executable)
            proc = run_script("export_smoke.py", source, "--json", env=env)
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertEqual(json.loads(proc.stdout)["discovery"], "environment")

    def test_path_discovery_is_supported(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.drawio"
            source.write_text(MINIMAL_DRAWIO, encoding="utf-8")
            make_fake_drawio(temp, name="drawio")
            env = os.environ.copy()
            env.pop("DRAWIO_CLI", None)
            env["PATH"] = temp
            proc = run_script("export_smoke.py", source, "--json", env=env)
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertEqual(json.loads(proc.stdout)["discovery"], "path")

    def test_missing_configured_binary_is_unavailable_not_passed(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.drawio"
            source.write_text(MINIMAL_DRAWIO, encoding="utf-8")
            proc = run_script(
                "export_smoke.py",
                source,
                "--drawio",
                Path(temp) / "missing",
                "--json",
            )
            self.assertEqual(proc.returncode, 2, proc.stderr + proc.stdout)
            report = json.loads(proc.stdout)
            self.assertEqual(report["summary"]["status"], "unavailable")
            self.assertEqual(report["findings"][0]["code"], "export.cli.unavailable")

    def test_truncated_png_reports_iend_integrity_error(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.drawio"
            source.write_text(MINIMAL_DRAWIO, encoding="utf-8")
            executable = make_fake_drawio(temp, payload_hex="89504e470d0a1a0a")
            proc = run_script("export_smoke.py", source, "--drawio", executable, "--json")
            self.assertEqual(proc.returncode, 1, proc.stderr + proc.stdout)
            codes = {item["code"] for item in json.loads(proc.stdout)["findings"]}
            self.assertIn("export.png.iend", codes)

    def test_non_png_output_reports_signature_error(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.drawio"
            source.write_text(MINIMAL_DRAWIO, encoding="utf-8")
            executable = make_fake_drawio(temp, payload_hex="6e6f742d612d706e67")
            proc = run_script("export_smoke.py", source, "--drawio", executable, "--json")
            self.assertEqual(proc.returncode, 1, proc.stderr + proc.stdout)
            codes = {item["code"] for item in json.loads(proc.stdout)["findings"]}
            self.assertIn("export.png.signature", codes)

    def test_cli_failure_is_distinct_from_png_integrity(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.drawio"
            source.write_text(MINIMAL_DRAWIO, encoding="utf-8")
            executable = make_fake_drawio(temp, exit_code=7)
            proc = run_script("export_smoke.py", source, "--drawio", executable, "--json")
            self.assertEqual(proc.returncode, 1, proc.stderr + proc.stdout)
            report = json.loads(proc.stdout)
            self.assertEqual(report["findings"][0]["code"], "export.command.failed")


class DeterminismTests(unittest.TestCase):
    def test_roadmap_is_generated_twice_byte_identically(self):
        source = FIXTURES / "roadmap" / "basic.yaml"
        proc = run_script("verify_determinism.py", "roadmap", source, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        report = json.loads(proc.stdout)
        self.assertEqual(report["summary"]["status"], "passed")
        self.assertGreater(report["bytes"], 0)

    def test_gitflow_uses_builtin_routing_and_is_byte_identical(self):
        source = FIXTURES / "gitflow" / "classic.json"
        proc = run_script("verify_determinism.py", "gitflow", source, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        report = json.loads(proc.stdout)
        self.assertEqual(report["route"], "builtin")
        self.assertEqual(report["summary"]["status"], "passed")

    def test_first_difference_reports_byte_and_text_position(self):
        module = load_script("verify_determinism")
        detail = module.first_difference(b"one\ntwo\n", b"one\ntXo\n")
        self.assertEqual(detail["offset"], 5)
        self.assertEqual(detail["line"], 2)
        self.assertEqual(detail["column"], 2)


class SelfCheckTests(unittest.TestCase):
    def test_default_self_check_is_local_and_runs_minimal_pipelines(self):
        proc = run_script("self_check.py", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        report = json.loads(proc.stdout)
        self.assertEqual(report["summary"]["status"], "passed")
        by_name = {item["name"]: item for item in report["checks"]}
        self.assertEqual(by_name["registry"]["status"], "skipped")
        self.assertEqual(by_name["source:roadmap"]["status"], "passed")
        self.assertEqual(by_name["artifact:roadmap"]["status"], "passed")
        self.assertEqual(by_name["source:gitflow"]["status"], "passed")
        self.assertEqual(by_name["artifact:gitflow"]["status"], "passed")

    def test_supported_version_boundaries_are_explicit(self):
        module = load_script("self_check")
        self.assertTrue(module.DEPENDENCIES["PyYAML"]["supported"](module.version_parts("6.0.3")))
        self.assertFalse(module.DEPENDENCIES["PyYAML"]["supported"](module.version_parts("7.0.0")))
        self.assertTrue(module.DEPENDENCIES["jsonschema"]["supported"](module.version_parts("4.18.0")))
        self.assertFalse(module.DEPENDENCIES["jsonschema"]["supported"](module.version_parts("4.17.3")))

    def test_registry_check_uses_temporary_download_not_install_dry_run(self):
        module = load_script("self_check")
        completed = subprocess.CompletedProcess([], 0, "resolved", "")
        with mock.patch.object(module.subprocess, "run", return_value=completed) as execute:
            records = module.registry_checks()
        self.assertTrue(records)
        self.assertTrue(all(item["status"] == "passed" for item in records))
        for call in execute.call_args_list:
            command = call.args[0]
            self.assertIn("download", command)
            self.assertIn("--dest", command)
            self.assertNotIn("install", command)
            self.assertNotIn("--dry-run", command)


if __name__ == "__main__":
    unittest.main()
