import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mastermind_bridge.executor import execute_codex_prompt


class ExecutorResumeTests(unittest.TestCase):
    def _write_fake_codex(self, root: Path) -> Path:
        fake_codex = root / "fake_codex.py"
        fake_codex.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import json",
                    "import sys",
                    "from pathlib import Path",
                    "",
                    "args = sys.argv[1:]",
                    "last_message = None",
                    "for index, value in enumerate(args):",
                    "    if value in ('-o', '--output-last-message'):",
                    "        last_message = Path(args[index + 1])",
                    "if last_message is not None:",
                    "    last_message.write_text('Resumed Codex session.\\n', encoding='utf-8')",
                    "print(json.dumps({'type': 'thread.started', 'thread_id': 'exec-thread-resume'}), flush=True)",
                    "print(json.dumps({'type': 'item.completed', 'item': {'id': 'item_0', 'type': 'agent_message', 'text': 'Resumed Codex session.'}}), flush=True)",
                    "print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 12, 'output_tokens': 5}}), flush=True)",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        fake_codex.chmod(0o755)
        return fake_codex

    def test_execute_codex_prompt_supports_resume_sessions(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            fake_codex = self._write_fake_codex(tmp_path)
            prompt_path = tmp_path / "NEXT_PROMPT.md"
            prompt_path.write_text("# Prompt\nResume the prior session.\n", encoding="utf-8")
            workdir = tmp_path / "workspace"
            workdir.mkdir()
            artifacts_root = tmp_path / "runs"

            report, execution = execute_codex_prompt(
                prompt_path=prompt_path,
                workdir=workdir,
                artifacts_root=artifacts_root,
                thread_id="thread-2",
                codex_bin=str(fake_codex),
                resume_session_id="exec-session-123",
            )

            self.assertEqual(report.observed_codex_thread_id, "exec-thread-resume")
            self.assertEqual(report.final_agent_message, "Resumed Codex session.")
            self.assertIn("resume", report.command)
            self.assertIn("exec-session-123", report.command)
            self.assertLess(report.command.index("-C"), report.command.index("resume"))
            self.assertLess(report.command.index("--json"), report.command.index("resume"))
            self.assertEqual(report.context_window_tokens, 200000)
            self.assertEqual(report.context_used_tokens, 17)
            self.assertEqual(report.estimated_context_remaining_percent, 99)
            self.assertEqual(execution["exit_code"], 0)

    @patch("mastermind_bridge.executor._verify_resumed_thread_turn_materialized")
    def test_execute_codex_prompt_fails_closed_when_resumed_turn_verification_fails(self, verify_mock):
        verify_mock.side_effect = RuntimeError("same_thread verification failed")

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            fake_codex = self._write_fake_codex(tmp_path)
            prompt_path = tmp_path / "NEXT_PROMPT.md"
            prompt_path.write_text("# Prompt\nResume the prior session.\n", encoding="utf-8")
            workdir = tmp_path / "workspace"
            workdir.mkdir()
            artifacts_root = tmp_path / "runs"

            with self.assertRaisesRegex(RuntimeError, "same_thread verification failed"):
                execute_codex_prompt(
                    prompt_path=prompt_path,
                    workdir=workdir,
                    artifacts_root=artifacts_root,
                    thread_id="thread-2",
                    codex_bin=str(fake_codex),
                    resume_session_id="exec-session-123",
                )

    def test_execute_codex_prompt_can_stop_a_running_process_via_callback(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            fake_codex = tmp_path / "fake_codex_stop.py"
            fake_codex.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env python3",
                        "import json",
                        "import time",
                        "import sys",
                        "from pathlib import Path",
                        "",
                        "args = sys.argv[1:]",
                        "last_message = None",
                        "for index, value in enumerate(args):",
                        "    if value in ('-o', '--output-last-message'):",
                        "        last_message = Path(args[index + 1])",
                        "if last_message is not None:",
                        "    last_message.write_text('Long running Codex session.\\n', encoding='utf-8')",
                        "print(json.dumps({'type': 'thread.started', 'thread_id': 'exec-thread-stop'}), flush=True)",
                        "print(json.dumps({'type': 'item.completed', 'item': {'id': 'item_0', 'type': 'agent_message', 'text': 'Long running Codex session.'}}), flush=True)",
                        "time.sleep(2.0)",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)
            prompt_path = tmp_path / "NEXT_PROMPT.md"
            prompt_path.write_text("# Prompt\nStop this session.\n", encoding="utf-8")
            workdir = tmp_path / "workspace"
            workdir.mkdir()
            artifacts_root = tmp_path / "runs"

            calls = {"count": 0}

            def _stop_checker():
                calls["count"] += 1
                return "stop" if calls["count"] >= 2 else None

            report, execution = execute_codex_prompt(
                prompt_path=prompt_path,
                workdir=workdir,
                artifacts_root=artifacts_root,
                thread_id="thread-stop",
                codex_bin=str(fake_codex),
                stop_checker=_stop_checker,
                stop_check_interval_seconds=0.01,
            )

            self.assertEqual(report.interruption_reason, "stop_requested")
            self.assertEqual(report.exit_code, 130)
            self.assertIn("stopped by control request", "\n".join(report.blockers).lower())
            self.assertEqual(execution["exit_code"], 130)

    def test_execute_codex_prompt_emits_progress_callbacks_while_waiting(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            fake_codex = tmp_path / "fake_codex_progress.py"
            fake_codex.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env python3",
                        "import json",
                        "import time",
                        "import sys",
                        "from pathlib import Path",
                        "",
                        "args = sys.argv[1:]",
                        "last_message = None",
                        "for index, value in enumerate(args):",
                        "    if value in ('-o', '--output-last-message'):",
                        "        last_message = Path(args[index + 1])",
                        "if last_message is not None:",
                        "    last_message.write_text('Progress heartbeat session.\\n', encoding='utf-8')",
                        "print(json.dumps({'type': 'thread.started', 'thread_id': 'exec-thread-progress'}), flush=True)",
                        "time.sleep(0.15)",
                        "print(json.dumps({'type': 'item.completed', 'item': {'id': 'item_0', 'type': 'agent_message', 'text': 'Progress heartbeat session.'}}), flush=True)",
                        "time.sleep(0.15)",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)
            prompt_path = tmp_path / "NEXT_PROMPT.md"
            prompt_path.write_text("# Prompt\nTrack progress callbacks.\n", encoding="utf-8")
            workdir = tmp_path / "workspace"
            workdir.mkdir()
            artifacts_root = tmp_path / "runs"

            heartbeat_calls = {"count": 0}
            progress_calls = {"count": 0}

            report, execution = execute_codex_prompt(
                prompt_path=prompt_path,
                workdir=workdir,
                artifacts_root=artifacts_root,
                thread_id="thread-progress",
                codex_bin=str(fake_codex),
                stop_checker=lambda: None,
                stop_check_interval_seconds=0.01,
                heartbeat_callback=lambda: heartbeat_calls.__setitem__("count", heartbeat_calls["count"] + 1),
                progress_callback=lambda: progress_calls.__setitem__("count", progress_calls["count"] + 1),
            )

            self.assertEqual(report.exit_code, 0)
            self.assertEqual(execution["exit_code"], 0)
            self.assertGreater(progress_calls["count"], 1)
            self.assertGreater(heartbeat_calls["count"], progress_calls["count"])


if __name__ == "__main__":
    unittest.main()
