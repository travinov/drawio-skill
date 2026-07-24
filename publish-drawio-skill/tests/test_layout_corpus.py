import copy
import json
import stat
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
FIXTURES = ROOT / "tests" / "fixtures" / "layout"
CORPUS = FIXTURES / "corpus"
sys.path.insert(0, str(SCRIPTS))

import diagram_orchestrator as orchestrator
import diagram_supervisor as supervisor
import layout_backend
import lifecycle_host_v2 as lifecycle_v2
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


def pipeline_summary(request, attempt, report):
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


VOLATILE_TIMESTAMP_FIELDS = frozenset({
    "captured_at",
    "created_at",
    "finished_at",
    "started_at",
    "timestamp",
    "updated_at",
})
VOLATILE_IDENTIFIER_FIELDS = frozenset({
    "bundle_id",
    "event_id",
    "receipt_id",
    "snapshot_id",
    "transaction_id",
})
VOLATILE_PREDECESSOR_FIELDS = frozenset({
    "previous_event_sha256",
    "previous_snapshot_sha256",
})
RECEIPT_ARTIFACT_NAMES = (
    "validation_receipt",
    "validation_receipt_legacy",
)


def _walk_volatile_identifiers(value, aliases, counters):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in VOLATILE_IDENTIFIER_FIELDS and isinstance(item, str):
                if item not in aliases:
                    counters[key] = counters.get(key, 0) + 1
                    aliases[item] = f"<{key}:{counters[key]}>"
            else:
                _walk_volatile_identifiers(item, aliases, counters)
    elif isinstance(value, list):
        for item in value:
            _walk_volatile_identifiers(item, aliases, counters)


def _volatile_identifier_aliases(trace):
    aliases, counters = {}, {}
    _walk_volatile_identifiers(trace["events"], aliases, counters)
    for path in sorted(trace["snapshot_payloads"]):
        _walk_volatile_identifiers(
            trace["snapshot_payloads"][path]["document"], aliases, counters,
        )
    for name in sorted(trace["receipt_payloads"]):
        _walk_volatile_identifiers(
            trace["receipt_payloads"][name]["document"], aliases, counters,
        )
    return aliases


def _normalize_durable_value(
    value, *, workspace, identifier_aliases, content_hash_replacements,
):
    if isinstance(value, dict):
        normalized = {}
        for key, item in value.items():
            if key in VOLATILE_TIMESTAMP_FIELDS:
                normalized[key] = f"<volatile:{key}>"
            elif key in VOLATILE_IDENTIFIER_FIELDS:
                normalized[key] = (
                    identifier_aliases.get(item, f"<volatile:{key}>")
                    if isinstance(item, str) else item
                )
            elif key in VOLATILE_PREDECESSOR_FIELDS:
                normalized[key] = f"<volatile:{key}>"
            else:
                normalized[key] = _normalize_durable_value(
                    item,
                    workspace=workspace,
                    identifier_aliases=identifier_aliases,
                    content_hash_replacements=content_hash_replacements,
                )
        return normalized
    if isinstance(value, list):
        return [
            _normalize_durable_value(
                item,
                workspace=workspace,
                identifier_aliases=identifier_aliases,
                content_hash_replacements=content_hash_replacements,
            )
            for item in value
        ]
    if isinstance(value, str):
        if value in content_hash_replacements:
            return content_hash_replacements[value]
        if value == workspace:
            return "<workspace>"
        if value.startswith(workspace + "/"):
            return "<workspace>/" + value[len(workspace) + 1:]
    return value


def _snapshot_hash_targets(trace):
    targets = {}
    for event in trace["events"]:
        for descriptor in event["snapshots"]:
            path = descriptor["path"]
            for field in ("canonical_sha256", "sha256"):
                targets[descriptor[field]] = path
    return targets


def _snapshot_dependencies(value, snapshot_targets):
    dependencies = set()
    if isinstance(value, dict):
        for item in value.values():
            dependencies.update(_snapshot_dependencies(item, snapshot_targets))
    elif isinstance(value, list):
        for item in value:
            dependencies.update(_snapshot_dependencies(item, snapshot_targets))
    elif isinstance(value, str) and value in snapshot_targets:
        dependencies.add(snapshot_targets[value])
    return dependencies


