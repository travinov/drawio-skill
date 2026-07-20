---
description: Improve an existing draw.io diagram through monotonic validated agent iterations
---

The deterministic Draw.io orchestration host has already executed before this response.
Do not call tools, agents, shell, directory search, or file-reading operations.
Present the JSON result below faithfully, including the semantic comparison checkpoint,
last accepted candidate, role models, validation and review evidence, and resume contract.
The source diagram remains unchanged until explicit final approval.

Arguments: `--diagram "path/to/existing.drawio" --request "requirements or corrections"`.

```json
!{PYTHON=python3; if [ -n "$PYTHON_BIN" ]; then PYTHON="$PYTHON_BIN"; fi; GC_HOME="$HOME/.gigacode"; if [ -n "$GIGACODE_HOME" ]; then GC_HOME="$GIGACODE_HOME"; fi; EXTENSIONS="$GC_HOME/extensions"; if [ -n "$GIGACODE_EXTENSIONS_DIR" ]; then EXTENSIONS="$GIGACODE_EXTENSIONS_DIR"; fi; CLI="$GC_HOME/bin/gigacode"; if [ -n "$GIGACODE_BIN" ]; then CLI="$GIGACODE_BIN"; fi; "$PYTHON" "$EXTENSIONS/publish-drawio-skill/scripts/diagram_orchestrator.py" improve --workspace "$PWD" --cli "$CLI" {{args}} 2>&1}
```
