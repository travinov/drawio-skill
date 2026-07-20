---
description: Resume the same draw.io agent run with a human decision or feedback
---

The deterministic host has applied the supplied human decision to the persisted run.
Do not call tools, agents, shell, directory search, or file-reading operations. Present
the result faithfully. A continuation is a new iteration from the last accepted candidate,
not a restarted generation.

Arguments: `--run "run-id-or-directory" --decision continue|approve|approve_with_findings|pause|stop|manual_handoff --feedback "optional notes"`.

```json
!{PYTHON=python3; if [ -n "$PYTHON_BIN" ]; then PYTHON="$PYTHON_BIN"; fi; GC_HOME="$HOME/.gigacode"; if [ -n "$GIGACODE_HOME" ]; then GC_HOME="$GIGACODE_HOME"; fi; EXTENSIONS="$GC_HOME/extensions"; if [ -n "$GIGACODE_EXTENSIONS_DIR" ]; then EXTENSIONS="$GIGACODE_EXTENSIONS_DIR"; fi; CLI="$GC_HOME/bin/gigacode"; if [ -n "$GIGACODE_BIN" ]; then CLI="$GIGACODE_BIN"; fi; "$PYTHON" "$EXTENSIONS/publish-drawio-skill/scripts/diagram_orchestrator.py" resume --workspace "$PWD" --cli "$CLI" {{args}} 2>&1}
```