def normalize_durable_layout_trace(trace):
    """Normalize actual ledger, snapshot, and receipt payloads for comparison."""
    workspace = trace["workspace"]
    identifier_aliases = _volatile_identifier_aliases(trace)
    snapshot_targets = _snapshot_hash_targets(trace)
    snapshot_dependencies = {
        path: _snapshot_dependencies(payload["document"], snapshot_targets)
        for path, payload in trace["snapshot_payloads"].items()
    }
    snapshot_fingerprints = {}
    pending = set(trace["snapshot_payloads"])
    while pending:
        ready = sorted(
            path for path in pending
            if snapshot_dependencies[path] <= snapshot_fingerprints.keys()
        )
        if not ready:
            raise AssertionError(
                f"cyclic normalized snapshot references: {sorted(pending)}",
            )
        for path in ready:
            known_hash_replacements = {
                digest: snapshot_fingerprints[target]
                for digest, target in snapshot_targets.items()
                if target in snapshot_fingerprints
            }
            document = _normalize_durable_value(
                trace["snapshot_payloads"][path]["document"],
                workspace=workspace,
                identifier_aliases=identifier_aliases,
                content_hash_replacements=known_hash_replacements,
            )
            snapshot_fingerprints[path] = canonical_json_sha256(document)
            pending.remove(path)
    snapshot_hash_replacements = {
        digest: snapshot_fingerprints[path]
        for digest, path in snapshot_targets.items()
    }

    normalized_receipt_documents = {}
    receipt_fingerprints = {}
    for name, payload in trace["receipt_payloads"].items():
        document = _normalize_durable_value(
            payload["document"],
            workspace=workspace,
            identifier_aliases=identifier_aliases,
            content_hash_replacements=snapshot_hash_replacements,
        )
        normalized_receipt_documents[name] = document
        receipt_fingerprints[name] = canonical_json_sha256(document)
    receipt_hash_replacements = {
        payload["descriptor"]["sha256"]: receipt_fingerprints[name]
        for name, payload in trace["receipt_payloads"].items()
    }
    all_content_hash_replacements = {
        **snapshot_hash_replacements,
        **receipt_hash_replacements,
    }

    normalized_snapshot_payloads = {}
    for path, payload in trace["snapshot_payloads"].items():
        normalized_snapshot_payloads[path] = {
            "document": _normalize_durable_value(
                payload["document"],
                workspace=workspace,
                identifier_aliases=identifier_aliases,
                content_hash_replacements=all_content_hash_replacements,
            ),
            "normalized_content_sha256": snapshot_fingerprints[path],
        }

    normalized_receipt_payloads = {}
    for name, payload in trace["receipt_payloads"].items():
        normalized_receipt_payloads[name] = {
            "descriptor": _normalize_durable_value(
                payload["descriptor"],
                workspace=workspace,
                identifier_aliases=identifier_aliases,
                content_hash_replacements=all_content_hash_replacements,
            ),
            "document": normalized_receipt_documents[name],
            "normalized_content_sha256": receipt_fingerprints[name],
        }

    events = _normalize_durable_value(
        trace["events"],
        workspace=workspace,
        identifier_aliases=identifier_aliases,
        content_hash_replacements=all_content_hash_replacements,
    )
    for event in events:
        for descriptor in event["snapshots"]:
            path = descriptor["path"]
            descriptor["document"] = copy.deepcopy(
                normalized_snapshot_payloads[path]["document"],
            )
            descriptor["normalized_content_sha256"] = snapshot_fingerprints[path]
    return {
        "events": events,
        "event_order": [event["event_type"] for event in events],
        "snapshot_order": [
            [snapshot["schema_kind"] for snapshot in event["snapshots"]]
            for event in events
        ],
        "snapshot_payloads": normalized_snapshot_payloads,
        "receipt_payloads": normalized_receipt_payloads,
        "layout_evidence": _normalize_durable_value(
            trace["layout_evidence"],
            workspace=workspace,
            identifier_aliases=identifier_aliases,
            content_hash_replacements=all_content_hash_replacements,
        ),
    }


def durable_layout_traces_equal(first, second):
    return (
        normalize_durable_layout_trace(first)
        == normalize_durable_layout_trace(second)
    )


