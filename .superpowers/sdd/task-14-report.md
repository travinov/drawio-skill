# Task 14 report

## Scope completed

- Added the exact 13-fixture deterministic layout corpus, the shared-x-350
  regression artifact, local-improve preservation checks, forced ELK fallback,
  and independent scenario documentation.
- Added a real v2 lifecycle/orchestrator run for `linear-process` and compared
  two durable event-ledger traces, including event order, snapshot order,
  stable payloads, and persisted layout-evidence validation.
- Added explicit BPMN lane assertions: `receive` remains parented by `sales`,
  `fulfil` by `ops`, both parents render as swimlanes, and both activities fit
  inside their lane-relative geometry.
- Replaced the disconnected strict-failure check with one causal flow. The
  same strict-failed candidate is bound to its report and v2 receipt, accepted
  only under the safe best-effort classifier, and transactionally published to
  a separate no-clobber target with identical bytes.
- Mirrored that causal strict-failure flow in local `self_check.py` without
  depending on test fixtures.
- No production layout, lifecycle, validation, or publication behavior was
  changed by the independent-review follow-up.

## Commits

- `08619b2 test(drawio): cover deterministic layout corpus`
- `13343e8 test(drawio): align supervisor fallback fixture`
- `9e9085c fix(drawio): keep gitflow layout within strict aspect ratio`

## Independent-review fixes

The initial independent review requested three Important corrections:

1. The original `normalized_trace` was a summary rather than durable trace
   evidence. The follow-up runs the actual orchestrator and lifecycle host
   twice and compares normalized real ledger records.
2. The original strict-failure test validated one bad artifact, then rendered
   an unrelated good artifact. The follow-up validates, accepts, and publishes
   the same candidate, with candidate/report/receipt/publication SHA equality
   and requested-target no-clobber assertions.
3. The BPMN scenario named lane semantics without a lane-specific assertion.
   The follow-up now verifies lane parents, swimlane style, and child bounds.

Durable-event normalization preserves schema version, run id, sequence, event
type, actor, ordered snapshot kind/path/version/byte length, and the full stable
payload including stable artifact hashes. It removes only:

- lifecycle-generated event id, timestamp, transaction id, and
  previous-event hash;
- snapshot content/transaction/predecessor hashes, because the snapshot
  documents embed the generated ids and timestamps above;
- the v1/v2 validation-receipt artifact hashes, because receipt bytes embed
  validator start/finish timestamps.

Receipt paths and byte lengths remain compared.

## TDD evidence

Independent-review RED before helper implementation:

```text
python3 -m unittest tests.test_layout_corpus
Ran 8 tests in 2.767s
FAILED (errors=3)
```

The three errors were the intentionally undefined
`run_durable_layout_trace`, `render_bpmn_lane_geometry`, and
`publish_strict_failed_candidate_best_effort` helpers.

GREEN after implementation:

```text
python3 -m unittest tests.test_layout_corpus
Ran 8 tests in 5.102s
OK

../.venv/bin/python scripts/self_check.py --json
54 passed, 1 registry check skipped, 0 errors
layout:strict-best-effort:
  strict_passed=false
  publication_status=committed
```

Independent focused runner:

```text
../.venv/bin/python -m unittest tests.test_layout_corpus
Ran 8 tests in 7.558s
OK

../.venv/bin/python scripts/self_check.py --json
54 passed, 1 registry check skipped, 0 errors
timing rerun: 5.939s

../.venv/bin/python -m unittest \
  tests.test_artifact_tools tests.test_lifecycle_v2 \
  tests.test_diagram_orchestrator
Ran 109 tests in 222.512s
OK

../.venv/bin/python -m py_compile \
  scripts/self_check.py tests/test_layout_corpus.py
git diff --check
```

## Full-package evidence

The pre-review Task 14 package run is retained at
`/private/tmp/task14-gitflow-full-package.log`:

```text
Ran 426 tests in 272.746s
OK
real 272.91
user 221.54
sys 15.36
```

After the independent-review test changes, exactly one new full package run was
executed and retained at `/private/tmp/task14-review-full-package.log`:

```text
../.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
Ran 428 tests in 279.296s
OK
real 279.46
user 227.80
sys 15.53
```

## Remaining risks

- The deterministic durable-trace run uses the bundled Python backend so CI
  does not depend on an installed Node/ELK runtime. ELK selection and forced
  failure remain covered separately.
- Receipt hashes are intentionally not compared across runs because receipt
  timestamps make their bytes volatile; the receipt path/length and every
  candidate/report binding are still verified, and the causal best-effort test
  independently proves receipt-to-candidate SHA equality.
