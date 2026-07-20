## Why

Corporate GigaCode 26.5.17 loaded the installed extension and recursively invoked `diagram-supervisor` inside an already isolated Supervisor process. The child then spent 99 tool calls and 453,314 tokens in a directory/todo loop before returning prose instead of the required role JSON, so the multi-agent lifecycle never reached semantic analysis, validation, repair, or review.

## What Changes

- Run isolated roles in a customization-free headless session when the CLI exposes the required isolation capability.
- Separate the role system contract from the runtime JSON input and prohibit all role tool calls, native agent delegation, interactive questions, and slash-command execution.
- Bound isolated role turns/tool usage and fail closed when the CLI cannot provide the required isolation surface.
- Audit raw GigaCode events for tool calls and loaded custom agents/extensions before accepting role output.
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
- No change to interactive `/model`; each role retains its existing explicit model mapping.
