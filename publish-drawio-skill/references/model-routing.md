# Per-role model routing

The bundled `gemini-extension.json` is converted by corporate GigaCode into an
active `gigacode-extension.json`. GigaCode 26.5.17 identifies its engine as Qwen
Code 0.13.1. It discovers `agents/*.md`, but its agent wizard may not expose the
effective model. Exact role models are declared consistently in the routing
policy and agent frontmatter, while runtime system/assistant/stats proof remains
the only execution evidence.

The Diagram Supervisor uses portable role prompts and a runtime-neutral routing policy instead of assuming one GigaCode agent-manifest format. GigaCode builds can inherit different agent and model interfaces from Qwen CLI or Gemini CLI, so an adapter must detect capabilities before starting a role.

Use `data/model-routing.default.json` as the default policy and validate custom policies against `data/model-routing.v1.schema.json`. The default assignments are:

| Role | Requested model | Activation | Mutation policy |
| --- | --- | --- | --- |
| Supervisor | `GigaChat-3-Ultra` | permanent | orchestration only |
| Reviewer | `vllm/DeepSeek-V4-Flash-262k` | permanent | read-only |
| Repair | `vllm/MiniMax-M3-113k` | on demand | patch proposal only |
| Semantic Analyst | `vllm/Qwen3.6-35B-262k` | on demand | patch proposal only |

A normal layout run starts Supervisor and Reviewer. Start Repair only when structured findings need a patch proposal. Start Semantic Analyst only for process reconciliation, semantic ambiguity, or a conflict involving user input or OpenSpec.

The lifecycle command host invokes Supervisor itself in an isolated process,
just like Reviewer, Repair, and Semantic Analyst. The selected interactive
`/model` controls only the parent presentation session. It is neither the
Supervisor model nor evidence for any child role.

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
The adapter probes the CLI for the required headless/model/output/approval and
isolation flags, builds an argument array, disables extensions with
`--extensions none`, supplies the immutable role contract through
`--system-prompt`, removes every core tool from discovery with a non-empty
`--core-tools` sentinel, excludes fork/MCP tools with `--exclude-tools`, applies
`--max-session-turns`, uses non-interactive default approval, captures output,
and never executes model output. The canonical runtime JSON alone is supplied
on stdin. Never concatenate a shell command and never interpolate diagram
labels, XML, IDs, user text, or links into command metadata.

The conceptual shape is:

```json
["<cli-executable>", "<non-interactive-flag>", "<prompt-input>", "<model-flag>", "<requested-model>"]
```

Dry-run the locally installed compatibility path before use:

```bash
python3 scripts/agent_runtime.py reviewer reviewer-input.json \
  --output reviewer-output.json --cli gigacode --dry-run
```

The verified upstream Qwen Code 0.13.1 contract supports `--model`, `--prompt`,
`--output-format json`, `--approval-mode default`, `--extensions none`,
`--system-prompt`, `--max-session-turns`, `--core-tools`, and `--exclude-tools`. Corporate
GigaCode must advertise the same required isolation controls before a role is
started. It also supports `--auth-type gigacode`; the adapter adds that auth
type when CLI help identifies GigaCode.

Do not use `--approval-mode plan` for these tool-free JSON roles. Qwen Code
0.13.1 injects a Plan-mode reminder that requires the model to call
`exit_plan_mode`; the isolated registry deliberately contains no such tool, so
that contradictory contract can repeat until `FatalTurnLimitedError`. Default
approval avoids the reminder without granting any capability: the non-empty
`--core-tools` sentinel still removes all core tools, the deny list remains
active, and the event audit rejects any observed tool call.

Use a bounded timeout, explicit working directory, captured stdout/stderr, and a minimal allowlisted environment. Do not inherit arbitrary API keys, tokens, passwords, or unrelated process secrets. The Reviewer process must receive read-only inputs and no artifact-publication capability. A model response is data for the Supervisor or deterministic tools; it is not permission to run response text as a command.

