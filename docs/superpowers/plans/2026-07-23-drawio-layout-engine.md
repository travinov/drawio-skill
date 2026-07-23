# Draw.io Deterministic Layout Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an offline, deterministic LayoutIR pipeline for `/drawio:create` and local `/drawio:improve`, using vendored ELK Layered with a mandatory Python fallback, stronger layout validation, bounded autonomous retries, and automatic safe best-effort publication.

**Architecture:** Keep the existing four-agent lifecycle as the control plane. Insert host-owned intake and layout contracts between `semantic-plan.v2` and draw.io XML generation. Backends consume one immutable `layout-request.v1` and return one validated `layout-result.v1`; models never own coordinates, ports, waypoints, validation, candidate comparison, or publication.

**Tech Stack:** Python 3 standard library, `jsonschema`, draw.io XML, Node.js 22, vendored `elkjs` 0.11.1, JSON Schema Draft 2020-12, `unittest`, existing lifecycle v2 evidence and release tooling.

## Global Constraints

- Keep `semantic-plan.v2` unchanged.
- Keep exactly four roles: Supervisor, Semantic Analyst, Repair, Reviewer.
- Do not discover or search for OpenSpec documents.
- Do not invoke npm, a package registry, or the network during installation or runtime.
- Preserve `sequence-local`, `roadmap-local`, and `git-flow-local` domain layout behavior.
- Preserve lifecycle v2, model isolation/proof, receipts, resume/recovery, no-clobber publication, and existing best-effort guarantees.
- Preserve the current generic renderer as explicit backend `legacy-generic-v2`; it is never the new automatic default.
- For improve, lock every out-of-scope cell and route. Full reflow requires explicit user intent.
- Existing runs without `quality_profile_version: 2` continue using the legacy comparison vector.
- Create a full `.diagram-runs/<run-id>` only after intake is complete.
- Every accepted or best-effort layout must have a validated contract, deterministic report, receipt, trace snapshots, and hash-bound publication record.
- Use small commits in the order below. Do not combine tasks or silently refactor unrelated modules.

## File Map

### New contracts and fixtures

- `publish-drawio-skill/data/diagram-intake.v1.schema.json`
- `publish-drawio-skill/data/diagram-intake-analysis.v1.schema.json`
- `publish-drawio-skill/data/layout-request.v1.schema.json`
- `publish-drawio-skill/data/layout-result.v1.schema.json`
- `publish-drawio-skill/data/layout-repair-intent.v1.schema.json`
- `publish-drawio-skill/tests/fixtures/layout/*.json`
- `publish-drawio-skill/tests/fixtures/layout/*.drawio`

### New deterministic host modules

- `publish-drawio-skill/scripts/diagram_intake.py`
- `publish-drawio-skill/scripts/layout_geometry.py`
- `publish-drawio-skill/scripts/layout_model.py`
- `publish-drawio-skill/scripts/layout_builtin.py`
- `publish-drawio-skill/scripts/layout_backend.py`
- `publish-drawio-skill/scripts/layout_renderer.py`
- `publish-drawio-skill/scripts/elk_runner.mjs`
- `publish-drawio-skill/vendor/elkjs/elk.bundled.js`
- `publish-drawio-skill/vendor/elkjs/LICENSE`
- `publish-drawio-skill/vendor/elkjs/NOTICE.json`

### New tests

- `publish-drawio-skill/tests/test_diagram_intake.py`
- `publish-drawio-skill/tests/test_layout_contracts.py`
- `publish-drawio-skill/tests/test_layout_geometry.py`
- `publish-drawio-skill/tests/test_layout_model.py`
- `publish-drawio-skill/tests/test_layout_builtin.py`
- `publish-drawio-skill/tests/test_layout_backend.py`
- `publish-drawio-skill/tests/test_layout_renderer.py`
- `publish-drawio-skill/tests/test_layout_corpus.py`
- `publish-drawio-skill/tests/test_command_ux.py`
- `publish-drawio-skill/tests/test_renderer_adapters.py`
- `publish-drawio-skill/tests/test_agent_runtime.py`
- `publish-drawio-skill/tests/test_agent_contracts.py`

### Existing integration files

- `publish-drawio-skill/scripts/validate.py`
- `publish-drawio-skill/scripts/diagram_supervisor.py`
- `publish-drawio-skill/scripts/command_ux.py`
- `publish-drawio-skill/scripts/renderer_adapters.py`
- `publish-drawio-skill/scripts/diagram_orchestrator.py`
- `publish-drawio-skill/scripts/agent_runtime.py`
- `publish-drawio-skill/scripts/lifecycle_host_v2.py`
- `publish-drawio-skill/scripts/evidence_v2.py`
- `publish-drawio-skill/scripts/implementation_snapshot_v2.py`
- `publish-drawio-skill/scripts/self_check.py`
- `publish-drawio-skill/agents/*.md`
- `publish-drawio-skill/commands/drawio/{create,improve,resume,trace}.md`
- `publish-drawio-skill/references/diagram-intake.md`
- `publish-drawio-skill/SKILL.md`
- `publish-drawio-skill/README.md`
- `publish-drawio-skill/config.example.json`
- `publish-drawio-skill/docs/drawio-agent-extension-corporate-test-commands.md`
- `release/skills.json`
- `scripts/gigacode/{install,verify}_drawio_agent_extension.sh`
- `tests/gigacode/test_extension_installers.py`
- `tests/test_release_skills.py`

---

## Task 1: Freeze Strict Intake and Layout Contracts

**Files:**

- Create: `publish-drawio-skill/data/diagram-intake.v1.schema.json`
- Create: `publish-drawio-skill/data/diagram-intake-analysis.v1.schema.json`
- Create: `publish-drawio-skill/data/layout-request.v1.schema.json`
- Create: `publish-drawio-skill/data/layout-result.v1.schema.json`
- Create: `publish-drawio-skill/data/layout-repair-intent.v1.schema.json`
- Create: `publish-drawio-skill/tests/test_layout_contracts.py`
- Modify: `publish-drawio-skill/tests/test_contracts.py`

- [ ] **Step 1: Write failing schema-presence and strictness tests**

Add `LayoutContractTests` that loads all five schemas through
`lifecycle_contracts.load_schema(kind, 1)`, compiles them with
`Draft202012Validator.check_schema`, validates one positive document for each
contract, and rejects:

```python
def test_layout_result_rejects_diagonal_route(self):
    value = valid_layout_result()
    value["pages"][0]["edges"][0]["waypoints"] = [
        {"x": 100, "y": 100},
        {"x": 140, "y": 130},
    ]
    diagnostics = lifecycle_contracts.validate_contract(value, "layout-result", 1)
    self.assertIn("waypoints", json.dumps(diagnostics))

def test_layout_request_rejects_unlocked_out_of_scope_node(self):
    value = valid_layout_request(mode="local_reflow")
    value["pages"][0]["nodes"][1]["locked"] = False
    value["scope"]["movable_nodes"] = []
    diagnostics = lifecycle_contracts.validate_contract(value, "layout-request", 1)
    self.assertTrue(diagnostics)
```

- [ ] **Step 2: Run the focused test and confirm it fails**

Run:

```bash
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_layout_contracts.py'
```

Expected: `contract.schema_missing` or file-not-found failures for the five new
schemas.

- [ ] **Step 3: Implement the five schemas**

