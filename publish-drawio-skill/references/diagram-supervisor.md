# Diagram Supervisor workflow

Use this workflow when the user asks to improve, repair, validate, iterate on, or independently review an existing `.drawio`, or explicitly requests an agent loop with evidence. Existing XML is the rendering source of truth: never regenerate the whole diagram merely to fix its layout.

## Roles

- **Supervisor** owns the run state and user communication. Default model: `GigaChat-3-Ultra`.
- **Independent Reviewer** is read-only and returns findings/verdicts. Default model: `vllm/DeepSeek-V4-Flash-262k`.
- **Repair** is on demand and proposes patch intent, never raw XML. Default model: `vllm/MiniMax-M3-113k`.
- **Semantic Analyst / Arbiter** is on demand for OpenSpec reconciliation and semantic conflicts. Default model: `vllm/Qwen3.6-35B-262k`.

Model output is advisory. `scripts/diagram_supervisor.py` and `scripts/validate.py` own parsing, mutation, comparison, evidence, and state transitions.

## Main-host ownership on corporate GigaCode

On GigaCode 26.5.17 / Qwen Code 0.13.1, the main interactive GigaChat session is
the extension host and Supervisor executor. Do not send the whole request to
native `diagram-supervisor` through the `agent` tool. That native role is
planning-only because its successful status neither proves shell execution nor
model isolation.

For a normal read-only audit, use the extension command as the only supported
entry point:

```text
/drawio:review "/absolute/path/to/diagram.drawio"
```

The custom command executes `scripts/diagram_host.py` before the interactive
model can select tools. It creates `.diagram-runs/<run-id>`, performs
host-preflight and strict validation, invokes the Reviewer through the isolated
CLI adapter, verifies hash bindings and model proof, then supplies a structured
result to the interactive model for presentation only. Do not replace this with
prompt instructions asking the model to call `list_directory`, `grep_search`, a
shell tool, or the native supervisor agent.

The command honors `PYTHON_BIN`, `GIGACODE_HOME`, `GIGACODE_EXTENSIONS_DIR`, and
`GIGACODE_BIN` when those installer-supported overrides are present. Its
`{{args}}` placeholder is intentionally left to Qwen's custom-command processor,
which applies shell escaping before `!{...}` execution. A non-zero host exit is
not suppressed or presented as a successful review.

The main host starts every run with the absolute installed extension path:

```bash
EXT="$HOME/.gigacode/extensions/publish-drawio-skill"
RUN_DIR="$PWD/.diagram-runs/<run-id>"
python3 "$EXT/scripts/diagram_supervisor.py" host-preflight \
  --workspace "$PWD" --run-dir "$RUN_DIR" \
  --cli "$HOME/.gigacode/bin/gigacode"
```

Stop before analysis if preflight fails. Later commands must use the same
`EXT` and `RUN_DIR`. The main host invokes isolated Reviewer, Repair, and
Semantic Analyst processes itself; it never changes the interactive `/model`.

## Source reconciliation

Before changing an existing diagram:

1. Read the user's description and any files explicitly supplied.
2. Search the current repository for relevant OpenSpec material (`openspec/specs/`, active `openspec/changes/`, or a user-selected specification).
3. Compare the process facts with the current diagram and build source references containing `kind`, `uri`, `revision`, `fragment`, `content_hash`, and `confidence`.
4. Apply this priority: explicit user decision > confirmed clarification > selected OpenSpec > existing diagram > agent assumption.
5. Tell the user when their description implies semantic diagram changes. If user intent conflicts with selected OpenSpec, show one consolidated conflict and pause. Do not silently choose one.
6. If no relevant specification exists, continue and record that fact. Never create or rewrite OpenSpec automatically from the diagram.

If the user supplies an existing role, actor, system, step, or stable element, reuse it rather than constructing a duplicate.

## Working artifacts

Create a run directory outside the skill installation, for example `.diagram-runs/<run-id>/`. Keep:

- `diagram-spec.json` — semantic working model and source references;
- `state.json` — resumable state and last accepted artifact;
- `run-manifest.jsonl` — append-only event ledger;
- patch proposals and candidate `.drawio` files;
- `validation-report.json`, captured stdout/stderr, and `validation-receipt.json`.

