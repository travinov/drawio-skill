## ADDED Requirements

### Requirement: Checkpoint decisions are persisted workflow inputs
Human decisions and feedback SHALL be stored as immutable run inputs and SHALL resume the existing state machine rather than restart analysis from the original request.

#### Scenario: User continues with corrections
- **WHEN** the user resumes with `continue` and textual findings
- **THEN** the host SHALL add the feedback to source priority, reconcile it against the accepted DiagramSpec, and continue from the accepted candidate

#### Scenario: User accepts with findings
- **WHEN** the user chooses `approve_with_findings`
- **THEN** the host SHALL publish the accepted candidate and record the unresolved findings in the terminal result and trace

#### Scenario: User pauses or stops
- **WHEN** the user chooses `pause`, `stop`, or `manual_handoff`
- **THEN** the host SHALL preserve resumable or terminal state as appropriate without discarding the best accepted candidate

### Requirement: Checkpoints are consolidated
The host SHALL NOT request human confirmation after every successful tool or role step; it SHALL request input only for semantic approval, plateau/confusion, or final acceptance unless execution cannot safely continue.

#### Scenario: Automatic layout improvements remain monotonic
- **WHEN** successive repair attempts pass the automated gates and require no semantic decision
- **THEN** the host SHALL continue automatically until final review, plateau, or the iteration limit
