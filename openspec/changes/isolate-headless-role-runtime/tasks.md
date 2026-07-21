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
