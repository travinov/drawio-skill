# Origin

This repository mirrors the original skill folder from Agents365-ai/drawio-skill.

- Source repository: https://github.com/Agents365-ai/drawio-skill
- Source path: skills/drawio-skill
- Source commit: 4cb39bbeab09f1caa6959d3f60ef56e3cb685f08
- Skill version: 1.19.0

## Corporate changes

- Added `DRAWIO_BIN` and `~/.drawio-skill/config.json` guidance for non-standard draw.io executable paths.
- Added macOS and Windows internal marketplace / SberUserSoft install guidance.
- Replaced Russian README with `metadata.md` in the requested extension metadata format.
- Disabled external CDN icon resolution in `scripts/aiicons.py`.
- Added hybrid timeline-aware git-flow diagram generation and validation (`scripts/gitflow.py`, `scripts/gitflow_validate.py`).
- Added general Diagram Intake Agent guidance for clarifying broad natural-language diagram requests.
- Added Russian `README.md` with extension overview, usage examples, supported diagrams, installation, and validation guidance.
- Packaged version: 1.19.0-corporate.9
