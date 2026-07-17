## ADDED Requirements

### Requirement: Resolve a model independently for each logical role
The extension SHALL resolve Supervisor, Reviewer, Repair, and Semantic Analyst model assignments independently and SHALL NOT silently change the global interactive model.

#### Scenario: Native per-agent model override is available
- **WHEN** the runtime supports a role-specific model declaration
- **THEN** each role uses its configured model and records requested and resolved model identifiers

### Requirement: Use the approved default role mapping
The default routing policy SHALL request `GigaChat-3-Ultra` for Supervisor, `DeepSeek-V4-Flash` for Reviewer, `vllm/MiniMax-M3-113k` for Repair, and `vllm/Qwen3.6-35B-262k` for Semantic Analyst/Arbiter.

#### Scenario: Normal layout run has no semantic ambiguity
- **WHEN** a run needs supervision and independent review but no repair or arbitration
- **THEN** only the Supervisor and Reviewer roles are required

### Requirement: Record model fallback and degradation
Model resolution SHALL record `requested_model`, `resolved_model`, provider, resolution mode, and `fallback_used`. If native overrides are unavailable, the adapter SHALL prefer an isolated CLI invocation with an explicit model argument before inherited-model degradation.

#### Scenario: Requested model is unavailable
- **WHEN** a role cannot use its requested model
- **THEN** the run records the fallback and the user-visible result states that model diversity was degraded

### Requirement: Keep independent review read-only
The Reviewer role SHALL be unable to publish or mutate candidate artifacts and SHALL return findings and a verdict only.

#### Scenario: Reviewer recommends a repair
- **WHEN** the Reviewer detects a problem
- **THEN** it emits a finding for the Supervisor or Repair role and does not edit the diagram itself

