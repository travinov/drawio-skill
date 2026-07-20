## ADDED Requirements

### Requirement: Creation begins from structured intent
For a creation run, the system SHALL combine user intent, available applicable specifications, and approved assumptions into a schema-valid semantic diagram plan before rendering.

#### Scenario: Applicable specification exists
- **WHEN** the workspace contains an applicable specification for the requested process or system
- **THEN** Semantic Analyst SHALL compare the request to that specification, record the selected source and differences, and base the plan on the higher-priority approved source

#### Scenario: No applicable specification exists
- **WHEN** no applicable specification is found
- **THEN** the system SHALL build the plan from user intent and explicitly record assumptions rather than inventing an existing authoritative source

### Requirement: Existing diagrams are reconciled without regeneration
For an improve run, the system SHALL derive a DiagramSpec and semantic digest from the existing `.drawio`, compare supplied process information to it, and express changes as approved plan deltas or bounded patches.

#### Scenario: User description conflicts with diagram
- **WHEN** supplied process information differs from the imported diagram semantics
- **THEN** the checkpoint SHALL tell the user which changes would be made on the basis of that description before semantic mutations are applied

#### Scenario: Layout-only improvement
- **WHEN** the request does not authorize semantic change
- **THEN** the system SHALL preserve node and edge meaning and restrict Repair to layout and routing operations

### Requirement: Multiple diagram types share one evidence contract
The system SHALL support a generic deterministic renderer and SHALL allow specialized local adapters while producing the same candidate, validation, comparison, review, and trace artifacts.

#### Scenario: No specialized adapter matches
- **WHEN** the requested diagram type has no specialized adapter
- **THEN** the generic renderer SHALL create a valid draw.io candidate from the semantic plan and the normal improvement pipeline SHALL continue
