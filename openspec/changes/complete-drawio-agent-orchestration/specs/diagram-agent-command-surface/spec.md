## ADDED Requirements

### Requirement: Executable diagram lifecycle commands
The extension SHALL expose `/drawio:create`, `/drawio:improve`, `/drawio:resume`, and `/drawio:trace` as deterministic command-host entry points and SHALL preserve `/drawio:review` as a read-only command.

#### Scenario: Create command receives intent and target
- **WHEN** the user invokes `/drawio:create` with a target `.drawio` path and a process or system description
- **THEN** the command SHALL start a persisted creation run and return its run id, current state, checkpoint or terminal result, and evidence paths

#### Scenario: Improve command receives an existing diagram
- **WHEN** the user invokes `/drawio:improve` with an existing `.drawio` path and optional requirements
- **THEN** the command SHALL import that diagram as the initial accepted candidate without overwriting it

#### Scenario: Resume command continues the same run
- **WHEN** the user invokes `/drawio:resume` with a run id or run directory and a supported human decision
- **THEN** the command SHALL continue from the persisted state and SHALL NOT create a replacement run

#### Scenario: Trace command is read-only
- **WHEN** the user invokes `/drawio:trace` for a run
- **THEN** the command SHALL return a chronological, hash-verifiable account without invoking a model or changing run state

### Requirement: Stable command argument and result contracts
Each lifecycle command MUST validate its arguments before orchestration and MUST write a machine-readable `host-result.json` even when the request fails after a run directory is created.

#### Scenario: Invalid source path
- **WHEN** an improve request references a missing or non-file source
- **THEN** the command SHALL return a non-zero status with the normalized rejected path and SHALL NOT begin role execution

#### Scenario: Awaiting user decision
- **WHEN** orchestration reaches a human checkpoint
- **THEN** `host-result.json` SHALL identify the allowed decisions, the best candidate, blocking findings, and the exact resume command contract

### Requirement: Local-only command execution
Lifecycle commands SHALL keep diagram content, role inputs, outputs, and evidence within the local workspace and extension runtime.

#### Scenario: Offline corporate execution
- **WHEN** a lifecycle command runs without public network access
- **THEN** it SHALL complete or produce a precise local runtime blocker without requiring GitHub, SaaS rendering, or an external MCP server