Use `additionalProperties: false` at every contract-owned object. Define these
stable top-level bindings:

```text
diagram-intake.v1:
  schema_version, intake_id, mode, request_sha256, status,
  classification, questions, answers, assumptions, completeness

diagram-intake-analysis.v1:
  schema_version, role, status, result

layout-request.v1:
  schema_version, request_id, run_id, semantic_plan_sha256,
  diagram_type, direction, mode, backend, strategy,
  quality_profile_version, pages, scope, constraints

layout-result.v1:
  schema_version, result_id, request_sha256, backend,
  pages, metrics

layout-repair-intent.v1:
  schema_version, role, status, run_id, baseline_sha256,
  result
```

Use the exact enums:

```json
{
  "diagram_type": [
    "flowchart", "bpmn", "c4", "er", "dependency",
    "sequence", "roadmap", "git-flow", "generic"
  ],
  "layout_mode": ["create", "preserve", "local_reflow", "full_reflow"],
  "edge_class": ["main", "branch", "feedback", "self_loop"],
  "repair_action": [
    "reroute_edges", "move_adjacent_nodes", "expand_layer",
    "expand_component", "finish_best_effort"
  ]
}
```

Represent routes as `source_port`, `target_port`, and a waypoint array. Add a
JSON Schema custom keyword-free Manhattan constraint by representing every
segment as:

```json
{
  "required": ["orientation", "from", "to"],
  "properties": {
    "orientation": {"enum": ["H", "V"]},
    "from": {"$ref": "#/$defs/point"},
    "to": {"$ref": "#/$defs/point"}
  },
  "allOf": [
    {
      "if": {"properties": {"orientation": {"const": "H"}}},
      "then": {"properties": {"from": {"required": ["y"]}, "to": {"required": ["y"]}}}
    }
  ]
}
```

The Python contract test must additionally verify coordinate equality because
JSON Schema cannot compare sibling numeric values.

- [ ] **Step 4: Run schema and existing contract tests**

Run:

```bash
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_layout_contracts.py'
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_contracts.py'
```

Expected: both pass.

- [ ] **Step 5: Commit**

```bash
git add publish-drawio-skill/data publish-drawio-skill/tests/test_layout_contracts.py publish-drawio-skill/tests/test_contracts.py
git commit -m "feat(drawio): add strict layout contracts"
```

---

## Task 2: Add Shared Geometry Primitives and Validator Findings

**Files:**

- Create: `publish-drawio-skill/scripts/layout_geometry.py`
- Create: `publish-drawio-skill/tests/test_layout_geometry.py`
- Create: `publish-drawio-skill/tests/fixtures/layout/shared-trunk.drawio`
- Create: `publish-drawio-skill/tests/fixtures/layout/allowed-fanout.drawio`
- Create: `publish-drawio-skill/tests/fixtures/layout/intentional-bus.drawio`
- Create: `publish-drawio-skill/tests/fixtures/layout/label-collisions.drawio`
- Create: `publish-drawio-skill/tests/fixtures/layout/detour-bends.drawio`
- Create: `publish-drawio-skill/tests/fixtures/layout/feedback-intrusion.drawio`
- Create: `publish-drawio-skill/tests/fixtures/layout/extreme-aspect.drawio`
- Modify: `publish-drawio-skill/scripts/validate.py`
- Modify: `publish-drawio-skill/tests/test_validate.py`
- Modify: `publish-drawio-skill/tests/test_finding_fixtures.py`

- [ ] **Step 1: Write failing geometry unit tests**

Cover full/partial collinear overlap, endpoint-only contact, common-endpoint
fan-out, route/rectangle intersection, label rectangle collision, bend count,
Manhattan length, detour ratio, and stable segment normalization:

```python
def test_collinear_overlap_returns_shared_length(self):
    self.assertEqual(
        layout_geometry.collinear_overlap(
            ((0, 10), (100, 10)),
            ((40, 10), (140, 10)),
        ),
        60.0,
    )

def test_endpoint_touch_is_not_shared_segment(self):
    self.assertEqual(
        layout_geometry.collinear_overlap(
            ((0, 0), (20, 0)),
            ((20, 0), (40, 0)),
        ),
        0.0,
    )
```

- [ ] **Step 2: Run and confirm failure**

Run:

```bash
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_layout_geometry.py'
```

Expected: import failure for `layout_geometry`.

- [ ] **Step 3: Implement reusable geometry functions**

Expose these exact public signatures:

- `canonical_segment(a: Point, b: Point) -> Segment`
- `route_segments(points: list[Point]) -> list[Segment]`
- `collinear_overlap(first: Segment, second: Segment, *, epsilon: float = 1e-6) -> float`
- `shared_route_length(first: list[Point], second: list[Point]) -> float`
- `segment_hits_rect(segment: Segment, rect: Rect, *, clearance: float = 0.0) -> bool`
- `rects_overlap(first: Rect, second: Rect, *, clearance: float = 0.0) -> bool`
- `bend_count(points: list[Point]) -> int`
- `manhattan_length(points: list[Point]) -> float`
- `detour_ratio(points: list[Point]) -> float`
- `is_manhattan(points: list[Point], *, epsilon: float = 1e-6) -> bool`

Define `Point = tuple[float, float]`, `Segment = tuple[Point, Point]`, and
`Rect = tuple[float, float, float, float]`.

Round only at serialization boundaries. Comparisons use epsilon; stable ordering
uses exact `(x, y)` tuples after grid normalization.

- [ ] **Step 4: Write failing validator fixture assertions**

Add expected codes:

```text
artifact.readability.shared_segment
artifact.readability.route_congestion
artifact.readability.edge_label_collision
artifact.readability.port_congestion
artifact.layout.excessive_detour
artifact.layout.excessive_bends
artifact.layout.feedback_intrusion
artifact.layout.aspect_ratio
```

Assert that `allowed-fanout.drawio` and `intentional-bus.drawio` do not emit
`shared_segment`.

- [ ] **Step 5: Implement validator profile v2 diagnostics**

In `validate.py`:

- bump `VALIDATOR_VERSION` to `2.1.0`;
- reuse `layout_geometry` instead of duplicating new predicates;
- group route segments by page and canonical segment;
- exempt only short common-endpoint fan-out, explicit
  `data-route-group="bus"`, self-loops, and parallel same-endpoint edges;
- calculate label-node and label-label collisions;
- calculate endpoint port occupancy;
- calculate detour, bends, feedback intrusion, and canvas aspect;
- add `_code()` and `_remediation()` mappings;
- add metrics to `report.details["layout_metrics_v2"]`.

Set strict default thresholds in one constant:

```python
LAYOUT_THRESHOLDS_V2 = {
    "shared_segment_min": 30.0,
    "fanout_exemption_max": 24.0,
    "route_congestion_count": 3,
    "port_congestion_count": 4,
    "detour_ratio": 3.0,
    "bend_count": 8,
    "aspect_ratio": 4.0,
}
```

- [ ] **Step 6: Run focused validator tests**

Run:

```bash
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_layout_geometry.py'
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_validate.py'
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_finding_fixtures.py'
```

Expected: all pass and the known shared trunk is now strict-failed.

- [ ] **Step 7: Commit**

```bash
git add publish-drawio-skill/scripts/layout_geometry.py publish-drawio-skill/scripts/validate.py publish-drawio-skill/tests
git commit -m "feat(drawio): detect congested layout geometry"
```

