#!/usr/bin/env bash
set -Eeuo pipefail

EXTENSION_NAME="publish-drawio-skill"
ARCHIVE_NAME="drawio-skill-agent-extension.zip"
DEFAULT_VERSION="1.22.0-corporate.1"
DEFAULT_BRANCH="codex/drawio-agent-extension-v1.22.0"
DEFAULT_BASE_URL="https://raw.githubusercontent.com/travinov/corporate-agent-skills/refs/heads/${DEFAULT_BRANCH}/dist"

GIGACODE_HOME="${GIGACODE_HOME:-$HOME/.gigacode}"
GIGACODE_BIN="${GIGACODE_BIN:-$GIGACODE_HOME/bin/gigacode}"
GIGACODE_SKILLS_DIR="${GIGACODE_SKILLS_DIR:-$GIGACODE_HOME/skills}"
GIGACODE_EXTENSIONS_DIR="${GIGACODE_EXTENSIONS_DIR:-$GIGACODE_HOME/extensions}"
GIGACODE_EXTENSION_SOURCES_DIR="${GIGACODE_EXTENSION_SOURCES_DIR:-$GIGACODE_HOME/extension-sources}"
GIGACODE_BACKUP_DIR="${GIGACODE_BACKUP_DIR:-$GIGACODE_HOME/backups/drawio-agent-extension}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

archive=""
source_dir=""
checksum_file=""
expected_sha256=""
base_url="${DRAWIO_EXTENSION_BASE_URL:-$DEFAULT_BASE_URL}"
dry_run=0
skip_deps=0
keep_download=0
work_dir=""
backup_path=""
mutation_started=0
install_completed=0

usage() {
  cat <<'EOF'
Usage:
  install_drawio_agent_extension.sh [options]

Options:
  --source PATH        Install an already extracted drawio-skill directory.
  --archive PATH       Install from a transferred local ZIP (offline mode).
  --checksum PATH      SHA-256 file for --archive (default: PATH.sha256).
  --sha256 HEX         Expected SHA-256 value instead of a checksum file.
  --base-url URL       Override the GitHub/raw download directory.
  --skip-deps          Do not install Python dependencies.
  --dry-run            Validate inputs and print actions without mutation.
  --keep-download      Keep the temporary downloaded files.
  -h, --help           Show this help.

Environment overrides:
  GIGACODE_HOME, GIGACODE_BIN, GIGACODE_SKILLS_DIR,
  GIGACODE_EXTENSIONS_DIR, GIGACODE_EXTENSION_SOURCES_DIR,
  GIGACODE_BACKUP_DIR, PYTHON_BIN, DRAWIO_EXTENSION_BASE_URL.
EOF
}

log() { printf '[drawio-extension] %s\n' "$*"; }
die() { printf '[drawio-extension] ERROR: %s\n' "$*" >&2; exit 1; }
quote_cmd() { printf ' %q' "$@"; printf '\n'; }

