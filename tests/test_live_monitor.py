from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mastermind_bridge.control_panel_runtime import control_panel_runtime_fingerprint, run_terminal_live_monitor
from mastermind_bridge.live_monitor import LiveMonitor, format_live_log_line


class LiveMonitorTests(unittest.TestCase):
    def test_formats_agent_messages_readably(self):
        lines = format_live_log_line(
            'STDOUT | {"type":"item.completed","item":{"id":"item_14","type":"agent_message","text":"There is a real status update here."}}'
        )

        self.assertIn("Agent update [item_14]", lines)
        self.assertTrue(any("real status update" in line for line in lines))

    def test_formats_command_completions_readably(self):
        lines = format_live_log_line(
            'STDOUT | {"type":"item.completed","item":{"id":"item_17","type":"command_execution","command":"sqlite3 state/indexes/pab.sqlite3","aggregated_output":"6\\n181194\\n73\\n","exit_code":0,"status":"completed"}}'
        )

        self.assertIn("Ran command [item_17]", lines)
        self.assertIn("  sqlite3 state/indexes/pab.sqlite3", lines)
        self.assertIn("  Result:", lines)
        self.assertIn("    181194", lines)

    def test_formats_long_command_output_compactly(self):
        lines = format_live_log_line(
            'STDOUT | {"type":"item.completed","item":{"id":"item_2","type":"command_execution","command":"/bin/zsh -lc \\"find .. -name README.md | head -n 50\\"","aggregated_output":"a\\nb\\nc\\nd\\ne\\n","exit_code":0,"status":"completed"}}'
        )

        self.assertIn("Ran command [item_2]", lines)
        self.assertIn("  find .. -name README.md | head -n 50", lines)
        self.assertIn("  Result: 5 lines", lines)
        self.assertNotIn("    a", lines)

    def test_suppresses_command_started_lines(self):
        lines = format_live_log_line(
            'STDOUT | {"type":"item.started","item":{"id":"item_2","type":"command_execution","command":"echo hi","aggregated_output":"","exit_code":null,"status":"in_progress"}}'
        )

        self.assertEqual(lines, [])

    def test_shows_command_started_lines_in_expanded_mode(self):
        lines = format_live_log_line(
            'STDOUT | {"type":"item.started","item":{"id":"item_2","type":"command_execution","command":"echo hi","aggregated_output":"","exit_code":null,"status":"in_progress"}}',
            detail="expanded",
        )

        self.assertEqual(lines, ["", "Started command [item_2]", "  echo hi"])

    def test_summarizes_path_heavy_command_output_as_paths(self):
        lines = format_live_log_line(
            'STDOUT | {"type":"item.completed","item":{"id":"item_2","type":"command_execution","command":"find .. -name README.md","aggregated_output":"../a/README.md\\n../b/README.md\\n../c/README.md\\n../d/README.md\\n","exit_code":0,"status":"completed"}}'
        )

        self.assertIn("  Result: 4 paths", lines)

    def test_shows_head_and_tail_for_long_output_in_expanded_mode(self):
        payload = "\\n".join(f"line-{index:02d}" for index in range(40)) + "\\n"
        lines = format_live_log_line(
            'STDOUT | {"type":"item.completed","item":{"id":"item_2","type":"command_execution","command":"python3 - <<\'PY\'","aggregated_output":"'
            + payload
            + '","exit_code":0,"status":"completed"}}',
            detail="expanded",
        )

        self.assertIn("  Result: 40 lines (showing first 24 and last 8)", lines)
        self.assertIn("    line-00", lines)
        self.assertIn("    line-23", lines)
        self.assertIn("    … 8 lines omitted …", lines)
        self.assertIn("    line-32", lines)
        self.assertIn("    line-39", lines)

    def test_formats_turn_started_without_dumping_json(self):
        lines = format_live_log_line('STDOUT | {"type":"turn.started"}')

        self.assertEqual(lines, ["", "Turn started"])

    def test_formats_run_banners_readably(self):
        self.assertEqual(
            format_live_log_line("=== run started 2026-04-19T04:08:03+02:00 ==="),
            ["", "Run started: 2026-04-19T04:08:03+02:00"],
        )
        self.assertEqual(
            format_live_log_line("command=/Applications/Codex.app/Contents/Resources/codex exec -m gpt-5.4-mini"),
            ["Command: /Applications/Codex.app/Contents/Resources/codex exec -m gpt-5.4-mini"],
        )

    def test_formats_stderr_warnings_readably(self):
        lines = format_live_log_line(
            "STDERR | 2026-04-19T01:02:49.508047Z  WARN bridge_runtime: unusual but actionable warning"
        )

        self.assertIn("Warning 2026-04-19T01:02:49.508047Z", lines)
        self.assertTrue(any("unusual but actionable warning" in line for line in lines))

    def test_terminal_detail_shows_agent_messages_without_headers(self):
        lines = format_live_log_line(
            'STDOUT | {"type":"item.completed","item":{"id":"item_14","type":"agent_message","text":"There is a real status update here."}}',
            detail="terminal",
        )

        self.assertEqual(lines, ["", "There is a real status update here."])

    def test_terminal_detail_shows_command_success_and_hides_warnings(self):
        command_lines = format_live_log_line(
            'STDOUT | {"type":"item.completed","item":{"id":"item_17","type":"command_execution","command":"sqlite3 state/indexes/pab.sqlite3","aggregated_output":"6\\n181194\\n73\\n","exit_code":0,"status":"completed"}}',
            detail="terminal",
        )
        warning_lines = format_live_log_line(
            "STDERR | 2026-04-19T01:02:49.508047Z  WARN codex_core::plugins::manifest: ignoring interface.defaultPrompt",
            detail="terminal",
        )

        self.assertIn("Ran command [item_17]", command_lines)
        self.assertIn("  sqlite3 state/indexes/pab.sqlite3", command_lines)
        self.assertIn("  Result:", command_lines)
        self.assertIn("    181194", command_lines)
        self.assertEqual(warning_lines, [])

    def test_terminal_detail_shows_file_changes(self):
        lines = format_live_log_line(
            'STDOUT | {"type":"item.completed","item":{"id":"item_8","type":"file_change","status":"completed","changes":[{"kind":"update","path":"mastermind_bridge/live_monitor.py"},{"kind":"create","path":"docs/example.md"}]}}',
            detail="terminal",
        )

        self.assertIn("Edited 2 files [item_8]", lines)
        self.assertIn("  update: mastermind_bridge/live_monitor.py", lines)
        self.assertIn("  create: docs/example.md", lines)

    def test_compact_detail_hides_recurring_codex_environment_warnings(self):
        lines = format_live_log_line(
            "STDERR | 2026-04-19T01:02:49.508047Z  WARN codex_core_plugins::manifest: ignoring interface.defaultPrompt: maximum of 3 prompts is supported"
        )

        self.assertEqual(lines, [])

    def test_compact_detail_hides_recurring_codex_shutdown_and_cleanup_noise(self):
        samples = [
            "STDERR | 2026-04-19T01:02:50.000000Z ERROR codex_core::tools::router: error=agent with id stale-agent not found",
            "STDERR | 2026-04-19T01:02:51.000000Z  WARN codex_mcp::rmcp_client: failed to initialize MCP client during shutdown: MCP startup failed",
            "STDERR | 2026-04-19T01:02:52.000000Z  WARN codex_rmcp_client::stdio_server_launcher: Failed to kill MCP process group 123: No such process (os error 3)",
            "STDERR | 2026-04-19T01:02:53.000000Z  WARN codex_core_plugins::manifest: ignoring interface.defaultPrompt: prompt must be at most 128 characters path=fixtures/codex/tmp/plugins/plugin.json",
            "STDERR | 2026-04-19T01:02:54.000000Z  WARN codex_core_skills::loader: ignoring interface.icon_small: icon path must not contain '..'",
        ]

        for sample in samples:
            with self.subTest(sample=sample):
                self.assertEqual(format_live_log_line(sample, detail="compact"), [])

    def test_compact_detail_summarizes_collab_tool_calls_without_raw_json(self):
        lines = format_live_log_line(
            'STDOUT | {"type":"item.completed","item":{"id":"item_81","type":"collab_tool_call","tool":"close_agent","status":"completed","agents_states":{"agent-1":{"status":"completed","message":"Reviewed files only (read-only), plus related tests.\\n\\n- noisy detail"}}}}',
            detail="compact",
        )
        rendered = "\n".join(lines)

        self.assertIn("Subagent closed [item_81] (completed)", lines)
        self.assertIn("  agent-1: completed - Reviewed files only (read-only), plus related tests.", lines)
        self.assertNotIn('"agents_states"', rendered)
        self.assertNotIn('"message"', rendered)

    def test_terminal_detail_hides_collab_tool_calls(self):
        lines = format_live_log_line(
            'STDOUT | {"type":"item.completed","item":{"id":"item_81","type":"collab_tool_call","tool":"close_agent","status":"completed","agents_states":{"agent-1":{"status":"completed","message":"Done"}}}}',
            detail="terminal",
        )

        self.assertEqual(lines, [])

    def test_terminal_detail_keeps_failures_without_html_noise(self):
        lines = format_live_log_line(
            'STDOUT | {"type":"item.completed","item":{"id":"item_17","type":"command_execution","command":"make check","aggregated_output":"line 1\\nline 2\\nline 3\\n","exit_code":1,"status":"failed"}}',
            detail="terminal",
        )
        html_noise_lines = format_live_log_line("STDERR | <head>", detail="terminal")
        error_lines = format_live_log_line("STDERR | error: unexpected argument '-a' found", detail="terminal")

        self.assertIn("Command failed [item_17] exit=1", lines)
        self.assertIn("  make check", lines)
        self.assertIn("  Error output: 3 lines", lines)
        self.assertEqual(html_noise_lines, [])
        self.assertEqual(error_lines, ["", "error: unexpected argument '-a' found"])

    def test_summarizes_file_changes_readably(self):
        lines = format_live_log_line(
            'STDOUT | {"type":"item.completed","item":{"id":"item_8","type":"file_change","status":"completed","changes":[{"kind":"update","path":"mastermind_bridge/live_monitor.py"},{"kind":"update","path":"tests/test_live_monitor.py"},{"kind":"create","path":"docs/example.md"}]}}'
        )

        self.assertIn("Edited 3 files [item_8]", lines)
        self.assertIn("  update: mastermind_bridge/live_monitor.py", lines)
        self.assertIn("  … 1 more changes", lines)

    def test_shows_all_file_changes_in_expanded_mode(self):
        lines = format_live_log_line(
            'STDOUT | {"type":"item.completed","item":{"id":"item_8","type":"file_change","status":"completed","changes":[{"kind":"update","path":"mastermind_bridge/live_monitor.py"},{"kind":"update","path":"tests/test_live_monitor.py"},{"kind":"create","path":"docs/example.md"}]}}',
            detail="expanded",
        )

        self.assertIn("Edited 3 files [item_8]", lines)
        self.assertIn("  update: mastermind_bridge/live_monitor.py", lines)
        self.assertIn("  update: tests/test_live_monitor.py", lines)
        self.assertIn("  create: docs/example.md", lines)
        self.assertNotIn("  … 1 more changes", lines)

    def test_renders_initial_tail_from_existing_log(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_path = Path(tmp_dir) / "session.log"
            log_path.write_text(
                "\n".join(
                    [
                        'STDOUT | {"type":"thread.started","thread_id":"thread-123"}',
                        'STDOUT | {"type":"item.completed","item":{"id":"item_1","type":"agent_message","text":"Monitoring works."}}',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            output = io.StringIO()
            monitor = LiveMonitor(stream=output)

            start_offset = monitor.render_initial_tail(log_path, tail_lines=200)

            rendered = output.getvalue()
            self.assertEqual(start_offset, log_path.stat().st_size)
            self.assertIn("Thread started: thread-123", rendered)
            self.assertIn("Agent update [item_1]", rendered)

    def test_control_panel_runtime_fingerprint_is_stable_for_same_tree(self):
        first = control_panel_runtime_fingerprint()
        second = control_panel_runtime_fingerprint()

        self.assertEqual(first, second)
        self.assertTrue(first)

    def test_control_panel_runtime_fingerprint_changes_when_executor_runtime_changes(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            runtime_dir = root / "mastermind_bridge"
            orchestrator_dir = runtime_dir / "orchestrator"
            orchestrator_dir.mkdir(parents=True)
            (runtime_dir / "executor.py").write_text("print('old executor')\n", encoding="utf-8")
            (runtime_dir / "control_panel_runtime.py").write_text("print('runtime')\n", encoding="utf-8")
            (orchestrator_dir / "control_panel.py").write_text("print('panel')\n", encoding="utf-8")
            (orchestrator_dir / "control_panel_view.py").write_text("print('view')\n", encoding="utf-8")

            first = control_panel_runtime_fingerprint(root)
            (runtime_dir / "executor.py").write_text("print('new executor')\n", encoding="utf-8")
            second = control_panel_runtime_fingerprint(root)

        self.assertNotEqual(first, second)

    def test_run_terminal_live_monitor_prints_headers_and_reuses_live_monitor(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            artifacts_root = Path(tmp_dir) / "artifacts" / "runs"
            run_dir = artifacts_root / "20260419T000000-session-1"
            run_dir.mkdir(parents=True)
            (run_dir / "prompt.md").write_text("Prompt line 1\nPrompt line 2\n", encoding="utf-8")
            output = io.StringIO()

            def fake_live_monitor_main(argv):
                output.write(f"live-monitor argv={argv}\n")
                return 0

            with mock.patch("mastermind_bridge.control_panel_runtime.live_monitor_main", side_effect=fake_live_monitor_main):
                with mock.patch("sys.stdout", output):
                    exit_code = run_terminal_live_monitor(
                        session_id="session-1",
                        workspace_path="/tmp/workspace",
                        artifacts_root=artifacts_root,
                        tail_lines=50,
                        poll_interval=0.5,
                    )

            rendered = output.getvalue()
            self.assertEqual(exit_code, 0)
            self.assertIn("[bridge] watching formatted live session log:", rendered)
            self.assertIn("[bridge] observed workspace: /tmp/workspace", rendered)
            self.assertIn("[bridge] prompt sent to Codex:", rendered)
            self.assertIn("=== prompt sent to Codex ===", rendered)
            self.assertIn("Prompt line 1", rendered)
            self.assertIn("live-monitor argv=['--log',", rendered)
            self.assertIn("'--detail', 'terminal'", rendered)

    def test_run_terminal_live_monitor_can_skip_initial_prompt(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            artifacts_root = Path(tmp_dir) / "artifacts" / "runs"
            run_dir = artifacts_root / "20260419T000000-session-1"
            run_dir.mkdir(parents=True)
            (run_dir / "prompt.md").write_text("Huge prompt that should stay hidden\n", encoding="utf-8")
            output = io.StringIO()

            def fake_live_monitor_main(argv):
                output.write(f"live-monitor argv={argv}\n")
                return 0

            with mock.patch("mastermind_bridge.control_panel_runtime.live_monitor_main", side_effect=fake_live_monitor_main):
                with mock.patch("sys.stdout", output):
                    exit_code = run_terminal_live_monitor(
                        session_id="session-1",
                        workspace_path="/tmp/workspace",
                        artifacts_root=artifacts_root,
                        tail_lines=0,
                        poll_interval=0.5,
                        emit_initial_prompt=False,
                    )

            rendered = output.getvalue()
            self.assertEqual(exit_code, 0)
            self.assertIn("[bridge] watching formatted live session log:", rendered)
            self.assertIn("[bridge] observed workspace: /tmp/workspace", rendered)
            self.assertNotIn("Huge prompt that should stay hidden", rendered)
            self.assertNotIn("=== prompt sent to Codex ===", rendered)
            self.assertIn("'--tail-lines', '0'", rendered)


if __name__ == "__main__":
    unittest.main()
