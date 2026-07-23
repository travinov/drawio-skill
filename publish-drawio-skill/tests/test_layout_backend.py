import copy
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

log = os.environ.get("FAKE_NODE_LOG")
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(os.path.basename(sys.argv[0]) + " " + " ".join(sys.argv[1:]) + "\n")
mode = os.environ.get("FAKE_NODE_MODE", "success")
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
if mode == "nonzero":
    print("synthetic elk failure", file=sys.stderr)
    raise SystemExit(2)
if mode == "invalid-json":
    print("not json")
    raise SystemExit(0)
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
"""


class LayoutBackendTests(unittest.TestCase):
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

    def _environment(self, **values):
        result = dict(os.environ)
        result.update({key: str(value) for key, value in values.items()})
        return result

    def test_configured_absolute_node_wins_after_version_and_bridge_probe(self):
        path_node = self._executable("node", FAKE_NODE)
        log = self.root / "node.log"
        environ = self._environment(
            PATH=str(self.root),
            FAKE_NODE_LOG=log,
            FAKE_NODE_MODE="success",
        )
        resolved = layout_backend.resolve_node({"node_bin": str(self.node)}, environ=environ)
        self.assertEqual(resolved, self.node.resolve())
        calls = log.read_text(encoding="utf-8")
        self.assertIn("configured-node --version", calls)
        self.assertIn("configured-node ", calls)
        self.assertNotIn(f"\n{path_node.name} --version\n", f"\n{calls}")

    def test_path_node_requires_a_valid_version_and_bridge_probe(self):
        self._executable("node", FAKE_NODE)
        good = self._environment(PATH=str(self.root), FAKE_NODE_MODE="success")
        bad_version = self._environment(PATH=str(self.root), FAKE_NODE_MODE="bad-version")
        bad_probe = self._environment(PATH=str(self.root), FAKE_NODE_MODE="bad-probe")
        self.assertEqual(layout_backend.resolve_node({"node_bin": None}, environ=good), (self.root / "node").resolve())
        self.assertIsNone(layout_backend.resolve_node({"node_bin": None}, environ=bad_version))
        self.assertIsNone(layout_backend.resolve_node({"node_bin": None}, environ=bad_probe))

    def test_run_elk_accepts_only_strict_request_bound_json_and_records_proof(self):
        request = layout_request()
        with mock.patch.dict(os.environ, {"FAKE_NODE_MODE": "success"}, clear=False):
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
            with self.subTest(mode=mode), mock.patch.dict(
                os.environ,
                {"FAKE_NODE_MODE": mode, "PATH": str(self.root)},
                clear=False,
            ):
                attempt = layout_backend.run_layout(
                    request,
                    config={"node_bin": str(self.node), "layout_backend": "auto", "layout_timeout_seconds": 0.1},
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
        trap = """#!/bin/sh
echo "$0 $@" >> "$FORBIDDEN_LOG"
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
                "FAKE_NODE_MODE": "success",
                "FORBIDDEN_LOG": str(marker),
            },
            clear=False,
        ):
            attempt = layout_backend.run_layout(request, config={"layout_backend": "auto", "node_bin": None})
        self.assertEqual(attempt.result["backend"], "elk-layered-0.11.1")
        self.assertEqual(attempt.evidence["backend_requested"], "auto")
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
