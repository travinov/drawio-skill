# diagram-transactional-repair Specification

## Purpose
TBD - created by archiving change add-diagram-supervisor-extension. Update Purpose after archive.
## Requirements
### Requirement: Apply explicit preconditioned patch operations
The patcher SHALL support `set_edge_route`, `set_edge_pins`, `set_label_offset`, `move_vertex`, `resize_vertex`, `resize_container`, `add_semantic_element`, and `remove_semantic_element` operations with target ID, precondition, proposed value, semantic effect, reason/finding IDs, and rollback data.

#### Scenario: Patch precondition does not match
- **WHEN** the target cell or expected old value/hash differs from the patch precondition
- **THEN** the entire patch transaction fails without modifying the original or accepted baseline

### Requirement: Repair straight connectors with explicit routing
The deterministic router SHALL identify repairable straight waypoint-free connectors and SHALL be able to create obstacle-aware orthogonal routes with explicit waypoints, distinct terminal pins, and label offsets where required.

#### Scenario: Straight connector crosses an obstacle
- **WHEN** a waypoint-free edge intersects a non-terminal vertex
- **THEN** the router proposes a local orthogonal route that avoids the obstacle and records the originating finding

### Requirement: Preserve semantics and untouched regions
Layout-only patches SHALL preserve the semantic digest and SHALL NOT change cells outside the declared affected region.

#### Scenario: Local edge repair alters an unrelated vertex
- **WHEN** a candidate changes an unrelated cell outside the affected region
- **THEN** candidate acceptance fails and the prior accepted artifact remains active

### Requirement: Accept only monotonic improvements
Candidate comparison SHALL use the ordered vector semantic violations, structural errors, route-through-node, container/lane violations, edge crossings, overlaps, routing uncertainty, text overflow, and route complexity. A candidate SHALL be accepted only if no higher-priority category worsens and at least one category improves.

#### Scenario: Crossings decrease but structure breaks
- **WHEN** a candidate reduces crossings and introduces a structural error
- **THEN** the candidate is rejected

#### Scenario: Route-through findings decrease without regressions
- **WHEN** a candidate preserves semantics and all higher-priority categories while reducing route-through-node findings
- **THEN** the candidate is accepted as the next baseline

### Requirement: Publish candidates atomically
The patcher SHALL write and validate a temporary candidate before atomic publication and SHALL retain sufficient rollback information for the run.

#### Scenario: Candidate write is interrupted
- **WHEN** patch application fails before atomic publication
- **THEN** the original and last accepted artifact remain unchanged

