---
description: Create a validated draw.io diagram through the resumable multi-model agent workflow
---

The deterministic Draw.io orchestration host has already executed before this response.
Do not call tools, agents, shell, directory search, or file-reading operations.
Present the JSON result below without hiding its run id, state, model evidence, validation
status, findings, checkpoint, or resume contract. Never describe `awaiting_human`,
`approved_with_findings`, `manual_handoff`, `stopped`, or `error` as strict success.

Arguments: `--diagram "path/to/result.drawio" --request "what the diagram must show"`.

```json
!{PYTHON=python3; if [ -n "$PYTHON_BIN" ]; then PYTHON="$PYTHON_BIN"; fi; GC_HOME="$HOME/.gigacode"; if [ -n "$GIGACODE_HOME" ]; then GC_HOME="$GIGACODE_HOME"; fi; EXTENSIONS="$GC_HOME/extensions"; if [ -n "$GIGACODE_EXTENSIONS_DIR" ]; then EXTENSIONS="$GIGACODE_EXTENSIONS_DIR"; fi; CLI="$GC_HOME/bin/gigacode"; if [ -n "$GIGACODE_BIN" ]; then CLI="$GIGACODE_BIN"; fi; "$PYTHON" "$EXTENSIONS/publish-drawio-skill/scripts/diagram_orchestrator.py" create --workspace "$PWD" --cli "$CLI" {{args}} 2>&1}
```
