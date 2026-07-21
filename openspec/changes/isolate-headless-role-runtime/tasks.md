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