def run_durable_layout_trace(name):
    fixture = load_fixture(name)
    plan = semantic_plan(name, fixture)
    run_id = "corpus-" + name
    with tempfile.TemporaryDirectory(prefix="layout-corpus-trace-") as temporary:
        workspace = Path(temporary) / "workspace"
        run_dir = workspace / ".diagram-runs" / run_id
        target = workspace / "requested.drawio"
        workspace.mkdir()
        lifecycle_v2.initialize(
            run_dir=run_dir,
            workspace=workspace,
            target=target,
            run_id=run_id,
            mode="create",
            request=f"deterministic corpus {name}",
            extension_root=ROOT,
        )
        (run_dir / ".run-id").write_text(run_id + "\n", encoding="utf-8")
        plan_path = run_dir / "semantic-plan.v2.json"
        supervisor.write_json(plan_path, plan)
        workflow, _ = lifecycle_v2.latest_document(run_dir, "workflow")
        workflow.update({
            "target": str(target),
            "semantic_plan_v2": {
                "path": str(plan_path),
                "sha256": supervisor.sha256_file(plan_path),
            },
            "layout_attempt_keys": [],
            "layout_attempts": [],
            "iteration": 0,
        })
        orchestrator.write_workflow(run_dir, workflow)
        attempt = orchestrator.execute_layout_attempt(
            workflow,
            plan,
            run_dir=run_dir,
            adapter_input=SimpleNamespace(options={"backend": "python"}),
            mode="create",
            scope=None,
            strategy=("python-fallback", {}),
            timeout=10,
        )
        workflow["layout_attempts"].append(attempt)
        orchestrator.write_workflow(run_dir, workflow)
        replayed = lifecycle_v2.replay(run_dir)
        events = [
            copy.deepcopy(record["event"])
            for record in replayed["events"]
        ]
        snapshot_payloads = {}
        receipt_payloads = {}
        for event in events:
            for descriptor in event["snapshots"]:
                path = descriptor["path"]
                snapshot_payloads[path] = {
                    "document": json.loads(
                        (run_dir / path).read_text(encoding="utf-8"),
                    ),
                }
            artifact_snapshots = (
                event.get("payload", {}).get("artifact_snapshots", {})
            )
            for name in RECEIPT_ARTIFACT_NAMES:
                descriptor = artifact_snapshots.get(name)
                if descriptor is None:
                    continue
                path = descriptor["path"]
                receipt_payloads[name] = {
                    "descriptor": copy.deepcopy(descriptor),
                    "document": json.loads(
                        (run_dir / path).read_text(encoding="utf-8"),
                    ),
                }
        return {
            "events": events,
            "snapshot_payloads": snapshot_payloads,
            "receipt_payloads": receipt_payloads,
            "layout_evidence": orchestrator._trace_layout_evidence(
                run_dir, workflow, replayed,
            ),
            "workspace": str(workspace.resolve()),
        }


def render_bpmn_lane_geometry():
    fixture = load_fixture("bpmn-lanes")
    plan, request = create_request("bpmn-lanes", fixture)
    attempt = layout_backend.run_layout(
        request, config={"layout_backend": "python"},
    )
    with tempfile.TemporaryDirectory(prefix="layout-corpus-bpmn-") as temporary:
        artifact = Path(temporary) / "bpmn.drawio"
        render_layout(plan, attempt.result, artifact)
        cells = ET.parse(artifact).getroot().findall(".//mxCell")
        geometry = {}
        for cell in cells:
            cell_id = cell.get("id")
            if cell_id not in {"sales", "ops", "receive", "fulfil"}:
                continue
            value = cell.find("mxGeometry")
            geometry[cell_id] = {
                "parent": cell.get("parent"),
                "style": cell.get("style", ""),
                "x": float(value.get("x", 0)),
                "y": float(value.get("y", 0)),
                "width": float(value.get("width", 0)),
                "height": float(value.get("height", 0)),
            }
        return geometry


