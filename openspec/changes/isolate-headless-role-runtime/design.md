## Context

`agent_runtime.py` currently starts a headless GigaCode process with a generic `--prompt` and sends the role definition, output schema, and input together on stdin. The captured GigaCode 26.5.17 / Qwen Code 0.13.1 event stream proves that this process loaded the installed Draw.io extension, exposed `diagram-supervisor`, and let the root model invoke that same agent recursively. Plan mode still allows read-only and agent tools, so it did not prevent the 99-call loop.

Upstream Qwen Code 0.13.1 provides the compatible controls needed here: `--extensions none`, `--system-prompt`, `--max-session-turns`, and `--exclude-tools`. The extension must capability-detect these controls because GigaCode is a fork and must fail closed if its supported surface differs.

## Goals / Non-Goals

**Goals:**

- Make each role a single bounded model decision with explicit model proof and schema-valid JSON output.
- Prevent native/custom subagent recursion and all other role tool calls.
- Keep runtime JSON on stdin, outside command metadata, while placing immutable role instructions in the system prompt.
- Make incompatible corporate CLI builds fail early with a useful diagnostic instead of consuming an unbounded model/tool loop.

**Non-Goals:**

- Do not change the interactive session model or role-to-model mapping.
- Do not accept prose by extracting opportunistic JSON fragments.
- Do not make model output responsible for deterministic validation, patch application, or publication.

## Decisions

1. **Use extension-free headless sessions.** Pass `--extensions none` on every isolated role invocation. This is preferred over changing installed extension state because it is process-local and reversible.
2. **Separate instructions from data.** Pass the agent role body and required output schema through `--system-prompt`; pass only the canonical runtime input JSON through stdin. The short `--prompt` tells the model to consume stdin and return one object.
3. **Remove tools, deny tools, then audit.** Pass `--core-tools` with a deliberately nonexistent sentinel so Qwen's non-empty allowlist removes every core tool from the advertised registry. Retain `--exclude-tools` for known, fork-specific, and MCP tools, then audit the returned event array and reject any `tool_use` content regardless of name.
4. **Bound the run.** Pass a small `--max-session-turns` value and retain the existing subprocess timeout. The turn limit remains a safety fuse; removing tools from the registry prevents denied-tool retries from consuming it during a valid one-decision role.
5. **Fail closed on missing controls.** Treat `--extensions`, `--system-prompt`, `--max-session-turns`, and `--exclude-tools` as required isolated-role capabilities, alongside the existing model/prompt/output/approval flags.
6. **Audit customization leakage.** Reject a success stream if system initialization still advertises Draw.io extension commands or diagram custom agents. Preserve raw capture and failure evidence for traceability.
7. **Capture before interpreting exit status.** Atomically persist stdout and redacted stderr immediately after every completed child process, including non-zero exits, and bind both files into `role_failed` or `role_finished` manifest evidence.
8. **Do not put tool-free roles in Plan mode.** Use `--approval-mode default` for isolated JSON decisions. Qwen Code 0.13.1 injects an extra user-message reminder in Plan mode that orders the model to call `exit_plan_mode`; that conflicts with the empty tool registry and caused the corporate `.4` Supervisor to retry until exit 53. Default approval does not expose or approve tools because the core-tool sentinel, deny list, and event audit remain authoritative.
9. **Capture streamed events when the CLI advertises them.** Prefer `--output-format stream-json` when `--help` names that value and retain the buffered JSON parser for compatible forks. Persist the JSONL stream before interpreting the exit code so a turn-limit failure still proves model identity, event count, tool use, and customization isolation when those events were emitted.
10. **Recover only from a policy-approved Supervisor turn limit.** The primary Supervisor remains `GigaChat-3-Ultra`. After `FatalTurnLimitedError`, and only then, invoke Supervisor once with the configured `vllm/DeepSeek-V4-Flash-262k` runtime fallback. Do not retry capability, isolation, leakage, timeout, or integrity failures. The fallback must satisfy the same schema, model-proof, zero-tool, and evidence rules.
11. **Preserve both attempts.** Store primary and fallback runtime captures under separate attempt directories. Mark the primary failure `terminal: false`, record its fallback target, and publish a single `role_finished` result with `fallback_used: true` only after the fallback succeeds. A failed fallback is terminal.
12. **Treat `{{args}}` as a transport string, not argv.** Qwen shell-escapes a custom-command `{{args}}` expansion as one argument. Each command template assigns that value to `DRAWIO_COMMAND_ARGS`; the Python host parses it with `shlex.split`, normalizes a leading Draw.io `@` reference, rejects host-owned options and `--`, then inserts the reconstructed user tokens before fixed host arguments. No input is evaluated as shell code.
13. **Keep generated commands executable.** `next_commands` uses the same documented grammar as the bridge. A review result carries its selected diagram into the improve command, and explicit resume/trace commands remain valid even when multiple diagrams or runs exist.

## Risks / Trade-offs

- **GigaCode help advertises a flag but implements it differently** -> raw event auditing detects tool usage/customization leakage and fails before output publication.
- **A model returns invalid JSON without tools** -> existing strict JSON/schema validation remains fail closed; no prose scraping is added.
- **Static tool deny list misses a new tool** -> any observed `tool_use` event is rejected independent of its name.
- **Role prompt size grows** -> schemas remain in the system prompt while variable runtime JSON stays on stdin, avoiding user data in argv and keeping the current input pipeline.
- **Turn limit is consumed by denied-tool retries** -> the non-empty `--core-tools` sentinel removes core tool schemas before inference; the limit stays small and any remaining failure is preserved for diagnosis instead of hidden.
- **Non-zero CLI exit loses structured events** -> capture stdout/stderr before checking the return code and expose their integrity plus isolation audit through `/drawio:trace`.
- **Default approval appears less restrictive than Plan mode** -> tool availability is controlled independently by the empty core-tool allowlist and deny list; default approval only removes the contradictory Plan-mode reminder.
- **Stream JSON omits aggregate model statistics** -> require one system-init model and one consistent assistant-message model; require aggregate `stats.models` only when the runtime actually supplies it.
- **Supervisor fallback reduces model diversity** -> allow exactly one configured fallback, expose the degradation in `host-result.json` and `/drawio:trace`, and retain the primary attempt as hashed evidence.
- **Internal tokenization could reintroduce shell injection** -> use `shlex.split` only as a parser, never pass its result through `eval` or `shell=True`, reject host-owned options, and keep subprocess calls as argument arrays.
- **Qwen changes custom-command escaping** -> package tests cover the documented one-value transport and verifier checks every command template for the bridge marker; corporate retest remains required.

## Migration Plan

1. Ship the follow-up as a new side-by-side `1.23.0-corporate.7` release ZIP and preserve
   `1.23.0-corporate.6` plus the earlier packages for rollback.
2. Reinstall from the approved local archive on the corporate Mac.
3. Re-run the captured review/improve argument cases, then the same `/drawio:create` smoke test, and inspect the per-attempt `runtime-output.jsonl` captures plus `/drawio:trace`.
4. Roll back by reinstalling the previous ZIP if capability detection reports that the corporate fork lacks a required flag.

## Open Questions

- None for implementation. Corporate runtime validation is required because GigaCode itself is not installed on the development Mac.
