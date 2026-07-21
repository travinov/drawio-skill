## ADDED Requirements

### Requirement: Bound non-interactive role execution
The lifecycle host SHALL treat every isolated Supervisor, Semantic Analyst, Repair, and Reviewer invocation as a bounded non-interactive JSON decision and SHALL reject interactive questions, native-agent recursion, slash-command execution, and any other tool call.

#### Scenario: Supervisor requests interactive clarification inside headless execution
- **WHEN** the isolated Supervisor attempts to call an interactive question tool
- **THEN** the role fails with preserved runtime evidence and the host does not mark its todo or lifecycle phase complete

#### Scenario: Supervisor repeats read or todo operations
- **WHEN** the isolated Supervisor emits one or more directory, search, read, or todo tool calls
- **THEN** the role fails without consuming the returned prose as a supervisor decision and no downstream role starts

#### Scenario: Role returns a bounded schema-valid decision
- **WHEN** the isolated role emits no tool calls, uses the configured model, and returns exactly one schema-valid JSON object
- **THEN** the host records the role result and continues from the existing persisted workflow state

#### Scenario: Initial improve Supervisor declares only Repair and Reviewer
- **WHEN** a model-proven schema-valid initial improve decision selects `repair` and declares only Repair and Reviewer
- **THEN** the host preserves those declared roles unchanged, adds Supervisor and Semantic Analyst plus its complete initial lifecycle policy, and continues through semantic analysis instead of rejecting the decision

#### Scenario: Initial create omits Repair
- **WHEN** a model-proven schema-valid initial create decision does not declare Repair
- **THEN** the host still authorizes Repair for later deterministic validation or Reviewer findings, records that host addition, and invokes Repair only if such findings require it

#### Scenario: Resume uses the approved semantic plan
- **WHEN** the user continues a persisted run after initial semantic analysis
- **THEN** the host authorizes Supervisor, Repair, and Reviewer for continuation without automatically rerunning Semantic Analyst

#### Scenario: Host and model role evidence remains distinguishable
- **WHEN** the host applies a phase role policy to a schema-valid Supervisor result
- **THEN** the workflow retains the raw Supervisor decision, records `supervisor_declared_roles` and `host_mandatory_roles` separately, and records their union as effective `required_roles`

#### Scenario: Supervisor selects a phase-incompatible action
- **WHEN** a schema-valid Supervisor decision selects an action that the current create, improve, or resume phase cannot execute
- **THEN** the host fails closed before invoking another role even though host-mandatory roles are available

#### Scenario: Denied tools consume the bounded turn budget
- **WHEN** a corporate model would repeatedly select denied tools instead of returning its JSON decision
- **THEN** the role invocation advertises no core tools or globally configured MCP servers, remains bounded, and preserves the failed runtime if the model still exhausts the limit

#### Scenario: Supervisor parent profile contains Jira or Bitbucket MCP
- **WHEN** the interactive GigaCode session has global MCP servers but starts an isolated Supervisor role
- **THEN** the role process filters all MCP servers before discovery and proceeds directly to its bounded JSON decision without selecting an MCP tool

#### Scenario: Plan mode conflicts with the empty tool registry
- **WHEN** Qwen Code Plan mode would instruct the isolated model to finish through `exit_plan_mode`
- **THEN** the host uses default non-interactive approval for the tool-free child and retains the same empty registry, deny list, turn limit, timeout, and event audit

#### Scenario: Operator traces an unsuccessful role
- **WHEN** `/drawio:trace` inspects a run containing `role_failed`
- **THEN** it reports the failed role, failure phase, capture integrity, isolation evidence, and diagnostic without misclassifying an expected failed workflow as a successfully accepted artifact

#### Scenario: Bare trace follows a newer read-only review
- **WHEN** a completed read-only review is newer than existing create or improve workflows
- **THEN** bare `/drawio:trace` selects that review workflow and reports its validator, Reviewer, model, binding, and failure evidence instead of an older run

#### Scenario: Explicit trace uses the published review run ID
- **WHEN** the user executes the exact `/drawio:trace --run <run_id>` command published by review
- **THEN** the host resolves the persisted UUID even when the run directory has a timestamp-based name and traces that same review

#### Scenario: Supervisor primary attempt is recovered by policy
- **WHEN** a nonterminal `role_failed` event for Supervisor is followed by a schema-valid, model-proven fallback `role_finished` event
- **THEN** the workflow continues, `/drawio:trace` validates both attempts, and host results report the run as model-diversity degraded rather than terminally failed

### Requirement: Parse Qwen custom-command arguments deterministically
The extension SHALL transport the complete Qwen `{{args}}` expansion as one shell-escaped value and SHALL parse that value inside the deterministic Python host without evaluating it as shell code.

#### Scenario: User supplies quoted conversational text
- **WHEN** the user invokes create, improve, or resume with quoted multi-word text
- **THEN** the host removes only the command-language quoting, preserves the text as one logical value, and does not include literal quote characters in the request or feedback

#### Scenario: User supplies advanced flags
- **WHEN** the user supplies `--diagram`, `--request`, `--run`, `--decision`, `--feedback`, or another supported command option
- **THEN** the bridge reconstructs separate argument tokens before the command parser runs and the host-owned workspace and CLI values cannot be overridden

#### Scenario: User selects a Draw.io file through Qwen file-reference syntax
- **WHEN** a diagram argument arrives with one leading `@`
- **THEN** the bridge removes the reference marker only from a `.drawio` path token and preserves the remaining path

#### Scenario: User input contains malformed quoting or host-owned options
- **WHEN** raw arguments cannot be parsed or attempt to override the workspace, CLI, or argument separator
- **THEN** the command fails before orchestration with a structured actionable error and does not use shell evaluation

#### Scenario: Host publishes a follow-up command
- **WHEN** review or orchestration returns `next_commands`
- **THEN** each published short or explicit command conforms to the same bridge contract and identifies the selected diagram or run whenever automatic selection would be ambiguous

### Requirement: Continue read-only review without repeated improve arguments
The extension SHALL support a bare `/drawio:improve` as the normal continuation of a completed read-only review and SHALL resolve its inputs deterministically before any agent or validator work starts.

#### Scenario: Latest completed review still matches its diagram
- **WHEN** the user invokes `/drawio:improve` without arguments and the workspace contains a completed read-only review whose artifact path is inside the workspace and whose recorded SHA-256 matches the current file
- **THEN** the host selects the latest eligible review artifact, supplies the default repair request, records the source review run, and starts the normal monotonic improve lifecycle

#### Scenario: No eligible review and one diagram exists
- **WHEN** the user invokes `/drawio:improve` without arguments, no eligible review handoff exists, and exactly one root-level `.drawio` file exists
- **THEN** the host selects that diagram, supplies the default repair request, and records both automatic resolution sources

#### Scenario: Review context is stale or diagram selection is ambiguous
- **WHEN** no hash-matching completed review can identify a diagram and the workspace has zero or multiple root-level `.drawio` files
- **THEN** the host returns an actionable selection-required result without creating a run or invoking any model

#### Scenario: User overrides an automatic improve input
- **WHEN** the user supplies conversational text, `--request`, or `--diagram`
- **THEN** each explicit value overrides its automatic counterpart while omitted values still use the deterministic handoff or default

#### Scenario: Review publishes its normal continuation
- **WHEN** a read-only review completes
- **THEN** its primary next command is exactly `/drawio:improve`, with an explicit equivalent retained only as advanced evidence and recovery guidance
