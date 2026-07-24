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
- `4d638fd test(drawio): prove durable layout acceptance`

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

The second independent review found that the first follow-up compared snapshot
descriptors but did not read the snapshot and receipt JSON payloads. The final
comparison reads all four event-bound lifecycle snapshots and both v1/v2
validation receipts, recursively normalizes them, and retains all stable
semantic, status, binding, backend, quality, descriptor, path, and byte-length
fields.

The exact volatile-field allowlist is:

- timestamp fields: `captured_at`, `created_at`, `finished_at`, `started_at`,
  `timestamp`, and `updated_at`;
- generated identifier fields: `bundle_id`, `event_id`, `receipt_id`,
  `snapshot_id`, and `transaction_id`;
- generated predecessor fields: `previous_event_sha256` and
  `previous_snapshot_sha256`;
- strings equal to or beneath the captured absolute temporary workspace.

`run_id` and every identifier not named above remain compared. Snapshot
`canonical_sha256`/`sha256` and validation-receipt artifact hashes are not
discarded. Raw values derived from volatile payload bytes are replaced with
SHA256 fingerprints of the normalized JSON content they identify; unrelated
stable hashes remain byte-for-byte compared.

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

Second-review RED:

```text
../.venv/bin/python -m unittest tests.test_layout_corpus
Ran 9 tests in 5.639s
FAILED (errors=2)
```

The two errors proved the missing common comparator and missing actual receipt
payload capture. The added negative regression changes the stable receipt
`result` from `passed` to same-length `failed` and exercises the same comparator
as the positive two-run test.

Second-review GREEN:

```text
../.venv/bin/python -m unittest tests.test_layout_corpus
Ran 9 tests in 5.281s
OK

../.venv/bin/python scripts/self_check.py --json
54 passed, 1 registry check skipped, 0 errors

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

No third full suite was run for the second-review test/report-only
normalization change; Task 16 owns the final package gate.

## Remaining risks

- The deterministic durable-trace run uses the bundled Python backend so CI
  does not depend on an installed Node/ELK runtime. ELK selection and forced
  failure remain covered separately.
- Timestamp-bearing raw snapshot/receipt hashes cannot be equal across runs;
  normalized-content SHA256 fingerprints are compared instead. Stable
  candidate/report/validator hashes and every lifecycle binding remain
  unmodified and compared.
