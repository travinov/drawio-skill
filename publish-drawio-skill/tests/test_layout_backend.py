import hashlib
import json
import os
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import layout_backend
import layout_contracts
from lifecycle_contracts import canonical_json_sha256


SHA = "a" * 64


def layout_request(*, backend="auto", strategy="layered"):
    nodes = [
        {"node_id": "start", "x": 0, "y": 0, "width": 100, "height": 60, "locked": False},
        {"node_id": "end", "x": 0, "y": 0, "width": 100, "height": 60, "locked": False},
    ]
    edges = [{
        "edge_id": "edge-1",
        "source": "start",
        "target": "end",
        "edge_class": "main",
        "source_port": "south",
        "target_port": "north",
        "locked": False,
        "label_size": {"width": 40, "height": 20},
    }]
    return {
        "schema_version": 1,
        "request_id": "request-layout-backend",
        "run_id": "run-layout-backend",
        "semantic_plan_sha256": SHA,
        "diagram_type": "flowchart",
        "direction": "TB",
        "mode": "create",
        "backend": backend,
        "strategy": strategy,
        "quality_profile_version": 2,
        "pages": [{"page_id": "page-a", "name": "A", "nodes": nodes, "edges": edges}],
        "scope": {
            "page_ids": ["page-a"],
            "node_refs": [{"page_id": "page-a", "cell_id": item["node_id"]} for item in nodes],
            "edge_refs": [{"page_id": "page-a", "cell_id": "edge-1"}],
            "movable_node_refs": [{"page_id": "page-a", "cell_id": item["node_id"]} for item in nodes],
            "reroutable_edge_refs": [{"page_id": "page-a", "cell_id": "edge-1"}],
        },
        "constraints": {"grid_size": 10, "node_separation": 40, "layer_separation": 80},
    }


def valid_elk_result(request):
    digest = canonical_json_sha256(request)
    return {
        "schema_version": 1,
        "result_id": "elk-" + digest[:16],
        "request_sha256": digest,
        "backend": "elk-layered-0.11.1",
        "pages": [{
            "page_id": "page-a",
            "name": "A",
            "nodes": [
                {"node_id": "start", "x": 20, "y": 20, "width": 100, "height": 60, "locked": False},
                {"node_id": "end", "x": 20, "y": 180, "width": 100, "height": 60, "locked": False},
            ],
            "edges": [{
                "edge_id": "edge-1",
                "source": "start",
                "target": "end",
                "edge_class": "main",
                "source_port": "south",
                "target_port": "north",
                "source_pin": 0.5,
                "target_pin": 0.5,
                "waypoints": [{"x": 70, "y": 80}, {"x": 70, "y": 180}],
                "label_bounds": {"x": 75, "y": 120, "width": 40, "height": 20},
            }],
            "channel_reservations": [{
                "edge_id": "edge-1",
                "start": {"x": 70, "y": 80},
                "end": {"x": 70, "y": 180},
            }],
        }],
        "metrics": {
            "crossings": 0,
            "overlaps": 0,
            "route_length": 100,
            "bend_count": 0,
            "shared_route_length": 0,
            "label_collisions": 0,
        },
    }


