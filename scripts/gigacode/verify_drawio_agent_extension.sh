#!/usr/bin/env bash
set -Eeuo pipefail

EXTENSION_NAME="publish-drawio-skill"
EXPECTED_VERSION="${DRAWIO_EXTENSION_VERSION:-1.22.0-corporate.2}"
GIGACODE_HOME="${GIGACODE_HOME:-$HOME/.gigacode}"
GIGACODE_BIN="${GIGACODE_BIN:-$GIGACODE_HOME/bin/gigacode}"
GIGACODE_SKILLS_DIR="${GIGACODE_SKILLS_DIR:-$GIGACODE_HOME/skills}"
GIGACODE_EXTENSIONS_DIR="${GIGACODE_EXTENSIONS_DIR:-$GIGACODE_HOME/extensions}"
GIGACODE_EXTENSION_SOURCES_DIR="${GIGACODE_EXTENSION_SOURCES_DIR:-$GIGACODE_HOME/extension-sources}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
skip_self_check=0
source_path=""

usage() {
  cat <<'EOF'
Usage: verify_drawio_agent_extension.sh [--source PATH] [--skip-self-check]

Checks the installed manifest, four agent definitions, native GigaCode
registration, legacy-skill conflicts, and the extension self-check.
EOF
}

log() { printf '[drawio-extension:verify] %s\n' "$*"; }
die() { printf '[drawio-extension:verify] ERROR: %s\n' "$*" >&2; exit 1; }

extensions_supports_validate() {
  local help_text
  help_text="$("$GIGACODE_BIN" extensions --help 2>&1 || true)"

  if grep -Eq '^[[:space:]]*gigacode[[:space:]]+extensions[[:space:]]+validate([[:space:]<]|$)' <<<"$help_text"; then
    return 0
  fi

  if grep -Eq '^[[:space:]]*(Commands:|gigacode[[:space:]]+extensions[[:space:]]+<command>)' <<<"$help_text"; then
    return 1
  fi

  "$GIGACODE_BIN" extensions validate --help >/dev/null 2>&1
}

while (($#)); do
  case "$1" in
    --source) [[ $# -ge 2 ]] || die "--source requires PATH"; source_path="$2"; shift 2 ;;
    --skip-self-check) skip_self_check=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
done

[[ -x "$GIGACODE_BIN" ]] || die "GigaCode CLI not executable: $GIGACODE_BIN"
command -v "$PYTHON_BIN" >/dev/null 2>&1 || die "Python not found: $PYTHON_BIN"

current="$GIGACODE_EXTENSION_SOURCES_DIR/$EXTENSION_NAME/current"
installed="$GIGACODE_EXTENSIONS_DIR/$EXTENSION_NAME"
[[ -f "$installed/gemini-extension.json" ]] || die "Active extension is missing: $installed/gemini-extension.json"
if [[ -n "$source_path" ]]; then
  source_path="$(cd "$source_path" 2>/dev/null && pwd)" || die "Source path not found: $source_path"
elif [[ -f "$current/gemini-extension.json" ]]; then
  source_path="$current"
else
  source_path=""
fi

"$PYTHON_BIN" - "$installed" "$source_path" "$current" "$EXPECTED_VERSION" <<'PY'
import hashlib
import json
import os
import re
import sys
from pathlib import Path, PurePosixPath

active = Path(sys.argv[1]).resolve()
source = Path(sys.argv[2]).resolve() if sys.argv[2] else None
current = Path(sys.argv[3])
expected_version = sys.argv[4]
expected_models = {
    "supervisor": "GigaChat-3-Ultra",
    "reviewer": "vllm/DeepSeek-V4-Flash-262k",
    "repair": "vllm/MiniMax-M3-113k",
    "semantic_analyst": "vllm/Qwen3.6-35B-262k",
}
agent_files = {
    "supervisor": "diagram-supervisor.md",
    "reviewer": "diagram-reviewer.md",
    "repair": "diagram-repair.md",
    "semantic_analyst": "diagram-semantic-analyst.md",
}


def fail(message):
    raise SystemExit(message)


def load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"Cannot read JSON {path}: {exc}")


def frontmatter(path):
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        fail(f"Missing agent definition {path}: {exc}")
    match = re.match(r"\A---\n(.*?)\n---\n", text, re.DOTALL)
    if not match:
        fail(f"Malformed YAML frontmatter: {path}")
    values = {}
    for line in match.group(1).splitlines():
        item = re.match(r"^([A-Za-z][A-Za-z0-9]*):\s*(.*?)\s*$", line)
        if item:
            values[item.group(1)] = item.group(2)
    return values


def verify_tree(root, label):
    manifest = load_json(root / "gemini-extension.json")
    if manifest.get("name") != "publish-drawio-skill":
        fail(f"Unexpected {label} manifest name: {manifest.get('name')!r}")
    if manifest.get("version") != expected_version:
        fail(f"Expected {label} version {expected_version}, found {manifest.get('version')!r}")
    generated = root / "gigacode-extension.json"
    if generated.exists():
        native = load_json(generated)
        for key in ("name", "version", "contextFileName"):
            if native.get(key) != manifest.get(key):
                fail(f"Active GigaCode manifest mismatch for {key}: {native.get(key)!r} != {manifest.get(key)!r}")
    policy = load_json(root / "data/model-routing.default.json")
    actual_models = {
        role: config.get("requested_model") for role, config in policy.get("roles", {}).items()
    }
    if actual_models != expected_models:
        fail(f"Unexpected {label} model routing: {actual_models!r}")
    for role, config in policy["roles"].items():
        if config.get("fallback_order") != ["isolated_cli", "native_per_agent", "inherited_current"]:
            fail(f"Unexpected {label} fallback order for {role}: {config.get('fallback_order')!r}")
    if policy["roles"]["reviewer"].get("provider") != "vllm":
        fail(f"Unexpected {label} reviewer provider")
    for role, filename in agent_files.items():
        values = frontmatter(root / "agents" / filename)
        if values.get("name") != f"diagram-{role.replace('_', '-')}":
            fail(f"Unexpected {label} agent name in {filename}: {values.get('name')!r}")
        if values.get("model") != "inherit":
            fail(f"{label} {filename} must use model: inherit; exact routing belongs in policy")
        if not re.fullmatch(r"[1-9][0-9]*", values.get("maxTurns", "")):
            fail(f"{label} {filename} must declare positive maxTurns")
        for forbidden in ("max_turns", "kind", "temperature"):
            if forbidden in values:
                fail(f"Unsupported {label} frontmatter field {forbidden} in {filename}")
        if role != "supervisor" and values.get("approvalMode") != "plan":
            fail(f"{label} {filename} must use approvalMode: plan")
    return manifest


active_manifest = verify_tree(active, "active")
if source:
    verify_tree(source, "source")


def manifest_entries(root):
    manifest_path = root / "MANIFEST.sha256"
    try:
        lines = manifest_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        fail(f"Missing package manifest {manifest_path}: {exc}")
    entries = {}
    for number, line in enumerate(lines, 1):
        try:
            digest, relative = line.split("  ", 1)
        except ValueError:
            fail(f"Malformed MANIFEST.sha256 line {number}")
        path = PurePosixPath(relative)
        if (
            not re.fullmatch(r"[0-9a-f]{64}", digest)
            or path.is_absolute()
            or ".." in path.parts
            or "\\" in relative
            or re.match(r"^[A-Za-z]:", relative)
        ):
            fail(f"Unsafe MANIFEST.sha256 line {number}")
        if relative in entries:
            fail(f"Duplicate MANIFEST.sha256 entry: {relative}")
        entries[relative] = digest
    return entries


def payload_inventory(root):
    files = set()
    for current_dir, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current_dir)
        for directory in directories:
            candidate = current_path / directory
            if candidate.is_symlink():
                fail(f"Symlinked directory in active package: {candidate.relative_to(root).as_posix()}")
        for filename in filenames:
            candidate = current_path / filename
            relative = candidate.relative_to(root).as_posix()
            if candidate.is_symlink():
                fail(f"Symlinked file in active package: {relative}")
            if filename == ".DS_Store" or relative in {
                "MANIFEST.sha256",
                "gigacode-extension.json",
                ".gigacode-extension-install.json",
            }:
                continue
            files.add(relative)
    return files