In an installed extension, resolve the extension root first and substitute the
absolute `<extension-root>/scripts/diagram_supervisor.py` path in the examples
below. Do not assume `scripts/` exists in the user's current directory.

Inspect without changing the source:

```bash
python3 scripts/diagram_supervisor.py inspect input.drawio --output <run-dir>/diagram-spec.json
python3 scripts/diagram_supervisor.py state <run-dir> analyzed --artifact input.drawio
```

## Executable end-to-end sequence

Run the following from the extension root. Set `INPUT`, `EDGE_ID`, and `ROLE_CLI` to real values. Every file passed to a later command is created by an earlier command in this sequence; the source diagram is never edited in place.

```bash
RUN_DIR=.diagram-runs/example-run
INPUT=input.drawio
EDGE_ID=edge-id
ROLE_CLI="$HOME/.gigacode/bin/gigacode"

python3 scripts/diagram_supervisor.py inspect "$INPUT" \
  --output "$RUN_DIR/diagram-spec.json"
python3 scripts/diagram_supervisor.py state "$RUN_DIR" analyzed \
  --artifact "$INPUT"
python3 scripts/diagram_supervisor.py validate "$INPUT" \
  --run-dir "$RUN_DIR" --attempt-id baseline

python3 scripts/diagram_supervisor.py state "$RUN_DIR" patching \
  --artifact "$INPUT"
python3 scripts/diagram_supervisor.py route-edge "$INPUT" "$EDGE_ID" \
  --output "$RUN_DIR/edge.patch.json"
python3 scripts/diagram_supervisor.py patch "$INPUT" "$RUN_DIR/edge.patch.json" \
  --output "$RUN_DIR/candidate.drawio" --result "$RUN_DIR/patch-result.json"
python3 scripts/diagram_supervisor.py state "$RUN_DIR" validating \
  --artifact "$INPUT"
python3 scripts/diagram_supervisor.py validate "$RUN_DIR/candidate.drawio" \
  --run-dir "$RUN_DIR" --attempt-id candidate

python3 scripts/diagram_supervisor.py review-input "$RUN_DIR" \
  "$RUN_DIR/candidate.drawio" \
  "$RUN_DIR/attempts/candidate/validation-report.json" \
  "$RUN_DIR/attempts/candidate/validation-receipt.json" \
  "$RUN_DIR/edge.patch.json" \
  --output "$RUN_DIR/reviewer-input.json"
python3 scripts/agent_runtime.py reviewer "$RUN_DIR/reviewer-input.json" \
  --cli "$ROLE_CLI" --run-dir "$RUN_DIR" \
  --output "$RUN_DIR/reviewer-verdict.json"

python3 scripts/diagram_supervisor.py candidate "$RUN_DIR" \
  "$RUN_DIR/candidate.drawio" \
  "$RUN_DIR/attempts/baseline/validation-report.json" \
  "$RUN_DIR/attempts/candidate/validation-report.json" \
  "$RUN_DIR/edge.patch.json" \
  "$RUN_DIR/attempts/baseline/validation-receipt.json" \
  "$RUN_DIR/attempts/candidate/validation-receipt.json" \
  --reviewer-verdict "$RUN_DIR/reviewer-verdict.json" \
  --repair-class edge-route
```

The Reviewer output must conform to `data/reviewer-verdict.v1.schema.json` and bind `run_id`, candidate hash, report hash, and receipt hash. A missing, rejected, or mismatched verdict cannot promote the candidate. The only bypass is an explicitly recorded `--review-exception approved_degraded_review` or `manual_handoff` decision; never use it silently in a normal run.

## Patch-only repair loop

Allowed patch operations are `set_edge_route`, `set_edge_pins`, `set_label_offset`, `move_vertex`, `resize_vertex`, `resize_container`, `add_semantic_element`, and `remove_semantic_element`. Every operation must identify the stable target ID, current precondition, proposed value, semantic effect, reason/finding IDs, and rollback data.

