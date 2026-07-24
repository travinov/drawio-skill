# Layout corpus scenario coverage

`tests/test_layout_corpus.py` is a deterministic, end-to-end corpus over the
immutable layout request, selected backend, renderer, and artifact validator.
Each JSON fixture has one primary behaviour so a failure names the affected
contract rather than a large mixed diagram.

| Scenario | Primary assertion |
|---|---|
| `linear-process` | Linear create layout is byte- and trace-deterministic. |
| `two-way-decision` | Two branch routes retain deterministic geometry. |
| `three-way-decision` | Three branch routes avoid the shared-trunk regression. |
| `return-loop` | A feedback return route remains deterministic. |
| `order-processing` | Order decision branches render through the standard pipeline. |
| `c4-services` | C4 service dependency create layout is deterministic. |
| `microservices` | Service fan-out create layout is deterministic. |
| `er-dependency` | Entity dependency create layout is deterministic. |
| `bpmn-lanes` | Parent/lane semantics bind to rendered geometry. |
| `local-edge-improve` | Local reroute keeps semantic digest and locked-cell hashes. |
| `local-node-move` | Scoped movable node keeps untouched hashes and semantics. |
| `elk-failure-fallback` | A verified-but-failing ELK runner falls back to Python. |
| `strict-failure-best-effort` | A strict readability failure still leaves a valid best-effort artifact. |

Every graph create case is run twice. The test compares canonical request and
result hashes, rendered draw.io bytes, quality vectors, normalized findings,
and rejects validator errors. The two improve cases additionally compare the
semantic digest and every declared untouched cell hash.

`tests/fixtures/layout/shared-x-350.drawio` is deliberately bad legacy input:
the validator must report its 350px shared route trunk. The generated
three-way decision replacement must not report a shared trunk.

The local self-check repeats the pipeline obligations independently of this
test module: schema compilation, Python create, ELK create when verified Node
is available (or a passed Python fallback otherwise), forced ELK failure,
local preservation, and strict-failure best effort.
