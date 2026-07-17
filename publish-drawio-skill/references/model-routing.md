# Per-role model routing

The bundled `gemini-extension.json` and `agents/*.md` use the locally verified
Gemini CLI extension/subagent contract as the compatibility baseline for
GigaCode forks. Validate the package with
`gemini extensions validate <extension-path>` before installation.

The Diagram Supervisor uses portable role prompts and a runtime-neutral routing policy instead of assuming one GigaCode agent-manifest format. GigaCode builds can inherit different agent and model interfaces from Qwen CLI or Gemini CLI, so an adapter must detect capabilities before starting a role.

Use `data/model-routing.default.json` as the default policy and validate custom policies against `data/model-routing.v1.schema.json`. The default assignments are:

| Role | Requested model | Activation | Mutation policy |
| --- | --- | --- | --- |
| Supervisor | `GigaChat-3-Ultra` | permanent | orchestration only |
| Reviewer | `DeepSeek-V4-Flash` | permanent | read-only |
| Repair | `vllm/MiniMax-M3-113k` | on demand | patch proposal only |
| Semantic Analyst | `vllm/Qwen3.6-35B-262k` | on demand | patch proposal only |

A normal layout run starts Supervisor and Reviewer. Start Repair only when structured findings need a patch proposal. Start Semantic Analyst only for process reconciliation, semantic ambiguity, or a conflict involving user input or OpenSpec.

## Resolution order

Resolve every role independently in this order:

1. `native_per_agent`
2. `isolated_cli`
3. `inherited_current`

Never issue the interactive `/model` command as part of agent startup. The adapter must not silently change the model used by the user's main GigaCode session.

### Native per-agent override

First inspect the installed runtime's supported agent configuration or capability output. If it supports a model on each subagent/agent definition, use the verified bundled Gemini declaration or translate the portable role prompt and requested model into the fork's native declaration. Do not assume that every GigaCode build implements the full Gemini contract.

Record the native model identifier returned by the runtime, not only the requested alias. If the runtime accepts the declaration but resolves a different provider/model, that is a fallback.

### Isolated CLI invocation

If native role-specific models are unavailable, use `scripts/agent_runtime.py`
to start a separate non-interactive process with the role's requested model.
The adapter probes the CLI for the required headless/model/output/approval
flags, builds an argument array, enforces plan/read-only approval mode, bounds
runtime, captures output, and never executes model output. Never concatenate a
shell command and never interpolate diagram labels, XML, IDs, user text, or
links.

The conceptual shape is:

```json
["<cli-executable>", "<non-interactive-flag>", "<prompt-input>", "<model-flag>", "<requested-model>"]
```

Dry-run the locally installed compatibility path before use:

```bash
python3 scripts/agent_runtime.py reviewer reviewer-input.json \
  --output reviewer-output.json --cli gigacode --dry-run
```

The bundled adapter implements the Gemini-compatible flags (`--model`,
`--prompt`, `--output-format`, `--approval-mode`). A GigaCode/Qwen build with
different flags requires a small runtime adapter extension; it must not guess.

Use a bounded timeout, explicit working directory, captured stdout/stderr, and a minimal allowlisted environment. Do not inherit arbitrary API keys, tokens, passwords, or unrelated process secrets. The Reviewer process must receive read-only inputs and no artifact-publication capability. A model response is data for the Supervisor or deterministic tools; it is not permission to run response text as a command.

Publish role output atomically only after the process exits successfully and its JSON conforms to the role schema: Reviewer uses `reviewer-verdict.v1.schema.json`, Repair uses `diagram-patch.v1.schema.json`, and Supervisor/Semantic Analyst use `agent-role-output.v1.schema.json`. Stock Gemini JSON output is an outer envelope: reject non-empty `error`/`errors`, extract and JSON-decode `response`, then validate only that inner role payload. Preserve redacted envelope stats/errors as runtime evidence, not as the verdict. Direct top-level role JSON remains an intentional compatibility mode for GigaCode/Qwen forks and test adapters. On timeout, non-zero exit, or invalid output, append `role_failed`; do not create the output file, `model_resolved`, `review_verdict`, or `patch_proposed` success events.

### Inherited-model degradation

If neither native override nor isolated invocation is available, or the requested isolated model is explicitly reported unavailable, run the role with the current session model without changing it. Set `resolution_mode` to `inherited_current`, set `fallback_used` to `true`, and record why the requested model was unavailable. Record the actual current model and provider; use provider `unknown` when the runtime cannot report it, never the requested provider.

The user-visible result must state that model diversity was degraded. This is especially important when Reviewer resolves to the same model as Supervisor. Deterministic validation is still authoritative, but the run must not describe the review as model-independent.

## Stock Gemini recursion boundary

Gemini extension subagents cannot invoke other subagents. Therefore the main
extension host owns orchestration on stock Gemini; `diagram-supervisor` can
return an orchestration decision but cannot directly hire Reviewer, Repair, or
Semantic Analyst. The host invokes those roles natively as siblings or through
`agent_runtime.py`. GigaCode-only model aliases in the default policy are not
claimed to run on stock Gemini; unsupported models must resolve to an explicit
fallback/degradation record.

## Resolution record

Append one `model_resolved` event per activated role to `run-manifest.jsonl`. The event payload must include:

```json
{
  "role": "reviewer",
  "requested_model": "DeepSeek-V4-Flash",
  "resolved_model": "<runtime-returned-model-id>",
  "provider": "<resolved-provider>",
  "resolution_mode": "native_per_agent",
  "fallback_used": false,
  "degradation_reason": null
}
```

Do not infer success from the requested value. A resolution is complete only after the role process succeeds, its output validates, and the runtime reports or the adapter can otherwise prove which model was used. Append `model_resolved` only at that point.

## Portable prompts

Role prompts live in `agents/` and carry runtime-neutral front matter. An adapter may translate that metadata into native configuration, but the normative permissions remain:

- Supervisor coordinates tools and state but does not edit raw XML.
- Reviewer is read-only and returns findings/verdict only.
- Repair returns a patch proposal only.
- Semantic Analyst returns source reconciliation and, after approval, a semantic patch proposal only.

Deterministic tools remain responsible for XML parsing, patch application, routing, validation, quality comparison, hashing, receipts, and publication regardless of which model or runtime adapter is selected.
