## Context

The installed extension exposes a reliable read-only `/drawio:review` command. That command creates a run directory, executes deterministic validation, and invokes an isolated Reviewer with model proof. The remaining registered roles are prompt definitions rather than a connected runtime: the interactive model can describe Supervisor, Semantic Analyst, and Repair work, but no command host guarantees that those roles run, that their outputs bind to the same candidate, or that a rejected iteration leaves the accepted diagram unchanged.

The corporate environment is offline with respect to public GitHub and uses GigaCode 26.5.17 with Qwen/Gemini-compatible extension commands. Diagram content and evidence must remain local. The existing `diagram_supervisor.py` already owns deterministic state transitions, DiagramSpec inspection, patch application, validation receipts, comparison, and append-only run evidence; `agent_runtime.py` already owns isolated model invocation and proof. The change connects these components without delegating XML mutation or publication to model prose.

## Goals / Non-Goals

**Goals:**

- Provide stable `/drawio:create`, `/drawio:improve`, `/drawio:resume`, and `/drawio:trace` commands while preserving `/drawio:review`.
- Run each activated role in an isolated GigaCode process using the configured model and record proof of the effective model.
- Convert user intent and available specifications into a structured DiagramSpec before deterministic generation or repair.
- Improve candidates monotonically from the last accepted artifact, with strict validation, independent review, cycle and plateau detection, and atomic publication.
- Pause only at consolidated semantic, plateau/confusion, and final checkpoints; resume the same run with user feedback, stop, or manual handoff.
- Make the full role/tool chain reconstructable from local, hash-bound evidence.

**Non-Goals:**

- Replacing draw.io with an online renderer or external MCP service.
- Allowing a role to write arbitrary diagram XML or publish without deterministic gates.
- Guaranteeing that every source format can use a specialized renderer in the first release; unsupported types use the generic deterministic graph renderer.
- Hiding runtime incompatibilities through same-session model fallback.
- Automatically overwriting an existing source diagram before explicit final acceptance.

## Decisions

### 1. One deterministic orchestration host owns the workflow

Add `diagram_orchestrator.py` as the executable host for create, improve, resume, and trace. It calls reusable functions from `diagram_supervisor.py` and `agent_runtime.py`; the command Markdown only passes arguments and presents `host-result.json`.

This keeps state transitions independent of the interactive model. Extending `diagram_host.py` was considered, but that file deliberately implements a small read-only review contract. Keeping it stable reduces regression risk and preserves a known-good diagnostic path.

### 2. Roles propose typed decisions; deterministic tools own mutations

Supervisor produces the next bounded workflow decision, Semantic Analyst produces a structured semantic plan, Repair produces `diagram-patch.v1`, and Reviewer produces a hash-bound verdict. The host validates every output schema before acting on it and consumes Supervisor `action`, `required_roles`, and `max_iterations`; incomplete or phase-incompatible plans fail closed. A deterministic renderer creates the initial `.drawio`; the existing patch engine applies later changes.

Direct model-generated XML was rejected because it cannot reliably preserve identifiers, untouched regions, or monotonic progress and makes validation evidence difficult to bind.

### 3. Runs are persisted state machines, not chat sessions

Every run lives under `<workspace>/.diagram-runs/<run-id>` and contains immutable inputs, attempts, role receipts, validation receipts, checkpoints, state, and an append-only manifest. Resume accepts a run id or run directory and a typed decision plus optional feedback. It never rebuilds context from chat history.

### 4. The last accepted candidate is the only iteration baseline

Each attempt starts from `accepted/current.drawio`. A proposed patch is applied to a new attempt directory, validated, compared, and reviewed. Only a candidate that satisfies the monotonic gate becomes the next accepted candidate. Rejected and invalid attempts remain evidence but cannot become a future baseline.

The quality vector is the existing ordered transactional-repair vector: semantic violations, structural errors, route-through-node, container/lane violations, edge crossings, overlaps, routing uncertainty, text overflow, and route complexity. Reviewer blocking findings are an acceptance gate bound to that comparison, not an additional reordered vector component. A candidate cannot improve a later component by regressing an earlier component.