---

## Task 3: Version the Monotonic Quality Vector

**Files:**

- Modify: `publish-drawio-skill/scripts/diagram_supervisor.py`
- Modify: `publish-drawio-skill/tests/test_diagram_supervisor.py`
- Modify: `publish-drawio-skill/data/workflow.v2.schema.json`
- Modify: `publish-drawio-skill/tests/test_lifecycle_v2.py`

- [ ] **Step 1: Write failing quality-profile tests**

Add tests proving:

- v1 vectors remain byte-for-byte unchanged;
- v2 uses the approved category order;
- any higher-priority regression rejects a candidate;
- total route length or canvas penalty cannot hide a new shared segment;
- a v2 workflow must persist `quality_profile_version: 2`.

Expected v2 keys:

```python
QUALITY_KEYS_V2 = (
    "semantic_violations",
    "structural_errors",
    "overlaps",
    "route_through",
    "edge_label_collisions",
    "shared_path_congestion",
    "crossings",
    "port_congestion",
    "routing_uncertainty",
    "excessive_detours",
    "excessive_bends",
    "route_length",
    "canvas_penalty",
)
```

- [ ] **Step 2: Run and confirm failure**

Run:

```bash
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_diagram_supervisor.py'
```

Expected: missing profile parameter or missing v2 keys.

- [ ] **Step 3: Implement versioned comparison**

Change the public signatures to
`quality_vector(report, *, profile_version=1)` and
`compare_reports(baseline, candidate, *, semantic_equal=True,
profile_version=1)`.

Add `quality_profile_version` to workflow v2 with enum `[1, 2]`, default it to
`2` only for new runs, and always read the persisted value on resume.

- [ ] **Step 4: Run focused tests**

Run:

```bash
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_diagram_supervisor.py'
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_lifecycle_v2.py'
```

Expected: all legacy and v2 cases pass.

- [ ] **Step 5: Commit**

```bash
git add publish-drawio-skill/scripts/diagram_supervisor.py publish-drawio-skill/data/workflow.v2.schema.json publish-drawio-skill/tests
git commit -m "feat(drawio): version monotonic layout quality"
```

---

## Task 4: Implement Pre-run Type and Semantic Intake

**Files:**

- Create: `publish-drawio-skill/scripts/diagram_intake.py`
- Create: `publish-drawio-skill/tests/test_diagram_intake.py`
- Modify: `publish-drawio-skill/scripts/command_ux.py`
- Modify: `publish-drawio-skill/scripts/diagram_orchestrator.py`
- Modify: `publish-drawio-skill/scripts/agent_runtime.py`
- Modify: `publish-drawio-skill/agents/diagram-semantic-analyst.md`
- Modify: `publish-drawio-skill/commands/drawio/create.md`
- Modify: `publish-drawio-skill/commands/drawio/improve.md`
- Modify: `publish-drawio-skill/references/diagram-intake.md`
- Modify: `publish-drawio-skill/tests/test_intake_docs.py`
- Create: `publish-drawio-skill/tests/test_command_ux.py`
- Create: `publish-drawio-skill/tests/test_agent_runtime.py`

- [ ] **Step 1: Write failing intake state-machine tests**

Cover explicit type, inferred unambiguous type, ambiguous native selection,
existing diagram type preservation, maximum three sequential blocking
questions, consolidated fourth prompt, explicit acceptance of host assumptions,
and no run directory before completion.

Use:

```python
result = diagram_intake.advance(
    request="Покажи сервисы и их зависимости",
    mode="create",
    existing_evidence=None,
    answers=[],
    analysis={
        "diagram_type": "dependency",
        "confidence": 0.55,
        "alternatives": ["c4"],
        "blocking_questions": [],
        "assumptions": [],
    },
)
self.assertEqual(result["status"], "awaiting_input")
self.assertEqual(result["classification"]["candidates"], ["c4", "dependency"])
self.assertLessEqual(len(result["questions"]), 1)
```

- [ ] **Step 2: Run and confirm failure**

Run:

```bash
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_diagram_intake.py'
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_intake_docs.py'
```

Expected: missing module and old documentation assertions (`at most 5
questions`, mandatory visual preference) fail.

- [ ] **Step 3: Implement deterministic intake state**

Define `DIAGRAM_TYPES` in this exact stable order:

```python
DIAGRAM_TYPES = (
    "flowchart", "bpmn", "c4", "er", "dependency",
    "sequence", "roadmap", "git-flow", "generic",
)
```

Expose:

- `explicit_type(request: str) -> str | None`
- `infer_existing_type(diagram: Path, evidence: Mapping[str, Any] | None) -> dict`
- `classify_request(request: str) -> dict`
- `blocking_gaps(request: str, diagram_type: str, answers: list[dict]) -> list[dict]`
- `advance(*, request, mode, existing_evidence, answers, analysis,
  accept_assumptions=False) -> dict`

Every question contains:

```json
{
  "question_id": "question-<20 hex>",
  "prompt": "Куда возвращается процесс при ошибке оплаты?",
  "reason": "Ответ определяет цель возвратного ребра.",
  "recommended": {"value": "payment_check", "label": "К проверке оплаты"},
  "choices": [{"value": "payment_check", "label": "К проверке оплаты"}],
  "allow_free_text": true
}
```

Classification heuristics only produce an automatic answer at high confidence.
Ambiguity is an `awaiting_input` result, not an exception.

- [ ] **Step 4: Add isolated Semantic Analyst intake phase**

Before a lifecycle run exists, call the same isolated Semantic Analyst with
`phase: "intake"` and store its immutable input/output under
`.diagram-intake/<intake-id>/`. `agent_runtime.py` selects
`diagram-intake-analysis.v1.schema.json` for that phase. The output contains:

```json
{
  "schema_version": 1,
  "role": "semantic_analyst",
  "status": "needs_human",
  "result": {
    "diagram_type": "dependency",
    "confidence": 0.55,
    "alternatives": ["c4"],
    "sufficient": false,
    "blocking_questions": [],
    "assumptions": []
  }
}
```

The host, not the model, assigns intake/question ids, enforces allowlisted
diagram types, caps questions, binds answers, and decides whether the intake is
complete. Explicit types and preserved existing types skip the model
classification question, but the analyst still performs the semantic
completeness check when the request lacks process detail.

- [ ] **Step 5: Add command transport for intake answers**

Add parser options:

```text
--intake-id
--intake-answer
--accept-intake-assumptions
```

`command_ux` must return structured `awaiting_input` JSON with a
`selection_required` object. Command Markdown instructs GigaCode to call native
`ask_user` for that object and replay the same short command with the hidden
answer flags. In headless mode the JSON is returned unchanged.

Do not allocate the lifecycle run directory until `status == "complete"`.
Persist the completed intake as `.diagram-intake/<intake-id>.json`, then copy it
into the new run's immutable inputs and delete no intake evidence.

- [ ] **Step 6: Update intake documentation**

Replace the old five-question/free-form-visual contract with:

- maximum three blocking semantic questions;
- one question per turn;
- no mandatory visual preference question;
- non-blocking visual choices become assumptions;
- explicit type skips confirmation;
- ambiguous type uses native selection;
- full run starts only after completion.

- [ ] **Step 7: Run intake and command tests**

Run:

