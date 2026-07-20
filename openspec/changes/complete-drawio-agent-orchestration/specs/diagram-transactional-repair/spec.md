## ADDED Requirements

### Requirement: Closed repair transaction
Each repair iteration MUST use the last accepted candidate as its baseline and MUST execute patch schema validation, precondition checks, deterministic application, strict validation, monotonic comparison, and independent review before acceptance.

#### Scenario: Candidate improves monotonically
- **WHEN** a patched candidate has no regression in earlier quality dimensions, passes required semantic bindings, and receives a non-blocking review verdict
- **THEN** the host SHALL atomically promote it as the new accepted candidate and use it as the next baseline

#### Scenario: Patch regresses quality
- **WHEN** a patched candidate increases an earlier quality dimension or fails a required gate
- **THEN** the host SHALL reject it, preserve it as attempt evidence, and retain the previous accepted candidate as the next baseline

### Requirement: Deterministic routing repair
Connections that require orthogonal routing SHALL have explicit geometry or waypoints, and missing or crossing-prone routes SHALL be actionable validator findings available to Repair.

#### Scenario: Edge has no required waypoint geometry
- **WHEN** validation finds a connection that is straight or under-specified in a congested region
- **THEN** the report SHALL identify the edge and Repair SHALL be able to propose a bounded `set_edge_route` or pin adjustment rather than regenerate the diagram

### Requirement: Publication follows acceptance
The host SHALL publish only the final accepted candidate and SHALL use an atomic write; rejected attempts SHALL never replace the target or source.

#### Scenario: User stops after several iterations
- **WHEN** the user chooses stop or manual handoff
- **THEN** the original source SHALL remain unchanged and the best accepted candidate SHALL remain available inside the run directory
