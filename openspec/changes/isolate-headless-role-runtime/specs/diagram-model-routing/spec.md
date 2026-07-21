## MODIFIED Requirements

### Requirement: Resolve a model independently for each logical role
The extension SHALL resolve Supervisor, Reviewer, Repair, and Semantic Analyst model assignments independently and SHALL NOT silently change the global interactive model.

#### Scenario: Native per-agent model override is available
- **WHEN** the runtime supports a role-specific model declaration
- **THEN** each role uses its configured model and records requested and resolved model identifiers

#### Scenario: Corporate GigaCode inherits the parent model for native agents
- **WHEN** GigaCode 26.5.17 / Qwen Code 0.13.1 exposes native agents without a model selector
- **THEN** the extension uses an isolated headless CLI with an explicit model before considering native or inherited routing

#### Scenario: Main host owns corporate execution
- **WHEN** the corporate runtime starts a diagram workflow from the interactive session
- **THEN** the main session executes deterministic commands and isolated child roles itself and does not delegate the whole workflow to native `diagram-supervisor`

#### Scenario: Isolated role excludes extension context and tools
- **WHEN** the main host invokes an isolated diagram role
- **THEN** the headless process disables installed extensions, supplies the role contract as a system prompt, uses default non-interactive approval without a Plan-mode reminder, uses a non-empty allowlist sentinel that removes every core tool from the model registry, uses an explicitly empty MCP-server allowlist that removes globally configured MCP servers before discovery, excludes fork-specific and MCP tools as defense in depth, and applies a bounded turn limit

#### Scenario: Corporate profile configures global MCP servers
- **WHEN** Jira, Bitbucket, or another MCP server is configured in the parent GigaCode profile
- **THEN** the isolated role starts with no allowed MCP servers and the configured server names and tool schemas are absent from the child event stream

#### Scenario: Tool-free role runs on Qwen Code 0.13.1
- **WHEN** the isolated role starts on a runtime where Plan approval injects a reminder requiring `exit_plan_mode`
- **THEN** the adapter selects default approval for that child process and still proves that no tools or Draw.io customizations were exposed or called

#### Scenario: Isolated role attempts delegation or another tool
- **WHEN** any isolated-role event contains a tool call or still advertises Draw.io custom agents or commands
- **THEN** the role fails closed without publishing its output or invoking the next lifecycle role

#### Scenario: Required isolation controls are unavailable
- **WHEN** the CLI does not advertise the required extension-disable, system-prompt, core-tool allowlist, empty MCP-server allowlist, tool-exclusion, or turn-limit control
- **THEN** capability detection fails before model execution with an actionable diagnostic

#### Scenario: MCP tool still leaks through a fork
- **WHEN** the isolated event stream contains an MCP tool call despite the empty server allowlist
- **THEN** the wildcard deny and zero-tool event audit fail the role closed without accepting its JSON result or invoking a fallback

#### Scenario: Isolated role exits before returning a decision
- **WHEN** the CLI exits non-zero, reports `FatalTurnLimitedError`, or returns invalid role output
- **THEN** stdout, redacted stderr, their hashes, and any independently auditable isolation evidence are persisted and exposed by the run trace

#### Scenario: CLI supports streamed JSON events
- **WHEN** the CLI help advertises `stream-json`
- **THEN** the adapter captures JSONL events incrementally, preserves partial events on failure, and retains compatibility with buffered JSON event arrays

#### Scenario: Review slash command invokes isolated Reviewer
- **WHEN** the user starts `/drawio:review` while any supported model is selected in the interactive session
- **THEN** the command host invokes Reviewer with the routing policy model through the isolated CLI and returns its requested model, resolved model, and verified runtime model proof

#### Scenario: Reviewer returns a mismatched legacy receipt hash
- **WHEN** a model-proven schema-valid Reviewer decision includes a legacy `receipt_sha256` that differs from the validated role input
- **THEN** the host records the declared mismatch, derives all final evidence bindings from the trusted input, and publishes a final verdict only if that deterministic envelope validates

#### Scenario: Reviewer omits every evidence binding
- **WHEN** the isolated Reviewer returns only its analytical verdict metadata and findings
- **THEN** the host adds `run_id`, candidate, report, and receipt hashes from the validated role input and the final verdict remains traceably hash-bound

#### Scenario: Native supervisor reports completion
- **WHEN** the native agent tool reports that `diagram-supervisor` completed
- **THEN** the extension does not treat that status as validation or model-routing evidence

### Requirement: Record model fallback and degradation
Model resolution SHALL record `requested_model`, `resolved_model`, provider, resolution mode, and `fallback_used`. The adapter SHALL prefer a verified isolated CLI invocation with an explicit model argument, use native routing only when the runtime proves its model override, and use inherited-model degradation last.

#### Scenario: GigaCode headless model is proven
- **WHEN** an isolated role exits successfully without tools or customization leakage and with schema-valid output
- **THEN** `system.model` and every `assistant.message.model` agree with the attempted explicit model, and `result.stats.models` also agrees when supplied, before `model_resolved` is appended

#### Scenario: Model evidence is missing or inconsistent
- **WHEN** any required GigaCode model evidence is absent or names a different primary model
- **THEN** the role fails closed without publishing its output or any success event and the interactive model remains unchanged

#### Scenario: Requested model is unavailable
- **WHEN** a role cannot use its requested model
- **THEN** the run records the fallback and the user-visible result states that model diversity was degraded

#### Scenario: Primary Supervisor exhausts its turn budget
- **WHEN** the explicitly requested Supervisor model reports `FatalTurnLimitedError` and policy declares one runtime fallback for that failure kind
- **THEN** the adapter preserves the primary attempt as nonterminal evidence, invokes the configured fallback exactly once under the same isolation controls, and continues only after schema-valid model-proven output

#### Scenario: Failure is not eligible for runtime fallback
- **WHEN** capability detection, customization isolation, zero-tool enforcement, timeout, or evidence integrity fails
- **THEN** the role fails closed without invoking a fallback model
