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
Finder-created `.DS_Store` metadata is ignored during the strict inventory check.
The dependency step may still contact the Python package registry already
configured by the corporate environment.

Corporate mode disables `gigacode extensions update`. To upgrade, transfer and
unpack the new ZIP, then run its bundled installer again. Do not manually delete
the previous extension: the installer backs it up before local reinstall.

Use `--skip-deps` only if the locked Python dependencies are already installed
or the corporate Python environment is managed separately. Use `--dry-run` to
validate the complete internal manifest, invoke native GigaCode validation when
the installed CLI supports it, then show all later filesystem/install actions
without executing them.

## Verification and rollback

The installer runs the bundled verifier automatically. It can also be repeated
from the extracted `drawio-skill` directory:

```bash
./install/verify_drawio_agent_extension.sh
```

Verification requires `--allowed-mcp-server-names` in corporate
`gigacode --help` and confirms that every isolated role passes that option with
one empty value. This removes globally configured Jira, Bitbucket, and other MCP
servers before the child model discovers tool schemas. A missing flag is a
preflight failure; do not continue with an unisolated role process.

After verification, restart GigaCode and run `/agents manage`. Expected extension agents:

- `diagram-supervisor`
- `diagram-reviewer`
- `diagram-repair`
- `diagram-semantic-analyst`

Then open the diagram project as the GigaCode working directory and run:

```text
/drawio:review
/drawio:create "what to show"
/drawio:improve
/drawio:improve "optional additional change"
/drawio:resume continue "additional requirement"
/drawio:resume approve
/drawio:trace
```

The command creates `.diagram-runs/<run-id>` itself. Do not create that
directory manually and do not ask the chat model to execute the workflow step
by step.

After a completed `/drawio:review`, the normal continuation is the bare
`/drawio:improve`. It reuses the reviewed diagram only while its SHA-256 still
matches. With no eligible review it can select one root-level `.drawio`; with
multiple candidates it stops before model execution and asks for an explicit
selection.

The package uses Qwen Code's canonical Markdown command format under
`commands/drawio/`. Every active command therefore remains covered by
the package manifest's exact checksum instead of relying on a TOML migration.
The installer invokes the bundled verifier through `/bin/bash`, so Finder/ZIP
executable-bit loss does not skip verification.

For a real diagram task, the main interactive GigaChat session owns execution;
it does not send the whole workflow to native `diagram-supervisor`. A successful
run creates `.diagram-runs/<run-id>/host-preflight.json`,
`run-manifest.jsonl`, validation receipts, and isolated-role model evidence in
the user's project. The native supervisor entry remains visible for advisory
planning compatibility only.

In `1.23.0-corporate.11` and newer, Supervisor `required_roles` is an advisory
downstream list. The host records the model-declared list separately, adds the
phase-mandatory lifecycle roles, and records the effective union. Initial
create/improve always authorizes Semantic Analyst, conditional Repair, and
Reviewer; resume authorizes conditional Repair and Reviewer. Incompatible
actions and all isolation, schema, and model-proof gates still fail closed.

In `1.23.0-corporate.12` and newer, Reviewer returns analysis rather than
copying host-known run and SHA fields. The host publishes the final hash-bound
verdict and exposes `binding_proof`; a legacy declared mismatch is diagnostic,
not an opportunity to replace the trusted input value. Read-only review also
persists `workflow.json`, so bare and explicit `/drawio:trace` select that same
fresh review instead of an older improve run.

`./install/rollback_drawio_agent_extension.sh --backup PATH` selects a specific backup.
Backups are stored under
`~/.gigacode/backups/drawio-agent-extension/<UTC timestamp>`.
