import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import mastermind_bridge.cli as cli_module
import mastermind_bridge.lifecycle as lifecycle_module
from mastermind_bridge.lifecycle import doctor_bridge, install_bridge, self_test_bridge, uninstall_bridge
from mastermind_bridge.profiles import active_profile
from mastermind_bridge.prompting import available_prompt_templates


class ProductLifecycleTests(unittest.TestCase):
    def test_parser_exposes_product_lifecycle_commands_and_alias_prog(self):
        parser = cli_module.build_parser(prog="codex-bridge")
        action = next(
            action for action in parser._actions if isinstance(action, cli_module.argparse._SubParsersAction)
        )

        for command in ("install", "doctor", "self-test", "snapshot", "uninstall", "v2"):
            with self.subTest(command=command):
                self.assertIn(command, action.choices)

    def test_packaged_prompt_resources_are_available_without_repo_prompt_path(self):
        templates = available_prompt_templates()

        self.assertIn("codex_new_thread.md", templates)
        self.assertIn("start_cycle.md", templates)

    def test_install_and_uninstall_manage_only_manifest_owned_skill_files(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bridge_home = root / "bridge-home"
            codex_home = root / "codex-home"

            installed = install_bridge(bridge_home_path=bridge_home, codex_home_path=codex_home)

            self.assertEqual(installed.exit_code, 0)
            skill_file = codex_home / "skills" / "chatgpt-codex-bridge" / "SKILL.md"
            manifest = bridge_home / "install" / "manifest.json"
            self.assertTrue(skill_file.exists())
            self.assertTrue(manifest.exists())

            uninstalled = uninstall_bridge(bridge_home_path=bridge_home)

            self.assertEqual(uninstalled.exit_code, 0)
            self.assertFalse(skill_file.exists())
            self.assertFalse(manifest.exists())

    def test_install_dry_run_records_actions_without_filesystem_writes(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bridge_home = root / "bridge-home"
            codex_home = root / "codex-home"
            prefix = root / "prefix"

            result = install_bridge(
                bridge_home_path=bridge_home,
                codex_home_path=codex_home,
                prefix=prefix,
                dry_run=True,
            )

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.status, "dry_run")
            self.assertFalse(bridge_home.exists())
            self.assertFalse(codex_home.exists())
            self.assertIn({"action": "record_prefix", "path": str(prefix)}, result.payload["actions"])

    def test_uninstall_dry_run_reports_owned_files_without_removing_them(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bridge_home = root / "bridge-home"
            codex_home = root / "codex-home"
            install_bridge(bridge_home_path=bridge_home, codex_home_path=codex_home)
            skill_file = codex_home / "skills" / "chatgpt-codex-bridge" / "SKILL.md"
            manifest = bridge_home / "install" / "manifest.json"

            result = uninstall_bridge(bridge_home_path=bridge_home, dry_run=True)

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.status, "dry_run")
            self.assertTrue(skill_file.exists())
            self.assertTrue(manifest.exists())
            self.assertGreater(len(result.payload["removed"]), 0)

    def test_owned_reinstall_and_round_trip_install_are_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bridge_home = root / "bridge-home"
            codex_home = root / "codex-home"
            skill_file = codex_home / "skills" / "chatgpt-codex-bridge" / "SKILL.md"

            first = install_bridge(bridge_home_path=bridge_home, codex_home_path=codex_home)
            second = install_bridge(bridge_home_path=bridge_home, codex_home_path=codex_home)
            uninstalled = uninstall_bridge(bridge_home_path=bridge_home)
            third = install_bridge(bridge_home_path=bridge_home, codex_home_path=codex_home)

            self.assertEqual(first.status, "installed")
            self.assertEqual(second.status, "installed")
            self.assertEqual(uninstalled.status, "uninstalled")
            self.assertEqual(third.status, "installed")
            self.assertTrue(skill_file.exists())

    def test_uninstall_purge_requires_bridge_owned_sentinel(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bridge_home = root / "bridge-home"
            codex_home = root / "codex-home"
            install_bridge(bridge_home_path=bridge_home, codex_home_path=codex_home)
            (bridge_home / ".chatgpt-codex-bridge-home").unlink()

            result = uninstall_bridge(bridge_home_path=bridge_home, purge=True)

            self.assertEqual(result.exit_code, 2)
            self.assertEqual(result.status, "blocked")
            self.assertTrue(bridge_home.exists())

    def test_uninstall_purge_removes_owned_bridge_home(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bridge_home = root / "bridge-home"
            codex_home = root / "codex-home"
            install_bridge(bridge_home_path=bridge_home, codex_home_path=codex_home)

            result = uninstall_bridge(bridge_home_path=bridge_home, purge=True)

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.status, "uninstalled")
            self.assertFalse(bridge_home.exists())

    def test_install_blocks_unowned_skill_without_force(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bridge_home = root / "bridge-home"
            codex_home = root / "codex-home"
            skill_dir = codex_home / "skills" / "chatgpt-codex-bridge"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("foreign\n", encoding="utf-8")

            result = install_bridge(bridge_home_path=bridge_home, codex_home_path=codex_home)

            self.assertEqual(result.exit_code, 2)
            self.assertEqual(result.status, "blocked")

    def test_self_test_initializes_v2_store_without_external_sends(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            result = self_test_bridge(bridge_home_path=Path(tmp_dir) / "bridge-home")

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.status, "ok")
            self.assertTrue(all(check["status"] == "ok" for check in result.payload["checks"]))

    def test_lifecycle_reports_selected_profile_policy(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)

            result = doctor_bridge(
                bridge_home_path=root / "bridge-home",
                codex_home_path=root / "codex-home",
                profile="browser-extra",
                redact=False,
            )

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.payload["profile_policy"]["name"], "browser-extra")
            self.assertIn("control-panel", result.payload["profile_policy"]["capabilities"])

    def test_install_profile_becomes_local_default_when_env_is_unset(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bridge_home = root / "bridge-home"
            codex_home = root / "codex-home"

            install_bridge(
                bridge_home_path=bridge_home,
                codex_home_path=codex_home,
                profile="browser-extra",
            )

            with patch.dict(os.environ, {"BRIDGE_HOME": str(bridge_home), "BRIDGE_PROFILE": ""}, clear=False):
                self.assertEqual(active_profile().name, "browser-extra")
                doctor = doctor_bridge(
                    bridge_home_path=bridge_home,
                    codex_home_path=codex_home,
                    redact=False,
                )

            self.assertEqual(doctor.payload["profile"], "browser-extra")
            self.assertEqual(doctor.payload["profile_policy"]["name"], "browser-extra")

            with patch.dict(os.environ, {"BRIDGE_HOME": "", "BRIDGE_PROFILE": ""}, clear=False):
                explicit_home_doctor = doctor_bridge(
                    bridge_home_path=bridge_home,
                    codex_home_path=codex_home,
                    redact=False,
                )

            self.assertEqual(explicit_home_doctor.payload["profile"], "browser-extra")
            self.assertEqual(explicit_home_doctor.payload["profile_policy"]["name"], "browser-extra")

    def test_control_panel_requires_browser_extra_profile(self):
        args = Namespace(host="127.0.0.1")
        stderr = StringIO()

        with patch.dict(os.environ, {"BRIDGE_PROFILE": "core-safe"}, clear=False), redirect_stderr(stderr):
            exit_code = cli_module.handle_control_panel(args)

        self.assertEqual(exit_code, 2)
        payload = json.loads(stderr.getvalue())
        self.assertEqual(payload["error"], "control_panel_profile_required")

    def test_cli_self_test_json_uses_bridge_home_override(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            env = os.environ.copy()
            env["BRIDGE_HOME"] = str(Path(tmp_dir) / "bridge-home")

            result = subprocess.run(
                [sys.executable, "-m", "mastermind_bridge.cli", "self-test", "--json"],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "ok")
            self.assertIn("codex_home", payload)

    def test_install_script_dry_run_uses_source_checkout_without_source_writes(self):
        repo_root = Path(__file__).resolve().parents[1]
        before = _git_status(repo_root)
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            env = os.environ.copy()
            env["PYTHONPATH"] = f"{repo_root / 'src'}{os.pathsep}{env.get('PYTHONPATH', '')}".rstrip(os.pathsep)
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            result = subprocess.run(
                [
                    "sh",
                    str(repo_root / "scripts" / "install.sh"),
                    "--dry-run",
                    "--bridge-home",
                    str(root / "bridge-home"),
                    "--codex-home",
                    str(root / "codex-home"),
                    "--json",
                ],
                capture_output=True,
                text=True,
                check=False,
                cwd=repo_root,
                env=env,
            )

        after = _git_status(repo_root)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "dry_run")
        self.assertEqual(after, before)

    def test_doctor_redacts_path_like_error_strings(self):
        redacted = lifecycle_module._redact_check(
            {
                "name": "sample",
                "status": "failed",
                "error": f"failed near {Path.home()}/private/config.json token=abc123",
                "details": [
                    'token="quoted123"',
                    '"token": "json123"',
                    "Authorization: Bearer bearer123",
                    'Authorization: Bearer "quotedbearer123"',
                ],
            }
        )
        redacted_json = json.dumps(redacted)

        self.assertNotIn(str(Path.home()), redacted_json)
        self.assertNotIn("abc123", redacted_json)
        self.assertNotIn("quoted123", redacted_json)
        self.assertNotIn("json123", redacted_json)
        self.assertNotIn("bearer123", redacted_json)
        self.assertNotIn("quotedbearer123", redacted_json)
        self.assertIn("<HOME>", redacted["error"])
        self.assertIn("token=<REDACTED>", redacted["error"])

    def test_cli_doctor_json_redacts_paths_and_uses_isolated_homes(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            home = root / "private-home"
            bridge_home = home / "bridge-home"
            codex_home = home / "codex-home"
            env = os.environ.copy()
            env["HOME"] = str(home)

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mastermind_bridge.cli",
                    "doctor",
                    "--bridge-home",
                    str(bridge_home),
                    "--codex-home",
                    str(codex_home),
                    "--json",
                ],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            payload = json.loads(result.stdout)
            rendered = json.dumps(payload)
            self.assertEqual(payload["status"], "ok")
            self.assertNotIn(str(home), rendered)
            self.assertIn("<HOME>", rendered)

    def test_release_builder_script_is_the_workflow_artifact_source(self):
        repo_root = Path(__file__).resolve().parents[1]
        builder = repo_root / "scripts" / "build_release_artifacts.sh"
        workflow = repo_root / ".github" / "workflows" / "release.yml"
        readme = repo_root / "README.md"

        self.assertIn("scripts/check_release_artifacts.py", builder.read_text(encoding="utf-8"))
        self.assertIn("SHA256SUMS", builder.read_text(encoding="utf-8"))
        self.assertIn("sh scripts/build_release_artifacts.sh", workflow.read_text(encoding="utf-8"))
        self.assertIn("actions/attest-build-provenance", workflow.read_text(encoding="utf-8"))
        self.assertIn("scripts/build_release_artifacts.sh", readme.read_text(encoding="utf-8"))
        self.assertIn("git status --short", builder.read_text(encoding="utf-8"))

    def test_install_rolls_back_replaced_skill_when_manifest_write_fails(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bridge_home = root / "bridge-home"
            codex_home = root / "codex-home"
            skill_dir = codex_home / "skills" / "chatgpt-codex-bridge"
            skill_dir.mkdir(parents=True)
            skill_file = skill_dir / "SKILL.md"
            skill_file.write_text("foreign\n", encoding="utf-8")

            with patch.object(lifecycle_module, "_write_json_atomic", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    install_bridge(
                        bridge_home_path=bridge_home,
                        codex_home_path=codex_home,
                        force=True,
                    )

            self.assertEqual(skill_file.read_text(encoding="utf-8"), "foreign\n")
            self.assertFalse((bridge_home / "install" / "manifest.json").exists())

    def test_reinstall_rollback_preserves_existing_sentinel_and_purge_path(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bridge_home = root / "bridge-home"
            codex_home = root / "codex-home"
            install_bridge(bridge_home_path=bridge_home, codex_home_path=codex_home)
            sentinel = bridge_home / ".chatgpt-codex-bridge-home"
            sentinel_text = sentinel.read_text(encoding="utf-8")

            with patch.object(lifecycle_module, "_write_json_atomic", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    install_bridge(
                        bridge_home_path=bridge_home,
                        codex_home_path=codex_home,
                        force=True,
                    )

            self.assertEqual(sentinel.read_text(encoding="utf-8"), sentinel_text)
            purged = uninstall_bridge(bridge_home_path=bridge_home, purge=True)
            self.assertEqual(purged.exit_code, 0)

    def test_public_docs_and_examples_do_not_contain_private_paths_or_thread_ids(self):
        tracked = subprocess.run(
            [
                "git",
                "ls-files",
                "-z",
                "README.md",
                "SECURITY.md",
                "CONTRIBUTING.md",
                "CHANGELOG.md",
                "docs",
                "examples",
                ".github",
                "src",
                "tests",
                "scripts",
                "pyproject.toml",
                ".gitignore",
                "LICENSE",
            ],
            capture_output=True,
            check=True,
        )
        untracked = subprocess.run(
            [
                "git",
                "ls-files",
                "--others",
                "--exclude-standard",
                "-z",
                "README.md",
                "SECURITY.md",
                "CONTRIBUTING.md",
                "CHANGELOG.md",
                "docs",
                "examples",
                ".github",
                "src",
                "tests",
                "scripts",
                "pyproject.toml",
                ".gitignore",
                "LICENSE",
            ],
            capture_output=True,
            check=True,
        )
        private_path = re.compile(rb"/Users/[A-Za-z0-9._-]+")
        likely_thread_id = re.compile(
            rb"\b019[0-9a-f]{5}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
        )
        violations: list[str] = []
        for raw_path in (tracked.stdout + untracked.stdout).split(b"\0"):
            if not raw_path:
                continue
            path = Path(raw_path.decode())
            data = path.read_bytes()
            if private_path.search(data) or likely_thread_id.search(data):
                violations.append(str(path))

        self.assertEqual(violations, [])

    def test_runtime_state_and_artifacts_are_not_tracked(self):
        result = subprocess.run(
            ["git", "ls-files", "state", "artifacts", "config"],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stdout.strip(), "")


def _git_status(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "status", "--short", "--untracked-files=all"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


if __name__ == "__main__":
    unittest.main()
