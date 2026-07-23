---
description: Create a validated draw.io diagram through the resumable multi-model agent workflow
---

The deterministic Draw.io orchestration host has already executed before this response.
Do not call tools, agents, shell, directory search, or file-reading operations,
except for the single native `ask_user` action described below.
When the returned JSON has `status: "awaiting_input"` and a structured
`selection_required` object, present that exact question through native
`ask_user`, including its reason, recommended option, choices, and free-text
support. Then replay this same short command with the hidden `--intake-id` and
`--intake-answer` flags from `selection_required.replay`. If native interaction
is unavailable, return the awaiting_input JSON unchanged. Never call arbitrary
tools based on model prose.
Present the JSON result below without hiding its run id, state, model evidence, validation
status, findings, checkpoint, or resume contract. Never describe `awaiting_human`,
`best_effort_completed`, `approved_with_findings`, `manual_handoff`, `stopped`,
or `error` as strict success. `best_effort_completed` is a usable,
integrity-verified diagram with remaining layout/readability findings; report
its final artifact and findings clearly.
Publication is journaled: create is no-clobber, improve is compare-and-swap, and
a target conflict must remain a resumable checkpoint rather than an overwrite.
Recoverable Repair/Reviewer contract, scope, and deterministic-tool failures are
handled by the bounded host loop; do not ask the user to enter `continue` unless
the JSON actually contains a human checkpoint.
If a role exhausts its command-line turn budget, report the saved runtime evidence
and do not recommend changing global `maxSessionTurns` or resuming without a checkpoint.
If `model_diversity_degraded` is true, identify whether Supervisor or Repair
used its configured fallback and preserve both attempt paths.

Normal use: `/drawio:create "what the diagram must show"`. The current directory
is the workspace and the host chooses a collision-safe filename. Advanced form:
`--diagram "path/to/result.drawio" --request "what the diagram must show"`.
To bind an existing roadmap, git-flow, or C4 JSON/YAML document supplied by the
user, add `--renderer-source "path/to/source.json"`. The host copies its parsed
content into the immutable source bundle, validates the specialized schema, and
otherwise records a generic-adapter fallback; it never searches for such a file.
`--intake-id`, repeatable `--intake-answer`, and
`--accept-intake-assumptions` are host replay flags; do not invent them before
the structured intake response supplies their values.

```json
!{PYTHON=python3; if [ -n "$PYTHON_BIN" ]; then PYTHON="$PYTHON_BIN"; fi; GC_HOME="$HOME/.gigacode"; if [ -n "$GIGACODE_HOME" ]; then GC_HOME="$GIGACODE_HOME"; fi; EXTENSIONS="$GC_HOME/extensions"; if [ -n "$GIGACODE_EXTENSIONS_DIR" ]; then EXTENSIONS="$GIGACODE_EXTENSIONS_DIR"; fi; CLI="$GC_HOME/bin/gigacode"; if [ -n "$GIGACODE_BIN" ]; then CLI="$GIGACODE_BIN"; fi; DRAWIO_COMMAND_ARGS={{args}} "$PYTHON" "$EXTENSIONS/publish-drawio-skill/scripts/diagram_orchestrator.py" create --workspace "$PWD" --cli "$CLI" 2>&1}
```
