## ADDED Requirements

### Requirement: Emit hash-bound validation receipts
Each validation attempt SHALL produce a receipt containing artifact SHA-256, exact argument-array command, exit code, validator version and file hash, report/stdout/stderr hashes, timestamps, platform, and relevant tool versions.

#### Scenario: Strict validation completes
- **WHEN** the supervisor invokes strict validation for a candidate
- **THEN** it writes a receipt that cryptographically identifies the candidate and all captured validation outputs

### Requirement: Maintain an append-only run ledger
The extension SHALL append JSON Lines events for run creation, source selection, model resolution, patch attempts, candidate decisions, user decisions, validation receipts, and terminal state changes.

#### Scenario: Repair candidate is rejected
- **WHEN** comparison rejects a candidate
- **THEN** the ledger retains the attempted artifact hash, report hash, quality vector, reasons, and baseline hash

### Requirement: Verify evidence before reporting success
The extension SHALL provide an evidence verification command that recomputes artifact and report hashes and rejects missing, mismatched, or non-zero strict validation receipts.

#### Scenario: Receipt was copied from another artifact
- **WHEN** receipt verification finds a different artifact SHA-256
- **THEN** verification fails and success is not reported

