# Per-role model routing

The bundled `gemini-extension.json` is converted by corporate GigaCode into an
active `gigacode-extension.json`. GigaCode 26.5.17 identifies its engine as Qwen
Code 0.13.1. It discovers `agents/*.md`, but its agent wizard exposes no model
selection and observed native agents inherit the interactive model. Exact role
models therefore live in the routing policy; native agent frontmatter uses
`model: inherit` and is not model-resolution evidence.

The Diagram Supervisor uses portable role prompts and a runtime-neutral routing policy instead of assuming one GigaCode agent-manifest format. GigaCode builds can inherit different agent and model interfaces from Qwen CLI or Gemini CLI, so an adapter must detect capabilities before starting a role.

Use `data/model-routing.default.json` as the default policy and validate custom policies against `data/model-routing.v1.schema.json`. The default assignments are:

| Role | Requested model | Activation | Mutation policy |
| --- | --- | --- | --- |
| Supervisor | `GigaChat-3-Ultra` | permanent | orchestration only |
| Reviewer | `vllm/DeepSeek-V4-Flash-262k` | permanent | read-only |
| Repair | `vllm/MiniMax-M3-113k` | on demand | patch proposal only |
| Semantic Analyst | `vllm/Qwen3.6-35B-262k` | on demand | patch proposal only |

A normal layout run starts Supervisor and Reviewer. Start Repair only when structured findings need a patch proposal. Start Semantic Analyst only for process reconciliation, semantic ambiguity, or a conflict involving user input or OpenSpec.

On corporate GigaCode 26.5.17 the top-level `diagram-supervisor` itself inherits
the interactive model. To use GigaChat Ultra for that role, select
`GigaChat-3-Ultra` once before starting the diagram task. The Supervisor then
keeps that session model unchanged while Reviewer, Repair, and Semantic Analyst
run in isolated processes with their policy models. If the task starts under a
different main model, report the Supervisor as inherited-model degradation;
never claim it ran on GigaChat merely because the policy requested it.

## Resolution order

Resolve every role independently in this order:

1. `isolated_cli`
2. `native_per_agent`
3. `inherited_current`

Never issue the interactive `/model` command as part of agent startup. The adapter must not silently change the model used by the user's main GigaCode session.

### Native per-agent override

Use native per-agent routing only when the installed runtime exposes a model
selector and reports the resolved model. The `model:` line in an extension
agent file is never sufficient evidence. Corporate GigaCode 26.5.17 does not
meet this gate and must not use this mode for model diversity.

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

The verified GigaCode 26.5.17 contract supports `--model`, `--prompt`,
`--output-format json`, `--approval-mode plan`, and `--auth-type gigacode`.
The adapter adds the corporate auth type when the CLI help identifies GigaCode,
without changing the interactive session.

Use a bounded timeout, explicit working directory, captured stdout/stderr, and a minimal allowlisted environment. Do not inherit arbitrary API keys, tokens, passwords, or unrelated process secrets. The Reviewer process must receive read-only inputs and no artifact-publication capability. A model response is data for the Supervisor or deterministic tools; it is not permission to run response text as a command.

Publish role output atomically only after the process exits successfully and its JSON conforms to the role schema: Reviewer uses `reviewer-verdict.v1.schema.json`, Repair uses `diagram-patch.v1.schema.json`, and Supervisor/Semantic Analyst use `agent-role-output.v1.schema.json`. GigaCode JSON output is an event array: require one consistent primary model in the `system` init event, every assistant message, and `result.stats.models`; extract and JSON-decode the final result payload. Stock Gemini JSON output is an outer envelope: reject non-empty `error`/`errors`, extract and JSON-decode `response`, then validate only that inner role payload. Preserve redacted runtime stats/errors as evidence, not as the verdict. Direct top-level role JSON may be parsed for compatibility but cannot publish a successful isolated role because it lacks model proof. On timeout, non-zero exit, invalid output, missing proof, or model mismatch, append `role_failed`; do not create the output file, `model_resolved`, `review_verdict`, or `patch_proposed` success events.

### Inherited-model degradation

If neither native override nor isolated invocation is available, or the requested isolated model is explicitly reported unavailable, run the role with the current session model without changing it. Set `resolution_mode` to `inherited_current`, set `fallback_used` to `true`, and record why the requested model was unavailable. Record the actual current model and provider; use provider `unknown` when the runtime cannot report it, never the requested provider.

The user-visible result must state that model diversity was degraded. This is especially important when Reviewer resolves to the same model as Supervisor. Deterministic validation is still authoritative, but the run must not describe the review as model-independent.

## Host and recursion boundary

Corporate Qwen Code 0.13.1 cannot be trusted to provide nested native agents or
native model diversity. The main extension host or `diagram-supervisor` invokes
Reviewer, Repair, and Semantic Analyst through `agent_runtime.py` using the
trusted absolute extension path. Stock Gemini uses the same host boundary.
Unsupported models must resolve to an explicit fallback/degradation record.

## Resolution record

Append one `model_resolved` event per activated role to `run-manifest.jsonl`. The event payload must include:

```json
{
  "role": "reviewer",
  "requested_model": "vllm/DeepSeek-V4-Flash-262k",
  "resolved_model": "<runtime-returned-model-id>",
  "provider": "<resolved-provider>",
  "resolution_mode": "isolated_cli",
  "fallback_used": false,
  "degradation_reason": null
}
```

Do not infer success from the requested value. A resolution is complete only after the role process succeeds, its output validates, and the runtime reports or the adapter can otherwise prove which model was used. Append `model_resolved` only at that point.

`/stats model` in the parent session describes the parent process and is not
proof for an isolated role. Inspect `run-manifest.jsonl` instead: a successful
role has a `model_resolved` event followed by `review_verdict` or
`patch_proposed`; the latter contains `runtime_metadata.model_proof` with the
matching `system_model`, `assistant_model`, and `stats_models`. A `role_failed`
event means no role output was published.

## Portable prompts

Role prompts live in `agents/` and carry runtime-neutral front matter. An adapter may translate that metadata into native configuration, but the normative permissions remain:

- Supervisor coordinates tools and state but does not edit raw XML.
- Reviewer is read-only and returns findings/verdict only.
- Repair returns a patch proposal only.
- Semantic Analyst returns source reconciliation and, after approval, a semantic patch proposal only.

Deterministic tools remain responsible for XML parsing, patch application, routing, validation, quality comparison, hashing, receipts, and publication regardless of which model or runtime adapter is selected.