Publish role output atomically only after the process exits successfully and its JSON conforms to the role schema: Reviewer uses `reviewer-verdict.v1.schema.json`, Repair uses `diagram-patch.v1.schema.json`, Supervisor uses `supervisor-decision.v1.schema.json`, and Semantic Analyst uses `semantic-plan.v1.schema.json`. GigaCode JSON output is an event array: reject any `tool_use`, `diagram-*` custom agent, or `drawio:*` command leaked into the isolated process; then require one consistent primary model in the `system` init event, every assistant message, and `result.stats.models`, and JSON-decode the final result payload. Stock Gemini JSON output is an outer envelope: reject non-empty `error`/`errors`, extract and JSON-decode `response`, then validate only that inner role payload. Preserve redacted runtime stats/errors as evidence, not as the verdict. Direct top-level role JSON may be parsed for compatibility but cannot publish a successful isolated role because it lacks model proof. On timeout, non-zero exit, tool call, customization leak, invalid output, missing proof, or model mismatch, append `role_failed`; do not create the output file or any role-success event.

Some approved models wrap an otherwise valid role object in exactly one
Markdown `json` fence. The adapter may unwrap that single fence only when the
entire payload consists of the fence and one JSON object. Prose outside the
fence, multiple fences, an unterminated fence, schema-invalid JSON, or missing
model proof still fails closed.

The isolated prompt must embed the actual role output Schema. For Reviewer it
must also embed the exact evidence bindings derived from the runtime input;
prompt guidance never replaces deterministic equality checks before
publication. If the runtime proves one consistent model but the returned role
object fails its Schema or evidence bindings, preserve the bounded model proof
in `role_failed` and the host result while keeping Reviewer status `failed`.
This proves which model executed, but it does not make the invalid verdict
usable and must not emit `model_resolved` or `review_verdict` success events.

### Inherited-model degradation outside lifecycle commands

Diagnostic adapter callers may explicitly permit inherited degradation and must
record it. `/drawio:create`, `/drawio:improve`, and `/drawio:resume` do not:
when an exact isolated requested model is unavailable, that role step fails
closed and the accepted artifact is preserved.

Any diagnostic degraded result must state that model diversity was degraded and
must not describe the review as independent.

## Host and recursion boundary

Corporate Qwen Code 0.13.1 cannot be trusted to provide nested native agents or
native model diversity. On corporate GigaCode 26.5.17,
`diagram_orchestrator.py` is the extension host. It invokes Supervisor,
Reviewer, Repair, and Semantic Analyst through `agent_runtime.py` using the
trusted absolute extension path. Unsupported lifecycle models fail the role
step rather than being replaced by the interactive model.

The main host must run `diagram_supervisor.py host-preflight` before analysis.
The resulting `host-preflight.json` and `host_preflight` manifest event prove
that the parent session could access the installed scripts and corporate CLI.
A successful native `agent` tool status does not provide that proof.

The corporate commands enforce this boundary mechanically. `/drawio:review`
uses `diagram_host.py`; create/improve/resume use `diagram_orchestrator.py`.
The interactive session receives a completed structured result only after the
deterministic host exits. A global `/model` choice affects presentation only.

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

Persist CLI stdout as `runtime-output.json` and redacted stderr as
`runtime-stderr.txt` before interpreting the exit code. Bind both hashes in
`role_finished` or `role_failed`. `/drawio:trace` must re-parse a successful
capture, re-derive the
reported model/proof, validate the typed role output and compare the model with
`model-routing.default.json`. It must not accept edited `resolved_model` or
`model_proof` manifest fields as independent evidence. This is a local evidence
check, not a remote signature or hardware-backed attestation. A failed but
untampered child appears as `failed_verified` with `valid: false`, its failure
phase, capture integrity, and isolation evidence; this is not workflow success.

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