```bash
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_diagram_intake.py'
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_command_ux.py'
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_intake_docs.py'
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_agent_runtime.py'
```

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add publish-drawio-skill/scripts/diagram_intake.py publish-drawio-skill/scripts/command_ux.py publish-drawio-skill/scripts/diagram_orchestrator.py publish-drawio-skill/scripts/agent_runtime.py publish-drawio-skill/agents/diagram-semantic-analyst.md publish-drawio-skill/commands publish-drawio-skill/references/diagram-intake.md publish-drawio-skill/tests
git commit -m "feat(drawio): add bounded semantic intake"
```

---

## Task 5: Build Immutable LayoutIR and Preserve Scopes

**Files:**

- Create: `publish-drawio-skill/scripts/layout_model.py`
- Create: `publish-drawio-skill/tests/test_layout_model.py`
- Create: `publish-drawio-skill/tests/fixtures/layout/order-process-plan.json`
- Create: `publish-drawio-skill/tests/fixtures/layout/local-improve.drawio`
- Modify: `publish-drawio-skill/scripts/diagram_model_v2.py`

- [ ] **Step 1: Write failing LayoutIR builder tests**

Prove:

- stable order is `(page_id, cell_id)`;
- create omits model-generated routes;
- explicit locked routes are preserved;
- node/edge label sizes are deterministic;
- feedback and self-loop classification is stable;
- improve starts with edge-only scope and locks every other cell;
- scope expansion is exactly edge → adjacent nodes → layer → component;
- the same semantic plan produces identical canonical JSON and SHA256.

- [ ] **Step 2: Run and confirm failure**

Run:

```bash
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_layout_model.py'
```

Expected: import failure for `layout_model`.

- [ ] **Step 3: Implement LayoutIR construction**

Expose:

- `build_layout_request(semantic_plan: Mapping[str, Any], *, run_id: str,
  semantic_plan_sha256: str, mode: str, backend: str, strategy_id: str,
  quality_profile_version: int, baseline: Mapping[str, Any] | None = None,
  scope: Mapping[str, Any] | None = None) -> dict`
- `infer_scope_from_findings(diagram_spec: Mapping[str, Any], findings:
  list[Mapping[str, Any]]) -> dict`
- `expand_scope(diagram_spec: Mapping[str, Any], scope: Mapping[str, Any],
  level: str) -> dict`

Use grid-normalized measured defaults:

```python
NODE_SIZE_BY_TYPE = {
    "decision": (140, 90),
    "start": (100, 50),
    "end": (100, 50),
    "database": (140, 80),
    "default": (160, 70),
}
GRID = 10
```

All node bounds, route locks, and element hashes from an improve baseline are
explicit in the request. Never use an omitted lock as permission.

- [ ] **Step 4: Run model tests**

Run:

```bash
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_layout_model.py'
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add publish-drawio-skill/scripts/layout_model.py publish-drawio-skill/scripts/diagram_model_v2.py publish-drawio-skill/tests
git commit -m "feat(drawio): build immutable layout requests"
```

---

## Task 6: Implement the Deterministic Python Layered Placement

**Files:**

- Create: `publish-drawio-skill/scripts/layout_builtin.py`
- Create: `publish-drawio-skill/tests/test_layout_builtin.py`
- Create: `publish-drawio-skill/tests/fixtures/layout/cycle.json`
- Create: `publish-drawio-skill/tests/fixtures/layout/nested-containers.json`

- [ ] **Step 1: Write failing placement tests**

Cover:

- Tarjan SCCs and deterministic feedback-edge choice;
- longest-path layer assignment;
- stable barycenter/median sweeps;
- TB and LR directions;
- nested containers and lanes;
- fixed/locked coordinates;
- no node overlap;
- deterministic repeated result.

The regression assertion for the known defect must ensure root nodes do not all
share one x-coordinate when the graph has branches:

```python
result = layout_builtin.layout(branching_request())
xs = {node["bounds"]["x"] for node in result["pages"][0]["nodes"]}
self.assertGreater(len(xs), 1)
```

- [ ] **Step 2: Run and confirm failure**

Run:

```bash
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_layout_builtin.py'
```

Expected: import failure for `layout_builtin`.

- [ ] **Step 3: Implement placement without routing**

Implement these exact public functions:

- `strongly_connected_components(nodes, edges) -> list[Sequence[str]]`
- `choose_feedback_edges(nodes, edges) -> set[str]`
- `assign_layers(nodes, edges, feedback_edges) -> dict[str, int]`
- `minimize_crossings(layers, edges, *, sweeps=4) -> list[list[str]]`
- `assign_coordinates(request, layers) -> dict[str, dict]`

Tie-break every traversal by stable cell id. Use four fixed sweeps; never use
randomness. Respect locked nodes first and expand spacing, rather than moving a
lock.

- [ ] **Step 4: Run placement tests**

Run:

```bash
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_layout_builtin.py'
```

Expected: placement tests pass; route-specific tests remain absent until Task 7.

- [ ] **Step 5: Commit**

```bash
git add publish-drawio-skill/scripts/layout_builtin.py publish-drawio-skill/tests
git commit -m "feat(drawio): add deterministic layered placement"
```

---

## Task 7: Add Ports, Obstacle-aware Routing, Loops, and Labels

**Files:**

- Modify: `publish-drawio-skill/scripts/layout_builtin.py`
- Modify: `publish-drawio-skill/scripts/layout_geometry.py`
- Modify: `publish-drawio-skill/tests/test_layout_builtin.py`
- Create: `publish-drawio-skill/tests/fixtures/layout/routing-obstacles.json`
- Create: `publish-drawio-skill/tests/fixtures/layout/fan-in-out.json`
- Create: `publish-drawio-skill/tests/fixtures/layout/return-loop.json`

- [ ] **Step 1: Write failing routing tests**

Cover:

- distinct degree-aware pins for fan-in/fan-out;
- visibility-graph route around expanded obstacles;
- every segment orthogonal;
- self-loop outside the node;
- feedback route outside the primary flow;
- shared-segment nudge;
- label bounds avoiding nodes and other labels;
- locked manual route retained;
- deterministic route ordering and metrics.

- [ ] **Step 2: Run and confirm failure**

Run:

```bash
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_layout_builtin.py'
```

Expected: missing route fields or diagonal/direct routes through obstacles.

- [ ] **Step 3: Implement deterministic port allocation**

Expose `allocate_ports(request, bounds) -> dict[str, dict]`.

Sort incident edges by opposite endpoint layer, coordinate, and edge id. Spread
ports across the appropriate side with normalized coordinates in `[0.1, 0.9]`.

- [ ] **Step 4: Implement rectilinear visibility routing**

Expose `route_edges(request, bounds, ports) -> dict[str, dict]`.

Algorithm:

1. Expand every obstacle by configured clearance.
2. Build candidate x/y channels from ports, grid, and obstacle sides.
3. Create only intersections outside obstacles.
4. Connect axis-adjacent visible vertices.
5. Run Dijkstra over `(point, previous_orientation)` state.
6. Cost = length + bend penalty + occupied-channel penalty.
7. Reserve accepted segments before routing the next stable edge id.
8. Route feedback edges and self-loops through the external channel first.
9. Canonicalize duplicate/collinear points and assert `is_manhattan`.

- [ ] **Step 5: Implement label placement**

Place the label on the longest unoccupied segment. Try deterministic offsets
`0, +20, -20, +40, -40`; if all collide, reserve an external label position
and expose the collision metric rather than hiding it.

- [ ] **Step 6: Complete `layout_builtin.layout()`**

Return a contract-valid result:

```python
def layout(request: Mapping[str, Any]) -> dict:
    feedback_edges = choose_feedback_edges(
        request["pages"][0]["nodes"],
        request["pages"][0]["edges"],
    )
    layers = assign_layers(
        request["pages"][0]["nodes"],
        request["pages"][0]["edges"],
        feedback_edges,
    )
    bounds = assign_coordinates(request, layers)
    ports = allocate_ports(request, bounds)
    routes = route_edges(request, bounds, ports)
    labels = place_edge_labels(request, bounds, routes)
    return build_layout_result(request, bounds, routes, labels, backend={
        "id": "python-layered",
        "version": "1",
        "options": request["strategy"]["options"],
    })
