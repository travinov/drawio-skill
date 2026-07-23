---
description: Improve an existing draw.io diagram through monotonic validated agent iterations
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
Present the JSON result below faithfully, including the semantic comparison checkpoint,
working artifact, publishable candidate, role models, validation and review evidence,
and resume contract. A strict-failed working artifact is not strict success.
It may be returned only as `best_effort_completed` after deterministic structural,
receipt, hash, and semantic-safety checks. When no safe monotonic improvement is
available, preserve the original source byte-for-byte and report that disposition.
The source diagram remains unchanged until explicit final approval.
The host automatically continues bounded recoverable repair iterations; request
human continuation only when the returned JSON contains a real checkpoint.
If a role exhausts its command-line turn budget, report the saved runtime evidence
and do not recommend changing global `maxSessionTurns` or resuming without a checkpoint.
If `model_diversity_degraded` is true, identify the configured Supervisor or Repair fallback.

Normal use after `/drawio:review`, or when the workspace contains one `.drawio`:
`/drawio:improve`. The host reuses only a completed review whose artifact hash still
matches; otherwise it selects the only root-level diagram. Optional conversational
corrections remain supported: `/drawio:improve "requirements or corrections"`. Advanced form:
`--diagram "path/to/existing.drawio" --request "requirements or corrections"`.
`--intake-id`, repeatable `--intake-answer`, and
`--accept-intake-assumptions` are host replay flags supplied only by the
structured intake response.

```json
!{PYTHON=python3; if [ -n "$PYTHON_BIN" ]; then PYTHON="$PYTHON_BIN"; fi; GC_HOME="$HOME/.gigacode"; if [ -n "$GIGACODE_HOME" ]; then GC_HOME="$GIGACODE_HOME"; fi; EXTENSIONS="$GC_HOME/extensions"; if [ -n "$GIGACODE_EXTENSIONS_DIR" ]; then EXTENSIONS="$GIGACODE_EXTENSIONS_DIR"; fi; CLI="$GC_HOME/bin/gigacode"; if [ -n "$GIGACODE_BIN" ]; then CLI="$GIGACODE_BIN"; fi; DRAWIO_COMMAND_ARGS={{args}} "$PYTHON" "$EXTENSIONS/publish-drawio-skill/scripts/diagram_orchestrator.py" improve --workspace "$PWD" --cli "$CLI" 2>&1}
```
