# diagram-model-routing Specification

## Purpose
Define auditable per-role model routing for diagram agents while preserving the
interactive session model and failing closed when the runtime cannot prove which
model produced a role result.
## Requirements
### Requirement: Resolve a model independently for each logical role
The extension SHALL resolve Supervisor, Reviewer, Repair, and Semantic Analyst model assignments independently and SHALL NOT silently change the global interactive model.

#### Scenario: Native per-agent model override is available
- **WHEN** the runtime supports a role-specific model declaration
- **THEN** each role uses its configured model and records requested and resolved model identifiers

#### Scenario: Corporate GigaCode inherits the parent model for native agents
- **WHEN** GigaCode 26.5.17 / Qwen Code 0.13.1 exposes native agents without a model selector
- **THEN** the extension uses an isolated headless CLI with an explicit model before considering native or inherited routing

### Requirement: Use the approved default role mapping
The default routing policy SHALL request `GigaChat-3-Ultra` for Supervisor, `vllm/DeepSeek-V4-Flash-262k` for Reviewer, `vllm/MiniMax-M3-113k` for Repair, and `vllm/Qwen3.6-35B-262k` for Semantic Analyst/Arbiter.

#### Scenario: Normal layout run has no semantic ambiguity
- **WHEN** a run needs supervision and independent review but no repair or arbitration
- **THEN** only the Supervisor and Reviewer roles are required

### Requirement: Record model fallback and degradation
Model resolution SHALL record `requested_model`, `resolved_model`, provider, resolution mode, and `fallback_used`. The adapter SHALL prefer a verified isolated CLI invocation with an explicit model argument, use native routing only when the runtime proves its model override, and use inherited-model degradation last.

#### Scenario: GigaCode headless model is proven
- **WHEN** an isolated role exits successfully with schema-valid output
- **THEN** `system.model`, every `assistant.message.model`, and `result.stats.models` agree with the requested model before `model_resolved` is appended

#### Scenario: Model evidence is missing or inconsistent
- **WHEN** any required GigaCode model evidence is absent or names a different primary model
- **THEN** the role fails closed without publishing its output or any success event and the interactive model remains unchanged

#### Scenario: Requested model is unavailable
- **WHEN** a role cannot use its requested model
- **THEN** the run records the fallback and the user-visible result states that model diversity was degraded

### Requirement: Keep independent review read-only
The Reviewer role SHALL be unable to publish or mutate candidate artifacts and SHALL return findings and a verdict only.

#### Scenario: Reviewer recommends a repair
- **WHEN** the Reviewer detects a problem
- **THEN** it emits a finding for the Supervisor or Repair role and does not edit the diagram itself
