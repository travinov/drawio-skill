## 1. Contracts and portable agent configuration

- [x] 1.1 Add versioned schemas for `DiagramSpec`, diagram patches, model routing, run events, and validation receipts
- [x] 1.2 Add default role-to-model routing and portable prompts for Supervisor, Reviewer, Repair, and Semantic Analyst roles
- [x] 1.3 Document native per-agent, isolated CLI, and inherited-model fallback behavior without changing the global interactive model

## 2. Working model and transactional repair

- [x] 2.1 Implement safe draw.io ingestion with stable IDs, source references, semantic digest, and resource limits
- [x] 2.2 Implement preconditioned atomic patch operations and rollback metadata while preserving unknown XML
- [x] 2.3 Implement local orthogonal routing for repairable straight waypoint-free edges and affected-region tracking
- [x] 2.4 Implement semantic/layout diff and ordered monotonic candidate comparison

## 3. Validation evidence and orchestration

- [x] 3.1 Extend validation output additively to report v2 with stable finding IDs, multiple elements, geometry, remediation metadata, validator identity, and artifact hash
- [x] 3.2 Implement strict validator invocation, captured output hashing, validation receipts, and receipt verification
- [x] 3.3 Implement append-only JSONL run events, persisted state transitions, cycle/plateau detection, resume, stop, manual handoff, and completion gating
- [x] 3.4 Implement consolidated human checkpoints and separate semantic/layout summaries

## 4. Skill integration and compatibility

- [x] 4.1 Update the skill instructions and references to route new/existing diagrams through the supervisor workflow when requested
- [x] 4.2 Add GigaCode/Qwen/Gemini runtime adapter guidance and keep existing generator and validator entry points compatible

## 5. Verification

- [x] 5.1 Add fixtures and focused tests for import preservation, semantic digest stability, patch rollback, routing, monotonic comparison, evidence tampering, state resume, and model fallback
- [x] 5.2 Run existing relevant tests, new supervisor tests, script help/smoke checks, and strict OpenSpec validation
- [x] 5.3 Perform final architecture and evidence review, address actionable findings, and record any intentionally deferred limitations
