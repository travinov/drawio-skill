# drawio-artifact-validation Specification

## Purpose
TBD - created by archiving change harden-drawio-skill-contracts. Update Purpose after archive.
## Requirements
### Requirement: Validate draw.io document structure
The artifact validator SHALL parse generated `.drawio` XML and report malformed XML, missing graph roots, duplicate cell ids, unresolved parent/source/target references, invalid vertex/edge contracts, and non-finite or invalid geometry as stable structural findings.

#### Scenario: Generated document has valid structure
- **WHEN** a generated document contains a valid draw.io graph root, unique cells, resolved references, and valid geometry
- **THEN** structural artifact validation succeeds

#### Scenario: Edge target is missing
- **WHEN** an edge references a target cell id that does not exist
- **THEN** validation reports an error-level artifact reference finding for that edge

#### Scenario: Cell id is duplicated
- **WHEN** two cells in the same graph use the same id
- **THEN** validation reports an error-level duplicate-cell finding

### Requirement: Verify generated label fidelity
Source-aware artifact validation SHALL compare mapped source labels with decoded draw.io cell values and SHALL require exact preservation of Unicode, XML-special characters, quotes, and line breaks.

#### Scenario: Label contains XML-special characters
- **WHEN** a source label contains `&`, `<`, `>`, or quotes and the generator writes the artifact
- **THEN** the decoded draw.io value equals the original label exactly and contains no double-escaped entity text

#### Scenario: Label contains Cyrillic and line breaks
- **WHEN** a source label contains Cyrillic text and embedded newlines
- **THEN** source-aware validation confirms the same Unicode text and line boundaries in the mapped cell

#### Scenario: Label mapping is missing
- **WHEN** a required source label has no mapped generated cell
- **THEN** validation reports an error-level text-integrity finding instead of accepting parseable XML

### Requirement: Validate generator-specific semantic coordinates
The artifact validator SHALL accept a source model and generator profile and SHALL verify the semantic relationship between source data and generated X/Y coordinates.

#### Scenario: Roadmap profile is valid
- **WHEN** a roadmap artifact places items according to its selected scale and contains each item in its resolved lane
- **THEN** roadmap coordinate validation succeeds

#### Scenario: Roadmap item is placed in the wrong lane
- **WHEN** a roadmap task or milestone cell falls outside the Y bounds of its resolved lane
- **THEN** validation reports an error-level lane-coordinate finding

#### Scenario: Git-flow chronology is reversed
- **WHEN** a later normalized git-flow event is placed to the left of an earlier event without being an allowed same-slot tie
- **THEN** validation reports an error-level chronology-coordinate finding

#### Scenario: Git-flow event is placed on the wrong branch lane
- **WHEN** a generated event marker is not vertically contained by its resolved branch lane
- **THEN** validation reports an error-level branch-coordinate finding

### Requirement: Verify semantic artifact coverage
Source-aware validation SHALL verify that source entities and relationships promised by the generator contract have corresponding artifact cells, edges, or report records.

#### Scenario: Roadmap outcome link is accepted
- **WHEN** a roadmap task or milestone references an outcome
- **THEN** the artifact contains the documented outcome annotation for that item or validation reports an error

#### Scenario: Roadmap risk and status are accepted
- **WHEN** an item declares both risk and status
- **THEN** validation confirms both documented visual channels are present and neither value was silently discarded

#### Scenario: Dependency edge is missing
- **WHEN** a source roadmap dependency or git-flow sequence/branch/merge relationship requires an edge and the edge is absent
- **THEN** validation reports an error-level semantic-coverage finding

### Requirement: Verify deterministic generation
The verification workflow SHALL generate an artifact twice from the same normalized source and options and SHALL require byte-identical `.drawio` output.

#### Scenario: Identical inputs are generated twice
- **WHEN** the same normalized input and routing options are used in two clean runs
- **THEN** the resulting `.drawio` files are byte-identical

#### Scenario: Output contains volatile metadata
- **WHEN** timestamps, random ids, environment paths, or unstable iteration cause otherwise identical runs to differ
- **THEN** deterministic verification fails and identifies the first differing artifact location when practical

### Requirement: Provide an explicit real-export smoke check
The skill SHALL provide an explicit smoke check that invokes an installed draw.io CLI locally, exports the generated artifact to PNG, and verifies a non-empty PNG signature and terminal `IEND` chunk.

#### Scenario: Draw.io CLI exports a valid PNG
- **WHEN** the configured draw.io CLI is available and successfully exports the artifact
- **THEN** the smoke check validates the PNG framing and reports success

#### Scenario: Draw.io CLI is unavailable
- **WHEN** export smoke is requested but no configured draw.io CLI executable is available
- **THEN** the check returns a non-success unavailable finding with a copyable remediation action and does not report the export as passed

#### Scenario: Export is truncated
- **WHEN** the CLI creates a file without a valid PNG signature or terminal `IEND` chunk
- **THEN** the smoke check reports an error-level export-integrity finding

### Requirement: Emit a stable artifact validation report
Artifact validation SHALL emit a versioned machine-readable finding envelope with stable codes and paths. Report v2 SHALL include `finding_id`, legacy primary `element`, `elements[]`, geometry evidence when available, remediation class, reconstructability, validator/tool version, and artifact hash, and SHALL support strict promotion of warnings without changing finding identity.

#### Scenario: Artifact has warnings only in relaxed mode
- **WHEN** artifact checks find readability warnings but no errors and strict mode is disabled
- **THEN** validation exits successfully while preserving the warnings and v2 metadata in the report

#### Scenario: Artifact has warnings in strict mode
- **WHEN** the same artifact is validated in strict mode
- **THEN** warnings are promoted to errors, validation exits non-zero, and finding IDs, codes, paths, elements, and geometry remain unchanged

#### Scenario: Finding involves multiple cells
- **WHEN** a crossing, overlap, or route-through finding relates to multiple diagram elements
- **THEN** the report identifies every involved stable cell ID in `elements[]` while retaining a primary `element` for legacy consumers

#### Scenario: Report is bound to an artifact
- **WHEN** validation reads a draw.io artifact
- **THEN** the report records that artifact's SHA-256 and validator identity so a receipt can prove the exact validated input