```

- [ ] **Step 7: Run placement/routing and contract tests**

Run:

```bash
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_layout_builtin.py'
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_layout_contracts.py'
```

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add publish-drawio-skill/scripts/layout_builtin.py publish-drawio-skill/scripts/layout_geometry.py publish-drawio-skill/tests
git commit -m "feat(drawio): route orthogonal layout in python"
```

---

## Task 8: Vendor ELK and Add the Offline Backend Router

**Files:**

- Create: `publish-drawio-skill/scripts/elk_runner.mjs`
- Create: `publish-drawio-skill/scripts/layout_backend.py`
- Create: `publish-drawio-skill/vendor/elkjs/elk.bundled.js`
- Create: `publish-drawio-skill/vendor/elkjs/LICENSE`
- Create: `publish-drawio-skill/vendor/elkjs/NOTICE.json`
- Create: `publish-drawio-skill/tests/test_layout_backend.py`
- Modify: `publish-drawio-skill/config.example.json`
- Modify: `publish-drawio-skill/DEPENDENCIES.md`

- [ ] **Step 1: Write failing backend tests**

Use fake Node executables/scripts to test:

- configured absolute Node path wins;
- PATH Node is accepted only after version/probe success;
- ELK success is normalized and contract-validated;
- timeout, non-zero exit, invalid JSON, NaN/Infinity, missing edge sections, and
  diagonal routes fall back to Python using the identical request digest;
- duplicate `(request_sha256, strategy_id, options_sha256)` is refused;
- no npm or network subprocess is invoked.

- [ ] **Step 2: Run and confirm failure**

Run:

```bash
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_layout_backend.py'
```

Expected: missing `layout_backend`.

- [ ] **Step 3: Vendor pinned elkjs**

On a network-enabled development machine only:

```bash
mkdir -p /tmp/drawio-elk-vendor
cd /tmp/drawio-elk-vendor
npm pack elkjs@0.11.1 --ignore-scripts
tar -xzf elkjs-0.11.1.tgz
cp package/lib/elk.bundled.js "$OLDPWD/publish-drawio-skill/vendor/elkjs/elk.bundled.js"
cp package/LICENSE.md "$OLDPWD/publish-drawio-skill/vendor/elkjs/LICENSE"
```

Write `NOTICE.json` with exact version, npm package name, upstream URL, tarball
integrity from `npm view elkjs@0.11.1 dist.integrity`, and SHA256 of both copied
files. The committed bundle is the runtime source of truth.

- [ ] **Step 4: Implement the stdin/stdout Node bridge**

`elk_runner.mjs`:

```javascript
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const ELK = require("../vendor/elkjs/elk.bundled.js");

const input = JSON.parse(await readStdin());
const graph = toElkGraph(input);
const output = await new ELK().layout(graph, {
  layoutOptions: effectiveOptions(input),
});
process.stdout.write(JSON.stringify(fromElkGraph(input, output)));
```

The bridge writes JSON only to stdout, diagnostics only to stderr, and exits 2
on any failure. Pin:

```text
elk.algorithm=layered
elk.direction=DOWN|RIGHT
elk.edgeRouting=ORTHOGONAL
elk.layered.considerModelOrder.strategy=NODES_AND_EDGES
elk.layered.nodePlacement.strategy=NETWORK_SIMPLEX
```

- [ ] **Step 5: Implement Python backend routing and proof**

Define:

```python
@dataclass(frozen=True)
class BackendAttempt:
    result: dict
    evidence: dict
```

Expose:

- `resolve_node(config: Mapping[str, Any], *, environ=None) -> Path | None`
- `run_elk(request, *, node, timeout_seconds) -> BackendAttempt`
- `run_layout(request, *, config, attempted_keys=frozenset()) -> BackendAttempt`

Evidence contains absolute executable, `node --version`, elkjs version, timeout,
exit code, stdout/stderr paths and hashes, request/result hashes, schema result,
fallback reason, strategy id, and effective options.

- [ ] **Step 6: Document configuration**

Add:

```json
{
  "drawio_bin": null,
  "node_bin": null,
  "layout_backend": "auto",
  "layout_timeout_seconds": 30,
  "layout_wall_clock_seconds": 180
}
```

`node_bin: null` means verified PATH discovery. `layout_backend` accepts
`auto`, `elk`, `python`, and `legacy-generic-v2`.

- [ ] **Step 7: Run backend tests with and without Node**

Run:

```bash
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_layout_backend.py'
PATH=/usr/bin:/bin .venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_layout_backend.py'
```

Expected: both pass; the second proves Python fallback.

- [ ] **Step 8: Commit**

```bash
git add publish-drawio-skill/scripts/elk_runner.mjs publish-drawio-skill/scripts/layout_backend.py publish-drawio-skill/vendor publish-drawio-skill/tests/test_layout_backend.py publish-drawio-skill/config.example.json publish-drawio-skill/DEPENDENCIES.md
git commit -m "feat(drawio): add offline elk layout backend"
```

---

## Task 9: Render XML Exclusively from Validated Layout Results

**Files:**

- Create: `publish-drawio-skill/scripts/layout_renderer.py`
- Create: `publish-drawio-skill/tests/test_layout_renderer.py`
- Modify: `publish-drawio-skill/scripts/diagram_orchestrator.py`
- Modify: `publish-drawio-skill/scripts/renderer_adapters.py`
- Create: `publish-drawio-skill/tests/test_renderer_adapters.py`

- [ ] **Step 1: Write failing renderer tests**

Prove:

- exact bounds, pins, waypoints, and label offsets come from
  `layout-result.v1`;
- no midpoint or invented waypoint is added;
- duplicate and collinear points are removed before XML serialization;
- diagonal result is refused;
- output is byte-identical for identical inputs;
- legacy renderer is available only by explicit backend id;
- specialized adapters retain their current implementation paths and defaults.

- [ ] **Step 2: Run and confirm failure**

Run:

```bash
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_layout_renderer.py'
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_renderer_adapters.py'
```

Expected: missing renderer module and generic adapter implementation mismatch.

- [ ] **Step 3: Implement renderer**

Expose `render_layout(semantic_plan: Mapping[str, Any], layout_result:
Mapping[str, Any], output: Path) -> Path`.

The renderer validates both bindings, creates stable mxCell order, writes
`exitX/exitY/entryX/entryY`, writes explicit `Array as="points"`, stores edge
class and route group in `data-*` attributes, and never performs placement or
routing.

- [ ] **Step 4: Register adapter lineage**

Change generic adapter implementation paths to include:

