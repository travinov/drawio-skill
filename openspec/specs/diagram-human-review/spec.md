# diagram-human-review Specification

## Purpose
TBD - created by archiving change add-diagram-supervisor-extension. Update Purpose after archive.
## Requirements
### Requirement: Consolidate human checkpoints
The extension SHALL request user input only for source conflicts, semantic changes or deletions, plateau/confusion, and final review, and SHALL group related issues into one checkpoint where practical.

#### Scenario: Several layout warnings are repairable
- **WHEN** findings require only semantics-preserving layout patches
- **THEN** the supervisor may continue after a consolidated notice without asking after every iteration

### Requirement: Present semantic and layout diffs separately
Before a user decision, the extension SHALL distinguish semantic additions/removals/relationship changes from coordinate, size, route, pin, and label-layout changes.

#### Scenario: Proposed patch adds a return loop and moves nodes
- **WHEN** a candidate contains both semantic and layout changes
- **THEN** the user sees separate semantic and layout summaries before approval

### Requirement: Support continuation and user-directed termination
At a checkpoint the user SHALL be able to continue iteration, approve, approve with findings, pause/resume, stop, or choose manual handoff while retaining current artifacts and evidence.

#### Scenario: User wants to finish by hand
- **WHEN** the user selects manual handoff
- **THEN** the run ends in `manual_handoff` with the last accepted diagram, remaining findings, and receipt status available

#### Scenario: User accepts remaining findings
- **WHEN** the user explicitly accepts a result with unresolved findings
- **THEN** the run ends in `approved_with_findings` and does not misreport strict validation as passed

