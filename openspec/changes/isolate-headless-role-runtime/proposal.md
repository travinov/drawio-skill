## Why

Corporate GigaCode 26.5.17 loaded the installed extension and recursively invoked `diagram-supervisor` inside an already isolated Supervisor process. The child then spent 99 tool calls and 453,314 tokens in a directory/todo loop before returning prose instead of the required role JSON, so the multi-agent lifecycle never reached semantic analysis, validation, repair, or review.

## What Changes

- Run isolated roles in a customization-free headless session when the CLI exposes the required isolation capability.
- Separate the role system contract from the runtime JSON input and prohibit all role tool calls, native agent delegation, interactive questions, and slash-command execution.
- Bound isolated role turns/tool usage and fail closed when the CLI cannot provide the required isolation surface.
- Audit raw GigaCode events for tool calls and loaded custom agents/extensions before accepting role output.
- Remove all core tools from the isolated model's advertised tool registry, rather than only denying their execution after selection.
- Preserve stdout, redacted stderr, and isolation evidence even when the headless CLI exits non-zero or exhausts its turn budget.
- Avoid Qwen 0.13.1 Plan-mode reminders in tool-free role sessions, because they require the deliberately unavailable `exit_plan_mode` tool and can consume the entire turn budget.
- Prefer capability-detected `stream-json` capture so partial system, assistant, and result events survive a non-zero child exit and can be audited.
- Permit one policy-declared Supervisor fallback model only after a proven `turn_limit`; record the primary failure as recovered evidence and expose the loss of model diversity.
- Treat Qwen custom-command `{{args}}` as one shell-escaped transport value, parse it inside Python without `eval`, and apply the same argument contract to all five `/drawio:*` entry points.
- Generate only commands that the installed custom-command bridge can execute, including explicit diagram, run, decision, request, and feedback values.
- Add a regression fixture based on the captured corporate runtime failure and report actionable isolation diagnostics.

## Capabilities

### New Capabilities

<!-- None. -->

### Modified Capabilities

- `diagram-model-routing`: Require an isolated headless role process to exclude extension/custom-agent context, make no tool calls, and prove the configured model before its JSON result is accepted.
- `diagram-supervisor-orchestration`: Require bounded non-interactive role execution and fail closed on recursive delegation, interactive prompts, or repeated tool loops before downstream lifecycle steps.

## Impact

- `publish-drawio-skill/scripts/agent_runtime.py` command construction, capability detection, event auditing, and diagnostics.
- Role prompts and runtime tests for Supervisor, Reviewer, Repair, and Semantic Analyst.
- Corporate installation/release package version and verification expectations.
- `/drawio:trace` output for both successful and failed role invocations.
- `/drawio:create`, `/drawio:improve`, `/drawio:review`, `/drawio:resume`, and `/drawio:trace` argument transport plus generated `next_commands`.
- No change to interactive `/model`; each role retains its explicit primary model mapping and only Supervisor gains a bounded, explicit recovery model.
