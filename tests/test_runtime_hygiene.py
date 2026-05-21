import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mastermind_bridge.executor import _snapshot_workspace_files
from mastermind_bridge.runtime_cleanup import cleanup_runtime_state


class RuntimeHygieneTests(unittest.TestCase):
    def test_snapshot_workspace_files_ignores_runtime_noise(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "README.md").write_text("keep\n", encoding="utf-8")
            (root / ".dual-graph").mkdir()
            (root / ".dual-graph" / "info_graph.json").write_text("noise\n", encoding="utf-8")
            (root / ".dual-graph-context").mkdir()
            (root / ".dual-graph-context" / "cache.json").write_text("noise\n", encoding="utf-8")
            (root / ".local").mkdir()
            (root / ".local" / "scratch.json").write_text("noise\n", encoding="utf-8")
            (root / "Chat GPT Exports").mkdir()
            (root / "Chat GPT Exports" / "export.json").write_text("noise\n", encoding="utf-8")
            (root / "artifacts" / "runs" / "run-1").mkdir(parents=True)
            (root / "artifacts" / "runs" / "run-1" / "stdout.jsonl").write_text("noise\n", encoding="utf-8")
            (root / "assistant-memory" / "compiled" / "cards").mkdir(parents=True)
            (root / "assistant-memory" / "compiled" / "cards" / "old.md").write_text("noise\n", encoding="utf-8")
            (root / "state" / "playwright-profile" / "Default" / "Cache").mkdir(parents=True)
            (root / "state" / "playwright-profile" / "Default" / "Cache" / "entry").write_text(
                "noise\n",
                encoding="utf-8",
            )
            (root / "state" / "runtime_prompts" / "session-1").mkdir(parents=True)
            (root / "state" / "runtime_prompts" / "session-1" / "NEXT_PROMPT.md").write_text(
                "scratch\n",
                encoding="utf-8",
            )
            (root / "state" / "session_locks").mkdir(parents=True)
            (root / "state" / "session_locks" / "session-1.json").write_text("{}", encoding="utf-8")

            snapshot = _snapshot_workspace_files(root)

            self.assertEqual(set(snapshot), {"README.md"})

    def test_cleanup_runtime_state_removes_safe_caches_and_preserves_durable_state(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "state" / "playwright-profile" / "Default" / "Cache").mkdir(parents=True)
            (root / "state" / "playwright-profile" / "Default" / "Cache" / "entry").write_text(
                "cache\n",
                encoding="utf-8",
            )
            (root / ".dual-graph").mkdir()
            (root / ".dual-graph" / "info_graph.json").write_text("graph cache\n", encoding="utf-8")
            (root / ".dual-graph-context").mkdir()
            (root / ".dual-graph-context" / "PROJECT_CONTEXT.md").write_text("keep\n", encoding="utf-8")
            (root / ".coa" / "codesearch" / "indexes").mkdir(parents=True)
            (root / ".coa" / "codesearch" / "indexes" / "index.bin").write_bytes(b"index")
            (root / ".coa" / "codesearch" / "logs").mkdir(parents=True)
            (root / ".coa" / "codesearch" / "logs" / "codesearch.log").write_text(
                "generated log\n",
                encoding="utf-8",
            )
            (root / "__pycache__").mkdir()
            (root / "__pycache__" / "module.cpython-314.pyc").write_bytes(b"noise")
            (root / ".pytest_cache").mkdir()
            (root / ".pytest_cache" / "state").write_text("noise\n", encoding="utf-8")
            (root / "artifacts").mkdir()
            (root / "artifacts" / "supervise-session-1.log").write_text("runtime log\n", encoding="utf-8")
            (root / "state" / "sessions").mkdir(parents=True)
            (root / "state" / "sessions" / "session-1.json").write_text("{}", encoding="utf-8")
            (root / "artifacts" / "runs" / "run-1").mkdir(parents=True)
            (root / "artifacts" / "runs" / "run-1" / "run_report.json").write_text("{}", encoding="utf-8")
            (root / "docs").mkdir()
            (root / "docs" / "note.md").write_text("keep\n", encoding="utf-8")

            result = cleanup_runtime_state(root)

            self.assertIn("state/playwright-profile/Default/Cache", result.removed_paths)
            self.assertIn(".dual-graph", result.removed_paths)
            self.assertIn(".coa/codesearch/indexes", result.removed_paths)
            self.assertIn(".coa/codesearch/logs", result.removed_paths)
            self.assertIn("__pycache__", result.removed_paths)
            self.assertIn(".pytest_cache", result.removed_paths)
            self.assertIn("artifacts/supervise-session-1.log", result.removed_paths)
            self.assertGreater(result.bytes_reclaimed, 0)
            self.assertFalse((root / "state" / "playwright-profile" / "Default" / "Cache").exists())
            self.assertFalse((root / ".dual-graph").exists())
            self.assertFalse((root / ".coa" / "codesearch" / "indexes").exists())
            self.assertFalse((root / ".coa" / "codesearch" / "logs").exists())
            self.assertFalse((root / "__pycache__").exists())
            self.assertFalse((root / ".pytest_cache").exists())
            self.assertFalse((root / "artifacts" / "supervise-session-1.log").exists())
            self.assertTrue((root / ".dual-graph-context" / "PROJECT_CONTEXT.md").exists())
            self.assertTrue((root / "state" / "sessions" / "session-1.json").exists())
            self.assertTrue((root / "artifacts" / "runs" / "run-1" / "run_report.json").exists())
            self.assertTrue((root / "docs" / "note.md").exists())

    def test_cleanup_runtime_state_preserves_session_locks_and_config(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "state" / "session_locks").mkdir(parents=True)
            (root / "state" / "session_locks" / "session-1.json").write_text("{}", encoding="utf-8")
            (root / "config").mkdir()
            (root / "config" / "operator-policy.json").write_text("{}", encoding="utf-8")

            result = cleanup_runtime_state(root)

            self.assertNotIn("state/session_locks", result.removed_paths)
            self.assertTrue((root / "state" / "session_locks" / "session-1.json").exists())
            self.assertTrue((root / "config" / "operator-policy.json").exists())

    def test_cleanup_runtime_state_cli_dry_run_reports_without_deleting(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "state" / "playwright-profile" / "Default" / "GPUCache").mkdir(parents=True)
            (root / "state" / "playwright-profile" / "Default" / "GPUCache" / "entry").write_text(
                "cache\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mastermind_bridge.cli",
                    "cleanup-runtime-state",
                    "--repo-root",
                    str(root),
                    "--dry-run",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["dry_run"])
            self.assertIn("state/playwright-profile/Default/GPUCache", payload["matched_paths"])
            self.assertEqual(payload["removed_paths"], [])
            self.assertTrue((root / "state" / "playwright-profile" / "Default" / "GPUCache").exists())


if __name__ == "__main__":
    unittest.main()
