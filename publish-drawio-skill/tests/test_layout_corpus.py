import copy
import json
import stat
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
FIXTURES = ROOT / "tests" / "fixtures" / "layout"
CORPUS = FIXTURES / "corpus"
sys.path.insert(0, str(SCRIPTS))

import diagram_orchestrator as orchestrator
import diagram_supervisor as supervisor
import layout_backend
import layout_model
import validate
from layout_renderer import render_layout
from lifecycle_contracts import canonical_json_sha256


SHA_A = "a" * 64
SHA_B = "b" * 64
CREATE_CASES = (
    "linear-process",
    "two-way-decision",
    "three-way-decision",
    "return-loop",
    "order-processing",
    "c4-services",
    "microservices",
    "er-dependency",
    "bpmn-lanes",
    "elk-failure-fallback",
    "strict-failure-best-effort",
)
IMPROVE_CASES = ("local-edge-improve", "local-node-move")
EXPECTED_CASES = frozenset((*CREATE_CASES, *IMPROVE_CASES))


def load_fixture(name):
    with (CORPUS / f"{name}.json").open(encoding="utf-8") as handle:
        return json.load(handle)


def semantic_plan(name, fixture):
    page_id = "page-" + name
    nodes = []
    for item in fixture["nodes"]:
        parent = item.get("parent")
        nodes.append({
            "stable_identity": {"page_id": page_id, "cell_id": item["id"]},
            "label": item["label"],
            "semantic_type": item.get("type", "process"),
            "parent": (
                {"page_id": page_id, "cell_id": parent} if parent else None
            ),
            "style_hint": None,
        })
    edges = []
    for item in fixture["edges"]:
        edges.append({
            "stable_identity": {"page_id": page_id, "cell_id": item["id"]},
            "label": item.get("label", ""),
            "source": {"page_id": page_id, "cell_id": item["source"]},
            "target": {"page_id": page_id, "cell_id": item["target"]},
            "relationship": item.get("relationship", "flow"),
            "parent": None,
            "style_hint": None,
        })
    return {
        "schema_version": 2,
        "role": "semantic_analyst",
        "status": "ok",
        "run_id": "corpus-" + name,
        "source_bundle_sha256": SHA_A,
        "baseline_semantic_digest": SHA_A,
        "result": {
            "mode": "create",
            "diagram_type": fixture["diagram_type"],
            "title": name,
            "direction": fixture["direction"],
            "pages": [{"page_id": page_id, "name": name, "nodes": nodes, "edges": edges}],
            "semantic_delta": {
                "schema_version": 2,
                "baseline_semantic_digest": SHA_A,
                "source_bundle_sha256": SHA_A,
                "operations": [],
            },
            "assumptions": [],
            "requires_human": False,
            "human_questions": [],
        },
    }


def create_request(name, fixture, *, backend="python", baseline=None, mode="create", scope=None):
    plan = semantic_plan(name, fixture)
    return plan, layout_model.build_layout_request(
        plan,
        run_id="corpus-" + name,
        semantic_plan_sha256=canonical_json_sha256(plan),
        mode=mode,
        backend=backend,
        strategy_id="layered",
        quality_profile_version=2,
        baseline=baseline,
        scope=scope,
    )


def report_for(path, *, strict=False):
    return validate.validate_tree(ET.parse(path), strict=strict)


def normalized_trace(request, attempt, report):
    return {
        "request_sha256": canonical_json_sha256(request),
        "result_sha256": canonical_json_sha256(attempt.result),
        "backend": attempt.result["backend"],
        "quality_vector": supervisor.quality_vector(report, profile_version=2),
        "findings": [
            (finding["severity"], finding["code"], finding["path"], tuple(finding.get("elements", [])))
            for finding in report["findings"]
        ],
    }


