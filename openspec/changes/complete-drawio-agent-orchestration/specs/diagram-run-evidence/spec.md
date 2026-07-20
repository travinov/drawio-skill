## ADDED Requirements

### Requirement: Complete invocation receipts
Every role invocation and deterministic tool step SHALL produce start and finish or failure evidence bound to its input and output hashes.

#### Scenario: Repair attempt succeeds
- **WHEN** Repair proposes a patch and deterministic tools apply and validate it
- **THEN** the run SHALL contain ordered receipts for Repair input/output, patch application, validation, comparison, review, and candidate acceptance or rejection

#### Scenario: Command process fails mid-step
- **WHEN** a role or tool exits unsuccessfully
- **THEN** the run SHALL append a failure event with sanitized diagnostics and retain all previously accepted evidence

### Requirement: Consolidated verifiable trace
The trace command SHALL verify the append-only event chain, referenced artifact hashes, state transitions, model proofs re-derived from raw runtime captures and the configured routing policy, and final candidate binding before reporting the run as verified. It SHALL describe this as local evidence verification rather than external cryptographic attestation.

#### Scenario: Artifact changes after the run
- **WHEN** a referenced receipt, candidate, role output, or checkpoint is modified
- **THEN** trace verification SHALL identify the first broken binding and SHALL NOT report the run as verified