FAKE_NODE = r"""#!/usr/bin/env python3
import json
import os
import sys
import time

log = sys.argv[0] + ".log"
with open(log, "a", encoding="utf-8") as handle:
    handle.write(os.path.basename(sys.argv[0]) + " " + " ".join(sys.argv[1:]) + "\n")
mode = "success"
if sys.argv[1:] == ["--version"]:
    if mode == "bad-version":
        print("not-node")
        raise SystemExit(0)
    print("v22.16.0")
    raise SystemExit(0)
if len(sys.argv) >= 3 and sys.argv[-1] == "--probe":
    if mode == "bad-probe":
        print("{}")
        raise SystemExit(0)
    print(json.dumps({"bridge": "drawio-elk-runner", "elkjs_version": "0.11.1"}))
    raise SystemExit(0)
request = json.load(sys.stdin)
digest = request.pop("__request_sha256")
if mode == "timeout":
    time.sleep(2)
if mode == "timeout-child":
    import subprocess
    marker = sys.argv[0] + ".marker"
    subprocess.Popen([
        sys.executable, "-c",
        "import pathlib,time,sys; time.sleep(0.35); pathlib.Path(sys.argv[1]).write_text('leaked')",
        marker,
    ])
    time.sleep(2)
if mode == "nonzero":
    print("synthetic elk failure", file=sys.stderr)
    raise SystemExit(2)
if mode == "invalid-json":
    print("not json")
    raise SystemExit(0)
if mode == "oversized-stdout":
    sys.stdout.write("x" * 8192)
    sys.stdout.flush()
    time.sleep(2)
if mode == "oversized-stderr":
    sys.stderr.write("x" * 8192)
    sys.stderr.flush()
    time.sleep(2)
if mode == "environment-check" and os.environ.get("DRAWIO_TEST_SECRET"):
    print("parent environment leaked", file=sys.stderr)
result = {
    "schema_version": 1,
    "result_id": "elk-" + digest[:16],
    "request_sha256": digest,
    "backend": "elk-layered-0.11.1",
    "pages": [{
        "page_id": "page-a", "name": "A",
        "nodes": [
            {"node_id": "start", "x": 20, "y": 20, "width": 100, "height": 60, "locked": False},
            {"node_id": "end", "x": 20, "y": 180, "width": 100, "height": 60, "locked": False}
        ],
        "edges": [{
            "edge_id": "edge-1", "source": "start", "target": "end", "edge_class": "main",
            "source_port": "south", "target_port": "north", "source_pin": 0.5, "target_pin": 0.5,
            "waypoints": [{"x": 70, "y": 80}, {"x": 70, "y": 180}],
            "label_bounds": {"x": 75, "y": 120, "width": 40, "height": 20}
        }],
        "channel_reservations": [{
            "edge_id": "edge-1", "start": {"x": 70, "y": 80}, "end": {"x": 70, "y": 180}
        }]
    }],
    "metrics": {
        "crossings": 0, "overlaps": 0, "route_length": 100,
        "bend_count": 0, "shared_route_length": 0, "label_collisions": 0
    }
}
if mode == "nonfinite":
    result["pages"][0]["nodes"][0]["x"] = float("nan")
if mode == "missing-edge":
    result["pages"][0]["edges"] = []
if mode == "diagonal":
    result["pages"][0]["edges"][0]["waypoints"][1] = {"x": 90, "y": 180}
if mode == "wrong-request":
    result["request_sha256"] = "b" * 64
print(json.dumps(result, allow_nan=True, separators=(",", ":")))
if mode == "stderr-warning":
    print("unexpected warning", file=sys.stderr)
if mode == "stderr-whitespace":
    print("  ", file=sys.stderr)
"""


