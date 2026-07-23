# Runtime dependencies

Install from the package source already configured for the Python environment:

```bash
python3 -m pip install -r requirements.txt
```

Supported ranges:

- `PyYAML>=6.0,<7`
- `jsonschema>=4.18,<5`
- `openpyxl>=3.1,<4`

Availability was verified on 2026-07-09 without changing the configured package
source. The tested environment resolved and imported PyYAML 6.0.3 and
jsonschema 4.26.0, and openpyxl 3.1.5.

Supported interpreter: Python 3.11-3.14.

Run `python3 scripts/self_check.py --check-registry` before first use to verify
both registry resolution and the installed runtime. The check never adds or
changes an index URL.

## Optional offline ELK layout backend

The extension includes the exact `elkjs@0.11.1` runtime bundle and upstream
license under `vendor/elkjs/`. `vendor/elkjs/NOTICE.json` records the npm
package, upstream repository, tarball integrity, tarball SHA256, and SHA256 of
the committed bundle and license. The committed bundle is the runtime source of
truth: installation and runtime never invoke `npm`, `npx`, a package registry,
`curl`, or any network/package resolver.

Node.js is optional when `layout_backend` is `auto`. The host accepts a Node
executable only after both `node --version` and the bundled JSON bridge probe
succeed. Set `node_bin` to an approved absolute executable path to pin it, or
leave it `null` for verified `PATH` discovery. If no verified Node executable
is available, `auto` deterministically uses the bundled Python layout backend.
Explicit `elk` mode fails closed when Node cannot be verified.

Configuration:

```json
{
  "drawio_bin": null,
  "node_bin": null,
  "layout_backend": "auto",
  "layout_timeout_seconds": 30,
  "layout_wall_clock_seconds": 180
}
```

`layout_backend` accepts:

- `auto`: verified vendored ELK first, then the Python backend on any bounded
  ELK execution or contract failure;
- `elk`: require a verified Node executable, while still falling back to
  Python if an ELK execution/result fails after Node verification;
- `python`: do not resolve or start Node;
- `legacy-generic-v2`: explicit compatibility renderer selection only; it is
  never an automatic layout default.

`layout_timeout_seconds` bounds one ELK subprocess. The lifecycle host owns the
larger `layout_wall_clock_seconds` budget across a finite strategy set.