reference = source or active
entries = manifest_entries(reference)
if source:
    active_manifest_digest = hashlib.sha256((active / "MANIFEST.sha256").read_bytes()).hexdigest()
    source_manifest_digest = hashlib.sha256((source / "MANIFEST.sha256").read_bytes()).hexdigest()
    if active_manifest_digest != source_manifest_digest:
        fail("Active/source mismatch: MANIFEST.sha256")
for root, label in ((reference, "source" if source else "active"), (active, "active")):
    actual = payload_inventory(root)
    missing = sorted(set(entries) - actual)
    extra = sorted(actual - set(entries))
    if missing or extra:
        fail(f"{label.capitalize()} inventory mismatch: missing={missing}, extra={extra}")
    for relative, digest in entries.items():
        target = root.joinpath(*PurePosixPath(relative).parts)
        actual_digest = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual_digest != digest:
            fail(f"Manifest checksum mismatch in {label}: {relative}")

install_metadata = active / ".gigacode-extension-install.json"
if install_metadata.exists():
    metadata = load_json(install_metadata)
    if metadata.get("type") != "local" or metadata.get("originSource") != "Gemini":
        fail(f"Unexpected GigaCode install metadata: {metadata!r}")
    if metadata.get("source") != str(current):
        fail(f"Active extension source mismatch: {metadata.get('source')!r} != {str(current)!r}")

PY
read -r manifest_name manifest_version < <(
  "$PYTHON_BIN" - "$installed/gemini-extension.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
print(data.get("name", ""), data.get("version", ""))
PY
)

if [[ -e "$GIGACODE_SKILLS_DIR/drawio-skill" ]]; then
  die "Legacy skill is still active at $GIGACODE_SKILLS_DIR/drawio-skill; it would compete with the extension"
fi

list_output="$($GIGACODE_BIN extensions list 2>&1)" || die "GigaCode extensions list failed: $list_output"
grep -Fq "$EXTENSION_NAME" <<<"$list_output" || die "$EXTENSION_NAME is absent from GigaCode extensions list"

if extensions_supports_validate; then
  log "Running native GigaCode extension validation"
  "$GIGACODE_BIN" extensions validate "$installed"
else
  log "Native 'extensions validate' is unavailable; package integrity and registration checks remain active"
fi

if (( ! skip_self_check )); then
  [[ -f "$installed/scripts/self_check.py" ]] || die "Missing scripts/self_check.py"
  log "Running extension self-check"
  "$PYTHON_BIN" "$installed/scripts/self_check.py"
else
  log "Self-check skipped by request"
fi

log "Verified $EXTENSION_NAME $manifest_version"
log "Restart GigaCode, run /agents manage, and confirm the four diagram-* extension agents."
