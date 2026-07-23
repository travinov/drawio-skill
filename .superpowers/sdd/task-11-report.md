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
- Added crash recovery for completed `local_reflow` attempts. Recovery verifies
  the current semantic plan, baseline, iteration, scope, deterministic patch
  replay, candidate, receipts, preservation, comparison, and lifecycle
  snapshots before selecting the finite-strategy candidate.
- Made every post-start layout stage terminal: backend/result, patch
  synthesis/application, strict validation/receipt, preservation, and
  comparison failures persist produced artifacts, a failure descriptor, a
  failed tool event, and a non-selectable workflow attempt.
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
- Second-review recovery regression: a completed, unindexed local attempt
  returned no candidate and tried the schedule again.
- Second-review tamper regression: changing indexed preservation state did not
  fail closed against its immutable evidence.
- Second-review terminality regressions: validator and patch-application
  exceptions advanced the schedule without persisting a terminal failed
  attempt.
- Final-review recovery regressions: a valid earlier local scope blocked
  expanded-scope recovery; indexed failed attempts were skipped; and
  ledger-only failed-event descriptors were trusted without file verification.
- Final classification regressions: mutable request mode and inline status
  could skip indexed attempts before immutable terminal verification.
- Terminal-retry regression: repeated resumes appended `skipped` events to
  already verified completed/failed local attempt histories.
- Raw-key regression: an unverified local attempt key could suppress backend
  execution and emit a false `skipped` event.

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
- Second-review focused recovery gate — 4 tests passed in 21.137s.
- The first authorized 67-test full run exposed one legacy mocked workflow
  without a working-artifact path; the production recovery path was guarded to
  run only for a real file. The failing unit plus all four recovery tests then
  passed (5 tests in 21.722s).
- Final authorized `tests.test_diagram_orchestrator` run — 67 tests passed in
  177.603s.
- Final-review three-test RED became green, then the combined seven-test local
  recovery gate passed in 39.528s.
- Single authorized final-review `tests.test_diagram_orchestrator` run —
  70 tests passed in 183.266s.
- Final classification gate passed 9 tests in 51.624s; the single authorized
  full orchestrator run passed 72 tests in 196.082s.
- Terminal retry plus recovery gate passed 10 tests in 58.104s. After correcting
  one invalid mock plan, the final full run passed 73 tests in 203.087s.
- Raw-key plus recovery gate passed 11 tests in 63.730s; the full orchestrator
  run passed 74 tests in 210.929s.

## Review cleanup

- Removed the superseded request-only recorder.
- Reused and corrected the existing scope-expansion helper instead of adding a
  second scope-state implementation.
- Reduced the independent-review fix from +567 net lines to +462 net lines
  before the final test run.
- Kept the second-review recovery commit at the agreed +550 net-line cap:
  orchestrator +495/-143 and tests +198.
- Kept the final-review recovery fix at +227 net lines before this report.
- Kept the final classification fix at +43 net lines before this report.
- Kept the terminal-retry fix under its +80 net-line cap.
- No known Task 11 correctness concern remains after the final 70-test pass.
