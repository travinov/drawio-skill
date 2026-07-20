## ADDED Requirements

### Requirement: Host-driven complete role workflow
The deterministic host SHALL drive the persisted workflow through required Supervisor, Semantic Analyst, Repair, Reviewer, validation, comparison, checkpoint, and publication steps; an interactive model description SHALL NOT count as execution of a role or tool step.

#### Scenario: Create workflow activates required roles
- **WHEN** a creation run starts from user intent
- **THEN** the host SHALL invoke Supervisor and Semantic Analyst before deterministic generation and SHALL invoke Reviewer against a validated candidate before final acceptance

#### Scenario: Improve workflow activates repair only when needed
- **WHEN** an imported accepted candidate has actionable validation, layout, or approved semantic findings
- **THEN** the host SHALL invoke Repair with hash-bound baseline evidence and SHALL process its patch through the transactional gate

### Requirement: Persisted continuation and bounded progress
The host SHALL persist every transition and SHALL enforce bounded iterations, cycle detection, and plateau detection across accepted and rejected candidates.

#### Scenario: Process exits between checkpoint and resume
- **WHEN** the command process exits after writing a checkpoint
- **THEN** a later resume SHALL continue from the same accepted candidate, iteration counter, evidence chain, and pending decision

#### Scenario: Candidate hashes repeat
- **WHEN** an attempt reproduces a prior candidate or quality vector without progress
- **THEN** the host SHALL transition to plateau or awaiting feedback instead of starting an unbounded new iteration