def publish_strict_failed_candidate_best_effort():
    run_id = "corpus-strict-failure-best-effort"
    with tempfile.TemporaryDirectory(prefix="layout-corpus-best-effort-") as temporary:
        workspace = Path(temporary) / "workspace"
        run_dir = workspace / ".diagram-runs" / run_id
        requested = workspace / "requested.drawio"
        published = workspace / "requested.best-effort.drawio"
        workspace.mkdir()
        lifecycle_v2.initialize(
            run_dir=run_dir,
            workspace=workspace,
            target=requested,
            run_id=run_id,
            mode="create",
            request="retain the strict-failing candidate as safe best effort",
            extension_root=ROOT,
        )
        (run_dir / ".run-id").write_text(run_id + "\n", encoding="utf-8")
        candidate = run_dir / "candidate.drawio"
        candidate.write_bytes(
            (ROOT / "tests" / "fixtures" / "artifact" / "readability_routes.drawio")
            .read_bytes()
        )
        legacy = supervisor.run_validation(
            candidate, run_dir, attempt_id="strict-candidate",
        )
        report_path = (
            run_dir / "attempts" / "strict-candidate" / "validation-report.json"
        )
        legacy_receipt_path = (
            run_dir / "attempts" / "strict-candidate" / "validation-receipt.json"
        )
        receipt, receipt_path = lifecycle_v2.mirror_validation_receipt(
            run_dir, legacy_receipt_path=legacy_receipt_path,
        )
        lifecycle_v2.transition(
            run_dir,
            "final_review",
            accepted_artifact=lifecycle_v2.make_file_descriptor(
                candidate, root=run_dir,
            ),
            validation_report=lifecycle_v2.make_file_descriptor(
                report_path, root=run_dir,
            ),
            validation_receipt=lifecycle_v2.make_file_descriptor(
                receipt_path, root=run_dir,
            ),
        )
        classification = lifecycle_v2.verify_best_effort_candidate(
            run_dir,
            artifact=candidate,
            report=report_path,
            receipt=receipt_path,
            require_accepted_binding=True,
        )
        unresolved = [
            {"source": "validator", "finding": finding}
            for finding in classification["findings"]
        ]
        publication = lifecycle_v2.publish_transaction(
            run_dir,
            accepted_artifact=candidate,
            validation_report=report_path,
            validation_receipt=receipt_path,
            unresolved_findings=unresolved,
            decision="best_effort",
            target_override=published,
        )
        report = json.loads(report_path.read_text(encoding="utf-8"))
        return {
            "strict_passed": classification["strict_passed"],
            "safe": classification["safe"],
            "publication_status": publication["status"],
            "candidate_sha256": supervisor.sha256_file(candidate),
            "report_artifact_sha256": report["artifact_sha256"],
            "receipt_candidate_sha256": receipt["bindings"]["candidate_sha256"],
            "published_sha256": supervisor.sha256_file(published),
            "requested_target_exists": requested.exists(),
            "validation_result": legacy["result"],
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
            self.assertEqual(pipeline_summary(request_one, first, first_report), pipeline_summary(request_two, second, second_report), name)
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

    def test_real_durable_trace_snapshots_and_order_are_deterministic(self):
        first = run_durable_layout_trace("linear-process")
        second = run_durable_layout_trace("linear-process")
        self.assertTrue(durable_layout_traces_equal(first, second))
        normalized = normalize_durable_layout_trace(first)
        self.assertEqual(
            normalized["event_order"],
            ["run_created", "tool_attempt", "tool_attempt", "candidate_accepted"],
        )
        self.assertEqual(
            normalized["snapshot_order"][0],
            ["source-bundle", "implementation-snapshot", "run-state", "workflow"],
        )
        self.assertEqual(
            set(normalized["receipt_payloads"]),
            {"validation_receipt", "validation_receipt_legacy"},
        )
        for receipt in normalized["receipt_payloads"].values():
            self.assertEqual(
                receipt["descriptor"]["sha256"],
                receipt["normalized_content_sha256"],
            )
            self.assertEqual(
                canonical_json_sha256(receipt["document"]),
                receipt["normalized_content_sha256"],
            )
            self.assertEqual(receipt["document"]["result"], "passed")
        for descriptor in normalized["events"][0]["snapshots"]:
            self.assertEqual(
                descriptor["canonical_sha256"],
                descriptor["normalized_content_sha256"],
            )
            self.assertEqual(
                canonical_json_sha256(descriptor["document"]),
                descriptor["normalized_content_sha256"],
            )
        self.assertTrue(normalized["layout_evidence"]["valid"])

    def test_durable_trace_detects_same_length_stable_receipt_change(self):
        original = run_durable_layout_trace("linear-process")
        changed = copy.deepcopy(original)
        receipt = changed["receipt_payloads"]["validation_receipt"]["document"]
        before = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
        self.assertEqual(receipt["result"], "passed")
        receipt["result"] = "failed"
        after = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
        self.assertEqual(len(before), len(after))
        self.assertFalse(durable_layout_traces_equal(original, changed))

    def test_bpmn_lanes_bind_children_to_containing_lane_geometry(self):
        lane_geometry = render_bpmn_lane_geometry()
        self.assertEqual(lane_geometry["receive"]["parent"], "sales")
        self.assertEqual(lane_geometry["fulfil"]["parent"], "ops")
        for child_id in ("receive", "fulfil"):
            child = lane_geometry[child_id]
            lane = lane_geometry[child["parent"]]
            self.assertIn("swimlane", lane["style"])
            self.assertGreaterEqual(child["x"], 0)
            self.assertGreaterEqual(child["y"], 0)
            self.assertLessEqual(child["x"] + child["width"], lane["width"])
            self.assertLessEqual(child["y"] + child["height"], lane["height"])

    def test_strict_failure_keeps_a_best_effort_artifact(self):
        proof = publish_strict_failed_candidate_best_effort()
        self.assertFalse(proof["strict_passed"])
        self.assertTrue(proof["safe"])
        self.assertEqual(proof["publication_status"], "committed")
        self.assertEqual(proof["candidate_sha256"], proof["report_artifact_sha256"])
        self.assertEqual(proof["candidate_sha256"], proof["receipt_candidate_sha256"])
        self.assertEqual(proof["candidate_sha256"], proof["published_sha256"])
        self.assertFalse(proof["requested_target_exists"])
        self.assertEqual(proof["validation_result"], "failed")


if __name__ == "__main__":
    unittest.main()
