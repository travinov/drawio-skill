# Task 11 report

## Scope

- Added phase-specific Repair output selection: layout-only improve requests use
  `layout-repair-intent.v1`, while semantic Repair stays on
  `diagram-patch.v1`.
- Made layout-intent scope host-owned and persisted: action, page, target
  edges, movable nodes, and the complete locked-node set are validated before
  a canonical local layout request is executed.
- Reused the Task 10 deterministic executor for local reflow:
  `build_layout_request -> run_layout -> replayable diagram patch -> immutable
  baseline candidate -> strict validation/v2 receipt -> preservation and
  monotonic quality comparison -> existing reviewer/candidate lifecycle`.
- Added the bounded scope order `edge_reroute -> adjacent_nodes -> one_layer
  -> connected_component`; only two automatic expansions are permitted.
- Rejected unmarked full reflow requests, and added canonical locked-cell hash
  verification. A preservation violation keeps the candidate as evidence and
  retains the accepted baseline.
- Scoped preservation to layout-intent candidates, so a later semantic patch
  is not rejected by stale layout-scope state.
- Added the Repair prompt/schema contract and made bounded no-progress finish
  via safe best effort instead of another human `continue` checkpoint.

## TDD evidence

RED:

- `test_agent_runtime.py`: Repair selected `diagram-patch.v1.schema.json`
  instead of `layout-repair-intent.v1.schema.json`.
- `test_layout_model.py`: `layout_model.SCOPE_EXPANSION_ORDER` was absent.
- Independent-review regression:
  `test_layout_intent_runs_deterministic_attempt_and_persists_candidate_before_selection`
  failed because `_run_layout_intent_attempts` did not exist.
- Independent-review regression:
  `test_semantic_patch_ignores_stale_layout_scope_preservation_gate` failed
  because preservation was not scoped to the current candidate origin.

GREEN:

- `.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_agent_runtime.py'` — 3 passed.
- `.venv/bin/python -m unittest discover -s publish-drawio-skill/tests -p 'test_layout_model.py'` — 15 passed.
- Targeted Task 11 orchestration preservation/scope test — passed.
- `test_layout_contracts.LayoutContractTests.test_schemas_compile_and_accept_positive_documents` — passed.
- `py_compile` for the three changed scripts and `git diff --check` — passed.
- Focused Task 11 worker/stale-scope regressions plus Task 10
  create/replay/no-clobber smoke — 6 tests passed in 12.579s.
- Full `tests.test_diagram_orchestrator` invocation after the review fix —
  63 tests passed in 143.791s. This full file was invoked exactly once for the
  final fix.

## Review cleanup

- Removed the superseded request-only recorder.
- Reused and corrected the existing scope-expansion helper instead of adding a
  second scope-state implementation.
- Reduced the independent-review fix from +567 net lines to +462 net lines
  before the final test run.
- No known Task 11 correctness concern remains after the focused and full
  orchestrator passes.
