## ADDED Requirements

### Requirement: Every activated role has verified isolated execution
The host MUST invoke each activated Supervisor, Semantic Analyst, Repair, and Reviewer role through the configured isolated CLI model route and MUST fail that role step when effective-model proof is absent or mismatched.

#### Scenario: Interactive and Supervisor models differ
- **WHEN** the command is started in an interactive session using a model different from the Supervisor route
- **THEN** the trace SHALL show a distinct isolated Supervisor invocation and its verified effective model

#### Scenario: Role model proof mismatch
- **WHEN** the isolated process reports an effective model different from the requested route
- **THEN** the host SHALL record a failed role receipt and SHALL NOT consume the output as an approved role result

### Requirement: Role routing is visible without chat statistics
The host SHALL persist requested and resolved model identifiers, provider, isolation mode, fallback state, and proof source for each role invocation.

#### Scenario: User inspects a completed trace
- **WHEN** the user runs `/drawio:trace`
- **THEN** the result SHALL identify which roles ran, which model each used, and whether every model proof passed

## MODIFIED Requirements

### Requirement: Record model fallback and degradation
Model resolution SHALL record `requested_model`, `resolved_model`, provider, resolution mode, and `fallback_used`. The adapter SHALL prefer a verified isolated CLI invocation with an explicit model argument. For `/drawio:create`, `/drawio:improve`, and `/drawio:resume`, every activated role MUST use the exact requested isolated model with `fallback_used: false`; native or inherited-model degradation SHALL NOT be consumed as a valid role result. Other diagnostic callers MAY record a degraded resolution, but such a result SHALL NOT prove independent multi-model execution.

#### Scenario: GigaCode headless model is proven
- **WHEN** an isolated role exits successfully with schema-valid output
- **THEN** `system.model`, every `assistant.message.model`, and `result.stats.models` agree with the requested model before `model_resolved` is appended

#### Scenario: Model evidence is missing or inconsistent
- **WHEN** any required GigaCode model evidence is absent or names a different primary model
- **THEN** the role fails closed without publishing its output or any success event and the interactive model remains unchanged

#### Scenario: Requested lifecycle model is unavailable
- **WHEN** a lifecycle role cannot use its requested model
- **THEN** the role step SHALL fail with a visible blocker and trace receipt, `fallback_used` SHALL remain false, and the host SHALL NOT execute that role with the interactive or inherited model

#### Scenario: Diagnostic caller records degradation
- **WHEN** a non-lifecycle diagnostic caller explicitly permits a native or inherited route
- **THEN** the run SHALL record the fallback and the user-visible result SHALL state that model diversity was degraded and unproven
