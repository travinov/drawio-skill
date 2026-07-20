## 1. Contracts and Schemas

- [x] 1.1 Add schema-valid Supervisor and Semantic Analyst workflow outputs for creation, reconciliation, and next-action decisions
- [x] 1.2 Extend run-event and host-result evidence contracts for role/tool start, finish, failure, checkpoints, model proof, and trace verification
- [x] 1.3 Add deterministic unit fixtures for generic diagram plans, existing diagrams, patches, reviewer verdicts, and human decisions

## 2. Deterministic Orchestration Host

- [x] 2.1 Implement create/improve run initialization, normalized paths, accepted-candidate storage, and atomic state writes
- [x] 2.2 Implement structured semantic-plan normalization and deterministic generic draw.io rendering with explicit routable edge geometry
- [x] 2.3 Implement isolated Supervisor, Semantic Analyst, Repair, and Reviewer invocation with hash-bound role receipts and verified models
- [x] 2.4 Implement the closed repair loop from last accepted candidate through patch application, strict validation, monotonic comparison, review, acceptance or rejection
- [x] 2.5 Implement bounded iterations, repeated-hash cycle detection, plateau classification, and consolidated checkpoints
- [x] 2.6 Implement resume decisions, immutable feedback inputs, stop/manual-handoff terminals, and atomic approved publication
- [x] 2.7 Implement read-only consolidated trace reconstruction and verification

## 3. GigaCode Command Surface

- [x] 3.1 Add deterministic `/drawio:create`, `/drawio:improve`, `/drawio:resume`, and `/drawio:trace` command definitions
- [x] 3.2 Update role prompts so outputs match the typed contracts and roles do not mutate files or recursively delegate
- [x] 3.3 Update SKILL and reference documentation with command syntax, state/checkpoint behavior, model routing, trace evidence, and corporate examples

## 4. Packaging and Corporate Installation

- [x] 4.1 Bump extension and release metadata to `1.23.0-corporate.1` while preserving the previous release branch
- [x] 4.2 Update installer, verifier, rollback, manifest, and release inventory checks for all command-host files and schemas
- [x] 4.3 Build a self-contained offline ZIP with executable installation scripts and verify its checksum and contents

## 5. Verification

- [x] 5.1 Add unit tests for all command modes, role routing proof, monotonic acceptance/rejection, cycle/plateau behavior, resume decisions, atomic publication, and tamper-detecting trace
- [x] 5.2 Run targeted orchestration, host, validator, installer, and release tests
- [x] 5.3 Run OpenSpec strict validation, skill quick validation, extension self-check, and a local forward test with stub isolated roles
- [x] 5.4 Perform final architecture and regression review, fix blocking findings, commit, push the new branch, and publish the versioned ZIP link
