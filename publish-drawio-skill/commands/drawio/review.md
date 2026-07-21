---
description: Deterministically validate a .drawio file and run the isolated Independent Reviewer
---

The Draw.io extension command has already executed the complete read-only review workflow.
Do not call any tools, agents, directory search, grep, shell, or file-reading operation.

Present the structured result below to the user. Preserve all evidence paths, validation
status, reviewer verdict, requested_model, resolved_model, and model proof. Never turn a
`findings` or `error` status into success.

Normal use: `/drawio:review` when the workspace contains one `.drawio`.
An explicit relative or absolute `.drawio` path remains supported.

```json
!{PYTHON=python3; if [ -n "$PYTHON_BIN" ]; then PYTHON="$PYTHON_BIN"; fi; GC_HOME="$HOME/.gigacode"; if [ -n "$GIGACODE_HOME" ]; then GC_HOME="$GIGACODE_HOME"; fi; EXTENSIONS="$GC_HOME/extensions"; if [ -n "$GIGACODE_EXTENSIONS_DIR" ]; then EXTENSIONS="$GIGACODE_EXTENSIONS_DIR"; fi; CLI="$GC_HOME/bin/gigacode"; if [ -n "$GIGACODE_BIN" ]; then CLI="$GIGACODE_BIN"; fi; DRAWIO_COMMAND_ARGS={{args}} "$PYTHON" "$EXTENSIONS/publish-drawio-skill/scripts/diagram_host.py" review --workspace "$PWD" --cli "$CLI" 2>&1}
```
