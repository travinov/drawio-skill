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

#### Scenario: Denied tools consume the bounded turn budget
- **WHEN** a corporate model would repeatedly select denied tools instead of returning its JSON decision
- **THEN** the role invocation advertises no core tools, remains bounded, and preserves the failed runtime if the model still exhausts the limit

#### Scenario: Plan mode conflicts with the empty tool registry
- **WHEN** Qwen Code Plan mode would instruct the isolated model to finish through `exit_plan_mode`
- **THEN** the host uses default non-interactive approval for the tool-free child and retains the same empty registry, deny list, turn limit, timeout, and event audit

#### Scenario: Operator traces an unsuccessful role
- **WHEN** `/drawio:trace` inspects a run containing `role_failed`
- **THEN** it reports the failed role, failure phase, capture integrity, isolation evidence, and diagnostic without misclassifying an expected failed workflow as a successfully accepted artifact

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
