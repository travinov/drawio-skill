# Why

The current draw.io skill can generate and validate diagrams, but it does not provide a durable orchestration loop that proves validation was executed, preserves an existing diagram during repair, or guarantees that every iteration improves the last accepted candidate. Model-only regeneration also makes layout changes non-deterministic and can silently alter diagram semantics.

The skill needs an extension layer that separates semantic analysis, deterministic diagram tools, independent review, repair, evidence collection, and human decisions. This layer must reuse existing diagrams and OpenSpec requirements when available, while allowing the user to approve, continue, stop, or take over manually without losing progress.

# What Changes

- Add a Diagram Supervisor workflow with explicit states for analysis, patching, validation, retries, plateau handling, final review, completion, stop, and manual handoff.
- Add a canonical `DiagramSpec` working model that records diagram semantics, stable element identities, source references, assumptions, and a semantic digest without replacing the original draw.io XML.
- Add transactional, preconditioned patch operations for local geometry and routing repair. Existing diagrams are modified in place through small reversible operations; rejected candidates never become the next baseline.
- Add deterministic candidate comparison using an ordered quality vector so that an iteration is accepted only when it preserves semantics and improves at least one validation category without worsening a higher-priority category.
- Add validation report v2, cryptographic validation receipts, and an append-only run manifest linking artifacts, commands, model selections, approvals, attempts, and final hashes.
- Add per-agent model routing with independent reviewer diversity and explicit fallback/degradation recording. Default mapping: GigaChat-3-Ultra for supervision, DeepSeek-V4-Flash for review, `vllm/MiniMax-M3-113k` for repair, and `vllm/Qwen3.6-35B-262k` for semantic analysis and arbitration.
- Add human-in-the-loop checkpoints only for source conflicts, semantic changes, plateaus/confusion, and final review. The user can continue, approve, stop, pause/resume, or switch to manual handoff.
- Integrate the workflow into the existing draw.io skill, documentation, agent definitions, fixtures, and tests while preserving existing generator and validator entry points.

# Capabilities

## New Capabilities

- `diagram-supervisor-orchestration`: Coordinate agent roles, deterministic tools, iteration state, retry/plateau behavior, and terminal outcomes.
- `diagram-working-model`: Import existing draw.io artifacts into a source-linked `DiagramSpec` with stable identities and semantic integrity checks.
- `diagram-transactional-repair`: Apply reversible, preconditioned local patches and accept only monotonic improvements from the last accepted candidate.
- `diagram-run-evidence`: Produce validation report v2, hash-bound receipts, and append-only run manifests that prove which artifact was validated.
- `diagram-model-routing`: Resolve per-agent model assignments, isolated fallbacks, and degradation metadata without relying on a shared global model switch.
- `diagram-human-review`: Present consolidated semantic/layout changes and support approval, continuation, stop, pause/resume, and manual handoff.

## Modified Capabilities

- `drawio-artifact-validation`: Extend machine-readable findings with multi-element geometry/remediation metadata while preserving compatibility, and bind successful validation to the exact final artifact through a receipt.

# Impact

- Updates `publish-drawio-skill/SKILL.md` and supporting references with the supervisor workflow and human decision policy.
- Adds schemas, default model routing, agent role definitions, deterministic supervisor/patch/evidence scripts, and test fixtures under `publish-drawio-skill/`.
- Extends validation output additively; existing validator commands and existing diagram generator commands remain supported.
- Introduces local run artifacts such as `DiagramSpec`, candidate files, validation reports, receipts, and manifests. No external service is required for the deterministic MVP.
- Preserves existing draw.io XML, page/layer/style metadata, and stable `mxCell` IDs whenever a diagram is repaired.
