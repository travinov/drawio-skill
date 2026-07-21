## 1. Runtime isolation

- [x] 1.1 Require and record the Qwen/GigaCode headless isolation capabilities used by every role invocation
- [x] 1.2 Build extension-free, tool-excluded, turn-bounded commands with a role system prompt and stdin runtime JSON
- [x] 1.3 Reject customization leakage and every role tool call from captured GigaCode events with actionable evidence

## 2. Regression coverage

- [x] 2.1 Add a minimized fixture reproducing the corporate recursive Supervisor/tool-loop event stream
- [x] 2.2 Add tests for the isolated command, clean schema-valid role output, leakage, tool calls, and missing capabilities
- [x] 2.3 Update runtime and operator documentation with the isolation contract and corporate trace expectations

## 3. Release

- [x] 3.1 Bump the side-by-side extension release and installer/verifier expectations
- [x] 3.2 Run targeted runtime/orchestrator tests, full skill/release checks, strict OpenSpec validation, and self-check
- [x] 3.3 Build the ZIP, verify its checksum and contents, commit the release branch, and publish it for corporate retest

## 4. Corporate turn-limit follow-up

- [x] 4.1 Remove all core tools from isolated role discovery with a capability-checked allowlist sentinel while retaining deny-list and event-audit defenses
- [x] 4.2 Persist and hash stdout plus redacted stderr before non-zero exit handling, and record failure evidence in the manifest
- [x] 4.3 Extend `/drawio:trace` and host results with successful isolation proof and failed-role capture diagnostics
- [x] 4.4 Add regression tests for `FatalTurnLimitedError`, tool-registry isolation, failed captures, and trace evidence
- [x] 4.5 Bump, build, verify, commit, and publish the side-by-side `1.23.0-corporate.4` ZIP

## 5. Corporate Plan-mode conflict follow-up

- [x] 5.1 Replace Plan approval with default approval only for isolated tool-free JSON roles and record the selected mode in evidence
- [x] 5.2 Add regression coverage proving the command avoids the Qwen 0.13.1 `exit_plan_mode` reminder contract while retaining zero-tool isolation
- [x] 5.3 Update the routing and operator contract with the verified Plan-mode root cause
- [x] 5.4 Bump, build, verify, commit, and publish the side-by-side `1.23.0-corporate.5` ZIP

## 6. Corporate streamed evidence and bounded Supervisor recovery

- [x] 6.1 Capture capability-detected `stream-json` JSONL while preserving buffered JSON compatibility and partial failure evidence
- [x] 6.2 Add one policy-controlled Supervisor fallback on `turn_limit`, separate attempt artifacts, and recovered-versus-terminal failure semantics
- [x] 6.3 Teach host results and `/drawio:trace` to verify and expose the approved degraded path without weakening isolation checks
- [x] 6.4 Add regression coverage for JSONL model proof, partial turn-limit evidence, one fallback only, fail-closed isolation, and trace integrity
- [x] 6.5 Bump, build, verify, commit, and publish the side-by-side `1.23.0-corporate.6` ZIP

## 7. Corporate Qwen custom-command argument transport

- [x] 7.1 Specify one shell-escaped raw argument value, safe internal tokenization, Draw.io `@` normalization, and host-owned option rejection
- [x] 7.2 Apply the bridge to create, improve, review, resume, and trace, and make every generated `next_commands` value executable through it
- [x] 7.3 Add regression coverage for the exact quoted, `@`-prefixed, advanced-flag, resume-feedback, and explicit-trace cases captured on corporate GigaCode
- [x] 7.4 Extend installer verification and operator documentation with the command-transport contract
- [x] 7.5 Bump, build, verify, commit, and publish the side-by-side `1.23.0-corporate.7` ZIP

## 8. Zero-argument review-to-improve handoff

- [x] 8.1 Specify deterministic, hash-bound review handoff selection, one-diagram fallback, default repair intent, and explicit override precedence
- [x] 8.2 Implement bare `/drawio:improve`, persist its resolution evidence, and publish it as the primary review continuation
- [x] 8.3 Add regression coverage for fresh, stale, missing, ambiguous, and explicitly overridden improve inputs
- [x] 8.4 Update command, operator, installer, and verifier contracts for the zero-argument workflow
- [x] 8.5 Bump, build, verify, commit, and publish the side-by-side `1.23.0-corporate.8` ZIP

## 9. Empty MCP discovery for isolated roles

- [x] 9.1 Specify an explicitly empty MCP-server allowlist, required capability detection, fail-closed leakage behavior, and corporate retest contract
- [x] 9.2 Add the empty allowlist to every role command and record it in isolation evidence
- [x] 9.3 Add regression coverage for global MCP configuration, missing CLI capability, exact command arguments, and defense-in-depth event rejection
- [x] 9.4 Extend installer verification and operator documentation with the MCP-discovery isolation contract
- [x] 9.5 Bump, build, verify, commit, and publish the side-by-side `1.23.0-corporate.9` ZIP

## 10. Supervisor downstream-role normalization

- [x] 10.1 Specify `required_roles` as downstream sibling selection while retaining Supervisor in host-owned bookkeeping
- [x] 10.2 Normalize only the already executed Supervisor role and preserve every sibling/action fail-closed invariant
- [x] 10.3 Add regression coverage and operator diagnostics for the captured schema-valid self-omitting Supervisor decision
- [x] 10.4 Bump, build, verify, commit, and publish the side-by-side `1.23.0-corporate.10` ZIP

## 11. Host-owned lifecycle role policy

- [x] 11.1 Specify phase-mandatory role authorization and separate model-versus-host evidence
- [x] 11.2 Implement the phase role union while retaining action, schema, isolation, model-proof, and semantic-approval gates
- [x] 11.3 Add exact corporate-output regressions plus command, operator, and installer verification contracts
- [x] 11.4 Bump, build, verify, commit, and publish the side-by-side `1.23.0-corporate.11` ZIP

## 12. Host-bound Reviewer verdict and unified trace selection

- [x] 12.1 Specify deterministic Reviewer evidence binding and first-class read-only review tracing
- [x] 12.2 Implement Reviewer analytical output normalization with preserved raw and binding evidence
- [x] 12.3 Persist review workflow state and resolve trace references by directory or persisted run ID
- [x] 12.4 Add corporate-export regressions, verifier markers, and operator documentation
- [ ] 12.5 Bump, build, verify, commit, and publish the side-by-side `1.23.0-corporate.12` ZIP
