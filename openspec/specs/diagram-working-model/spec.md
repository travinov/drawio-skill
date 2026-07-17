# diagram-working-model Specification

## Purpose
TBD - created by archiving change add-diagram-supervisor-extension. Update Purpose after archive.
## Requirements
### Requirement: Import existing diagrams without regeneration
The extension SHALL import supported draw.io XML into a `DiagramSpec` while preserving original XML, page/layer metadata, styles, unknown attributes, and stable `mxCell` IDs.

#### Scenario: Existing diagram is analyzed
- **WHEN** a user supplies a supported `.drawio` document
- **THEN** the extension creates a sidecar `DiagramSpec` and leaves the source artifact byte-identical

### Requirement: Record semantic identity and digest
`DiagramSpec` SHALL record stable element identity, semantic type, label, relationships, and a deterministic semantic digest used to detect semantic drift.

#### Scenario: Layout-only geometry changes
- **WHEN** a patch changes only coordinates, size, pins, waypoints, or label offsets
- **THEN** the semantic digest remains unchanged

### Requirement: Record source provenance and priority
The working model SHALL record source references with kind, URI, revision, fragment, content hash, and confidence and SHALL resolve them in the order explicit user decision, confirmed clarification, selected OpenSpec, existing diagram, then agent assumption.

#### Scenario: User description conflicts with OpenSpec
- **WHEN** current user intent conflicts with a selected OpenSpec requirement
- **THEN** the extension presents the conflict and does not silently override either source

#### Scenario: No relevant OpenSpec exists
- **WHEN** repository discovery finds no relevant OpenSpec
- **THEN** analysis continues using available sources and records the absence without creating a new specification automatically

### Requirement: Compare process information with diagram content
The extension SHALL compare user-provided process information with represented diagram elements and SHALL summarize the semantic additions, removals, or changes before applying them.

#### Scenario: User describes a missing return loop
- **WHEN** a process description contains a failure return path absent from the diagram
- **THEN** the extension identifies the missing loop as a semantic change requiring approval

