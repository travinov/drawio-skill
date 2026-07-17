# Skill release workflow

Builds two independent installable artifacts:

- `dist/drawio-skill-agent-extension.zip`
- `dist/bpmn-architect-skill.zip`

Install either archive independently for GigaCode CLI:

```bash
mkdir -p ~/.gigacode/skills
unzip dist/drawio-skill-agent-extension.zip -d ~/.gigacode/skills
unzip dist/bpmn-architect-skill.zip -d ~/.gigacode/skills
```

The archive roots are `drawio-skill/` and `bpmn-architect/`; there is no
umbrella extension and neither skill requires the other.

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
