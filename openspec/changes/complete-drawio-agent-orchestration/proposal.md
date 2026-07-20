## Why

The extension currently proves only a read-only validation and isolated Reviewer path; registered Supervisor, Semantic Analyst, and Repair roles are not connected to an executable end-to-end workflow. Users need one auditable local command surface that can create or iteratively improve different draw.io diagram types, resume after human feedback, and show every role and tool invocation without restarting from random regeneration.

## What Changes

- Add deterministic `/drawio:create`, `/drawio:improve`, `/drawio:resume`, and `/drawio:trace` command hosts alongside `/drawio:review`.
- Execute configured Supervisor, Semantic Analyst, Repair, and Reviewer roles through isolated per-role model routing with model proof and hash-bound inputs and outputs.
- Connect DiagramSpec construction, deterministic generation or patch application, strict validation, monotonic candidate comparison, independent review, cycle/plateau detection, and atomic publication into one resumable state machine.
- Preserve the last accepted candidate across iterations; never replace it with an unvalidated regeneration or rejected patch.
- Add consolidated human checkpoints for semantic approval, plateau/confusion, final acceptance, stop, and manual handoff.
- Add complete role/tool invocation receipts and a trace command that reconstructs the run from start to terminal state.
- Keep all diagram data local and preserve the existing read-only review command.

## Capabilities

### New Capabilities

- `diagram-agent-command-surface`: Define executable create, improve, resume, and trace commands and their stable user-facing contracts.

### Modified Capabilities

- `diagram-supervisor-orchestration`: Require the persisted state machine to execute the complete multi-role workflow rather than expose disconnected building blocks.
- `diagram-model-routing`: Require model-proven isolated execution for every activated role and distinguish the interactive host from the Supervisor role.
- `diagram-run-evidence`: Require start/finish/failure receipts for every role and deterministic tool step plus a verifiable consolidated trace.
- `diagram-transactional-repair`: Connect patch proposal, deterministic application, validation, comparison, review, acceptance, and the next iteration.
- `diagram-human-review`: Make human decisions resumable command inputs and preserve stop/manual-handoff outcomes.
- `diagram-working-model`: Support creation from user intent and selected specifications as well as import and reconciliation of existing diagrams.

## Impact

- Affects the draw.io command host, role runtime, Supervisor state engine, schemas, agent prompts, commands, tests, installer/verifier contracts, documentation, and release archive.
- Adds no external SaaS or MCP dependency; execution remains local to the corporate GigaCode CLI and bundled deterministic tools.
- Preserves `.drawio` source files until a candidate passes the configured publication gate or the user explicitly accepts a handoff.