```text
scripts/layout_model.py
scripts/layout_backend.py
scripts/layout_builtin.py
scripts/layout_renderer.py
scripts/elk_runner.mjs
vendor/elkjs/elk.bundled.js
```

Add adapter options:

```text
backend: auto|elk|python|legacy-generic-v2
reflow: preserve|local|full
```

Do not change roadmap/git-flow/sequence selection.

- [ ] **Step 5: Run renderer and adapter tests**

Run:

```bash
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_layout_renderer.py'
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_renderer_adapters.py'
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add publish-drawio-skill/scripts/layout_renderer.py publish-drawio-skill/scripts/diagram_orchestrator.py publish-drawio-skill/scripts/renderer_adapters.py publish-drawio-skill/tests
git commit -m "feat(drawio): render validated layout results"
```

---

## Task 10: Integrate the Layout Pipeline into `/drawio:create`

**Files:**

- Modify: `publish-drawio-skill/scripts/diagram_orchestrator.py`
- Modify: `publish-drawio-skill/scripts/lifecycle_host_v2.py`
- Modify: `publish-drawio-skill/scripts/evidence_v2.py`
- Modify: `publish-drawio-skill/scripts/implementation_snapshot_v2.py`
- Modify: `publish-drawio-skill/tests/test_diagram_orchestrator.py`
- Modify: `publish-drawio-skill/tests/test_lifecycle_v2.py`

- [ ] **Step 1: Write failing create-pipeline tests**

Patch role calls and backends so tests prove:

- completed intake is copied into run inputs;
- semantic plan is generated once;
- all strategy variants share its hash;
- layout request/result snapshots are hash-bound;
- strict validation happens after rendering;
- ELK failure invokes Python with the identical request digest;
- strategy list is finite and duplicate attempts are skipped;
- a strict candidate reaches Reviewer/publication;
- strict failure publishes a separate best-effort artifact;
- create target remains no-clobber.

- [ ] **Step 2: Run and confirm failure**

Run:

```bash
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_diagram_orchestrator.py'
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_lifecycle_v2.py'
```

Expected: missing layout evidence and old generic rendering path observed.

- [ ] **Step 3: Add one host-owned layout attempt function**

Implement `execute_layout_attempt(workflow, semantic_plan, *, run_dir,
adapter_input, mode, scope, strategy, timeout) -> dict`.

It must:

1. build and validate `layout-request.v1`;
2. atomically persist it;
3. append lifecycle v2 `tool_attempt` with request snapshot;
4. run backend;
5. validate and persist `layout-result.v1`;
6. render candidate;
7. run strict validator and receipt;
8. compare through the persisted quality profile;
9. append accepted/rejected event with all snapshots.

- [ ] **Step 4: Replace generic create rendering**

In initial create flow, replace direct `render_generic` invocation with
`execute_layout_attempt`. Keep `_render_generic_v2` callable only when the
explicit backend is `legacy-generic-v2`.

Use the exact finite strategy list:

```python
LAYOUT_STRATEGIES = (
    ("elk-default", {"spacing": 1.0, "port_separation": 1.0, "shared_penalty": 1.0}),
    ("elk-spacing", {"spacing": 1.35, "port_separation": 1.0, "shared_penalty": 1.0}),
    ("elk-separated", {"spacing": 1.35, "port_separation": 1.4, "shared_penalty": 1.6}),
    ("python-fallback", {}),
)
```

Apply no more than three layout variants per scope and one fallback, within the
persisted wall-clock deadline.

- [ ] **Step 5: Persist trace-ready evidence**

Reuse `tool_attempt` events; do not add an unnecessary event enum. Store
`diagram-intake`, `layout-request`, and `layout-result` descriptors in event
snapshots and implementation snapshot paths.

- [ ] **Step 6: Run create/lifecycle regression tests**

Run:

```bash
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_diagram_orchestrator.py'
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_lifecycle_v2.py'
```

Expected: all pass, including the existing best-effort cases in
`test_diagram_orchestrator.py`.

- [ ] **Step 7: Commit**

```bash
git add publish-drawio-skill/scripts/diagram_orchestrator.py publish-drawio-skill/scripts/lifecycle_host_v2.py publish-drawio-skill/scripts/evidence_v2.py publish-drawio-skill/scripts/implementation_snapshot_v2.py publish-drawio-skill/tests
git commit -m "feat(drawio): orchestrate deterministic create layouts"
```

---

## Task 11: Integrate Local `/drawio:improve` and Repair Intent

**Files:**

- Modify: `publish-drawio-skill/scripts/agent_runtime.py`
- Modify: `publish-drawio-skill/scripts/diagram_orchestrator.py`
- Modify: `publish-drawio-skill/scripts/layout_model.py`
- Modify: `publish-drawio-skill/agents/diagram-repair.md`
- Modify: `publish-drawio-skill/tests/test_agent_runtime.py`
- Modify: `publish-drawio-skill/tests/test_diagram_orchestrator.py`
- Modify: `publish-drawio-skill/tests/test_layout_model.py`

- [ ] **Step 1: Write failing repair-intent and preservation tests**

Prove:

- layout phase requires `layout-repair-intent.v1`;
- semantic edits still use existing patch contracts;
- Repair cannot add an out-of-scope target;
- edge-only repair locks every node and other edge;
- scope expands at most twice;
- unchanged cell hashes are verified before candidate acceptance;
- full reflow is rejected unless request contains explicit reflow intent;
- no progress ends in safe best-effort without repeated human `continue`.

- [ ] **Step 2: Run and confirm failure**

Run:

```bash
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_agent_runtime.py'
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_diagram_orchestrator.py'
```

Expected: Repair runtime expects only `diagram-patch.v1` and improve lacks
layout scope evidence.

- [ ] **Step 3: Add phase-specific Repair contract selection**

Use:

```python
def role_output_schema(role, input_value):
    if role == "repair" and input_value.get("repair_mode") == "layout_intent":
        return "layout-repair-intent.v1.schema.json"
    return existing_role_output_schema(role, input_value)
```

Host-owned scope validation checks all `target_edges`, `movable_nodes`,
`locked_nodes`, and action enum before building the next immutable request.

- [ ] **Step 4: Implement local scope lifecycle**

Persist:

```python
SCOPE_EXPANSION_ORDER = (
    "edge_reroute",
    "adjacent_nodes",
    "one_layer",
    "connected_component",
)
```

Only the first three stages are automatic under the two-expansion limit. The
connected component is permitted only when the second expansion maps to it for
a graph without a distinct layer. Full reflow is separate and explicit.

- [ ] **Step 5: Verify untouched cells**

Before accepting a candidate, compare canonical per-cell hashes for all locked
cells. On mismatch:

- reject candidate as `preservation_violation`;
- do not advance baseline;
- retain the candidate as evidence;
- proceed to next bounded strategy or safe best-effort.

- [ ] **Step 6: Run improve and runtime tests**

Run:

```bash
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_agent_runtime.py'
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_layout_model.py'
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_diagram_orchestrator.py'
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add publish-drawio-skill/scripts/agent_runtime.py publish-drawio-skill/scripts/diagram_orchestrator.py publish-drawio-skill/scripts/layout_model.py publish-drawio-skill/agents/diagram-repair.md publish-drawio-skill/tests
git commit -m "feat(drawio): add scoped deterministic improvement"
```

---

## Task 12: Align Supervisor, Semantic Analyst, and Reviewer Contracts