class LayoutBackendTests(unittest.TestCase):
    def test_effective_options_bind_strategy_port_and_shared_spacing(self):
        default = layout_request()
        separated = layout_request(strategy="elk-separated")
        default["strategy_options"] = {
            "spacing": 1.0,
            "port_separation": 1.0,
            "shared_penalty": 1.0,
        }
        separated["strategy_options"] = {
            "spacing": 1.35,
            "port_separation": 1.4,
            "shared_penalty": 1.6,
        }
        default_options = layout_backend.effective_options(default)
        separated_options = layout_backend.effective_options(separated)
        self.assertEqual(default_options["elk.spacing.portPort"], "10.0")
        self.assertEqual(separated_options["elk.spacing.portPort"], "14.0")
        self.assertEqual(separated_options["elk.spacing.edgeEdge"], "16.0")
        self.assertNotEqual(
            layout_backend.attempt_key(default),
            layout_backend.attempt_key(separated),
        )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.node = self._executable("configured-node", FAKE_NODE)

    def tearDown(self):
        self.temp.cleanup()

    def _executable(self, name, content):
        path = self.root / name
        source = textwrap.dedent(content)
        if source.startswith("#!/usr/bin/env python3"):
            source = f"#!{sys.executable}" + source[len("#!/usr/bin/env python3"):]
        path.write_text(source, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def _node_for_mode(self, mode, *, name=None):
        source = FAKE_NODE.replace('mode = "success"', f'mode = {mode!r}')
        return self._executable(name or f"node-{mode}", source)

    def _environment(self, **values):
        result = dict(os.environ)
        result.update({key: str(value) for key, value in values.items()})
        return result

    def test_configured_absolute_node_wins_after_version_and_bridge_probe(self):
        path_node = self._executable("node", FAKE_NODE)
        environ = self._environment(
            PATH=str(self.root),
        )
        resolved = layout_backend.resolve_node({"node_bin": str(self.node)}, environ=environ)
        self.assertEqual(resolved, self.node.resolve())
        calls = Path(str(self.node) + ".log").read_text(encoding="utf-8")
        self.assertIn("configured-node --version", calls)
        self.assertIn("configured-node ", calls)
        self.assertNotIn(f"\n{path_node.name} --version\n", f"\n{calls}")

    def test_path_node_requires_a_valid_version_and_bridge_probe(self):
        self._executable("node", FAKE_NODE)
        good = self._environment(PATH=str(self.root))
        self.assertEqual(layout_backend.resolve_node({"node_bin": None}, environ=good), (self.root / "node").resolve())
        self._node_for_mode("bad-version", name="node")
        self.assertIsNone(layout_backend.resolve_node({"node_bin": None}, environ=good))
        self._node_for_mode("bad-probe", name="node")
        self.assertIsNone(layout_backend.resolve_node({"node_bin": None}, environ=good))

    def test_run_elk_accepts_only_strict_request_bound_json_and_records_proof(self):
        request = layout_request()
        attempt = layout_backend.run_elk(request, node=self.node, timeout_seconds=1)
        self.assertEqual(attempt.result, valid_elk_result(request))
        self.assertEqual(layout_contracts.validate_layout_result(
            attempt.result, expected_request_sha256=canonical_json_sha256(request)
        ), [])
        self.assertEqual(attempt.evidence["node_executable"], str(self.node.resolve()))
        self.assertEqual(attempt.evidence["node_version"], "v22.16.0")
        self.assertEqual(attempt.evidence["elkjs_version"], "0.11.1")
        self.assertEqual(attempt.evidence["request_sha256"], canonical_json_sha256(request))
        self.assertEqual(attempt.evidence["schema_valid"], True)
        self.assertEqual(Path(attempt.evidence["stdout_path"]).read_text(encoding="utf-8").strip()[0], "{")
        self.assertEqual(Path(attempt.evidence["stderr_path"]).read_text(encoding="utf-8"), "")
        self.assertRegex(attempt.evidence["stdout_sha256"], r"^[a-f0-9]{64}$")
        self.assertRegex(attempt.evidence["stderr_sha256"], r"^[a-f0-9]{64}$")

    def test_auto_falls_back_to_python_for_every_untrusted_elk_failure(self):
        request = layout_request()
        expected_digest = canonical_json_sha256(request)
        for mode in ("timeout", "nonzero", "invalid-json", "nonfinite", "missing-edge", "diagonal", "wrong-request"):
            node = self._node_for_mode(mode)
            with self.subTest(mode=mode), mock.patch.dict(
                os.environ, {"PATH": str(self.root)}, clear=False
            ):
                attempt = layout_backend.run_layout(
                    request,
                    config={"node_bin": str(node), "layout_backend": "auto", "layout_timeout_seconds": 0.1},
                )
            self.assertEqual(attempt.result["backend"], "python-layered")
            self.assertEqual(attempt.result["request_sha256"], expected_digest)
            self.assertEqual(attempt.evidence["request_sha256"], expected_digest)
            self.assertEqual(attempt.evidence["fallback_backend"], "python-layered")
            self.assertTrue(attempt.evidence["fallback_reason"])

    def test_missing_node_auto_falls_back_but_explicit_elk_fails_closed(self):
        request = layout_request()
        with mock.patch.dict(os.environ, {"PATH": str(self.root / "missing")}, clear=False):
            automatic = layout_backend.run_layout(
                request, config={"node_bin": None, "layout_backend": "auto"}
            )
            with self.assertRaises(layout_backend.BackendUnavailableError):
                layout_backend.run_layout(
                    request, config={"node_bin": None, "layout_backend": "elk"}
                )
        self.assertEqual(automatic.result["backend"], "python-layered")
        self.assertEqual(automatic.evidence["fallback_reason"], "verified_node_unavailable")

    def test_duplicate_request_strategy_options_attempt_is_refused(self):
        request = layout_request(strategy="crossing-min")
        key = layout_backend.attempt_key(request)
        with self.assertRaises(layout_backend.DuplicateBackendAttemptError):
            layout_backend.run_layout(
                request,
                config={"layout_backend": "python"},
                attempted_keys=frozenset({key}),
            )

    def test_python_mode_and_sanitized_path_do_not_require_node(self):
        request = layout_request()
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}, clear=False):
            attempt = layout_backend.run_layout(request, config={"layout_backend": "python"})
        self.assertEqual(attempt.result["backend"], "python-layered")
        self.assertEqual(attempt.result["request_sha256"], canonical_json_sha256(request))

    def test_runtime_never_invokes_package_or_network_commands(self):
        marker = self.root / "forbidden.log"
        trap = f"""#!/bin/sh
echo "$0 $@" >> "{marker}"
exit 99
"""
        for name in ("npm", "npx", "curl"):
            self._executable(name, trap)
        self._executable("node", FAKE_NODE)
        request = layout_request()
        with mock.patch.dict(
            os.environ,
            {
                "PATH": str(self.root),
            },
            clear=False,
        ):
            attempt = layout_backend.run_layout(request, config={"layout_backend": "auto", "node_bin": None})
        self.assertEqual(attempt.result["backend"], "elk-layered-0.11.1")
        self.assertEqual(attempt.evidence["backend_requested"], "auto")
        self.assertFalse(marker.exists())

    def test_runtime_passes_only_the_environment_allowlist(self):
        request = layout_request()
        node = self._node_for_mode("environment-check")
        with mock.patch.dict(
            os.environ,
            {
                "DRAWIO_TEST_SECRET": "must-not-reach-node",
                "NODE_OPTIONS": "--require=/does/not/exist.js",
            },
            clear=False,
        ):
            attempt = layout_backend.run_layout(
                request,
                config={"layout_backend": "auto", "node_bin": str(node)},
            )
        self.assertEqual(attempt.result["backend"], "elk-layered-0.11.1")
        self.assertEqual(attempt.evidence["stderr_bytes_observed"], 0)

    def test_nonempty_stderr_is_rejected_but_whitespace_is_accepted(self):
        request = layout_request()
        warning_node = self._node_for_mode("stderr-warning")
        attempt = layout_backend.run_layout(
            request,
            config={"layout_backend": "auto", "node_bin": str(warning_node)},
        )
        self.assertEqual(attempt.result["backend"], "python-layered")
        self.assertEqual(attempt.evidence["fallback_reason"], "elk_stderr_nonempty")
        self.assertEqual(
            Path(attempt.evidence["stderr_path"]).read_text(encoding="utf-8").strip(),
            "unexpected warning",
        )
        with self.assertRaises(layout_backend.BackendExecutionError) as raised:
            layout_backend.run_layout(
                request,
                config={"layout_backend": "elk", "node_bin": str(warning_node)},
            )
        self.assertEqual(raised.exception.reason, "elk_stderr_nonempty")

        whitespace_node = self._node_for_mode("stderr-whitespace")
        accepted = layout_backend.run_layout(
            request,
            config={"layout_backend": "auto", "node_bin": str(whitespace_node)},
        )
        self.assertEqual(accepted.result["backend"], "elk-layered-0.11.1")

    def test_timeout_kills_the_process_group_and_reaps_spawned_helper(self):
        request = layout_request()
        node = self._node_for_mode("timeout-child")
        attempt = layout_backend.run_layout(
            request,
            config={
                "layout_backend": "auto",
                "node_bin": str(node),
                "layout_timeout_seconds": 0.1,
            },
        )
        self.assertEqual(attempt.evidence["fallback_reason"], "elk_timeout")
        self.assertTrue(attempt.evidence["process_group_isolated"])
        self.assertTrue(attempt.evidence["process_reaped"])
        import time
        time.sleep(0.5)
        self.assertFalse(Path(str(node) + ".marker").exists())

    def test_capture_limit_kills_oversized_stdout_and_stderr_with_truthful_evidence(self):
        request = layout_request()
        for mode, stream in (("oversized-stdout", "stdout"), ("oversized-stderr", "stderr")):
            node = self._node_for_mode(mode)
            with self.subTest(mode=mode):
                attempt = layout_backend.run_layout(
                    request,
                    config={
                        "layout_backend": "auto",
                        "node_bin": str(node),
                        "layout_timeout_seconds": 1,
                        "layout_capture_max_bytes": 1024,
                    },
                )
            self.assertEqual(
                attempt.evidence["fallback_reason"],
                f"elk_capture_limit_exceeded_{stream}",
            )
            self.assertEqual(attempt.evidence["capture_max_bytes"], 1024)
            self.assertTrue(attempt.evidence[f"{stream}_truncated"])
            path = Path(attempt.evidence[f"{stream}_path"])
            self.assertLessEqual(path.stat().st_size, 1024)
            self.assertEqual(
                hashlib.sha256(path.read_bytes()).hexdigest(),
                attempt.evidence[f"{stream}_sha256"],
            )
            self.assertGreater(attempt.evidence[f"{stream}_bytes_observed"], 1024)
            self.assertTrue(attempt.evidence["process_reaped"])


if __name__ == "__main__":
    unittest.main()
