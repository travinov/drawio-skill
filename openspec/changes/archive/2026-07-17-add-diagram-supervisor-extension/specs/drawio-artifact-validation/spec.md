## MODIFIED Requirements

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

