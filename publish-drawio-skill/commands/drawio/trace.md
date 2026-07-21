---
description: Verify and show the complete local role, model, tool, validation, and decision chain of a draw.io run
---

The deterministic trace verifier has already read the run. Do not call tools, agents,
shell, directory search, or file-reading operations. Present every role and effective
model plus any broken event or artifact binding. Never call an incomplete or tampered
trace verified.
`failed_verified` means failure evidence is intact, not that the diagram workflow
succeeded; present its failed role, capture paths, isolation proof, and diagnostic.
Treat a nonterminal Supervisor `role_failed` followed by an approved fallback
`role_finished` as recovered evidence, not as a hidden failure; report
`model_diversity_degraded` explicitly.

Normal use: `/drawio:trace` selects the most recently updated workflow.
Advanced form: `--run "run-id-or-directory"`.

```json
!{PYTHON=python3; if [ -n "$PYTHON_BIN" ]; then PYTHON="$PYTHON_BIN"; fi; GC_HOME="$HOME/.gigacode"; if [ -n "$GIGACODE_HOME" ]; then GC_HOME="$GIGACODE_HOME"; fi; EXTENSIONS="$GC_HOME/extensions"; if [ -n "$GIGACODE_EXTENSIONS_DIR" ]; then EXTENSIONS="$GIGACODE_EXTENSIONS_DIR"; fi; DRAWIO_COMMAND_ARGS={{args}} "$PYTHON" "$EXTENSIONS/publish-drawio-skill/scripts/diagram_orchestrator.py" trace --workspace "$PWD" 2>&1}
```
