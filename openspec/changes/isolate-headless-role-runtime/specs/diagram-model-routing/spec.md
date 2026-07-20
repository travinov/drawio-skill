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
- **THEN** the headless process disables installed extensions, supplies the role contract as a system prompt, excludes role-visible tools, and applies a bounded turn limit

#### Scenario: Isolated role attempts delegation or another tool
- **WHEN** any isolated-role event contains a tool call or still advertises Draw.io custom agents or commands
- **THEN** the role fails closed without publishing its output or invoking the next lifecycle role

#### Scenario: Required isolation controls are unavailable
- **WHEN** the CLI does not advertise the required extension-disable, system-prompt, tool-exclusion, or turn-limit control
- **THEN** capability detection fails before model execution with an actionable diagnostic

#### Scenario: Review slash command invokes isolated Reviewer
- **WHEN** the user starts `/drawio:review` while any supported model is selected in the interactive session
- **THEN** the command host invokes Reviewer with the routing policy model through the isolated CLI and returns its requested model, resolved model, and verified runtime model proof

#### Scenario: Native supervisor reports completion
- **WHEN** the native agent tool reports that `diagram-supervisor` completed
- **THEN** the extension does not treat that status as validation or model-routing evidence

### Requirement: Record model fallback and degradation
Model resolution SHALL record `requested_model`, `resolved_model`, provider, resolution mode, and `fallback_used`. The adapter SHALL prefer a verified isolated CLI invocation with an explicit model argument, use native routing only when the runtime proves its model override, and use inherited-model degradation last.

#### Scenario: GigaCode headless model is proven
- **WHEN** an isolated role exits successfully without tools or customization leakage and with schema-valid output
- **THEN** `system.model`, every `assistant.message.model`, and `result.stats.models` agree with the requested model before `model_resolved` is appended

#### Scenario: Model evidence is missing or inconsistent
- **WHEN** any required GigaCode model evidence is absent or names a different primary model
- **THEN** the role fails closed without publishing its output or any success event and the interactive model remains unchanged

#### Scenario: Requested model is unavailable
- **WHEN** a role cannot use its requested model
- **THEN** the run records the fallback and the user-visible result states that model diversity was degraded
