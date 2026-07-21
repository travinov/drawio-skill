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

## Risks / Trade-offs

- **GigaCode help advertises a flag but implements it differently** -> raw event auditing detects tool usage/customization leakage and fails before output publication.
- **A model returns invalid JSON without tools** -> existing strict JSON/schema validation remains fail closed; no prose scraping is added.
- **Static tool deny list misses a new tool** -> any observed `tool_use` event is rejected independent of its name.
- **Role prompt size grows** -> schemas remain in the system prompt while variable runtime JSON stays on stdin, avoiding user data in argv and keeping the current input pipeline.
- **Turn limit is consumed by denied-tool retries** -> the non-empty `--core-tools` sentinel removes core tool schemas before inference; the limit stays small and any remaining failure is preserved for diagnosis instead of hidden.
- **Non-zero CLI exit loses structured events** -> capture stdout/stderr before checking the return code and expose their integrity plus isolation audit through `/drawio:trace`.

## Migration Plan

1. Ship as a new side-by-side `1.23.0-corporate.4` release ZIP and preserve
   `1.23.0-corporate.3` plus the earlier `.2` package for rollback.
2. Reinstall from the approved local archive on the corporate Mac.
3. Re-run the same `/drawio:create` smoke test and inspect `runtime-output.json` plus `/drawio:trace`.
4. Roll back by reinstalling the previous ZIP if capability detection reports that the corporate fork lacks a required flag.

## Open Questions

- None for implementation. Corporate runtime validation is required because GigaCode itself is not installed on the development Mac.