**Files:**

- Modify: `publish-drawio-skill/agents/diagram-supervisor.md`
- Modify: `publish-drawio-skill/agents/diagram-semantic-analyst.md`
- Modify: `publish-drawio-skill/agents/diagram-reviewer.md`
- Modify: `publish-drawio-skill/scripts/agent_runtime.py`
- Create: `publish-drawio-skill/tests/test_agent_contracts.py`
- Modify: `publish-drawio-skill/tests/test_agent_runtime.py`

- [ ] **Step 1: Write failing documentation/contract tests**

Assert:

- Supervisor chooses only allowlisted strategy actions and never coordinates;
- Semantic Analyst omits normal routes and coordinates;
- Repair returns bounded intent for layout phase;
- Reviewer receives backend proof, changed/locked sets, congestion metrics, and
  quality profile version;
- Reviewer cannot approve blocking deterministic findings;
- no role searches for OpenSpec.

- [ ] **Step 2: Run and confirm failure**

Run:

```bash
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_agent_contracts.py'
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_agent_runtime.py'
```

Expected: missing layout policy wording/fields.

- [ ] **Step 3: Update role instructions**

Supervisor allowlist:

```text
create_layout
reroute_edges
expand_local_scope
retry_layout_strategy
request_semantic_clarification
finish_best_effort
```

Semantic Analyst owns type/intake/semantics and does not return ordinary
geometry. Reviewer treats validator findings as authoritative and reviews
backend/evidence integrity without asserting model proof itself.

- [ ] **Step 4: Extend role inputs without weakening isolation**

Pass only immutable JSON evidence. Do not add tools, nested agents, extension
context, or writable access. Retain existing model routing:

```text
Supervisor: GigaChat-3-Ultra
Semantic Analyst: vllm/Qwen3.6-35B-262k
Repair: vllm/MiniMax-M3-113k
Reviewer: vllm/DeepSeek-V4-Flash-262k
```

- [ ] **Step 5: Run role tests**

Run:

```bash
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_agent_contracts.py'
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_agent_runtime.py'
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add publish-drawio-skill/agents publish-drawio-skill/scripts/agent_runtime.py publish-drawio-skill/tests
git commit -m "docs(drawio): align agents with layout host"
```

---

## Task 13: Complete Trace, Resume, and User-facing Results

**Files:**

- Modify: `publish-drawio-skill/scripts/diagram_orchestrator.py`
- Modify: `publish-drawio-skill/commands/drawio/create.md`
- Modify: `publish-drawio-skill/commands/drawio/improve.md`
- Modify: `publish-drawio-skill/commands/drawio/resume.md`
- Modify: `publish-drawio-skill/commands/drawio/trace.md`
- Modify: `publish-drawio-skill/tests/test_diagram_orchestrator.py`
- Modify: `publish-drawio-skill/tests/test_command_ux.py`

- [ ] **Step 1: Write failing trace/result tests**

Assert final/trace JSON includes:

```text
diagram_type
intake status/questions/answers/assumptions
semantic_plan_sha256
layout_request_sha256
layout_result_sha256
backend id/version/executable/options
strategy attempts
quality_profile_version and vectors
changed and locked elements
validation receipts
review verdict
published artifact path/hash
strict or best_effort status
remaining findings
```

Also prove resume finds the latest pending run without a manually supplied run
id and does not re-ask answered intake questions.

- [ ] **Step 2: Run and confirm failure**

Run:

```bash
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_diagram_orchestrator.py'
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_command_ux.py'
```

Expected: missing trace fields.

- [ ] **Step 3: Extend trace verification**

Trace must verify:

- every snapshot file hash and canonical hash;
- request/result digest binding;
- backend attempt identity and no duplicate strategy key;
- Node proof when ELK was used;
- fallback reason when Python was used;
- quality-profile consistency across resume;
- publication target/hash.

Set trace status `tampered_or_incomplete` on any mismatch without deleting the
usable artifact.

- [ ] **Step 4: Update command presentation contracts**

Commands remain:

```text
/drawio:create "Создай процесс обработки заказа с возвратом при ошибке"
/drawio:improve
/drawio:resume
/drawio:trace
```

Do not instruct the user to enter `continue` for deterministic plateaus.
Mention resume only for semantic ambiguity, publication conflict, explicit
pause, or later optional improvement.

- [ ] **Step 5: Run trace/UX regression tests**

Run:

```bash
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_diagram_orchestrator.py'
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_command_ux.py'
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add publish-drawio-skill/scripts/diagram_orchestrator.py publish-drawio-skill/commands publish-drawio-skill/tests
git commit -m "feat(drawio): expose layout evidence and results"
```

---

## Task 14: Add Deterministic Corpus and End-to-end Acceptance Tests

**Files:**

- Create: `publish-drawio-skill/tests/test_layout_corpus.py`
- Create: `publish-drawio-skill/tests/fixtures/layout/corpus/*.json`
- Create: `publish-drawio-skill/tests/fixtures/layout/shared-x-350.drawio`
- Modify: `publish-drawio-skill/scripts/self_check.py`
- Create: `publish-drawio-skill/SCENARIO_COVERAGE.md`

- [ ] **Step 1: Add corpus fixtures**

Include:

```text
linear-process
two-way-decision
three-way-decision
return-loop
order-processing
c4-services
microservices
er-dependency
bpmn-lanes
local-edge-improve
local-node-move
elk-failure-fallback
strict-failure-best-effort
```

Keep each source small enough that failures identify one behavior.

- [ ] **Step 2: Write failing corpus tests**

For each graph-oriented create fixture:

- build request twice;
- run the chosen backend twice;
- render twice;
- validate twice;
- assert identical canonical request/result, draw.io bytes, quality vector,
  and normalized trace ordering;
- assert no required blocking finding.

For improve fixtures, assert semantic digest and untouched hashes remain equal.
For the `shared-x-350.drawio` regression, assert validator catches the old
artifact and the new layout eliminates the unintended shared trunk.

- [ ] **Step 3: Run and confirm failure**

Run:

```bash
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_layout_corpus.py'
```

Expected: fixture or integration failures until the corpus path is complete.

- [ ] **Step 4: Add self-check coverage**

Self-check must run:

- schema compilation;
- Python fallback create;
- ELK create when Node is available;
- forced ELK failure → Python fallback;
- one local improve preservation case;
- strict-failure → best-effort artifact case.

Node/ELK absence is a passed fallback check, not a skipped core pipeline.

- [ ] **Step 5: Run focused and complete package tests**

Run:

```bash
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_layout_corpus.py'
.venv/bin/python publish-drawio-skill/scripts/self_check.py
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_*.py'
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add publish-drawio-skill/tests publish-drawio-skill/scripts/self_check.py publish-drawio-skill/SCENARIO_COVERAGE.md
git commit -m "test(drawio): cover deterministic layout corpus"
```

---

## Task 15: Update Documentation, Offline Packaging, and Release Version

**Files:**

- Modify: `publish-drawio-skill/SKILL.md`
- Modify: `publish-drawio-skill/README.md`
- Modify: `publish-drawio-skill/metadata.md`
- Modify: `publish-drawio-skill/gemini-extension.json`
- Modify: `publish-drawio-skill/docs/drawio-agent-extension-corporate-test-commands.md`
- Modify: `publish-drawio-skill/DEPENDENCIES.md`
- Modify: `release/README.md`
- Modify: `release/skills.json`
- Modify: `scripts/gigacode/install_drawio_agent_extension.sh`
- Modify: `scripts/gigacode/verify_drawio_agent_extension.sh`
- Modify: `tests/gigacode/test_extension_installers.py`
- Modify: `tests/test_release_skills.py`

