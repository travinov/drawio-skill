# Layout corpus scenario coverage

`tests/test_layout_corpus.py` is a deterministic, end-to-end corpus over the
immutable layout request, selected backend, renderer, and artifact validator.
Each JSON fixture has one primary behaviour so a failure names the affected
contract rather than a large mixed diagram.

| Scenario | Primary assertion |
|---|---|
| `linear-process` | Linear create layout is byte-deterministic and its real durable lifecycle trace repeats exactly. |
| `two-way-decision` | Two branch routes retain deterministic geometry. |
| `three-way-decision` | Three branch routes avoid the shared-trunk regression. |
| `return-loop` | A feedback return route remains deterministic. |
| `order-processing` | Order decision branches render through the standard pipeline. |
| `c4-services` | C4 service dependency create layout is deterministic. |
| `microservices` | Service fan-out create layout is deterministic. |
| `er-dependency` | Entity dependency create layout is deterministic. |
| `bpmn-lanes` | Rendered BPMN activities retain their lane parent and fit inside the lane-relative geometry. |
| `local-edge-improve` | Local reroute keeps semantic digest and locked-cell hashes. |
| `local-node-move` | Scoped movable node keeps untouched hashes and semantics. |
| `elk-failure-fallback` | A verified-but-failing ELK runner falls back to Python. |
| `strict-failure-best-effort` | The same strict-failed candidate is accepted and published separately as hash-bound safe best effort. |

Every graph create case is run twice. The test compares canonical request and
result hashes, rendered draw.io bytes, quality vectors, normalized findings,
and rejects validator errors. The two improve cases additionally compare the
semantic digest and every declared untouched cell hash.

The `linear-process` case also runs twice through the actual orchestrator and
v2 lifecycle host. Its comparison retains each durable event's schema, run,
sequence, type, actor, ordered snapshot kind/path/version/byte length, and full
stable payload, including stable artifact hashes. Normalization removes only
lifecycle-generated event identity/time/transaction/previous-event hashes,
snapshot content/transaction/predecessor hashes whose documents embed those
volatile values, and the two validation-receipt artifact hashes because the
receipts embed validator start/finish timestamps. Receipt paths and byte
lengths remain compared. The test separately asserts the expected event and
snapshot order and validates the persisted layout evidence.

`tests/fixtures/layout/shared-x-350.drawio` is deliberately bad legacy input:
the validator must report its 350px shared route trunk. The generated
three-way decision replacement must not report a shared trunk.

The local self-check repeats the pipeline obligations independently of this
test module: schema compilation, Python create, ELK create when verified Node
is available (or a passed Python fallback otherwise), forced ELK failure,
local preservation, and a causal strict-failure best-effort flow. That last
check validates one candidate strictly, binds the failed report and receipt to
it, classifies only safe readability findings, and publishes the same bytes to
a separate no-clobber target.
