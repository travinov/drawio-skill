# GigaCode Draw.io extension installers

These scripts are prepared on a machine without GigaCode and are intended to
run on the corporate macOS laptop where the CLI is installed at:

```text
/Users/travinov-sv/.gigacode/bin/gigacode
/Users/travinov-sv/.gigacode/skills
/Users/travinov-sv/.gigacode/extensions
```

They never install the agent package as a skill. An active legacy
`skills/drawio-skill` is copied to a timestamped backup and removed from active
discovery before the new extension is registered.

## Offline installation from the self-contained ZIP

Transfer only `drawio-skill-agent-extension.zip` to the corporate Mac and
unpack it under `Downloads`. Then run:

```bash
cd ~/Downloads/drawio-skill
chmod +x install/*.sh
./install/install_drawio_agent_extension.sh
```

When started from `drawio-skill/install`, the installer automatically uses the
surrounding extracted extension and performs no GitHub download. An explicit
extracted path is also supported with `--source /path/to/drawio-skill`.
The dependency step may still contact the Python package registry already
configured by the corporate environment.

Use `--skip-deps` only if the locked Python dependencies are already installed
or the corporate Python environment is managed separately. Use `--dry-run` to
validate the complete internal manifest and invoke native GigaCode validation,
then show all later filesystem/install actions without executing them.

## Verification and rollback

The installer runs the bundled verifier automatically. It can also be repeated
from the extracted `drawio-skill` directory:

```bash
./install/verify_drawio_agent_extension.sh
```

After verification, restart GigaCode and run `/agents list`. Expected agents:

- `diagram-supervisor`
- `diagram-reviewer`
- `diagram-repair`
- `diagram-semantic-analyst`

`./install/rollback_drawio_agent_extension.sh --backup PATH` selects a specific backup.
Backups are stored under
`~/.gigacode/backups/drawio-agent-extension/<UTC timestamp>`.
