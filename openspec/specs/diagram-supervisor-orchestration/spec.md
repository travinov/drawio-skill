# diagram-supervisor-orchestration Specification

## Purpose
TBD - created by archiving change add-diagram-supervisor-extension. Update Purpose after archive.
## Requirements
### Requirement: Persist an explicit supervisor state machine
The extension SHALL persist each run state and SHALL support `analyzed`, `awaiting_decision`, `patching`, `validating`, `retrying`, `plateau`, `awaiting_feedback`, `final_review`, `completed`, `manual_handoff`, and `stopped` outcomes without losing the last accepted candidate.

#### Scenario: Corporate main host starts a run
- **WHEN** the workflow runs on corporate GigaCode 26.5.17
- **THEN** the main extension host completes a deterministic preflight and records `host-preflight.json` plus a `host_preflight` manifest event before diagram analysis

#### Scenario: User invokes deterministic review command
- **WHEN** the user runs `/drawio:review` with a `.drawio` path inside the current workspace
- **THEN** the custom command host creates a unique run directory and completes preflight, strict validation, and isolated independent review before the interactive model receives the structured result

#### Scenario: Interactive model cannot select workflow tools
- **WHEN** `/drawio:review` is running
- **THEN** correctness does not depend on the interactive model choosing directory, search, shell, or native agent tools

#### Scenario: Host evidence is absent
- **WHEN** the run lacks main-host preflight evidence
- **THEN** the workflow fails closed and does not claim that its agent, tools, or validation executed

#### Scenario: Validation finds repairable defects
- **WHEN** an analyzed diagram has deterministic repairable findings
- **THEN** the supervisor transitions through patching and validating from the last accepted candidate

#### Scenario: User resumes after clarification
- **WHEN** a run in `awaiting_feedback` receives clarification
- **THEN** the supervisor resumes the same run with its accepted baseline, manifest, decisions, and findings intact

### Requirement: Iterate from the last accepted candidate
The supervisor SHALL use only the last accepted candidate as the baseline for a subsequent repair and SHALL never promote a rejected candidate.

#### Scenario: Candidate regresses a higher-priority category
- **WHEN** candidate comparison rejects a repair
- **THEN** the next repair starts from the previous accepted artifact and the rejection is recorded

### Requirement: Detect cycles and plateaus
The supervisor SHALL detect repeated artifact hashes, repeated quality vectors, exhausted repair classes, and configured iteration limits and SHALL transition to plateau handling instead of restarting random regeneration.

#### Scenario: Repair repeats an earlier candidate
- **WHEN** a candidate hash has already appeared in the run
- **THEN** the supervisor records a cycle and requests consolidated feedback or manual handoff

### Requirement: Gate completion on exact validation evidence
The supervisor MUST NOT enter `completed` unless strict validation succeeded and the validation receipt artifact hash equals the current final artifact hash.

#### Scenario: Artifact changes after validation
- **WHEN** the final file hash differs from the receipt artifact hash
- **THEN** completion is refused and validation must run again
