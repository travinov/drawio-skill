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

#### Scenario: Operator traces an unsuccessful role
- **WHEN** `/drawio:trace` inspects a run containing `role_failed`
- **THEN** it reports the failed role, failure phase, capture integrity, isolation evidence, and diagnostic without misclassifying an expected failed workflow as a successfully accepted artifact