### 5. Human interaction is consolidated and resumable

The host creates a checkpoint only when semantic changes need approval, automated progress plateaus or cycles, or a final accepted candidate is ready. `/drawio:resume` records one of `continue`, `approve`, `approve_with_findings`, `pause`, `stop`, or `manual_handoff` and continues from the stored state. Semantic continuation binds the immutable checkpoint, semantic-plan hash, exact approved change list, and human decision into Repair input. Stop and manual handoff are valid terminal outcomes and preserve the best candidate and evidence.

### 6. Model routing fails closed per activated role

The interactive model is not treated as the Supervisor. Each role invocation is a new isolated CLI process with the exact configured model identifier. The host records requested model, resolved model, provider, process mode, fallback state, model proof, input/output hashes, schema result, timestamps, and exit status. For lifecycle commands, missing, unavailable, or mismatched proof fails that role step; native or inherited execution is not accepted as a degraded substitute and the interactive model is never silently reused.

Successful invocations also retain raw CLI stdout. Trace re-parses that capture,
re-derives the typed output and effective-model proof, and compares it with the
configured routing policy, so rewriting and re-chaining manifest model fields
alone is detected. This is local evidence verification, not external
cryptographic attestation against an actor who can replace the policy and every
artifact in the run directory.

### 7. Creation uses a generic renderer with specialized adapters

Semantic Analyst output is normalized into a diagram plan with nodes, containers, and edges. A deterministic layered renderer provides a safe baseline for general flow, architecture, dependency, and process diagrams. Existing specialized generators remain selectable when the requested type and available structured input match their contracts. Both paths produce the same DiagramSpec/validation/evidence pipeline.

### 8. Publication is explicit and atomic

Create writes the requested target only after final approval. Improve keeps the original source unchanged and publishes through an atomic temporary-file replacement after approval. `approve_with_findings` publishes while recording unresolved findings. Stop or manual handoff never replaces the source.

## Risks / Trade-offs

- [Risk] Some corporate GigaCode models may return prose around JSON. → The isolated runtime accepts only a direct object or one unambiguous JSON fence without a second model call; all other output records a failed role receipt with its digest but does not enter the workflow.
- [Risk] A four-role loop may consume substantial tokens. → Invoke only roles required by the current state, cache hash-bound successful outputs, cap attempts, and stop at plateau rather than regenerating.
- [Risk] Generic layout cannot eliminate every crossing. → Use deterministic routing and waypoint validation, then let Repair propose bounded route/geometry patches; preserve manual handoff with exact findings.
- [Risk] Specialized generators have heterogeneous inputs. → Normalize their output into the common candidate/evidence contract and fall back to the generic renderer when no adapter matches.
- [Risk] Process interruption can leave a partial attempt. → Write attempts in isolated directories, append failure events, and change accepted/publication pointers only through atomic writes.
- [Risk] User feedback can contradict the initial specification. → Record feedback as a new immutable source with explicit priority, rerun semantic reconciliation, and request approval for resulting semantic changes.

## Migration Plan

1. Release as `1.23.0-corporate.1` on a new branch and ZIP; keep `1.22.0-corporate.6` available.
2. Add schemas, host, commands, prompts, tests, and verifier checks without changing `/drawio:review` behavior.
3. Install from the transferred local ZIP; the installer backs up the active extension before replacement.
4. Run extension self-check, command-host verification, and a corporate smoke run that proves different effective models for at least Supervisor and Reviewer.
5. Roll back with the bundled rollback script or reinstall the preserved `.6` ZIP if corporate runtime incompatibility is found.

## Open Questions

- Corporate smoke testing must confirm whether GigaCode 26.5.17 permits all four isolated role processes under one command duration limit.
- The first release will cap automatic repair attempts conservatively; the default can be tuned from real trace evidence without changing the state contract.
