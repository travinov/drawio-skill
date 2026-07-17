from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts" / "gigacode"
ARCHIVE = ROOT / "dist" / "drawio-skill-agent-extension.zip"


FAKE_GIGACODE = r'''#!/usr/bin/env bash
set -euo pipefail
: "${GIGACODE_EXTENSIONS_DIR:?}"
registry="${FAKE_GIGACODE_REGISTRY:?}"
log="${FAKE_GIGACODE_LOG:?}"
printf '%q ' "$@" >>"$log"
printf '\n' >>"$log"

if [[ "${1:-}" != extensions ]]; then exit 2; fi
case "${2:-}" in
  --help)
    printf 'gigacode extensions <command>\nCommands:\n'
    printf '  gigacode extensions install <source>\n'
    printf '  gigacode extensions list\n'
    if [[ "${FAKE_GIGACODE_SUPPORTS_VALIDATE:-1}" == 1 ]]; then
      printf '  gigacode extensions validate <source>\n'
    fi
    ;;
  list)
    [[ -f "$registry" ]] && cat "$registry"
    ;;
  validate)
    [[ "${FAKE_GIGACODE_SUPPORTS_VALIDATE:-1}" == 1 ]] || exit 2
    [[ "${3:-}" == --help ]] && exit 0
    [[ "${FAKE_GIGACODE_FAIL_VALIDATE:-0}" == 1 ]] && exit 43
    [[ -f "${3:?}/gemini-extension.json" ]]
    ;;
  install)
    if [[ "${3:-}" == --help ]]; then echo 'Options: --yes --force'; exit 0; fi
    source_path="${3:?}"
    destination="$GIGACODE_EXTENSIONS_DIR/publish-drawio-skill"
    rm -rf "$destination"
    mkdir -p "$GIGACODE_EXTENSIONS_DIR"
    cp -aL "$source_path" "$destination"
    [[ "${FAKE_GIGACODE_FAIL_INSTALL:-0}" == 1 ]] && exit 42
    echo publish-drawio-skill >"$registry"
    ;;
  uninstall)
    if [[ "${3:-}" == --help ]]; then echo 'Options: --yes --force'; exit 0; fi
    rm -rf "$GIGACODE_EXTENSIONS_DIR/publish-drawio-skill"
    rm -f "$registry"
    ;;
  *) exit 2 ;;
esac
'''


class InstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / ".gigacode"
        self.bin = self.home / "bin" / "gigacode"
        self.bin.parent.mkdir(parents=True)
        self.bin.write_text(FAKE_GIGACODE, encoding="utf-8")
        self.bin.chmod(0o755)
        self.archive = self.root / ARCHIVE.name
        shutil.copy2(ARCHIVE, self.archive)
        digest = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.checksum = self.root / f"{ARCHIVE.name}.sha256"
        self.checksum.write_text(f"{digest}  {ARCHIVE.name}\n", encoding="utf-8")
        self.env = os.environ.copy()
        self.env.update(
            {
                "HOME": str(self.root),
                "GIGACODE_HOME": str(self.home),
                "GIGACODE_BIN": str(self.bin),
                "GIGACODE_SKILLS_DIR": str(self.home / "skills"),
                "GIGACODE_EXTENSIONS_DIR": str(self.home / "extensions"),
                "GIGACODE_EXTENSION_SOURCES_DIR": str(self.home / "extension-sources"),
                "GIGACODE_BACKUP_DIR": str(self.home / "backups" / "drawio-agent-extension"),
                "FAKE_GIGACODE_REGISTRY": str(self.root / "registry.txt"),
                "FAKE_GIGACODE_LOG": str(self.root / "gigacode.log"),
            }
        )

    def run_script(self, name: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [str(SCRIPTS / name), *args],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        if check and result.returncode:
            self.fail(f"{name} failed ({result.returncode}):\n{result.stdout}")
        return result

    def install(self, *, skip_deps: bool = True) -> subprocess.CompletedProcess[str]:
        args = [
            "--archive",
            str(self.archive),
            "--checksum",
            str(self.checksum),
        ]
        if skip_deps:
            args.append("--skip-deps")
        return self.run_script("install_drawio_agent_extension.sh", *args)

    def use_fake_pip(self) -> None:
        fake_python = self.root / "python-with-fake-pip"
        fake_python.write_text(
            """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == -m && "${2:-}" == pip ]]; then
  exit 0
fi
if [[ "${1:-}" == */scripts/self_check.py ]]; then
  printf 'fake self-check passed\n'
  exit 0
fi
exec "${FAKE_PYTHON_REAL:?}" "$@"
""",
            encoding="utf-8",
        )
        fake_python.chmod(0o755)
        self.env["PYTHON_BIN"] = str(fake_python)
        self.env["FAKE_PYTHON_REAL"] = sys.executable

    def extract_bundle(self, name: str) -> Path:
        extracted = self.root / name
        with zipfile.ZipFile(self.archive) as bundle:
            bundle.extractall(extracted)
        extension = extracted / "drawio-skill"
        for script in (extension / "install").glob("*.sh"):
            script.chmod(0o755)
        return extension

    def run_bundled(
        self, extension: Path, *args: str, bash_bin: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        command = [str(extension / "install" / "install_drawio_agent_extension.sh"), *args]
        if bash_bin is not None:
            command.insert(0, bash_bin)
        return subprocess.run(
            command,
            cwd=extension,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

    def test_install_backs_up_legacy_skill_and_verifies(self) -> None:
        legacy = self.home / "skills" / "drawio-skill"
        legacy.mkdir(parents=True)
        (legacy / "legacy.txt").write_text("old", encoding="utf-8")

        result = self.install()

        self.assertIn("Installed publish-drawio-skill 1.22.0-corporate.1", result.stdout)
        self.assertIn("Self-check skipped by request", result.stdout)
        self.assertFalse(legacy.exists())
        installed = self.home / "extensions" / "publish-drawio-skill"
        self.assertTrue((installed / "agents" / "diagram-supervisor.md").is_file())
        backups = list((self.home / "backups" / "drawio-agent-extension").iterdir())
        self.assertEqual(1, len(backups))
        self.assertEqual("old", (backups[0] / "legacy-skill" / "legacy.txt").read_text())
        self.run_script("verify_drawio_agent_extension.sh", "--skip-self-check")
        self.assertIn(
            "extensions validate", (self.root / "gigacode.log").read_text()
        )

    def test_bundled_install_with_dependencies_runs_verifier_under_system_bash(self) -> None:
        self.use_fake_pip()
        extension = self.extract_bundle("bash-3.2")

        result = self.run_bundled(extension, bash_bin="/bin/bash")

        self.assertEqual(0, result.returncode, result.stdout)
        self.assertIn("Installing pinned Python dependencies", result.stdout)
        self.assertIn("Running extension self-check", result.stdout)
        self.assertIn("fake self-check passed", result.stdout)
        self.assertIn("Installed publish-drawio-skill 1.22.0-corporate.1", result.stdout)
        self.assertNotIn("unbound variable", result.stdout)
        self.assertNotIn("restoring backup", result.stdout)
        self.assertEqual(
            "publish-drawio-skill\n", (self.root / "registry.txt").read_text()
        )

    def test_install_falls_back_when_native_validate_is_unavailable(self) -> None:
        self.env["FAKE_GIGACODE_SUPPORTS_VALIDATE"] = "0"

        result = self.install()

        self.assertIn("Native 'extensions validate' is unavailable", result.stdout)
        self.assertIn("Installed publish-drawio-skill 1.22.0-corporate.1", result.stdout)
        self.assertTrue(
            (self.home / "extensions" / "publish-drawio-skill" / "gemini-extension.json").is_file()
        )
        log = (self.root / "gigacode.log").read_text()
        self.assertNotIn("extensions validate", log)

        verify = self.run_script(
            "verify_drawio_agent_extension.sh", "--skip-self-check"
        )
        self.assertIn("package integrity and registration checks remain active", verify.stdout)

    def test_dry_run_falls_back_without_native_validate_or_mutation(self) -> None:
        self.env["FAKE_GIGACODE_SUPPORTS_VALIDATE"] = "0"

        result = self.run_script(
            "install_drawio_agent_extension.sh",
            "--archive",
            str(self.archive),
            "--checksum",
            str(self.checksum),
            "--skip-deps",
            "--dry-run",
        )

        self.assertIn("Native 'extensions validate' is unavailable", result.stdout)
        self.assertFalse((self.home / "extensions").exists())
        self.assertFalse((self.home / "extension-sources").exists())
        self.assertFalse((self.home / "backups").exists())

    def test_native_validate_failure_stops_before_mutation(self) -> None:
        legacy = self.home / "skills" / "drawio-skill"
        legacy.mkdir(parents=True)
        (legacy / "keep.txt").write_text("keep", encoding="utf-8")
        self.env["FAKE_GIGACODE_FAIL_VALIDATE"] = "1"

        result = self.run_script(
            "install_drawio_agent_extension.sh",
            "--archive",
            str(self.archive),
            "--checksum",
            str(self.checksum),
            "--skip-deps",
            check=False,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertTrue((legacy / "keep.txt").is_file())
        self.assertFalse((self.home / "extensions").exists())
        self.assertFalse((self.home / "extension-sources").exists())
        self.assertFalse((self.home / "backups").exists())
        log = (self.root / "gigacode.log").read_text()
        self.assertIn("extensions validate", log)
        self.assertNotIn("extensions install", log)

    def test_rollback_restores_legacy_skill(self) -> None:
        legacy = self.home / "skills" / "drawio-skill"
        legacy.mkdir(parents=True)
        (legacy / "legacy.txt").write_text("old", encoding="utf-8")
        self.install()

        self.run_script("rollback_drawio_agent_extension.sh", "--latest")

        self.assertEqual("old", (legacy / "legacy.txt").read_text())
        self.assertFalse((self.home / "extensions" / "publish-drawio-skill").exists())
        self.assertFalse((self.root / "registry.txt").exists())

    def test_rollback_preflights_backup_before_removing_current_install(self) -> None:
        legacy = self.home / "skills" / "drawio-skill"
        legacy.mkdir(parents=True)
        (legacy / "legacy.txt").write_text("old", encoding="utf-8")
        self.install()
        backup_root = self.home / "backups" / "drawio-agent-extension"
        backup = next(backup_root.iterdir())
        shutil.rmtree(backup / "legacy-skill")
        installed = self.home / "extensions" / "publish-drawio-skill"

        result = self.run_script(
            "rollback_drawio_agent_extension.sh", "--latest", check=False
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("Legacy skill backup is missing", result.stdout)
        self.assertTrue(installed.is_dir())
        self.assertEqual(
            "publish-drawio-skill\n", (self.root / "registry.txt").read_text()
        )

    def test_checksum_rejection_does_not_mutate_gigacode_home(self) -> None:
        legacy = self.home / "skills" / "drawio-skill"
        legacy.mkdir(parents=True)
        (legacy / "keep.txt").write_text("keep", encoding="utf-8")

        result = self.run_script(
            "install_drawio_agent_extension.sh",
            "--archive",
            str(self.archive),
            "--sha256",
            "0" * 64,
            "--skip-deps",
            check=False,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("Checksum mismatch", result.stdout)
        self.assertTrue((legacy / "keep.txt").is_file())
        self.assertFalse((self.home / "backups").exists())

    def test_dry_run_does_not_mutate_active_directories(self) -> None:
        legacy = self.home / "skills" / "drawio-skill"
        legacy.mkdir(parents=True)
        (legacy / "keep.txt").write_text("keep", encoding="utf-8")

        result = self.run_script(
            "install_drawio_agent_extension.sh",
            "--archive",
            str(self.archive),
            "--checksum",
            str(self.checksum),
            "--skip-deps",
            "--dry-run",
        )

        self.assertIn("[dry-run]", result.stdout)
        self.assertTrue((legacy / "keep.txt").is_file())
        self.assertFalse((self.home / "extensions").exists())
        self.assertFalse((self.home / "extension-sources").exists())
        self.assertFalse((self.home / "backups").exists())

    def test_verifier_rejects_active_legacy_conflict(self) -> None:
        self.install()
        (self.home / "skills" / "drawio-skill").mkdir(parents=True)

        result = self.run_script(
            "verify_drawio_agent_extension.sh", "--skip-self-check", check=False
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("would compete with the extension", result.stdout)

    def test_registered_extension_without_restorable_files_is_not_uninstalled(self) -> None:
        registry = self.root / "registry.txt"
        registry.write_text("publish-drawio-skill\n", encoding="utf-8")
        legacy = self.home / "skills" / "drawio-skill"
        legacy.mkdir(parents=True)
        (legacy / "keep.txt").write_text("keep", encoding="utf-8")

        result = self.install_with_expected_failure()

        self.assertNotEqual(0, result.returncode)
        self.assertIn("no restorable files were found", result.stdout)
        self.assertEqual("publish-drawio-skill\n", registry.read_text())
        self.assertTrue((legacy / "keep.txt").is_file())
        log = (self.root / "gigacode.log").read_text()
        self.assertNotIn("extensions uninstall", log)

    def test_failed_native_install_automatically_restores_legacy_skill(self) -> None:
        legacy = self.home / "skills" / "drawio-skill"
        legacy.mkdir(parents=True)
        (legacy / "legacy.txt").write_text("old", encoding="utf-8")
        self.env["FAKE_GIGACODE_FAIL_INSTALL"] = "1"

        result = self.install_with_expected_failure()

        self.assertNotEqual(0, result.returncode)
        self.assertIn("restoring backup", result.stdout)
        self.assertEqual("old", (legacy / "legacy.txt").read_text())
        self.assertFalse((self.home / "extensions" / "publish-drawio-skill").exists())
        version_dir = (
            self.home
            / "extension-sources"
            / "publish-drawio-skill"
            / "1.22.0-corporate.1"
        )
        self.assertFalse(version_dir.exists())

    def test_bundled_installer_uses_extracted_extension_without_network(self) -> None:
        extension = self.extract_bundle("unpacked")
        installer_dir = extension / "install"
        self.env["DRAWIO_EXTENSION_BASE_URL"] = "http://127.0.0.1:9/network-must-not-be-used"

        result = self.run_bundled(extension, "--skip-deps")

        self.assertEqual(0, result.returncode, result.stdout)
        self.assertIn("Using already extracted extension", result.stdout)
        self.assertNotIn("Downloading agent extension", result.stdout)
        self.assertTrue(
            (
                self.home
                / "extensions"
                / "publish-drawio-skill"
                / "install"
                / "verify_drawio_agent_extension.sh"
            ).is_file()
        )

    def test_bundled_installer_rejects_tampered_extracted_file(self) -> None:
        extension = self.extract_bundle("tampered")
        (extension / "SKILL.md").write_text("tampered\n", encoding="utf-8")

        result = self.run_bundled(extension, "--skip-deps")

        self.assertNotEqual(0, result.returncode)
        self.assertIn("Manifest checksum mismatch: SKILL.md", result.stdout)
        self.assertFalse((self.home / "extensions").exists())

    def test_bundled_installer_rejects_unlisted_file(self) -> None:
        extension = self.extract_bundle("extra-file")
        (extension / "unexpected.txt").write_text("not manifested\n", encoding="utf-8")

        result = self.run_bundled(extension, "--skip-deps")

        self.assertNotEqual(0, result.returncode)
        self.assertIn("Extracted inventory mismatch", result.stdout)
        self.assertIn("unexpected.txt", result.stdout)
        self.assertFalse((self.home / "extensions").exists())

    def test_bundled_installer_ignores_macos_ds_store_metadata(self) -> None:
        extension = self.extract_bundle("macos-metadata")
        (extension / ".DS_Store").write_bytes(b"finder metadata")
        (extension / "assets" / ".DS_Store").write_bytes(b"nested finder metadata")

        result = self.run_bundled(extension, "--skip-deps")

        self.assertEqual(0, result.returncode, result.stdout)
        self.assertIn("Installed publish-drawio-skill", result.stdout)

    def test_bundled_installer_rejects_removed_or_duplicate_manifest_entry(self) -> None:
        for case in ("removed", "duplicate"):
            with self.subTest(case=case):
                extension = self.extract_bundle(f"manifest-{case}")
                manifest = extension / "MANIFEST.sha256"
                lines = manifest.read_text(encoding="utf-8").splitlines()
                if case == "removed":
                    lines = [line for line in lines if not line.endswith("  SKILL.md")]
                else:
                    lines.append(lines[0])
                manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")

                result = self.run_bundled(extension, "--skip-deps")

                self.assertNotEqual(0, result.returncode)
                expected = "Extracted inventory mismatch" if case == "removed" else "Duplicate MANIFEST.sha256 entry"
                self.assertIn(expected, result.stdout)
                self.assertFalse((self.home / "extensions").exists())

    def test_bundled_installer_rejects_symlinked_directory(self) -> None:
        extension = self.extract_bundle("symlink-directory")
        agents = extension / "agents"
        real_agents = extension / "agents-real"
        agents.rename(real_agents)
        agents.symlink_to(real_agents, target_is_directory=True)

        result = self.run_bundled(extension, "--skip-deps")

        self.assertNotEqual(0, result.returncode)
        self.assertIn("Extracted directory must not be a symlink: agents", result.stdout)
        self.assertFalse((self.home / "extensions").exists())

    def test_explicit_source_mode_and_conflicting_options(self) -> None:
        extension = self.extract_bundle("explicit-source")
        result = self.run_script(
            "install_drawio_agent_extension.sh",
            "--source",
            str(extension),
            "--skip-deps",
        )
        self.assertIn("Using already extracted extension", result.stdout)

        other = self.extract_bundle("explicit-source-conflicts")
        for args, message in (
            (("--source", str(other), "--archive", str(self.archive)), "cannot be used together"),
            (("--source", str(other), "--checksum", str(self.checksum)), "Checksum options apply"),
            (("--source", str(other), "--sha256", "0" * 64), "Checksum options apply"),
        ):
            with self.subTest(args=args):
                conflict = self.run_script(
                    "install_drawio_agent_extension.sh", *args, check=False
                )
                self.assertNotEqual(0, conflict.returncode)
                self.assertIn(message, conflict.stdout)

    def install_with_expected_failure(self) -> subprocess.CompletedProcess[str]:
        return self.run_script(
            "install_drawio_agent_extension.sh",
            "--archive",
            str(self.archive),
            "--checksum",
            str(self.checksum),
            "--skip-deps",
            check=False,
        )


if __name__ == "__main__":
    unittest.main()
