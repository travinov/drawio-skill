## Context

`publish-drawio-skill` already contains generators and a strict structural/layout validator. The missing layer is orchestration: preserving an existing artifact, reconciling it with user/OpenSpec sources, producing deterministic local repairs, independently reviewing each accepted candidate, and proving that the final artifact is the one that passed validation.

The extension must remain local-first and compatible with GigaCode installations that are derived from Qwen CLI or Gemini CLI. Those runtimes differ in how they declare subagents and model overrides, so the deterministic workflow cannot depend on one proprietary agent manifest. Model prompts and routing policy will be portable data; a runtime adapter may translate that policy into native subagents or isolated headless CLI invocations.

## Goals / Non-Goals

**Goals:**

- Preserve existing draw.io XML and stable `mxCell` identifiers while making small reversible repairs.
- Maintain a canonical semantic working model and source provenance without serializing the whole diagram from scratch.
- Accept only sequential improvements from the last accepted candidate.
- Separate supervision, independent review, repair, and semantic arbitration roles, with per-role model selection and recorded fallbacks.
- Produce machine-verifiable validation evidence bound to the exact final file.
- Ask the user only at meaningful semantic/conflict/plateau/final-review checkpoints and retain resumable state.

**Non-Goals:**

- Replacing every existing generator or validator in the first release.
- Treating vision/model judgment as a substitute for deterministic structural validation.
- Automatically rewriting OpenSpec documents from a diagram.
- Guaranteeing optimal global layout for every multi-page, nested, compressed, or heavily customized draw.io document in the MVP.
- Requiring external SaaS, online renderers, or a specific model provider.

## Decisions

### Original XML remains the rendering source of truth

The extension imports a diagram into `DiagramSpec`, but `DiagramSpec` is a semantic/working sidecar rather than a replacement renderer. Patch operations mutate only addressed cells and geometry in a copied candidate. Unknown attributes, styles, pages, layers, and metadata remain intact.

Alternative considered: regenerate XML from `DiagramSpec`. Rejected because it would discard unsupported draw.io features and make every retry a potentially unrelated result.

### Repairs use an explicit transaction log

Each patch operation contains a stable target ID, precondition, proposed value, reason/finding IDs, semantic effect, and rollback data. The patcher applies all operations to a temporary candidate, verifies preconditions and semantic digest, then atomically publishes or rejects the candidate.

Alternative considered: let a model edit raw XML. Rejected because raw edits are difficult to audit, unsafe to roll back, and prone to semantic drift.

### Acceptance is lexicographic and monotonic

Reports are normalized into this ordered quality vector: semantic violations, structural errors, route-through-node, container/lane violations, edge crossings, overlaps, routing uncertainty, text overflow, and route complexity. A candidate is accepted only when its semantic digest and untouched-region invariant hold, no higher-priority category worsens, and at least one category improves. The next iteration starts from the last accepted candidate; rejected hashes and repeated quality vectors are cycle evidence.

Alternative considered: minimize a weighted score. Rejected because a score could trade a serious semantic or structural regression for many cosmetic improvements.

### Deterministic tools own artifact mutation and evidence

Models propose intent and review results. Local tools parse XML, apply patches, route edges, invoke validators, compare candidates, hash artifacts, and append run events. Shell commands use argument arrays and never interpolate diagram labels or IDs. XML input limits and untrusted HTML/link handling are enforced at the boundary.

### Logical roles map to two permanent and two on-demand agents

The Supervisor (`GigaChat-3-Ultra`) and independent Reviewer (`DeepSeek-V4-Flash`) are active during a normal run. Repair (`vllm/MiniMax-M3-113k`) is started only when deterministic findings require a proposed patch. Semantic Analyst/Arbiter (`vllm/Qwen3.6-35B-262k`) is started only for source reconciliation, OpenSpec conflicts, or semantic ambiguity.

The runtime resolves a role-specific model override first. If native per-agent overrides are unavailable, it uses an isolated non-interactive CLI process with an explicit model argument. If neither is available, the role inherits the current model and records a degraded routing event. The global interactive `/model` state is never silently changed.

On runtimes such as stock Gemini CLI that prohibit subagent recursion, the main
extension host owns orchestration and invokes the four logical roles as sibling
subagents or isolated headless processes. The Supervisor subagent returns the
next orchestration decision to that host; it does not recursively call the
other roles. `scripts/agent_runtime.py` implements the Gemini-compatible
isolated fallback and probes required flags before execution.

### Source reconciliation is explicit and ordered

Priority is: explicit user decision, confirmed clarification, selected OpenSpec, existing diagram, then agent assumption. Every source reference records kind, URI, revision, fragment, content hash, and confidence. A conflict between current user intent and a selected OpenSpec pauses for one consolidated decision. Absence of a relevant OpenSpec does not block the run.

### Evidence is append-only and hash-bound

The run manifest uses JSON Lines so each event can be appended without rewriting history. A final validation receipt records the exact command, exit code, validator identity/hash, artifact SHA-256, report/stdout/stderr hashes, timestamps, platform, and tool versions. `completed` is invalid unless the receipt artifact hash equals the current final file hash and strict validation succeeded.

### Human review is sparse and resumable

The supervisor pauses only for source conflicts, proposed semantic changes or deletions, plateau/confusion, and final review. The user can approve, continue, stop, pause/resume, accept with findings, or take over manually. Layout-only patches may proceed after a consolidated notice. State and accepted baseline hashes are persisted so clarification continues the same run.

## Risks / Trade-offs

- [GigaCode agent manifest formats differ across forks] → Keep role prompts and routing policy runtime-neutral, add an adapter contract, and record fallback behavior.
- [Local routing can improve one area while damaging another] → Use preconditions, untouched-region checks, semantic digests, ordered comparison, and atomic rejection.
- [Validator findings may not expose enough geometry for automatic repair] → Extend reports additively to v2 while retaining legacy fields and remediation classes.
- [Large or compressed draw.io documents can exhaust resources] → Apply file, page, cell, XML-depth, and decompression limits; unsupported cases end in manual handoff without modifying the original.
- [Independent models can still agree on a poor result] → Deterministic validation and receipts remain authoritative; model review is an additional gate, not proof.
- [JSONL is less convenient for consumers expecting one JSON object] → Provide a summary command/schema while retaining JSONL as the immutable event ledger.

## Migration Plan

1. Add schemas, role prompts, routing defaults, and deterministic tools without changing existing commands.
2. Extend validation reports additively and keep legacy `element` alongside v2 `elements`.
3. Add supervisor documentation and opt-in CLI entry points.
4. Verify existing validator/generator tests plus new import, patch, monotonicity, receipt, and resume tests.
5. Enable the supervisor workflow as an extension path. Existing direct generation remains available.

Rollback consists of removing the new extension entry points and data files; existing generators and validator remain usable because no destructive migration is required.

## Open Questions

- Which native subagent manifest schema the installed GigaCode build exposes; the runtime adapter will select Qwen/Gemini-compatible behavior after capability detection.
- Whether later releases should add a full global orthogonal router or delegate complex multi-page/grouped diagrams to a dedicated layout engine.
