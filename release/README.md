# Skill release workflow

Builds two independent installable artifacts:

- `dist/drawio-skill-agent-extension.zip`
- `dist/bpmn-architect-skill.zip`

Install the Draw.io agent package as a native GigaCode extension. Do not unpack
it into `skills`, because the agent extension and the legacy drawio skill would
compete for the same requests:

```bash
unzip dist/drawio-skill-agent-extension.zip -d ~/Downloads
cd ~/Downloads/drawio-skill
chmod +x install/*.sh
./install/install_drawio_agent_extension.sh
```

The installer defaults to `/Users/travinov-sv/.gigacode/bin/gigacode` through
`$HOME/.gigacode`, backs up an active legacy skill/extension, validates the
complete internal manifest of the extracted package, and calls native
`gigacode extensions install` and, when supported by the installed CLI,
`gigacode extensions validate`. All paths can be
overridden with the environment variables printed by `--help`. Roll back the
latest installation with:

```bash
./install/rollback_drawio_agent_extension.sh --latest
```

The BPMN archive remains an independent skill:

```bash
mkdir -p ~/.gigacode/skills
unzip dist/bpmn-architect-skill.zip -d ~/.gigacode/skills
```

The archive roots are `drawio-skill/` and `bpmn-architect/`; there is no
umbrella extension and neither skill requires the other.

In corporate GigaCode 26.5.17 the deterministic Draw.io command host performs
`host-preflight` and invokes isolated Supervisor, Semantic Analyst, Repair, and
Reviewer roles itself. Native agent visibility and parent `/stats model` are not
execution evidence.

The package exposes these deterministic entry points:

```text
/drawio:review "/absolute/path/to/diagram.drawio"
/drawio:create --diagram "/absolute/path/to/new.drawio" --request "process description"
/drawio:improve --diagram "/absolute/path/to/existing.drawio" --request "required changes"
/drawio:resume --run "<run-id>" --decision continue --feedback "correction"
/drawio:trace --run "<run-id>"
```

Run from the repository root:

```bash
# Verify installed tools and configured package registries.
python3 scripts/release_skills.py preflight --registry

# Build deterministic archives and basename-only checksum files.
python3 scripts/release_skills.py build

# Compare source files with ZIP contents and run smoke checks from clean extracts.
python3 scripts/release_skills.py verify

# Run every gate in order.
python3 scripts/release_skills.py all --registry

# Verify final checksums from the only supported working directory.
cd dist && shasum -a 256 -c SHA256SUMS.txt
```

Use `--skill drawio` or `--skill bpmn` for one product. Graphviz is optional for the draw.io skill because builtin git-flow routing is required to remain available.

The build command does not publish releases. Publish only archives that pass `verify` from their unpacked temporary installations.