- [ ] **Step 1: Write failing release inventory tests**

Assert ZIP contains:

```text
scripts/diagram_intake.py
scripts/layout_geometry.py
scripts/layout_model.py
scripts/layout_builtin.py
scripts/layout_backend.py
scripts/layout_renderer.py
scripts/elk_runner.mjs
vendor/elkjs/elk.bundled.js
vendor/elkjs/LICENSE
vendor/elkjs/NOTICE.json
data/diagram-intake.v1.schema.json
data/diagram-intake-analysis.v1.schema.json
data/layout-request.v1.schema.json
data/layout-result.v1.schema.json
data/layout-repair-intent.v1.schema.json
docs/drawio-agent-extension-corporate-test-commands.md
```

Assert installer and verifier expect `1.25.0-corporate.1`. Assert verifier fails
when ELK bundle/license/notice or any new runtime module is missing or has a
manifest mismatch.

- [ ] **Step 2: Run and confirm failure**

Run:

```bash
.venv/bin/python -m unittest tests.gigacode.test_extension_installers tests.test_release_skills
```

Expected: old version and missing inventory failures.

- [ ] **Step 3: Update release allowlist**

Add:

```json
"scripts/diagram_intake.py",
"scripts/layout_geometry.py",
"scripts/layout_model.py",
"scripts/layout_builtin.py",
"scripts/layout_backend.py",
"scripts/layout_renderer.py",
"scripts/elk_runner.mjs",
"vendor/elkjs/*"
```

Keep `node_modules` forbidden. Add `node` as optional, not required, because the
Python fallback is mandatory and must install successfully without Node.

- [ ] **Step 4: Bump version**

Change all current `1.24.0-corporate.5` release references to
`1.25.0-corporate.1`, including installer default and verifier expected version.
Set installer branch to the implementation branch used for this work.

- [ ] **Step 5: Update user and corporate test documentation**

Document:

- normal short create/improve commands;
- interactive intake behavior;
- strict vs best-effort output;
- Node proof command;
- forced Python fallback test;
- ELK trace fields;
- status check from another terminal;
- ZIP install, verify, checksum, and rollback;
- no npm/network runtime dependency.

- [ ] **Step 6: Run release tests**

Run:

```bash
.venv/bin/python -m unittest tests.gigacode.test_extension_installers tests.test_release_skills
.venv/bin/python scripts/release_skills.py verify --skill drawio
```

Expected: all pass and release verify reports the new version.

- [ ] **Step 7: Commit**

```bash
git add publish-drawio-skill release scripts/gigacode tests
git commit -m "chore(drawio): release layout engine 1.25.0"
```

---

## Task 16: Final Verification, ZIP, and Rollback Evidence

**Files:**

- Verify only unless a test exposes a defect.
- Generated: `dist/drawio-skill-agent-extension.zip`
- Generated: `dist/drawio-skill-agent-extension.zip.sha256`

- [ ] **Step 1: Run syntax and targeted regression gates**

```bash
.venv/bin/python -m compileall -q publish-drawio-skill/scripts publish-drawio-skill/tests
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_layout_*.py'
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_validate.py'
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_diagram_supervisor.py'
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_diagram_orchestrator.py'
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_lifecycle_v2.py'
```

Expected: all pass.

- [ ] **Step 2: Run complete repository gates**

```bash
.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_*.py'
.venv/bin/python -m unittest tests.gigacode.test_extension_installers tests.test_release_skills
.venv/bin/python scripts/release_skills.py verify --skill drawio
```

Expected: all pass.

- [ ] **Step 3: Build twice and prove deterministic ZIP**

```bash
.venv/bin/python scripts/release_skills.py build --skill drawio
shasum -a 256 dist/drawio-skill-agent-extension.zip
cp dist/drawio-skill-agent-extension.zip /tmp/drawio-layout-first.zip
.venv/bin/python scripts/release_skills.py build --skill drawio
cmp /tmp/drawio-layout-first.zip dist/drawio-skill-agent-extension.zip
shasum -a 256 dist/drawio-skill-agent-extension.zip > dist/drawio-skill-agent-extension.zip.sha256
```

Expected: `cmp` exits 0.

- [ ] **Step 4: Inspect ZIP inventory**

```bash
unzip -l dist/drawio-skill-agent-extension.zip
unzip -p dist/drawio-skill-agent-extension.zip drawio-skill/MANIFEST.sha256 | shasum -a 256
```

Expected: all new runtime, schemas, vendored ELK/license, tests, commands, and
corporate documentation are present; no `.DS_Store`, `.git`, `node_modules`, or
cache files.

- [ ] **Step 5: Export representative PNGs**

Run create/layout/render for order process, return loop, C4, microservices, ER,
and BPMN-like lane corpus fixtures. Export them with draw.io Desktop and inspect:

- no unintended shared trunks;
- no node overlaps or route-through;
- branch labels readable;
- feedback loop outside main flow;
- sensible aspect ratio;
- locked local-improve cells unchanged.

Store only test-approved source fixtures in git; do not commit temporary PNGs
unless release documentation explicitly links them.

- [ ] **Step 6: Review diff and commit any verification-only corrections**

```bash
git status --short
git diff --check
git log --oneline --decorate -20
```

If verification required no code changes, do not create an empty commit. If it
did, add a focused test first and commit with:

```bash
git commit -m "fix(drawio): close layout release regression"
```

- [ ] **Step 7: Record delivery evidence**

Report:

- branch and final commit;
- ZIP absolute path;
- ZIP SHA256;
- complete test counts;
- Node/ELK smoke evidence;
- Python fallback evidence;
- representative PNG review result;
- corporate install command;
- rollback command to the previous `1.24.0-corporate.5` archive.

Do not claim corporate GigaCode acceptance until the ZIP is installed and its
runtime commands are executed on the corporate laptop.

---

## Final Acceptance Checklist

- [ ] `/drawio:create` and `/drawio:improve` use the deterministic layout host.
- [ ] Intake asks at most three blocking semantic questions and no mandatory
      visual question.
- [ ] Ambiguous type uses native `ask_user`; headless returns stable
      `awaiting_input`.
- [ ] Normal semantic plans contain no model-generated coordinates or routes.
- [ ] Every generated connector has explicit Manhattan waypoints and ports.
- [ ] Feedback loops and self-loops use external channels.
- [ ] Shared segments, congestion, labels, ports, detours, bends, feedback
      intrusion, and aspect ratio are validator-visible.
- [ ] v2 monotonic comparison rejects every higher-priority regression.
- [ ] Improve preserves semantic digest and every untouched cell hash.
- [ ] ELK and Python fallback consume the same immutable request.
- [ ] No runtime npm/network access exists.
- [ ] Automatic loops are finite and publish a safe best-effort artifact when
      strict success is unavailable.
- [ ] Trace proves intake, models, semantic plan, backend, attempts, receipts,
      review, and publication.
- [ ] Specialized adapters and legacy quality profiles remain compatible.
- [ ] ZIP is deterministic, manifest-verified, installable, and rollback-safe.