For a straight waypoint-free edge, first request a deterministic route patch:

```bash
python3 scripts/diagram_supervisor.py route-edge accepted.drawio edge-id \
  --finding-id finding-id --output edge.patch.json
python3 scripts/diagram_supervisor.py patch accepted.drawio edge.patch.json \
  --output candidate.drawio --result patch-result.json
```

Never edit the accepted baseline in place. A failed precondition or transaction leaves it unchanged.
Semantic operations are rejected unless a consolidated human decision was
recorded and the deterministic patch command is invoked with
`--allow-semantic`. Layout-only repair never uses that flag.

Before a semantic candidate can become the accepted baseline, bind the exact run, patch, candidate, and semantic diff to an explicit human decision:

```bash
python3 scripts/diagram_supervisor.py semantic-approval <run-dir> \
  accepted.drawio semantic-candidate.drawio semantic.patch.json \
  --decision approve --approver user \
  --output <run-dir>/semantic-approval.json
```

Pass that file to the candidate gate with `--semantic-approval`. A missing, rejected, or hash-mismatched approval stops promotion. Approval does not bypass patch replay, structural checks, validation receipts, untouched-region checks, or the independent Reviewer. After approval, the semantic candidate becomes the new baseline and later layout patches use its new semantic digest and quality epoch.

## Validation and monotonic acceptance

Validate each candidate and produce evidence:

```bash
python3 scripts/diagram_supervisor.py validate candidate.drawio \
  --run-dir <run-dir> --attempt-id attempt-001
python3 scripts/diagram_supervisor.py verify-receipt \
  <run-dir>/attempts/attempt-001/validation-receipt.json --artifact candidate.drawio
```

Compare baseline and candidate reports:

```bash
python3 scripts/diagram_supervisor.py compare baseline-report.json candidate-report.json
python3 scripts/diagram_supervisor.py candidate <run-dir> candidate.drawio \
  baseline-report.json candidate-report.json edge.patch.json \
  <run-dir>/attempts/baseline/validation-receipt.json \
  <run-dir>/attempts/attempt-001/validation-receipt.json \
  --reviewer-verdict <run-dir>/reviewer-verdict.json
```

The ordered vector is semantic violations, structural errors, route-through-node, container/lane violations, crossings, overlaps, routing uncertainty, text overflow, and route complexity. Route complexity prioritizes explicit bend count, then rounded route length. Compare lexicographically: the first changed category decides; no lower-priority gain can compensate for a higher-priority regression. Reject a candidate if semantics or untouched regions changed unexpectedly or nothing improves. A rejected candidate never becomes the baseline. Start every retry from the last accepted candidate and treat repeated hashes/vectors as cycle or plateau evidence. The `candidate` command verifies the patch, affected region, report and strict receipt, persists the decision, and automatically moves a repeated artifact hash, quality vector, exhausted repair class, or configured attempt limit to `plateau`.

A partial improvement may still have a non-zero strict validator exit because
some findings remain. Its receipt can be cryptographically valid while
`passed: false`; the candidate gate may accept it as the next baseline after a
lexicographic improvement. Only final `completed` requires `passed: true`.

Vision review is supplemental. It cannot override a deterministic failure or a mismatched receipt.

## Human checkpoints

Do not ask after every iteration. Pause only for:

- a conflict between user intent and a selected source specification;
- semantic additions, removals, or relationship changes;
- plateau, repeated cycle, unsupported structure, or agent confusion;
- final review.

Present semantic and layout diffs separately. At a checkpoint, offer continuation, approval, pause/resume, stop, manual handoff, or explicit approval with findings. A manual handoff returns the last accepted file, remaining findings, and receipt status so the user can finish by hand.
`manual_handoff` never promotes the pending unreviewed candidate.

Completion requires an exact successful strict receipt:

```bash
python3 scripts/diagram_supervisor.py state <run-dir> final_review --artifact accepted.drawio
python3 scripts/diagram_supervisor.py state <run-dir> completed \
  --artifact accepted.drawio --receipt <attempt>/validation-receipt.json \
  --decision approve
```

If the file changed after validation, completion fails and validation must run again.