run() {
  if (( dry_run )); then
    printf '[dry-run]'
    quote_cmd "$@"
  else
    "$@"
  fi
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

path_is_within() {
  local child="$1" parent="$2"
  [[ -n "$child" && -n "$parent" && "$child" != "/" && "$parent" != "/" ]] || return 1
  case "$child" in
    "$parent"|"$parent"/*) return 0 ;;
    *) return 1 ;;
  esac
}

safe_remove_tree() {
  local target="$1" allowed_parent="$2"
  path_is_within "$target" "$allowed_parent" || die "Refusing to remove unsafe path: $target"
  [[ "$target" != "$allowed_parent" ]] || die "Refusing to remove parent directory: $target"
  run rm -rf -- "$target"
}

file_sha256() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  elif command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    die "Neither shasum nor sha256sum is available"
  fi
}

read_expected_sha256() {
  local file="$1" value
  [[ -f "$file" ]] || die "Checksum file not found: $file"
  value="$(awk -v name="$ARCHIVE_NAME" '$2 == name || $2 == "*" name {print $1; exit} NF == 1 {print $1; exit}' "$file")"
  [[ "$value" =~ ^[[:xdigit:]]{64}$ ]] || die "No valid SHA-256 for $ARCHIVE_NAME in $file"
  printf '%s\n' "$value" | tr '[:upper:]' '[:lower:]'
}

download_file() {
  local url="$1" destination="$2"
  if command -v curl >/dev/null 2>&1; then
    curl --fail --location --silent --show-error "$url" --output "$destination"
  elif command -v wget >/dev/null 2>&1; then
    wget -q "$url" -O "$destination"
  else
    die "curl or wget is required for online installation; use --archive for offline mode"
  fi
}

help_supports() {
  local token="$1" help_text="$2"
  grep -Eq -- "(^|[[:space:],])${token//-/\\-}([=[:space:],]|$)" <<<"$help_text"
}

extensions_list_contains() {
  "$GIGACODE_BIN" extensions list 2>/dev/null | grep -Fq "$EXTENSION_NAME"
}

extensions_supports_validate() {
  local help_text
  help_text="$("$GIGACODE_BIN" extensions --help 2>&1 || true)"

  if grep -Eq '^[[:space:]]*gigacode[[:space:]]+extensions[[:space:]]+validate([[:space:]<]|$)' <<<"$help_text"; then
    return 0
  fi

  # A populated command listing is authoritative. Do not probe an unknown
  # subcommand because some yargs-based CLIs return success when --help is set.
  if grep -Eq '^[[:space:]]*(Commands:|gigacode[[:space:]]+extensions[[:space:]]+<command>)' <<<"$help_text"; then
    return 1
  fi

  # Older/forked CLIs may not expose a parent command listing.
  "$GIGACODE_BIN" extensions validate --help >/dev/null 2>&1
}

native_validate() {
  local source_path="$1"
  if extensions_supports_validate; then
    log "Validating extension with the corporate GigaCode CLI"
    run "$GIGACODE_BIN" extensions validate "$source_path"
  else
    log "Native 'extensions validate' is unavailable; continuing with package integrity, registration, and self-check validation"
  fi
}

native_uninstall() {
  local help_text args=(extensions uninstall "$EXTENSION_NAME")
  help_text="$($GIGACODE_BIN extensions uninstall --help 2>&1 || true)"
  help_supports '--yes' "$help_text" && args+=(--yes)
  help_supports '--force' "$help_text" && args+=(--force)
  run "$GIGACODE_BIN" "${args[@]}"
}

native_install() {
  local source_path="$1" help_text
  local args=(extensions install "$source_path")
  help_text="$($GIGACODE_BIN extensions install --help 2>&1 || true)"
  help_supports '--yes' "$help_text" && args+=(--yes)
  help_supports '--force' "$help_text" && args+=(--force)
  run "$GIGACODE_BIN" "${args[@]}"
}

write_state() {
  local state_file="$backup_path/state.env"
  (( dry_run )) && return 0
  {
    printf 'EXTENSION_NAME=%q\n' "$EXTENSION_NAME"
    printf 'GIGACODE_HOME=%q\n' "$GIGACODE_HOME"
    printf 'GIGACODE_BIN=%q\n' "$GIGACODE_BIN"
    printf 'GIGACODE_SKILLS_DIR=%q\n' "$GIGACODE_SKILLS_DIR"
    printf 'GIGACODE_EXTENSIONS_DIR=%q\n' "$GIGACODE_EXTENSIONS_DIR"
    printf 'GIGACODE_EXTENSION_SOURCES_DIR=%q\n' "$GIGACODE_EXTENSION_SOURCES_DIR"
    printf 'HAD_LEGACY_SKILL=%q\n' "${HAD_LEGACY_SKILL:-0}"
    printf 'HAD_EXTENSION_DIR=%q\n' "${HAD_EXTENSION_DIR:-0}"
    printf 'HAD_SOURCE_DIR=%q\n' "${HAD_SOURCE_DIR:-0}"
    printf 'HAD_NATIVE_EXTENSION=%q\n' "${HAD_NATIVE_EXTENSION:-0}"
    printf 'PREVIOUS_SOURCE_PATH=%q\n' "${PREVIOUS_SOURCE_PATH:-}"
    printf 'INSTALLED_SOURCE_PATH=%q\n' "${INSTALLED_SOURCE_PATH:-}"
  } >"$state_file"
}

backup_existing_state() {
  local legacy="$GIGACODE_SKILLS_DIR/drawio-skill"
  local extension_dir="$GIGACODE_EXTENSIONS_DIR/$EXTENSION_NAME"
  local current_source="$GIGACODE_EXTENSION_SOURCES_DIR/$EXTENSION_NAME/current"

  HAD_LEGACY_SKILL=0
  HAD_EXTENSION_DIR=0
  HAD_SOURCE_DIR=0
  HAD_NATIVE_EXTENSION=0
  PREVIOUS_SOURCE_PATH=""

  extensions_list_contains && HAD_NATIVE_EXTENSION=1 || true
  [[ -e "$legacy" ]] && HAD_LEGACY_SKILL=1
  [[ -e "$extension_dir" ]] && HAD_EXTENSION_DIR=1
  [[ -L "$current_source" || -e "$current_source" ]] && HAD_SOURCE_DIR=1

  if (( HAD_NATIVE_EXTENSION )) && (( ! HAD_EXTENSION_DIR )) && (( ! HAD_SOURCE_DIR )); then
    die "$EXTENSION_NAME is registered in GigaCode, but no restorable files were found under $GIGACODE_EXTENSIONS_DIR or $GIGACODE_EXTENSION_SOURCES_DIR; refusing to uninstall it"
  fi

  run mkdir -p -- "$backup_path"
  if (( HAD_LEGACY_SKILL )); then
    log "Backing up active legacy skill outside the skills directory"
    run cp -a -- "$legacy" "$backup_path/legacy-skill"
  fi
  if (( HAD_EXTENSION_DIR )); then
    log "Backing up the currently installed extension"
    run cp -a -- "$extension_dir" "$backup_path/extension-dir"
  fi
  if (( HAD_SOURCE_DIR )); then
    PREVIOUS_SOURCE_PATH="$(readlink "$current_source" 2>/dev/null || printf '%s' "$current_source")"
    # Dereference the current symlink so rollback remains valid even when the
    # new installation replaces the same version directory.
    run cp -aL -- "$current_source" "$backup_path/source-current"
  fi
  write_state

  mutation_started=1
  if (( HAD_NATIVE_EXTENSION )); then
    log "Uninstalling the currently registered extension"
    native_uninstall
  fi
  if (( HAD_LEGACY_SKILL )); then
    safe_remove_tree "$legacy" "$GIGACODE_SKILLS_DIR"
  fi
  if [[ -e "$extension_dir" ]]; then
    safe_remove_tree "$extension_dir" "$GIGACODE_EXTENSIONS_DIR"
  fi
}

rollback_failed_install() {
  local legacy="$GIGACODE_SKILLS_DIR/drawio-skill"
  local extension_dir="$GIGACODE_EXTENSIONS_DIR/$EXTENSION_NAME"
  local current_source="$GIGACODE_EXTENSION_SOURCES_DIR/$EXTENSION_NAME/current"
  (( mutation_started )) || return 0
  log "Installation failed; restoring backup $backup_path"
  set +e
  if extensions_list_contains; then native_uninstall; fi
  [[ -e "$extension_dir" ]] && rm -rf -- "$extension_dir"
  [[ -L "$current_source" || -e "$current_source" ]] && rm -rf -- "$current_source"
  if [[ -n "${INSTALLED_SOURCE_PATH:-}" ]] && path_is_within "$INSTALLED_SOURCE_PATH" "$GIGACODE_EXTENSION_SOURCES_DIR/$EXTENSION_NAME"; then
    [[ -e "$INSTALLED_SOURCE_PATH" ]] && rm -rf -- "$INSTALLED_SOURCE_PATH"
  fi
  if [[ -e "$backup_path/source-current" ]]; then
    mkdir -p -- "$(dirname "$current_source")"
    cp -a -- "$backup_path/source-current" "$current_source"
  fi
  if [[ -e "$backup_path/extension-dir" ]]; then
    mkdir -p -- "$GIGACODE_EXTENSIONS_DIR"
    cp -a -- "$backup_path/extension-dir" "$extension_dir"
  fi
  if [[ -e "$backup_path/legacy-skill" ]]; then
    mkdir -p -- "$GIGACODE_SKILLS_DIR"
    cp -a -- "$backup_path/legacy-skill" "$legacy"
  fi
  if [[ "${HAD_NATIVE_EXTENSION:-0}" == 1 ]]; then
    if [[ -e "$current_source" ]]; then
      native_install "$current_source"
    elif [[ -e "$extension_dir" ]]; then
      native_install "$extension_dir"
    fi
  fi
  set -e
}

cleanup() {
  local status=$?
  if (( status != 0 )) && (( ! dry_run )) && (( ! install_completed )); then
    rollback_failed_install
  fi
  if [[ -n "$work_dir" && -d "$work_dir" ]] && (( ! keep_download )); then
    rm -rf -- "$work_dir"
  fi
  exit "$status"
}
trap cleanup EXIT

while (($#)); do
  case "$1" in
    --source) [[ $# -ge 2 ]] || die "--source requires PATH"; source_dir="$2"; shift 2 ;;
    --archive) [[ $# -ge 2 ]] || die "--archive requires PATH"; archive="$2"; shift 2 ;;
    --checksum) [[ $# -ge 2 ]] || die "--checksum requires PATH"; checksum_file="$2"; shift 2 ;;
    --sha256) [[ $# -ge 2 ]] || die "--sha256 requires HEX"; expected_sha256="$2"; shift 2 ;;
    --base-url) [[ $# -ge 2 ]] || die "--base-url requires URL"; base_url="${2%/}"; shift 2 ;;
    --skip-deps) skip_deps=1; shift ;;
    --dry-run) dry_run=1; shift ;;
    --keep-download) keep_download=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
done

require_command "$PYTHON_BIN"
[[ -x "$GIGACODE_BIN" ]] || die "GigaCode CLI not executable: $GIGACODE_BIN"

[[ -n "$source_dir" && -n "$archive" ]] && die "--source and --archive cannot be used together"

script_dir="$(cd "$(dirname "$0")" && pwd)"
bundled_source="$(cd "$script_dir/.." && pwd)"
if [[ -z "$source_dir" && -z "$archive" && -f "$bundled_source/gemini-extension.json" ]]; then
  source_dir="$bundled_source"
fi

if [[ -n "$source_dir" ]]; then
  [[ -z "$checksum_file" && -z "$expected_sha256" ]] || die "Checksum options apply to --archive, not --source"
  source_dir="$(cd "$source_dir" 2>/dev/null && pwd)" || die "Extracted extension directory not found: $source_dir"
  extension_root="$source_dir"
  for required in \
    gemini-extension.json \
    MANIFEST.sha256 \
    SKILL.md \
    agents/diagram-supervisor.md \
    agents/diagram-reviewer.md \
    agents/diagram-repair.md \
    agents/diagram-semantic-analyst.md; do
    [[ -f "$extension_root/$required" ]] || die "Extracted extension is missing: $required"
  done
  "$PYTHON_BIN" - "$extension_root" <<'PY'
import hashlib
import os
import re
import sys
from pathlib import Path, PurePosixPath

root = Path(sys.argv[1]).resolve()
manifest = root / "MANIFEST.sha256"
if manifest.is_symlink():
    raise SystemExit("MANIFEST.sha256 must not be a symlink")
expected = {}
for number, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
    try:
        digest, relative = line.split("  ", 1)
    except ValueError:
        raise SystemExit(f"Malformed MANIFEST.sha256 line {number}")
    path = PurePosixPath(relative)
    if (
        not re.fullmatch(r"[0-9a-f]{64}", digest)
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in relative
        or re.match(r"^[A-Za-z]:", relative)
    ):
        raise SystemExit(f"Unsafe MANIFEST.sha256 line {number}")
    if relative in expected:
        raise SystemExit(f"Duplicate MANIFEST.sha256 entry: {relative}")
    expected[relative] = digest

actual_files = set()
for current, directories, files in os.walk(root, followlinks=False):
    current_path = Path(current)
    for directory in directories:
        candidate = current_path / directory
        if candidate.is_symlink():
            relative = candidate.relative_to(root).as_posix()
            raise SystemExit(f"Extracted directory must not be a symlink: {relative}")
    for filename in files:
        candidate = current_path / filename
        if candidate == manifest:
            continue
        relative = candidate.relative_to(root).as_posix()
        if candidate.is_symlink():
            raise SystemExit(f"Extracted file must not be a symlink: {relative}")
        if filename == ".DS_Store":
            continue
        actual_files.add(relative)

missing = sorted(set(expected) - actual_files)
extra = sorted(actual_files - set(expected))
if missing or extra:
    raise SystemExit(f"Extracted inventory mismatch: missing={missing}, extra={extra}")

for relative, digest in expected.items():
    target = root.joinpath(*PurePosixPath(relative).parts)
    actual = hashlib.sha256(target.read_bytes()).hexdigest()
    if actual != digest:
        raise SystemExit(f"Manifest checksum mismatch: {relative}")
PY
  log "Using already extracted extension: $extension_root"
else
  work_dir="$(mktemp -d "${TMPDIR:-/tmp}/drawio-extension-install.XXXXXX")"
  if [[ -z "$archive" ]]; then
    archive="$work_dir/$ARCHIVE_NAME"
    checksum_file="$work_dir/$ARCHIVE_NAME.sha256"
    log "Downloading agent extension from $base_url"
    download_file "$base_url/$ARCHIVE_NAME" "$archive"
    download_file "$base_url/$ARCHIVE_NAME.sha256" "$checksum_file"
  else
    archive="$(cd "$(dirname "$archive")" && pwd)/$(basename "$archive")"
    [[ -f "$archive" ]] || die "Archive not found: $archive"
    if [[ -z "$checksum_file" && -z "$expected_sha256" ]]; then
      [[ -f "$archive.sha256" ]] || die "Offline install requires --checksum, --sha256, or $archive.sha256"
      checksum_file="$archive.sha256"
    fi
  fi

  if [[ -n "$expected_sha256" ]]; then
    [[ "$expected_sha256" =~ ^[[:xdigit:]]{64}$ ]] || die "--sha256 must contain exactly 64 hex characters"
    expected_sha256="$(printf '%s' "$expected_sha256" | tr '[:upper:]' '[:lower:]')"
  else
    expected_sha256="$(read_expected_sha256 "$checksum_file")"
  fi
  actual_sha256="$(file_sha256 "$archive")"
  [[ "$actual_sha256" == "$expected_sha256" ]] || die "Checksum mismatch: expected $expected_sha256, got $actual_sha256"
  log "Archive SHA-256 verified: $actual_sha256"

  extract_dir="$work_dir/extracted"
  "$PYTHON_BIN" - "$archive" "$extract_dir" <<'PY'
import os
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath

archive, destination = sys.argv[1:]
required = {
    "drawio-skill/gemini-extension.json",
    "drawio-skill/SKILL.md",
    "drawio-skill/agents/diagram-supervisor.md",
    "drawio-skill/agents/diagram-reviewer.md",
    "drawio-skill/agents/diagram-repair.md",
    "drawio-skill/agents/diagram-semantic-analyst.md",
}
with zipfile.ZipFile(archive) as zf:
    names = set(zf.namelist())
    missing = sorted(required - names)
    if missing:
        raise SystemExit("Archive is missing required members: " + ", ".join(missing))
    root = Path(destination).resolve()
    root.mkdir(parents=True, exist_ok=True)
    for info in zf.infolist():
        path = PurePosixPath(info.filename)
        mode = info.external_attr >> 16
        if path.is_absolute() or ".." in path.parts or "\\" in info.filename:
            raise SystemExit(f"Unsafe ZIP path: {info.filename}")
        if stat.S_ISLNK(mode):
            raise SystemExit(f"ZIP symlinks are not allowed: {info.filename}")
        target = (root / Path(*path.parts)).resolve()
        if os.path.commonpath((root, target)) != str(root):
            raise SystemExit(f"ZIP member escapes destination: {info.filename}")
    zf.extractall(root)
PY

  extension_root="$extract_dir/drawio-skill"
fi

manifest_version="$($PYTHON_BIN - "$extension_root/gemini-extension.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
if data.get("name") != "publish-drawio-skill":
    raise SystemExit("Unexpected extension name")
print(data.get("version", ""))
PY
)"
[[ -n "$manifest_version" ]] || die "Extension version is missing"
[[ "$manifest_version" == "$DEFAULT_VERSION" ]] || die "Unexpected extension version: $manifest_version"

native_validate "$extension_root"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_path="$GIGACODE_BACKUP_DIR/$timestamp"
while [[ -e "$backup_path" ]]; do backup_path="${backup_path}-$$"; done
backup_existing_state

version_source="$GIGACODE_EXTENSION_SOURCES_DIR/$EXTENSION_NAME/$manifest_version"
current_source="$GIGACODE_EXTENSION_SOURCES_DIR/$EXTENSION_NAME/current"
INSTALLED_SOURCE_PATH="$version_source"
write_state
run mkdir -p -- "$(dirname "$version_source")"
[[ -e "$version_source" ]] && safe_remove_tree "$version_source" "$(dirname "$version_source")"
run cp -a -- "$extension_root" "$version_source"
if [[ -L "$current_source" || -e "$current_source" ]]; then safe_remove_tree "$current_source" "$(dirname "$current_source")"; fi
run ln -s -- "$version_source" "$current_source"

if (( ! skip_deps )); then
  log "Installing pinned Python dependencies for the extension"
  run "$PYTHON_BIN" -m pip install --user -r "$version_source/requirements.lock.txt"
fi

log "Installing extension through the corporate GigaCode CLI"
native_install "$current_source"

verify_script="$(cd "$(dirname "$0")" && pwd)/verify_drawio_agent_extension.sh"
if [[ -x "$verify_script" ]]; then
  if (( skip_deps )); then
    run "$verify_script" --skip-self-check
  else
    run "$verify_script"
  fi
else
  log "Verifier script not found beside installer; checking native registration only"
  (( dry_run )) || extensions_list_contains || die "GigaCode did not list $EXTENSION_NAME after installation"
fi

install_completed=1
log "Installed $EXTENSION_NAME $manifest_version"
log "Backup for rollback: $backup_path"
log "Restart GigaCode and run /agents list; expect four diagram-* agents."