class LayoutCorpusTests(unittest.TestCase):
    def test_fixture_inventory_is_exact_and_small(self):
        actual = {path.stem for path in CORPUS.glob("*.json")}
        self.assertEqual(actual, EXPECTED_CASES)
        for path in CORPUS.glob("*.json"):
            self.assertLess(path.stat().st_size, 2_500, path.name)

    def _run_create_twice(self, name):
        fixture = load_fixture(name)
        plan_one, request_one = create_request(name, fixture)
        plan_two, request_two = create_request(name, fixture)
        first = layout_backend.run_layout(request_one, config={"layout_backend": "python"})
        second = layout_backend.run_layout(request_two, config={"layout_backend": "python"})
        with tempfile.TemporaryDirectory(prefix="layout-corpus-") as temporary:
            temp = Path(temporary)
            first_path, second_path = temp / "first.drawio", temp / "second.drawio"
            render_layout(plan_one, first.result, first_path)
            render_layout(plan_two, second.result, second_path)
            first_report, second_report = report_for(first_path), report_for(second_path)
            self.assertEqual(canonical_json_sha256(request_one), canonical_json_sha256(request_two), name)
            self.assertEqual(canonical_json_sha256(first.result), canonical_json_sha256(second.result), name)
            self.assertEqual(first_path.read_bytes(), second_path.read_bytes(), name)
            self.assertEqual(
                supervisor.quality_vector(first_report, profile_version=2),
                supervisor.quality_vector(second_report, profile_version=2),
                name,
            )
            self.assertEqual(normalized_trace(request_one, first, first_report), normalized_trace(request_two, second, second_report), name)
            self.assertEqual(first_report["summary"]["errors"], 0, first_report["findings"])

    def test_graph_create_corpus_is_deterministic_and_has_no_blocking_findings(self):
        for name in CREATE_CASES:
            with self.subTest(name=name):
                self._run_create_twice(name)

    def _local_candidate(self, name):
        fixture = load_fixture(name)
        plan, initial_request = create_request(name, fixture)
        initial = layout_backend.run_layout(initial_request, config={"layout_backend": "python"})
        with tempfile.TemporaryDirectory(prefix="layout-corpus-local-") as temporary:
            temp = Path(temporary)
            baseline_path = temp / "baseline.drawio"
            render_layout(plan, initial.result, baseline_path)
            baseline_spec = supervisor.make_spec(baseline_path)
            page_id = "page-" + name
            target_edge = fixture["target_edge"]
            scope = {
                "edge_refs": [{"page_id": page_id, "cell_id": target_edge}],
                "reroutable_edge_refs": [{"page_id": page_id, "cell_id": target_edge}],
            }
            movable = fixture.get("movable_node")
            if movable:
                scope["node_refs"] = [{"page_id": page_id, "cell_id": movable}]
                scope["movable_node_refs"] = [{"page_id": page_id, "cell_id": movable}]
            _, local_request = create_request(
                name, fixture, baseline=baseline_spec, mode="local_reflow", scope=scope,
            )
            local_attempt = layout_backend.run_layout(local_request, config={"layout_backend": "python"})
            candidate_path = temp / "candidate.drawio"
            render_layout(plan, local_attempt.result, candidate_path)
            before_digest, before_hashes = supervisor.artifact_invariants(baseline_path)
            after_digest, after_hashes = supervisor.artifact_invariants(candidate_path)
            preserved = orchestrator._verify_locked_cell_hashes(
                baseline_path, candidate_path, {page_id: fixture["locked_ids"]},
            )
            locked_hashes = {
                cell_id: (before_hashes[(page_id, cell_id)], after_hashes[(page_id, cell_id)])
                for cell_id in fixture["locked_ids"]
            }
            return before_digest, after_digest, preserved, locked_hashes

    def test_local_improve_preserves_semantics_and_untouched_hashes(self):
        for name in IMPROVE_CASES:
            with self.subTest(name=name):
                before_digest, after_digest, preserved, locked_hashes = self._local_candidate(name)
                self.assertEqual(before_digest, after_digest)
                self.assertTrue(preserved["valid"], preserved)
                for before_hash, after_hash in locked_hashes.values():
                    self.assertEqual(before_hash, after_hash)

    def test_shared_x_350_regression_is_reported_then_removed(self):
        old_path = FIXTURES / "shared-x-350.drawio"
        old_report = report_for(old_path)
        self.assertTrue(any("shared" in finding["code"] or "share" in finding["message"] for finding in old_report["findings"]), old_report["findings"])
        fixture = load_fixture("three-way-decision")
        plan, request = create_request("three-way-decision", fixture)
        attempt = layout_backend.run_layout(request, config={"layout_backend": "python"})
        with tempfile.TemporaryDirectory(prefix="layout-corpus-shared-") as temporary:
            candidate = Path(temporary) / "candidate.drawio"
            render_layout(plan, attempt.result, candidate)
            report = report_for(candidate)
        self.assertFalse(any("share" in finding["message"] for finding in report["findings"]), report["findings"])

    def test_forced_elk_failure_falls_back_to_python(self):
        fixture = load_fixture("elk-failure-fallback")
        _, request = create_request("elk-failure-fallback", fixture, backend="auto")
        with tempfile.TemporaryDirectory(prefix="layout-corpus-elk-") as temporary:
            fake_node = Path(temporary) / "node"
            fake_node.write_text(
                "#!" + sys.executable + "\nimport sys\n"
                "if sys.argv[-1:] == ['--version']: print('v22.16.0')\n"
                "elif sys.argv[-1:] == ['--probe']: print('{\\\"bridge\\\":\\\"drawio-elk-runner\\\",\\\"elkjs_version\\\":\\\"0.11.1\\\"}')\n"
                "else: raise SystemExit(2)\n",
                encoding="utf-8",
            )
            fake_node.chmod(fake_node.stat().st_mode | stat.S_IXUSR)
            attempt = layout_backend.run_layout(request, config={"layout_backend": "auto", "node_bin": str(fake_node)})
        self.assertEqual(attempt.result["backend"], "python-layered")
        self.assertEqual(attempt.evidence["fallback_reason"], fixture["expected_fallback"])

    def test_strict_failure_keeps_a_best_effort_artifact(self):
        strict_report = report_for(FIXTURES / "shared-x-350.drawio", strict=True)
        self.assertGreater(strict_report["summary"]["errors"], 0)
        fixture = load_fixture("strict-failure-best-effort")
        plan, request = create_request("strict-failure-best-effort", fixture)
        attempt = layout_backend.run_layout(request, config={"layout_backend": "python"})
        with tempfile.TemporaryDirectory(prefix="layout-corpus-best-effort-") as temporary:
            artifact = Path(temporary) / "best-effort.drawio"
            render_layout(plan, attempt.result, artifact)
            self.assertTrue(artifact.is_file())
            self.assertEqual(report_for(artifact)["summary"]["errors"], 0)


if __name__ == "__main__":
    unittest.main()
