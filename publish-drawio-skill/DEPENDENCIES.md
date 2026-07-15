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
