import io
import json
import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from mastermind_bridge.executor import (
    _CodexAppServerSession,
    _OPENED_CODEX_APP_THREADS,
    _can_execute_native_turn_start,
    _command_looks_like_codex_exec,
    _default_native_codex_thread_name,
    _derive_next_step,
    _derive_summary,
    _estimate_context_metrics,
    _derive_blockers,
    _preflight_openai_api_reachability,
    _extract_thread_id_from_rollout_session_path,
    _extract_explicit_report_fields,
    _infer_files_touched_from_snapshots,
    _infer_checks_from_commands,
    _resolve_codex_app_server_bin,
    _run_codex_app_server_request,
    _run_codex_native_turn_with_polling,
    _run_codex_with_polling,
    _verify_resumed_thread_turn_materialized,
    _sanitize_forked_rollout_session_file,
    _open_codex_app_thread_best_effort,
    _open_codex_app_thread_once_best_effort,
    _process_tree_has_observable_activity,
    _snapshot_workspace_files,
    codex_app_integration_enabled,
    compact_codex_thread_after_turn,
    execute_codex_prompt,
    prepare_native_codex_fork_thread,
    prepare_native_codex_start_thread,
    register_codex_app_thread_best_effort,
    parse_exec_events,
)


class _FakePopen:
    def __init__(self, *, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.args = ["codex", "app-server"]
        self.stdin = _FakeWritablePipe()
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)
        self.returncode = returncode
        self._running = True
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True
        self._running = False

    def kill(self):
        self.killed = True
        self._running = False
        self.returncode = self.returncode or -9

    def poll(self):
        return None if self._running else self.returncode

    def wait(self, timeout=None):
        self._running = False
        return self.returncode


class _FakeWritablePipe(io.StringIO):
    def close(self):
        self._closed = True

    @property
    def closed(self):
        return bool(getattr(self, "_closed", False))


class _CompletingPopen(_FakePopen):
    def __init__(self, *, stdout: str = "", stderr: str = "", returncode: int = 0, pending_polls: int = 1):
        super().__init__(stdout=stdout, stderr=stderr, returncode=returncode)
        self._pending_polls = pending_polls

    def poll(self):
        if self._running and self._pending_polls > 0:
            self._pending_polls -= 1
            return None
        self._running = False
        return self.returncode


class ExecuteCodexReportParsingTests(unittest.TestCase):
    def test_derive_blockers_skips_recurring_codex_environment_noise(self):
        stderr_text = "\n".join(
            [
                "2026-04-15T09:18:48.413415Z ERROR codex_core_skills::loader: failed to stat skills entry /tmp/example-home/.codex/skills/bun-runtime (symlink): No such file or directory (os error 2)",
                "2026-04-15T09:38:19.857130Z  WARN codex_core::plugins::manifest: ignoring interface.defaultPrompt: prompt must be at most 128 characters path=/tmp/example-home/.codex/.tmp/plugins/plugins/life-science-research/.codex-plugin/plugin.json",
                "runner failed",
            ]
        )

        blockers = _derive_blockers(7, stderr_text)

        self.assertEqual(blockers, ["codex exec exited with code 7", "runner failed"])

    def test_derive_blockers_omits_noise_only_stderr_lines(self):
        stderr_text = "\n".join(
            [
                "2026-04-15T09:18:48.413415Z ERROR codex_core_skills::loader: failed to stat skills entry /tmp/example-home/.codex/skills/bun-runtime (symlink): No such file or directory (os error 2)",
                "2026-05-05T01:00:00.000000Z  WARN codex_core_skills::loader: ignoring interface.icon_small: expected string path=/tmp/example-home/.codex/plugins/plugin.json",
                "2026-05-05T01:00:00.000000Z  WARN codex_core_skills::loader: ignoring interface.icon_large: expected string path=/tmp/example-home/.codex/plugins/plugin.json",
                "2026-04-15T09:38:19.857130Z  WARN codex_core::plugins::manifest: ignoring interface.defaultPrompt: prompt must be at most 128 characters path=/tmp/example-home/.codex/.tmp/plugins/plugins/life-science-research/.codex-plugin/plugin.json",
                "2026-04-25T02:21:33.961918Z  WARN codex_core_plugins::manifest: ignoring interface.defaultPrompt: maximum of 3 prompts is supported path=/tmp/example-home/.codex/.tmp/plugins/plugins/plugin-eval/.codex-plugin/plugin.json",
                "2026-04-30T07:56:26.405090Z  WARN codex_core::session::turn: after_agent hook failed; continuing turn_id=019ddd62 hook_name=legacy_notify error=No such file or directory (os error 2)",
                "2026-04-30T07:56:26.419892Z ERROR codex_core::session: failed to record rollout items: thread 019ddd62 not found",
                "2026-04-30T08:10:31.479236Z  WARN codex_rmcp_client::stdio_server_launcher: Failed to terminate MCP process group 26047: Operation not permitted (os error 1)",
                "2026-05-19T00:05:13.285568Z  WARN codex_otel::events::session_telemetry: metrics counter [codex.skill.injected] failed: tag value contains invalid characters: superpowers:executing-plans",
                "2026-05-18T23:48:29.826364Z  WARN codex_core_plugins::manager: failed to warm featured plugin ids cache error=remote plugin sync request to https://chatgpt.com/backend-api/plugins/featured failed with status 403 Forbidden: <html>",
            ]
        )

        blockers = _derive_blockers(7, stderr_text)

        self.assertEqual(blockers, ["codex exec exited with code 7"])

    def test_success_next_step_does_not_ask_chatgpt_to_paste_raw_noise(self):
        self.assertEqual(
            _derive_next_step(0, ""),
            "Continue from the final Codex output and clean execution trace in the same ChatGPT chat.",
        )

    def test_derive_blockers_skips_current_plugin_namespace_before_real_error(self):
        stderr_text = "\n".join(
            [
                "2026-04-25T02:21:33.961918Z  WARN codex_core_plugins::manifest: ignoring interface.defaultPrompt: maximum of 3 prompts is supported path=/tmp/example-home/.codex/.tmp/plugins/plugins/plugin-eval/.codex-plugin/plugin.json",
                "2026-04-25T02:23:05.032046Z ERROR codex_core::tools::router: error=failed to parse function arguments: EOF while parsing an object at line 75892 column 0",
            ]
        )

        blockers = _derive_blockers(1, stderr_text)

        self.assertEqual(
            blockers,
            [
                "codex exec exited with code 1",
                "2026-04-25T02:23:05.032046Z ERROR codex_core::tools::router: error=failed to parse function arguments: EOF while parsing an object at line 75892 column 0",
            ],
        )

    def test_derive_blockers_omits_transient_router_exec_failure_on_success(self):
        stderr_text = (
            "2026-04-25T03:37:42.406132Z ERROR codex_core::tools::router: error=exec_command failed for "
            "`/bin/zsh -lc \"sed -n '1470,1605p' Tests/PABBrainFeatureTests/BrainQueryServiceTests.swift\"`: "
            "CreateProcess { message: \"Rejected(\\\"Failed to create unified exec process: "
            "No such file or directory (os error 2)\\\")\" }"
        )

        blockers = _derive_blockers(0, stderr_text)

        self.assertEqual(blockers, [])

    def test_derive_blockers_omits_shell_snapshot_warning_on_success(self):
        stderr_text = (
            '2026-04-15T10:01:23.787195Z  WARN codex_core::shell_snapshot: Failed to delete shell snapshot at '
            '"/tmp/example-home/.codex/shell_snapshots/aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee.tmp-1776247283708756000": '
            'Os { code: 2, kind: NotFound, message: "No such file or directory" }'
        )

        blockers = _derive_blockers(0, stderr_text)

        self.assertEqual(blockers, [])

    def test_derive_blockers_skips_shell_snapshot_and_surfaces_real_error(self):
        stderr_text = "\n".join(
            [
                '2026-04-15T10:01:23.787195Z  WARN codex_core::shell_snapshot: Failed to delete shell snapshot at '
                '"/tmp/example-home/.codex/shell_snapshots/aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee.tmp-1776247283708756000": '
                'Os { code: 2, kind: NotFound, message: "No such file or directory" }',
                "runner failed",
            ]
        )

        blockers = _derive_blockers(7, stderr_text)

        self.assertEqual(blockers, ["codex exec exited with code 7", "runner failed"])

    def test_derive_blockers_classifies_openai_network_failure(self):
        stderr_text = "\n".join(
            [
                "2026-04-17T07:43:04.584582Z ERROR codex_api::endpoint::responses_websocket: failed to connect to websocket: IO error: failed to lookup address information: nodename nor servname provided, or not known, url: wss://api.openai.com/v1/responses",
                "2026-04-17T07:43:10.585263Z  WARN codex_core::client: falling back to HTTP",
            ]
        )

        blockers = _derive_blockers(1, stderr_text)

        self.assertEqual(
            blockers[0],
            "Codex could not reach the OpenAI API because network or DNS access was unavailable from this process.",
        )
        self.assertIn("responses_websocket", blockers[1])

    def test_derive_summary_and_next_step_classify_openai_network_failure(self):
        stderr_text = (
            "2026-04-17T07:43:04.584582Z ERROR codex_api::endpoint::responses_websocket: "
            "failed to connect to websocket: IO error: failed to lookup address information: "
            "nodename nor servname provided, or not known, url: wss://api.openai.com/v1/responses"
        )

        summary = _derive_summary("", "", 1, stderr_text)
        next_step = _derive_next_step(1, stderr_text)

        self.assertEqual(
            summary,
            "Codex could not reach the OpenAI API because network or DNS access was unavailable from this process.",
        )
        self.assertIn("network/API reachability", next_step)

    @patch("mastermind_bridge.executor.socket.getaddrinfo")
    def test_preflight_openai_api_reachability_surfaces_dns_failure(self, getaddrinfo_mock):
        getaddrinfo_mock.side_effect = socket.gaierror("nodename nor servname provided, or not known")

        stderr_text = _preflight_openai_api_reachability()

        self.assertIn("failed to lookup address information", stderr_text)
        self.assertIn("api.openai.com", stderr_text)
        self.assertIn("nodename nor servname provided", stderr_text)

    def test_should_preflight_openai_reachability_only_for_real_codex_cli(self):
        from mastermind_bridge.executor import _should_preflight_openai_reachability

        self.assertTrue(_should_preflight_openai_reachability(codex_bin="codex", enabled=True))
        self.assertTrue(
            _should_preflight_openai_reachability(
                codex_bin="/Applications/Codex.app/Contents/Resources/codex",
                enabled=True,
            )
        )
        self.assertFalse(
            _should_preflight_openai_reachability(
                codex_bin="/tmp/fake_codex.py",
                enabled=True,
            )
        )
        self.assertFalse(_should_preflight_openai_reachability(codex_bin="codex", enabled=False))

    def test_native_turn_start_is_disabled_by_default_for_bridge_stability(self):
        self.assertFalse(
            _can_execute_native_turn_start(
                "codex",
                resume_session_id="thread-123",
                stop_checker=None,
            )
        )

    @patch("mastermind_bridge.executor.subprocess.Popen")
    def test_run_codex_with_polling_streams_live_output_to_log_files(self, popen_mock):
        fake_process = _CompletingPopen(
            stdout='{"type":"thread.started","thread_id":"thread-123"}\n',
            stderr="warn line\n",
            returncode=0,
            pending_polls=1,
        )
        popen_mock.return_value = fake_process

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            run_log = tmp_path / "run.log"
            session_log = tmp_path / "session.log"

            completed, interruption_reason = _run_codex_with_polling(
                command=["codex", "exec"],
                prompt_text="Prompt text",
                timeout_seconds=5,
                progress_stall_seconds=300,
                env=None,
                stop_checker=lambda: None,
                stop_check_interval_seconds=0.01,
                live_log_paths=(run_log, session_log),
            )

            self.assertEqual(completed.returncode, 0)
            self.assertEqual(interruption_reason, "")
            self.assertIn("STDOUT | {\"type\":\"thread.started\"", run_log.read_text())
            self.assertIn("STDERR | warn line", session_log.read_text())

    @patch("mastermind_bridge.executor.subprocess.Popen")
    def test_run_codex_with_polling_terminates_when_output_stalls(self, popen_mock):
        fake_process = _CompletingPopen(
            stdout='{"type":"thread.started","thread_id":"thread-123"}\n',
            stderr="",
            returncode=0,
            pending_polls=999,
        )
        popen_mock.return_value = fake_process

        completed, interruption_reason = _run_codex_with_polling(
            command=["codex", "exec"],
            prompt_text="Prompt text",
            timeout_seconds=5,
            progress_stall_seconds=0.05,
            env=None,
            stop_checker=lambda: None,
            stop_check_interval_seconds=0.01,
        )

        self.assertEqual(completed.returncode, 124)
        self.assertEqual(interruption_reason, "progress_stall")
        self.assertIn("stalled without new output", completed.stderr)
        self.assertTrue(fake_process.terminated)

    @patch("mastermind_bridge.executor._process_tree_has_observable_activity", return_value=True)
    @patch("mastermind_bridge.executor.subprocess.Popen")
    def test_run_codex_with_polling_does_not_stall_while_process_tree_is_active(self, popen_mock, activity_mock):
        fake_process = _CompletingPopen(
            stdout='{"type":"thread.started","thread_id":"thread-123"}\n',
            stderr="",
            returncode=0,
            pending_polls=8,
        )
        popen_mock.return_value = fake_process

        completed, interruption_reason = _run_codex_with_polling(
            command=["codex", "exec"],
            prompt_text="Prompt text",
            timeout_seconds=5,
            progress_stall_seconds=0.02,
            env=None,
            stop_checker=lambda: None,
            stop_check_interval_seconds=0.01,
        )

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(interruption_reason, "")
        self.assertFalse(fake_process.terminated)
        self.assertTrue(activity_mock.called)

    @patch("mastermind_bridge.executor._process_tree_has_observable_activity", return_value=True)
    @patch("mastermind_bridge.executor.subprocess.Popen")
    def test_run_codex_with_polling_extends_timeout_while_process_tree_is_active(self, popen_mock, activity_mock):
        fake_process = _CompletingPopen(
            stdout='{"type":"thread.started","thread_id":"thread-123"}\n',
            stderr="",
            returncode=0,
            pending_polls=8,
        )
        popen_mock.return_value = fake_process

        completed, interruption_reason = _run_codex_with_polling(
            command=["codex", "exec"],
            prompt_text="Prompt text",
            timeout_seconds=0.02,
            progress_stall_seconds=None,
            env=None,
            stop_checker=lambda: None,
            stop_check_interval_seconds=0.01,
        )

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(interruption_reason, "")
        self.assertFalse(fake_process.terminated)
        self.assertTrue(activity_mock.called)

    @patch("mastermind_bridge.executor._process_tree_has_observable_activity", return_value=False)
    @patch("mastermind_bridge.executor.subprocess.Popen")
    def test_run_codex_with_polling_extends_hard_timeout_after_recent_output(self, popen_mock, activity_mock):
        fake_process = _CompletingPopen(
            stdout='{"type":"thread.started","thread_id":"thread-123"}\n',
            stderr="",
            returncode=0,
            pending_polls=8,
        )
        popen_mock.return_value = fake_process

        completed, interruption_reason = _run_codex_with_polling(
            command=["codex", "exec"],
            prompt_text="Prompt text",
            timeout_seconds=0.02,
            progress_stall_seconds=300,
            env=None,
            stop_checker=lambda: None,
            stop_check_interval_seconds=0.01,
        )

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(interruption_reason, "")
        self.assertFalse(fake_process.terminated)
        self.assertFalse(activity_mock.called)

    @patch("mastermind_bridge.executor.subprocess.run")
    def test_process_tree_activity_treats_protected_child_commands_as_active(self, run_mock):
        run_mock.return_value = subprocess.CompletedProcess(
            ["ps"],
            0,
            "\n".join(
                [
                    "100 1 S 0.0 /Applications/Codex.app/Contents/Resources/codex exec",
                    "101 100 S 0.0 /Library/Developer/CommandLineTools/usr/bin/swift-test --filter StateStoreAndAppleIngestionTests",
                ]
            ),
            "",
        )
        fake_process = type("FakeProcess", (), {"pid": 100})()

        self.assertTrue(_process_tree_has_observable_activity(fake_process))

    @patch("mastermind_bridge.executor.subprocess.run")
    def test_process_tree_activity_ignores_idle_unprotected_child_commands(self, run_mock):
        run_mock.return_value = subprocess.CompletedProcess(
            ["ps"],
            0,
            "\n".join(
                [
                    "100 1 S 0.0 /Applications/Codex.app/Contents/Resources/codex exec",
                    "101 100 S 0.0 ./Codex Computer Use.app/Contents/SharedSupport/SkyComputerUseClient.app/Contents/MacOS/SkyComputerUseClient mcp",
                ]
            ),
            "",
        )
        fake_process = type("FakeProcess", (), {"pid": 100})()

        self.assertFalse(_process_tree_has_observable_activity(fake_process))

    @patch("mastermind_bridge.executor.subprocess.run")
    def test_process_tree_activity_ignores_root_running_state_without_cpu_or_children(self, run_mock):
        run_mock.return_value = subprocess.CompletedProcess(
            ["ps"],
            0,
            "100 1 R 0.0 /tmp/fake_codex exec",
            "",
        )
        fake_process = type("FakeProcess", (), {"pid": 100})()

        self.assertFalse(_process_tree_has_observable_activity(fake_process))

    def test_command_looks_like_codex_exec_rejects_fake_codex_wrappers(self):
        self.assertTrue(_command_looks_like_codex_exec("/Applications/Codex.app/Contents/Resources/codex exec"))
        self.assertTrue(_command_looks_like_codex_exec("/opt/homebrew/bin/codex exec --json"))
        self.assertFalse(_command_looks_like_codex_exec("/tmp/fake_codex.py exec"))

    def test_extract_explicit_report_fields_from_inline_markers(self):
        last_message = "\n".join(
            [
                "Implemented the requested parser update.",
                "Files touched: README.md, mastermind_bridge/executor.py, tests/test_cli.py",
                "Checks run: python3 -m unittest discover -s tests",
            ]
        )

        files_touched, checks = _extract_explicit_report_fields(last_message)

        self.assertEqual(
            files_touched,
            ["README.md", "mastermind_bridge/executor.py", "tests/test_cli.py"],
        )
        self.assertEqual(checks, ["python3 -m unittest discover -s tests"])

    def test_extract_explicit_report_fields_from_bulleted_sections(self):
        last_message = "\n".join(
            [
                "Implemented the requested parser update.",
                "Files touched:",
                "- README.md",
                "- mastermind_bridge/executor.py",
                "",
                "Checks run:",
                "- python3 -m unittest tests.test_executor",
                "- python3 -m unittest tests.test_cli",
            ]
        )

        files_touched, checks = _extract_explicit_report_fields(last_message)

        self.assertEqual(files_touched, ["README.md", "mastermind_bridge/executor.py"])
        self.assertEqual(
            checks,
            [
                "python3 -m unittest tests.test_executor",
                "python3 -m unittest tests.test_cli",
            ],
        )

    def test_extract_explicit_report_fields_ignores_missing_markers(self):
        files_touched, checks = _extract_explicit_report_fields("No structured markers here.")

        self.assertEqual(files_touched, [])
        self.assertEqual(checks, [])

    def test_infer_checks_from_commands_uses_real_test_commands_only(self):
        commands_observed = [
            {
                "command": "/bin/zsh -lc \"sed -n '1,240p' tests/test_executor.py\"",
                "status": "completed",
                "exit_code": 0,
            },
            {
                "command": "/bin/zsh -lc 'python3 -m unittest tests.test_executor tests.test_cli'",
                "status": "failed",
                "exit_code": 1,
            },
            {
                "command": "/bin/zsh -lc 'python3 -m unittest tests.test_executor tests.test_cli'",
                "status": "completed",
                "exit_code": 0,
            },
            {
                "command": "/bin/zsh -lc 'git diff --stat'",
                "status": "completed",
                "exit_code": 0,
            },
        ]

        checks = _infer_checks_from_commands(commands_observed)

        self.assertEqual(checks, ["python3 -m unittest tests.test_executor tests.test_cli"])

    def test_infer_files_touched_from_snapshots_ignores_artifacts_and_cache_noise(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            workdir = Path(tmp_dir)
            artifacts_root = workdir / "artifacts"
            source_file = workdir / "README.md"
            source_file.write_text("before\n", encoding="utf-8")
            cached_file = workdir / "__pycache__" / "executor.cpython-314.pyc"
            cached_file.parent.mkdir()
            cached_file.write_bytes(b"before")
            compiled_memory_file = workdir / "assistant-memory" / "compiled" / "cards" / "old-run.md"
            compiled_memory_file.parent.mkdir(parents=True)
            compiled_memory_file.write_text("before\n", encoding="utf-8")

            before = _snapshot_workspace_files(workdir, ignored_roots=[artifacts_root])

            source_file.write_text("after\n", encoding="utf-8")
            new_file = workdir / "tests" / "test_executor.py"
            new_file.parent.mkdir()
            new_file.write_text("print('changed')\n", encoding="utf-8")
            cached_file.write_bytes(b"after")
            compiled_memory_file.write_text("after\n", encoding="utf-8")
            new_compiled_memory_file = workdir / "assistant-memory" / "compiled" / "cards" / "new-run.md"
            new_compiled_memory_file.write_text("generated\n", encoding="utf-8")
            artifact_file = artifacts_root / "runs" / "stdout.jsonl"
            artifact_file.parent.mkdir(parents=True)
            artifact_file.write_text("ignored\n", encoding="utf-8")

            after = _snapshot_workspace_files(workdir, ignored_roots=[artifacts_root])

            self.assertEqual(
                _infer_files_touched_from_snapshots(before, after),
                ["README.md", "tests/test_executor.py"],
            )

    def test_extract_explicit_report_fields_merges_partial_sources(self):
        last_message = "\n".join(
            [
                "Implemented partial timeout rec",
                "Files touched:",
                "- README.md",
                "- assistant-memory/compiled/cards/old-run.md",
            ]
        )
        final_agent_message = "\n".join(
            [
                "Implemented partial timeout recovery.",
                "Files touched: README.md, mastermind_bridge/executor.py, assistant-memory/compiled/cards/new-run.md",
                "Checks run: python3 -m unittest tests.test_executor",
            ]
        )

        files_touched, checks = _extract_explicit_report_fields(last_message, final_agent_message)

        self.assertEqual(files_touched, ["README.md", "mastermind_bridge/executor.py"])
        self.assertEqual(checks, ["python3 -m unittest tests.test_executor"])

    def test_parse_exec_events_recovers_truncated_agent_message_line(self):
        stdout_text = "\n".join(
            [
                '{"type":"thread.started","thread_id":"exec-thread-xyz"}',
                '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"Implemented partial timeout recovery.\\nFiles touched: README.md, mastermind_bridge/executor.py\\nChecks run: python3 -m unittest tests.test_executor',
            ]
        )

        parsed = parse_exec_events(stdout_text)

        self.assertEqual(parsed.observed_codex_thread_id, "exec-thread-xyz")
        self.assertIn("item.completed", parsed.event_types)
        self.assertEqual(
            parsed.final_agent_message,
            "\n".join(
                [
                    "Implemented partial timeout recovery.",
                    "Files touched: README.md, mastermind_bridge/executor.py",
                    "Checks run: python3 -m unittest tests.test_executor",
                ]
            ),
        )

    def test_estimate_context_metrics_uses_codex_usage_to_compute_remaining_percent(self):
        metrics = _estimate_context_metrics(
            {"input_tokens": 120000, "cached_input_tokens": 40000, "output_tokens": 1500},
            model=None,
        )

        self.assertEqual(metrics["context_window_tokens"], 200000)
        self.assertEqual(metrics["context_used_tokens"], 121500)
        self.assertEqual(metrics["estimated_context_remaining_percent"], 39)
        self.assertEqual(metrics["context_signal_source"], "default")

    @patch("mastermind_bridge.executor.subprocess.run")
    @patch("mastermind_bridge.executor.sys.platform", "darwin")
    def test_open_codex_app_thread_best_effort_uses_deeplink(self, run_mock):
        _open_codex_app_thread_best_effort("thread-123")

        run_mock.assert_called_once()
        command = run_mock.call_args.args[0]
        self.assertEqual(command, ["osascript", "-"])
        self.assertIn('application id "com.openai.codex"', run_mock.call_args.kwargs["input"])
        self.assertIn("codex://threads/thread-123", run_mock.call_args.kwargs["input"])

    @patch("mastermind_bridge.executor._open_codex_app_thread_best_effort")
    def test_open_codex_app_thread_once_best_effort_deduplicates_thread_ids(self, open_mock):
        _OPENED_CODEX_APP_THREADS.clear()
        open_mock.side_effect = [True, True]

        _open_codex_app_thread_once_best_effort("thread-123")
        _open_codex_app_thread_once_best_effort("thread-123")
        _open_codex_app_thread_once_best_effort("thread-456")

        self.assertEqual(open_mock.call_args_list[0].args, ("thread-123",))
        self.assertEqual(open_mock.call_args_list[1].args, ("thread-456",))
        self.assertEqual(open_mock.call_count, 2)

    @patch("mastermind_bridge.executor._open_codex_app_thread_best_effort")
    def test_open_codex_app_thread_once_best_effort_retries_same_thread_after_failed_open(self, open_mock):
        _OPENED_CODEX_APP_THREADS.clear()
        open_mock.side_effect = [False, True]

        _open_codex_app_thread_once_best_effort("thread-123")
        _open_codex_app_thread_once_best_effort("thread-123")

        self.assertEqual(open_mock.call_args_list[0].args, ("thread-123",))
        self.assertEqual(open_mock.call_args_list[1].args, ("thread-123",))
        self.assertEqual(open_mock.call_count, 2)

    def test_extract_thread_id_from_rollout_session_path(self):
        path = Path("/tmp/rollout-2026-04-16T02-58-18-aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee.jsonl")

        self.assertEqual(
            _extract_thread_id_from_rollout_session_path(path),
            "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        )

    def test_default_native_codex_thread_name_uses_workdir_name_and_short_id(self):
        name = _default_native_codex_thread_name(Path("/tmp/personal-assistant-bridge"), "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")

        self.assertEqual(name, "personal-assistant-bridge aaaaaaaa")

    def test_sanitize_forked_rollout_session_file_removes_foreign_session_meta(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "rollout.jsonl"
            path.write_text(
                "\n".join(
                    [
                        '{"type":"session_meta","payload":{"id":"child-thread-123"}}',
                        '{"type":"session_meta","payload":{"id":"parent-thread-456"}}',
                        '{"type":"event_msg","payload":{"type":"task_started"}}',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            _sanitize_forked_rollout_session_file(path, thread_id="child-thread-123")

            self.assertEqual(
                path.read_text(encoding="utf-8").splitlines(),
                [
                    '{"type":"session_meta","payload":{"id":"child-thread-123"}}',
                    '{"type":"event_msg","payload":{"type":"task_started"}}',
                ],
            )

    @patch("mastermind_bridge.executor._CODEX_APP_SERVER_BIN")
    def test_resolve_codex_app_server_bin_prefers_native_app_cli_over_dual_graph_wrapper(self, bundle_bin_mock):
        bundle_bin_mock.exists.return_value = True
        bundle_bin_mock.__str__.return_value = "/Applications/Codex.app/Contents/Resources/codex"

        resolved = _resolve_codex_app_server_bin("/tmp/example-home/.dual-graph/codex")

        self.assertEqual(resolved, "/Applications/Codex.app/Contents/Resources/codex")

    @patch("mastermind_bridge.executor._CODEX_APP_SERVER_BIN")
    def test_resolve_codex_app_server_bin_prefers_native_app_cli_for_default_codex_name(self, bundle_bin_mock):
        bundle_bin_mock.exists.return_value = True
        bundle_bin_mock.__str__.return_value = "/Applications/Codex.app/Contents/Resources/codex"

        resolved = _resolve_codex_app_server_bin("codex")

        self.assertEqual(resolved, "/Applications/Codex.app/Contents/Resources/codex")

    @patch.dict(os.environ, {}, clear=True)
    @patch("mastermind_bridge.executor._CODEX_APP_SERVER_BIN")
    @patch("mastermind_bridge.executor.sys.platform", "darwin")
    def test_codex_app_integration_enabled_defaults_on_when_native_app_exists(self, bundle_bin_mock):
        bundle_bin_mock.exists.return_value = True

        self.assertTrue(codex_app_integration_enabled())

    @patch.dict(os.environ, {"BRIDGE_ENABLE_CODEX_APP_INTEGRATION": "0"}, clear=False)
    @patch("mastermind_bridge.executor._CODEX_APP_SERVER_BIN")
    @patch("mastermind_bridge.executor.sys.platform", "darwin")
    def test_codex_app_integration_enabled_allows_explicit_opt_out(self, bundle_bin_mock):
        bundle_bin_mock.exists.return_value = True

        self.assertFalse(codex_app_integration_enabled())

    @patch("mastermind_bridge.executor.subprocess.Popen")
    @patch("mastermind_bridge.executor._resolve_codex_app_server_bin", return_value="/Applications/Codex.app/Contents/Resources/codex")
    def test_run_codex_app_server_request_performs_initialize_handshake_before_method(
        self,
        _resolve_mock,
        popen_mock,
    ):
        process = _FakePopen(
            stdout="\n".join(
                [
                    '{"id":1,"result":{"userAgent":"Codex Desktop"}}',
                    '{"method":"thread/started","params":{"thread":{"id":"child-thread-123"}}}',
                    '{"id":2,"result":{"thread":{"id":"child-thread-123"}}}',
                ]
            )
        )
        popen_mock.return_value = process

        result = _run_codex_app_server_request(
            codex_bin="codex",
            method="thread/fork",
            params={"threadId": "parent-thread-123", "cwd": "/tmp/workspace", "persistExtendedHistory": True},
        )

        self.assertEqual(result["thread"]["id"], "child-thread-123")
        self.assertEqual(popen_mock.call_args.args[0], ["/Applications/Codex.app/Contents/Resources/codex", "app-server"])
        written_lines = [json.loads(line) for line in process.stdin.getvalue().splitlines() if line.strip()]
        self.assertEqual(written_lines[0]["method"], "initialize")
        self.assertEqual(written_lines[1]["method"], "initialized")
        self.assertEqual(written_lines[2]["method"], "thread/fork")

    @patch("mastermind_bridge.executor.subprocess.Popen")
    @patch("mastermind_bridge.executor._resolve_codex_app_server_bin", return_value="/Applications/Codex.app/Contents/Resources/codex")
    def test_codex_app_server_session_reuses_single_initialized_process_for_multiple_requests(
        self,
        _resolve_mock,
        popen_mock,
    ):
        process = _FakePopen(
            stdout="\n".join(
                [
                    '{"id":1,"result":{"userAgent":"Codex Desktop"}}',
                    '{"id":2,"result":{"thread":{"id":"fresh-thread-123"}}}',
                    '{"id":3,"result":{}}',
                ]
            )
        )
        popen_mock.return_value = process

        with _CodexAppServerSession(codex_bin="codex") as session:
            start_result = session.request("thread/start", {"cwd": "/tmp/workspace"})
            name_result = session.request("thread/name/set", {"threadId": "fresh-thread-123", "name": "Bridge"})

        self.assertEqual(start_result["thread"]["id"], "fresh-thread-123")
        self.assertEqual(name_result, {})
        self.assertEqual(popen_mock.call_count, 1)
        written_lines = [json.loads(line) for line in process.stdin.getvalue().splitlines() if line.strip()]
        self.assertEqual(
            [payload["method"] for payload in written_lines],
            ["initialize", "initialized", "thread/start", "thread/name/set"],
        )

    @patch("mastermind_bridge.executor.subprocess.Popen")
    @patch("mastermind_bridge.executor._resolve_codex_app_server_bin", return_value="/Applications/Codex.app/Contents/Resources/codex")
    def test_codex_app_server_session_request_until_collects_notifications_for_turn_start(
        self,
        _resolve_mock,
        popen_mock,
    ):
        process = _FakePopen(
            stdout="\n".join(
                [
                    '{"id":1,"result":{"userAgent":"Codex Desktop"}}',
                    '{"id":2,"result":{"turn":{"id":"turn-123","status":"inProgress"}}}',
                    '{"method":"hook/started","params":{"eventName":"sessionStart"}}',
                    '{"method":"hook/started","params":{"eventName":"userPromptSubmit"}}',
                    '{"method":"item/agentMessage/delta","params":{"delta":"OK"}}',
                    '{"method":"thread/status/changed","params":{"status":"idle"}}',
                ]
            )
        )
        popen_mock.return_value = process

        with _CodexAppServerSession(codex_bin="codex") as session:
            response, notifications = session.request_until(
                "turn/start",
                {"threadId": "thread-123", "input": [{"type": "text", "text": "Hello"}]},
                until=lambda _response, payloads: any(
                    payload.get("method") == "thread/status/changed"
                    and str(payload.get("params", {}).get("status", "")) == "idle"
                    for payload in payloads
                ),
            )

        self.assertEqual(response["turn"]["id"], "turn-123")
        self.assertEqual(
            [payload["method"] for payload in notifications],
            [
                "hook/started",
                "hook/started",
                "item/agentMessage/delta",
                "thread/status/changed",
            ],
        )

    @patch("mastermind_bridge.executor.subprocess.Popen")
    @patch("mastermind_bridge.executor._resolve_codex_app_server_bin", return_value="/Applications/Codex.app/Contents/Resources/codex")
    def test_compact_codex_thread_after_turn_resumes_and_waits_for_compacted_notification(
        self,
        _resolve_mock,
        popen_mock,
    ):
        process = _FakePopen(
            stdout="\n".join(
                [
                    '{"id":1,"result":{"userAgent":"Codex Desktop"}}',
                    '{"id":2,"result":{"thread":{"id":"thread-123"}}}',
                    '{"id":3,"result":{"turn":{"id":"compact-turn-123"}}}',
                    '{"method":"thread/compacted","params":{"threadId":"thread-123"}}',
                ]
            )
        )
        popen_mock.return_value = process

        result = compact_codex_thread_after_turn(
            codex_bin="codex",
            thread_id="thread-123",
            workdir=Path("/tmp/workspace"),
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["thread_id"], "thread-123")
        self.assertEqual(result["completion"], "thread/compacted")
        written_lines = [json.loads(line) for line in process.stdin.getvalue().splitlines() if line.strip()]
        self.assertEqual(
            [payload["method"] for payload in written_lines],
            ["initialize", "initialized", "thread/resume", "thread/compact/start"],
        )
        self.assertEqual(
            written_lines[2]["params"],
            {"threadId": "thread-123", "excludeTurns": True, "cwd": "/tmp/workspace"},
        )
        self.assertEqual(written_lines[3]["params"], {"threadId": "thread-123"})

    @patch("mastermind_bridge.executor._CodexAppServerSession")
    @patch(
        "mastermind_bridge.executor._resolve_codex_app_server_bin",
        return_value="/Applications/Codex.app/Contents/Resources/codex",
    )
    def test_run_codex_native_turn_with_polling_synthesizes_exec_events_from_thread_read(
        self,
        _resolve_mock,
        session_cls_mock,
    ):
        session_mock = session_cls_mock.return_value.__enter__.return_value
        session_mock.request_until.return_value = (
            {"turn": {"id": "turn-123"}},
            [
                {"method": "hook/started", "params": {"eventName": "sessionStart"}},
                {"method": "item/agentMessage/delta", "params": {"delta": "Native "}},
                {"method": "item/agentMessage/delta", "params": {"delta": "response."}},
                {"method": "turn/completed", "params": {"usage": {"input_tokens": 12, "output_tokens": 5}}},
                {"method": "thread/status/changed", "params": {"status": "idle"}},
            ],
        )
        session_mock.request.return_value = {
            "thread": {
                "turns": [
                    {
                        "id": "turn-123",
                        "items": [
                            {
                                "id": "item-user-1",
                                "type": "userMessage",
                                "content": [{"type": "text", "text": "Prompt text"}],
                            },
                            {
                                "id": "item-cmd-1",
                                "type": "commandExecution",
                                "command": "/bin/zsh -lc 'python3 -m unittest tests.test_executor'",
                                "aggregatedOutput": "OK\n",
                                "exitCode": 0,
                                "status": "completed",
                            },
                            {
                                "id": "item-agent-1",
                                "type": "agentMessage",
                                "text": (
                                    "Native response.\n"
                                    "Files touched: mastermind_bridge/executor.py\n"
                                    "Checks run: python3 -m unittest tests.test_executor"
                                ),
                            },
                        ],
                    }
                ]
            }
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            last_message_path = Path(tmp_dir) / "last_message.md"

            completed, interruption_reason = _run_codex_native_turn_with_polling(
                codex_bin="codex",
                resume_session_id="thread-123",
                prompt_text="Prompt text",
                last_message_path=last_message_path,
                timeout_seconds=None,
                stop_checker=None,
                stop_check_interval_seconds=0.05,
                progress_callback=None,
            )
            persisted_last_message = last_message_path.read_text(encoding="utf-8")

        self.assertEqual(interruption_reason, "")
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(
            completed.args,
            ["/Applications/Codex.app/Contents/Resources/codex", "app-server", "turn/start", "thread-123"],
        )
        parsed = parse_exec_events(completed.stdout)
        self.assertEqual(parsed.observed_codex_thread_id, "thread-123")
        self.assertEqual(
            parsed.final_agent_message,
            "Native response.\nFiles touched: mastermind_bridge/executor.py\nChecks run: python3 -m unittest tests.test_executor",
        )
        self.assertEqual(
            parsed.commands_observed,
            [
                {
                    "id": "item-cmd-1",
                    "command": "/bin/zsh -lc 'python3 -m unittest tests.test_executor'",
                    "aggregated_output": "OK\n",
                    "exit_code": 0,
                    "status": "completed",
                }
            ],
        )
        self.assertEqual(
            persisted_last_message,
            "Native response.\nFiles touched: mastermind_bridge/executor.py\nChecks run: python3 -m unittest tests.test_executor\n",
        )
        self.assertEqual(
            session_mock.request_until.call_args.args,
            (
                "turn/start",
                {"threadId": "thread-123", "input": [{"type": "text", "text": "Prompt text"}]},
            ),
        )
        self.assertEqual(
            session_mock.request.call_args.args,
            ("thread/read", {"threadId": "thread-123", "includeTurns": True}),
        )

    @patch("mastermind_bridge.executor._run_codex_with_polling")
    @patch("mastermind_bridge.executor.subprocess.run")
    @patch("mastermind_bridge.executor._is_git_repo", return_value=False)
    @patch("mastermind_bridge.executor._can_execute_native_turn_start", return_value=True)
    @patch("mastermind_bridge.executor._run_codex_native_turn_with_polling")
    def test_execute_codex_prompt_prefers_native_turn_start_when_available(
        self,
        native_turn_mock,
        _can_native_mock,
        _is_git_repo_mock,
        subprocess_run_mock,
        polling_exec_mock,
    ):
        def _native_side_effect(**kwargs):
            kwargs["last_message_path"].write_text("Native response.\n", encoding="utf-8")
            return (
                subprocess.CompletedProcess(
                    ["/Applications/Codex.app/Contents/Resources/codex", "app-server", "turn/start", "exec-session-123"],
                    0,
                    "\n".join(
                        [
                            '{"type":"thread.started","thread_id":"exec-session-123"}',
                            '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"Native response."}}',
                            '{"type":"turn.completed","usage":{"input_tokens":12,"output_tokens":5}}',
                        ]
                    )
                    + "\n",
                    "",
                ),
                "",
            )

        native_turn_mock.side_effect = _native_side_effect

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            prompt_path = tmp_path / "NEXT_PROMPT.md"
            prompt_path.write_text("# Prompt\nResume the prior session.\n", encoding="utf-8")
            workdir = tmp_path / "workspace"
            workdir.mkdir()
            artifacts_root = tmp_path / "runs"

            report, execution = execute_codex_prompt(
                prompt_path=prompt_path,
                workdir=workdir,
                artifacts_root=artifacts_root,
                thread_id="thread-native",
                codex_bin="codex",
                resume_session_id="exec-session-123",
            )

        native_turn_mock.assert_called_once()
        polling_exec_mock.assert_not_called()
        subprocess_run_mock.assert_not_called()
        self.assertEqual(report.observed_codex_thread_id, "exec-session-123")
        self.assertEqual(report.final_agent_message, "Native response.")
        self.assertEqual(report.command, ["/Applications/Codex.app/Contents/Resources/codex", "app-server", "turn/start", "exec-session-123"])
        self.assertEqual(execution["exit_code"], 0)

    @patch("mastermind_bridge.executor._run_codex_with_polling")
    @patch("mastermind_bridge.executor.subprocess.run")
    @patch("mastermind_bridge.executor._is_git_repo", return_value=False)
    @patch("mastermind_bridge.executor._can_verify_resumed_thread_turn_materialized", return_value=False)
    @patch("mastermind_bridge.executor._can_execute_native_turn_start", return_value=True)
    @patch("mastermind_bridge.executor._run_codex_native_turn_with_polling")
    def test_execute_codex_prompt_skips_native_turn_start_when_execution_settings_are_explicit(
        self,
        native_turn_mock,
        _can_native_mock,
        _can_verify_resumed_mock,
        _is_git_repo_mock,
        subprocess_run_mock,
        polling_exec_mock,
    ):
        polling_exec_mock.return_value = (
            subprocess.CompletedProcess(["codex", "exec"], 0, "", ""),
            "",
        )
        subprocess_run_mock.return_value = subprocess.CompletedProcess(
            args=["codex", "exec"],
            returncode=0,
            stdout="",
            stderr="",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            prompt_path = tmp_path / "NEXT_PROMPT.md"
            prompt_path.write_text("# Prompt\nResume with explicit settings.\n", encoding="utf-8")
            workdir = tmp_path / "workspace"
            workdir.mkdir()
            artifacts_root = tmp_path / "runs"

            with patch(
                "mastermind_bridge.executor._resolve_codex_app_server_bin",
                return_value="/Applications/Codex.app/Contents/Resources/codex",
            ):
                execute_codex_prompt(
                    prompt_path=prompt_path,
                    workdir=workdir,
                    artifacts_root=artifacts_root,
                    thread_id="thread-native",
                    codex_bin="codex",
                    resume_session_id="exec-session-123",
                    model="gpt-5.4",
                    reasoning_effort="high",
                )

        native_turn_mock.assert_not_called()
        self.assertEqual(
            subprocess_run_mock.call_args.kwargs["args"][0],
            "/Applications/Codex.app/Contents/Resources/codex",
        )
        self.assertIn("resume", subprocess_run_mock.call_args.kwargs["args"])
        self.assertIn("gpt-5.4", subprocess_run_mock.call_args.kwargs["args"])
        self.assertIn('model_reasoning_effort="high"', subprocess_run_mock.call_args.kwargs["args"])

    @patch("mastermind_bridge.executor._codex_exec_supports_dangerous_bypass_flag", return_value=False)
    @patch("mastermind_bridge.executor.subprocess.run")
    @patch("mastermind_bridge.executor._is_git_repo", return_value=False)
    @patch("mastermind_bridge.executor._can_verify_resumed_thread_turn_materialized", return_value=False)
    @patch("mastermind_bridge.executor._can_execute_native_turn_start", return_value=False)
    def test_execute_codex_prompt_normalizes_cli_child_path(
        self,
        _can_native_mock,
        _can_verify_resumed_mock,
        _is_git_repo_mock,
        subprocess_run_mock,
        _supports_bypass_mock,
    ):
        subprocess_run_mock.return_value = subprocess.CompletedProcess(
            args=["codex", "exec"],
            returncode=0,
            stdout="",
            stderr="",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            prompt_path = tmp_path / "NEXT_PROMPT.md"
            prompt_path.write_text("# Prompt\nCheck path.\n", encoding="utf-8")
            workdir = tmp_path / "workspace"
            workdir.mkdir()
            artifacts_root = tmp_path / "runs"

            execute_codex_prompt(
                prompt_path=prompt_path,
                workdir=workdir,
                artifacts_root=artifacts_root,
                thread_id="thread-path",
                codex_bin="codex",
                env={"PATH": "/custom/bin", "KEEP_ME": "1"},
                preflight_openai_reachability=False,
            )

        child_env = subprocess_run_mock.call_args.kwargs["env"]
        path_parts = child_env["PATH"].split(os.pathsep)
        self.assertEqual(path_parts[:3], [str(Path.home() / ".local" / "bin"), "/opt/homebrew/bin", "/opt/homebrew/sbin"])
        self.assertIn("/Applications/Codex.app/Contents/Resources", path_parts)
        self.assertIn("/custom/bin", path_parts)
        self.assertEqual(child_env["KEEP_ME"], "1")
        self.assertEqual(child_env["SHELL"], "/bin/zsh")

    @patch.dict(os.environ, {"BRIDGE_ENABLE_CODEX_APP_INTEGRATION": "1"}, clear=False)
    @patch("mastermind_bridge.executor._run_codex_app_server_request")
    @patch("mastermind_bridge.executor._CODEX_APP_SERVER_BIN")
    @patch("mastermind_bridge.executor.sys.platform", "darwin")
    def test_verify_resumed_thread_turn_materialized_accepts_matching_last_turn(
        self,
        bundle_bin_mock,
        request_mock,
    ):
        bundle_bin_mock.exists.return_value = True
        request_mock.return_value = {
            "thread": {
                "turns": [
                    {
                        "id": "turn-123",
                        "items": [
                            {
                                "type": "userMessage",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "Continue the existing thread.\n",
                                    }
                                ],
                            },
                            {
                                "type": "agentMessage",
                                "text": "Completed the requested change.",
                            },
                        ],
                    }
                ]
            }
        }

        _verify_resumed_thread_turn_materialized(
            codex_bin="codex",
            thread_id="thread-123",
            prompt_text="Continue the existing thread.\n",
            final_agent_message="Completed the requested change.",
        )

        request_mock.assert_called_once_with(
            codex_bin="codex",
            method="thread/read",
            params={"threadId": "thread-123", "includeTurns": True},
        )

    @patch.dict(os.environ, {"BRIDGE_ENABLE_CODEX_APP_INTEGRATION": "1"}, clear=False)
    @patch("mastermind_bridge.executor._run_codex_app_server_request")
    @patch("mastermind_bridge.executor._CODEX_APP_SERVER_BIN")
    @patch("mastermind_bridge.executor.sys.platform", "darwin")
    def test_verify_resumed_thread_turn_materialized_rejects_missing_matching_turn(
        self,
        bundle_bin_mock,
        request_mock,
    ):
        bundle_bin_mock.exists.return_value = True
        request_mock.return_value = {
            "thread": {
                "turns": [
                    {
                        "id": "turn-123",
                        "items": [
                            {
                                "type": "userMessage",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "Older prompt.\n",
                                    }
                                ],
                            },
                            {
                                "type": "agentMessage",
                                "text": "Older answer.",
                            },
                        ],
                    }
                ]
            }
        }

        with self.assertRaisesRegex(RuntimeError, "did not materialize"):
            _verify_resumed_thread_turn_materialized(
                codex_bin="codex",
                thread_id="thread-123",
                prompt_text="Continue the existing thread.\n",
                final_agent_message="Completed the requested change.",
            )

    @patch("mastermind_bridge.executor._open_codex_app_thread_once_best_effort")
    @patch("mastermind_bridge.executor._CodexAppServerSession")
    def test_register_codex_app_thread_best_effort_sets_name_without_auto_opening_thread_by_default(
        self,
        session_cls_mock,
        open_mock,
    ):
        session_mock = session_cls_mock.return_value.__enter__.return_value
        session_mock.request.return_value = {}

        register_codex_app_thread_best_effort(
            codex_bin="codex",
            thread_id="child-thread-123",
            workdir=Path("/tmp/personal-assistant-bridge"),
            thread_name_hint="Bridge Visible Thread",
        )

        session_mock.request.assert_called_once_with(
            "thread/name/set",
            {
                "threadId": "child-thread-123",
                "name": "Bridge Visible Thread",
            },
        )
        open_mock.assert_not_called()

    @patch.dict(os.environ, {"BRIDGE_AUTO_OPEN_CODEX_APP_THREADS": "1"}, clear=False)
    @patch("mastermind_bridge.executor._open_codex_app_thread_once_best_effort")
    @patch("mastermind_bridge.executor._CodexAppServerSession")
    def test_register_codex_app_thread_best_effort_auto_opens_thread_when_enabled(self, session_cls_mock, open_mock):
        session_mock = session_cls_mock.return_value.__enter__.return_value
        session_mock.request.return_value = {}

        register_codex_app_thread_best_effort(
            codex_bin="codex",
            thread_id="child-thread-123",
            workdir=Path("/tmp/personal-assistant-bridge"),
            thread_name_hint="Bridge Visible Thread",
        )

        open_mock.assert_called_once_with("child-thread-123")

    @patch("mastermind_bridge.executor._open_codex_app_thread_once_best_effort")
    @patch("mastermind_bridge.executor._CodexAppServerSession")
    @patch("mastermind_bridge.executor.sys.platform", "darwin")
    def test_prepare_native_codex_start_thread_uses_app_server_start_and_name_set_without_auto_opening_by_default(
        self,
        session_cls_mock,
        open_mock,
    ):
        session_mock = session_cls_mock.return_value.__enter__.return_value
        session_mock.request.side_effect = [
            {"thread": {"id": "fresh-thread-123"}},
            {},
        ]

        thread_id = prepare_native_codex_start_thread(
            codex_bin="codex",
            workdir=Path("/tmp/personal-assistant-bridge"),
            thread_name_hint="Bridge Fresh Thread",
        )

        self.assertEqual(thread_id, "fresh-thread-123")
        self.assertEqual(
            session_mock.request.call_args_list,
            [
                call("thread/start", {"cwd": "/tmp/personal-assistant-bridge"}),
                call("thread/name/set", {"threadId": "fresh-thread-123", "name": "Bridge Fresh Thread"}),
            ],
        )
        open_mock.assert_not_called()

    @patch.dict(os.environ, {"BRIDGE_AUTO_OPEN_CODEX_APP_THREADS": "1"}, clear=False)
    @patch("mastermind_bridge.executor._open_codex_app_thread_once_best_effort")
    @patch("mastermind_bridge.executor._CodexAppServerSession")
    @patch("mastermind_bridge.executor.sys.platform", "darwin")
    def test_prepare_native_codex_start_thread_auto_opens_when_enabled(self, session_cls_mock, open_mock):
        session_mock = session_cls_mock.return_value.__enter__.return_value
        session_mock.request.side_effect = [
            {"thread": {"id": "fresh-thread-123"}},
            {},
        ]

        thread_id = prepare_native_codex_start_thread(
            codex_bin="codex",
            workdir=Path("/tmp/personal-assistant-bridge"),
            thread_name_hint="Bridge Fresh Thread",
        )

        self.assertEqual(thread_id, "fresh-thread-123")
        open_mock.assert_called_once_with("fresh-thread-123")

    @patch("mastermind_bridge.executor._open_codex_app_thread_once_best_effort")
    @patch("mastermind_bridge.executor._CodexAppServerSession")
    @patch("mastermind_bridge.executor.sys.platform", "darwin")
    @patch("mastermind_bridge.executor._wait_for_rollout_session_file", return_value=False)
    def test_prepare_native_codex_start_thread_keeps_valid_thread_when_rollout_file_is_not_immediately_visible(
        self,
        wait_mock,
        request_mock,
        open_mock,
    ):
        session_mock = request_mock.return_value.__enter__.return_value
        session_mock.request.side_effect = [
            {
                "thread": {
                    "id": "fresh-thread-123",
                    "path": "/tmp/bridge-missing-rollout.jsonl",
                }
            },
            {},
        ]

        thread_id = prepare_native_codex_start_thread(
            codex_bin="codex",
            workdir=Path("/tmp/personal-assistant-bridge"),
            thread_name_hint="Bridge Fresh Thread",
        )

        self.assertEqual(thread_id, "fresh-thread-123")
        wait_mock.assert_called_once()
        self.assertEqual(
            session_mock.request.call_args_list,
            [
                call("thread/start", {"cwd": "/tmp/personal-assistant-bridge"}),
                call("thread/name/set", {"threadId": "fresh-thread-123", "name": "Bridge Fresh Thread"}),
            ],
        )
        open_mock.assert_not_called()

    @patch("mastermind_bridge.executor._open_codex_app_thread_once_best_effort")
    @patch("mastermind_bridge.executor._CodexAppServerSession")
    @patch("mastermind_bridge.executor.sys.platform", "darwin")
    def test_prepare_native_codex_fork_thread_uses_app_server_fork_and_name_set_without_auto_opening_by_default(
        self,
        session_cls_mock,
        open_mock,
    ):
        session_mock = session_cls_mock.return_value.__enter__.return_value
        session_mock.request.side_effect = [
            {"thread": {"id": "child-thread-123"}},
            {},
        ]

        thread_id = prepare_native_codex_fork_thread(
            codex_bin="codex",
            source_thread_id="parent-thread-123",
            workdir=Path("/tmp/personal-assistant-bridge"),
            thread_name_hint="Bridge Native Thread",
        )

        self.assertEqual(thread_id, "child-thread-123")
        self.assertEqual(
            session_mock.request.call_args_list,
            [
                call(
                    "thread/fork",
                    {
                        "threadId": "parent-thread-123",
                        "cwd": "/tmp/personal-assistant-bridge",
                        "persistExtendedHistory": True,
                    },
                ),
                call("thread/name/set", {"threadId": "child-thread-123", "name": "Bridge Native Thread"}),
            ],
        )
        open_mock.assert_not_called()

    @patch.dict(os.environ, {"BRIDGE_AUTO_OPEN_CODEX_APP_THREADS": "1"}, clear=False)
    @patch("mastermind_bridge.executor._open_codex_app_thread_once_best_effort")
    @patch("mastermind_bridge.executor._CodexAppServerSession")
    @patch("mastermind_bridge.executor.sys.platform", "darwin")
    def test_prepare_native_codex_fork_thread_auto_opens_when_enabled(self, session_cls_mock, open_mock):
        session_mock = session_cls_mock.return_value.__enter__.return_value
        session_mock.request.side_effect = [
            {"thread": {"id": "child-thread-123"}},
            {},
        ]

        thread_id = prepare_native_codex_fork_thread(
            codex_bin="codex",
            source_thread_id="parent-thread-123",
            workdir=Path("/tmp/personal-assistant-bridge"),
            thread_name_hint="Bridge Native Thread",
        )

        self.assertEqual(thread_id, "child-thread-123")
        open_mock.assert_called_once_with("child-thread-123")

    def test_snapshot_workspace_files_ignores_build_products_and_ds_store(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            workdir = Path(tmp_dir)
            (workdir / "README.md").write_text("hello\n", encoding="utf-8")
            (workdir / ".DS_Store").write_text("ignored\n", encoding="utf-8")
            build_dir = workdir / ".build"
            build_dir.mkdir()
            (build_dir / "build.db").write_text("ignored\n", encoding="utf-8")

            snapshot = _snapshot_workspace_files(workdir)

            self.assertEqual(snapshot, {"README.md": snapshot["README.md"]})

    @patch.dict(os.environ, {"BRIDGE_ENABLE_CODEX_APP_INTEGRATION": "0"}, clear=False)
    @patch("mastermind_bridge.executor.register_codex_app_thread_best_effort")
    @patch("mastermind_bridge.executor._stop_codex_thread_open_watcher")
    @patch("mastermind_bridge.executor._start_codex_thread_open_watcher", return_value=None)
    @patch("mastermind_bridge.executor.subprocess.run")
    def test_execute_codex_prompt_skips_app_registration_by_default(
        self,
        run_mock,
        _start_watcher_mock,
        _stop_watcher_mock,
        register_mock,
    ):
        run_mock.return_value = subprocess.CompletedProcess(
            args=["codex", "exec"],
            returncode=0,
            stdout='{"type":"thread.started","thread_id":"new-thread-123"}\n',
            stderr="",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            prompt_path = tmp_path / "prompt.md"
            prompt_path.write_text("hello\n", encoding="utf-8")
            workdir = tmp_path / "workspace"
            workdir.mkdir()
            artifacts_root = tmp_path / "artifacts"

            execute_codex_prompt(
                prompt_path=prompt_path,
                workdir=workdir,
                artifacts_root=artifacts_root,
                thread_id="session-123",
                codex_bin="codex",
                observed_thread_name_hint="Bridge Visible Thread",
            )

        register_mock.assert_not_called()

    @patch.dict(os.environ, {"BRIDGE_ENABLE_CODEX_APP_INTEGRATION": "1"}, clear=False)
    @patch("mastermind_bridge.executor.register_codex_app_thread_best_effort")
    @patch("mastermind_bridge.executor._stop_codex_thread_open_watcher")
    @patch("mastermind_bridge.executor._start_codex_thread_open_watcher", return_value=None)
    @patch("mastermind_bridge.executor.subprocess.run")
    def test_execute_codex_prompt_registers_observed_new_thread_when_app_integration_is_enabled(
        self,
        run_mock,
        _start_watcher_mock,
        _stop_watcher_mock,
        register_mock,
    ):
        run_mock.return_value = subprocess.CompletedProcess(
            args=["codex", "exec"],
            returncode=0,
            stdout='{"type":"thread.started","thread_id":"new-thread-123"}\n',
            stderr="",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            prompt_path = tmp_path / "prompt.md"
            prompt_path.write_text("hello\n", encoding="utf-8")
            workdir = tmp_path / "workspace"
            workdir.mkdir()
            artifacts_root = tmp_path / "artifacts"

            execute_codex_prompt(
                prompt_path=prompt_path,
                workdir=workdir,
                artifacts_root=artifacts_root,
                thread_id="session-123",
                codex_bin="codex",
                observed_thread_name_hint="Bridge Visible Thread",
            )

        register_mock.assert_called_once_with(
            codex_bin="codex",
            thread_id="new-thread-123",
            workdir=workdir,
            thread_name_hint="Bridge Visible Thread",
        )
        _start_watcher_mock.assert_called_once_with(workdir, enabled=False)

    @patch("mastermind_bridge.executor.compact_codex_thread_after_turn")
    @patch("mastermind_bridge.executor._codex_exec_supports_dangerous_bypass_flag", return_value=False)
    @patch("mastermind_bridge.executor._can_execute_native_turn_start", return_value=False)
    @patch("mastermind_bridge.executor._can_verify_resumed_thread_turn_materialized", return_value=False)
    @patch("mastermind_bridge.executor._is_git_repo", return_value=False)
    @patch("mastermind_bridge.executor._stop_codex_thread_open_watcher")
    @patch("mastermind_bridge.executor._start_codex_thread_open_watcher", return_value=None)
    @patch("mastermind_bridge.executor.subprocess.run")
    def test_execute_codex_prompt_compacts_observed_thread_when_opted_in(
        self,
        run_mock,
        _start_watcher_mock,
        _stop_watcher_mock,
        _is_git_repo_mock,
        _can_verify_mock,
        _can_native_mock,
        _supports_bypass_mock,
        compact_mock,
    ):
        compact_mock.return_value = {
            "status": "completed",
            "thread_id": "new-thread-123",
            "method": "thread/compact/start",
            "completion": "thread/compacted",
        }
        run_mock.return_value = subprocess.CompletedProcess(
            args=["codex", "exec"],
            returncode=0,
            stdout="\n".join(
                [
                    '{"type":"thread.started","thread_id":"new-thread-123"}',
                    '{"type":"item.completed","item":{"id":"item_1","type":"agent_message","text":"Done."}}',
                    '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":5}}',
                ]
            )
            + "\n",
            stderr="",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            prompt_path = tmp_path / "prompt.md"
            prompt_path.write_text("hello\n", encoding="utf-8")
            workdir = tmp_path / "workspace"
            workdir.mkdir()
            artifacts_root = tmp_path / "artifacts"

            report, _execution = execute_codex_prompt(
                prompt_path=prompt_path,
                workdir=workdir,
                artifacts_root=artifacts_root,
                thread_id="session-123",
                codex_bin="codex",
                compact_after_success=True,
            )

        compact_mock.assert_called_once_with(
            codex_bin="codex",
            thread_id="new-thread-123",
            workdir=workdir,
            timeout_seconds=300.0,
        )
        self.assertEqual(report.codex_compaction["completion"], "thread/compacted")

    @patch("mastermind_bridge.executor.compact_codex_thread_after_turn", side_effect=RuntimeError("compact failed"))
    @patch("mastermind_bridge.executor._codex_exec_supports_dangerous_bypass_flag", return_value=False)
    @patch("mastermind_bridge.executor._can_execute_native_turn_start", return_value=False)
    @patch("mastermind_bridge.executor._can_verify_resumed_thread_turn_materialized", return_value=False)
    @patch("mastermind_bridge.executor._is_git_repo", return_value=False)
    @patch("mastermind_bridge.executor._stop_codex_thread_open_watcher")
    @patch("mastermind_bridge.executor._start_codex_thread_open_watcher", return_value=None)
    @patch("mastermind_bridge.executor.subprocess.run")
    def test_execute_codex_prompt_fails_closed_when_opt_in_compaction_fails(
        self,
        run_mock,
        _start_watcher_mock,
        _stop_watcher_mock,
        _is_git_repo_mock,
        _can_verify_mock,
        _can_native_mock,
        _supports_bypass_mock,
        _compact_mock,
    ):
        run_mock.return_value = subprocess.CompletedProcess(
            args=["codex", "exec"],
            returncode=0,
            stdout='{"type":"thread.started","thread_id":"new-thread-123"}\n',
            stderr="",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            prompt_path = tmp_path / "prompt.md"
            prompt_path.write_text("hello\n", encoding="utf-8")
            workdir = tmp_path / "workspace"
            workdir.mkdir()
            artifacts_root = tmp_path / "artifacts"

            with self.assertRaisesRegex(RuntimeError, "Codex post-turn compaction failed"):
                execute_codex_prompt(
                    prompt_path=prompt_path,
                    workdir=workdir,
                    artifacts_root=artifacts_root,
                    thread_id="session-123",
                    codex_bin="codex",
                    compact_after_success=True,
                )
            reports = sorted(artifacts_root.glob("*/run_report.json"))
            self.assertEqual(len(reports), 1)
            persisted_report = json.loads(reports[0].read_text(encoding="utf-8"))
            self.assertEqual(persisted_report["codex_compaction"]["status"], "failed")
            self.assertIn("compact failed", persisted_report["codex_compaction"]["error"])

    @patch("mastermind_bridge.executor.compact_codex_thread_after_turn")
    @patch("mastermind_bridge.executor._codex_exec_supports_dangerous_bypass_flag", return_value=False)
    @patch("mastermind_bridge.executor._can_execute_native_turn_start", return_value=False)
    @patch("mastermind_bridge.executor._can_verify_resumed_thread_turn_materialized", return_value=False)
    @patch("mastermind_bridge.executor._is_git_repo", return_value=False)
    @patch("mastermind_bridge.executor._stop_codex_thread_open_watcher")
    @patch("mastermind_bridge.executor._start_codex_thread_open_watcher", return_value=None)
    @patch("mastermind_bridge.executor.subprocess.run")
    def test_execute_codex_prompt_persists_report_before_post_run_compaction_finishes(
        self,
        run_mock,
        _start_watcher_mock,
        _stop_watcher_mock,
        _is_git_repo_mock,
        _can_verify_mock,
        _can_native_mock,
        _supports_bypass_mock,
        compact_mock,
    ):
        run_mock.return_value = subprocess.CompletedProcess(
            args=["codex", "exec"],
            returncode=0,
            stdout='{"type":"thread.started","thread_id":"new-thread-123"}\n',
            stderr="",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            prompt_path = tmp_path / "prompt.md"
            prompt_path.write_text("hello\n", encoding="utf-8")
            workdir = tmp_path / "workspace"
            workdir.mkdir()
            artifacts_root = tmp_path / "artifacts"

            def assert_running_report_exists(**_kwargs):
                reports = sorted(artifacts_root.glob("*/run_report.json"))
                self.assertEqual(len(reports), 1)
                persisted_report = json.loads(reports[0].read_text(encoding="utf-8"))
                self.assertEqual(persisted_report["codex_compaction"]["status"], "running")
                self.assertEqual(persisted_report["codex_compaction"]["thread_id"], "new-thread-123")
                return {
                    "status": "completed",
                    "thread_id": "new-thread-123",
                    "method": "thread/compact/start",
                    "completion": "thread/compacted",
                }

            compact_mock.side_effect = assert_running_report_exists

            report, _execution = execute_codex_prompt(
                prompt_path=prompt_path,
                workdir=workdir,
                artifacts_root=artifacts_root,
                thread_id="session-123",
                codex_bin="codex",
                compact_after_success=True,
            )

            reports = sorted(artifacts_root.glob("*/run_report.json"))
            persisted_report = json.loads(reports[0].read_text(encoding="utf-8"))
            self.assertEqual(report.codex_compaction["status"], "completed")
            self.assertEqual(persisted_report["codex_compaction"]["status"], "completed")

    @patch.dict(
        os.environ,
        {
            "BRIDGE_ENABLE_CODEX_APP_INTEGRATION": "1",
            "BRIDGE_AUTO_OPEN_CODEX_APP_THREADS": "1",
        },
        clear=False,
    )
    @patch("mastermind_bridge.executor.register_codex_app_thread_best_effort")
    @patch("mastermind_bridge.executor._stop_codex_thread_open_watcher")
    @patch("mastermind_bridge.executor._start_codex_thread_open_watcher", return_value=None)
    @patch("mastermind_bridge.executor.subprocess.run")
    def test_execute_codex_prompt_starts_open_watcher_only_when_auto_open_is_enabled(
        self,
        run_mock,
        _start_watcher_mock,
        _stop_watcher_mock,
        register_mock,
    ):
        run_mock.return_value = subprocess.CompletedProcess(
            args=["codex", "exec"],
            returncode=0,
            stdout='{"type":"thread.started","thread_id":"new-thread-123"}\n',
            stderr="",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            prompt_path = tmp_path / "prompt.md"
            prompt_path.write_text("hello\n", encoding="utf-8")
            workdir = tmp_path / "workspace"
            workdir.mkdir()
            artifacts_root = tmp_path / "artifacts"

            execute_codex_prompt(
                prompt_path=prompt_path,
                workdir=workdir,
                artifacts_root=artifacts_root,
                thread_id="session-123",
                codex_bin="codex",
                observed_thread_name_hint="Bridge Visible Thread",
            )

        register_mock.assert_called_once()
        _start_watcher_mock.assert_called_once_with(workdir, enabled=True)

    @patch("mastermind_bridge.executor._stop_codex_thread_open_watcher")
    @patch("mastermind_bridge.executor._start_codex_thread_open_watcher", return_value=None)
    @patch("mastermind_bridge.executor._is_git_repo", return_value=False)
    @patch("mastermind_bridge.executor.subprocess.run")
    def test_execute_codex_prompt_uses_safe_sandbox_flags_by_default(
        self,
        run_mock,
        _is_git_repo_mock,
        _start_watcher_mock,
        _stop_watcher_mock,
    ):
        from mastermind_bridge.executor import _codex_exec_help_text

        _codex_exec_help_text.cache_clear()
        run_mock.side_effect = [
            subprocess.CompletedProcess(
                args=["codex", "exec", "--help"],
                returncode=0,
                stdout="  -a, --approval-policy <APPROVAL_POLICY>\n  -s, --sandbox <SANDBOX_MODE>\n",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["codex", "exec"],
                returncode=0,
                stdout="",
                stderr="",
            ),
        ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            prompt_path = tmp_path / "prompt.md"
            prompt_path.write_text("hello\n", encoding="utf-8")
            workdir = tmp_path / "workspace"
            workdir.mkdir()
            artifacts_root = tmp_path / "artifacts"

            with patch.dict(
                os.environ,
                {"BRIDGE_PROFILE": "core-safe", "BRIDGE_ALLOW_DANGEROUS_CODEX_BYPASS": ""},
                clear=False,
            ):
                execute_codex_prompt(
                    prompt_path=prompt_path,
                    workdir=workdir,
                    artifacts_root=artifacts_root,
                    thread_id="session-123",
                    codex_bin="codex",
                )

        command = run_mock.call_args.kwargs["args"]
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertIn("-a", command)
        self.assertIn("on-request", command)
        self.assertIn("-s", command)
        self.assertIn("workspace-write", command)

    @patch("mastermind_bridge.executor._stop_codex_thread_open_watcher")
    @patch("mastermind_bridge.executor._start_codex_thread_open_watcher", return_value=None)
    @patch("mastermind_bridge.executor._is_git_repo", return_value=False)
    @patch("mastermind_bridge.executor.subprocess.run")
    def test_execute_codex_prompt_omits_approval_flag_when_codex_exec_does_not_support_it(
        self,
        run_mock,
        _is_git_repo_mock,
        _start_watcher_mock,
        _stop_watcher_mock,
    ):
        from mastermind_bridge.executor import _codex_exec_help_text

        _codex_exec_help_text.cache_clear()
        run_mock.side_effect = [
            subprocess.CompletedProcess(
                args=["codex", "exec", "--help"],
                returncode=0,
                stdout="  -s, --sandbox <SANDBOX_MODE>\n",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["codex", "exec"],
                returncode=0,
                stdout="",
                stderr="",
            ),
        ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            prompt_path = tmp_path / "prompt.md"
            prompt_path.write_text("hello\n", encoding="utf-8")
            workdir = tmp_path / "workspace"
            workdir.mkdir()
            artifacts_root = tmp_path / "artifacts"

            with patch.dict(
                os.environ,
                {"BRIDGE_PROFILE": "core-safe", "BRIDGE_ALLOW_DANGEROUS_CODEX_BYPASS": ""},
                clear=False,
            ):
                execute_codex_prompt(
                    prompt_path=prompt_path,
                    workdir=workdir,
                    artifacts_root=artifacts_root,
                    thread_id="session-123",
                    codex_bin="codex",
                )

        command = run_mock.call_args.kwargs["args"]
        self.assertNotIn("-a", command)
        self.assertNotIn("on-request", command)
        self.assertIn("-s", command)
        self.assertIn("workspace-write", command)

    @patch("mastermind_bridge.executor._stop_codex_thread_open_watcher")
    @patch("mastermind_bridge.executor._start_codex_thread_open_watcher", return_value=None)
    @patch("mastermind_bridge.executor._is_git_repo", return_value=False)
    @patch("mastermind_bridge.executor.subprocess.run")
    def test_execute_codex_prompt_honors_requested_sandbox_without_bypass_opt_in(
        self,
        run_mock,
        _is_git_repo_mock,
        _start_watcher_mock,
        _stop_watcher_mock,
    ):
        from mastermind_bridge.executor import _codex_exec_help_text

        _codex_exec_help_text.cache_clear()
        run_mock.side_effect = [
            subprocess.CompletedProcess(
                args=["codex", "exec", "--help"],
                returncode=0,
                stdout="  -a, --approval-policy <APPROVAL_POLICY>\n  -s, --sandbox <SANDBOX_MODE>\n",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["codex", "exec"],
                returncode=0,
                stdout="",
                stderr="",
            ),
        ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            prompt_path = tmp_path / "prompt.md"
            prompt_path.write_text("hello\n", encoding="utf-8")
            workdir = tmp_path / "workspace"
            workdir.mkdir()
            artifacts_root = tmp_path / "artifacts"

            with patch.dict(
                os.environ,
                {"BRIDGE_PROFILE": "trusted-local", "BRIDGE_ALLOW_DANGEROUS_CODEX_BYPASS": ""},
                clear=False,
            ):
                execute_codex_prompt(
                    prompt_path=prompt_path,
                    workdir=workdir,
                    artifacts_root=artifacts_root,
                    thread_id="session-123",
                    codex_bin="codex",
                    sandbox="workspace-write",
                )

        command = run_mock.call_args.kwargs["args"]
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertIn("-a", command)
        self.assertIn("on-request", command)
        self.assertIn("-s", command)
        self.assertIn("workspace-write", command)

    @patch("mastermind_bridge.executor._stop_codex_thread_open_watcher")
    @patch("mastermind_bridge.executor._start_codex_thread_open_watcher", return_value=None)
    @patch("mastermind_bridge.executor._is_git_repo", return_value=False)
    @patch("mastermind_bridge.executor.subprocess.run")
    def test_execute_codex_prompt_uses_bypass_only_with_profile_and_explicit_opt_in(
        self,
        run_mock,
        _is_git_repo_mock,
        _start_watcher_mock,
        _stop_watcher_mock,
    ):
        from mastermind_bridge.executor import _codex_exec_help_text

        _codex_exec_help_text.cache_clear()
        run_mock.side_effect = [
            subprocess.CompletedProcess(
                args=["codex", "exec", "--help"],
                returncode=0,
                stdout="  --dangerously-bypass-approvals-and-sandbox\n",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["codex", "exec"],
                returncode=0,
                stdout="",
                stderr="",
            ),
        ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            prompt_path = tmp_path / "prompt.md"
            prompt_path.write_text("hello\n", encoding="utf-8")
            workdir = tmp_path / "workspace"
            workdir.mkdir()
            artifacts_root = tmp_path / "artifacts"

            with patch.dict(
                os.environ,
                {
                    "BRIDGE_PROFILE": "trusted-local",
                    "BRIDGE_ALLOW_DANGEROUS_CODEX_BYPASS": "1",
                },
                clear=False,
            ):
                execute_codex_prompt(
                    prompt_path=prompt_path,
                    workdir=workdir,
                    artifacts_root=artifacts_root,
                    thread_id="session-123",
                    codex_bin="codex",
                    sandbox="danger-full-access",
                )

        command = run_mock.call_args.kwargs["args"]
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertNotIn("-a", command)
        self.assertNotIn("-s", command)

    @patch("mastermind_bridge.executor._stop_codex_thread_open_watcher")
    @patch("mastermind_bridge.executor._start_codex_thread_open_watcher", return_value=None)
    @patch("mastermind_bridge.executor.subprocess.run")
    def test_execute_codex_prompt_sets_reasoning_effort_override(
        self,
        run_mock,
        _start_watcher_mock,
        _stop_watcher_mock,
    ):
        run_mock.return_value = subprocess.CompletedProcess(
            args=["codex", "exec"],
            returncode=0,
            stdout="",
            stderr="",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            prompt_path = tmp_path / "prompt.md"
            prompt_path.write_text("hello\n", encoding="utf-8")
            workdir = tmp_path / "workspace"
            workdir.mkdir()
            artifacts_root = tmp_path / "artifacts"

            execute_codex_prompt(
                prompt_path=prompt_path,
                workdir=workdir,
                artifacts_root=artifacts_root,
                thread_id="session-123",
                codex_bin="codex",
                model="gpt-5.4-mini",
                reasoning_effort="medium",
            )

        command = run_mock.call_args.kwargs["args"]
        self.assertIn("-m", command)
        self.assertIn("gpt-5.4-mini", command)
        self.assertIn("-c", command)
        self.assertIn('model_reasoning_effort="medium"', command)

    @patch.dict(
        os.environ,
        {"BRIDGE_ENABLE_CODEX_APP_INTEGRATION": "0", "BRIDGE_CODEX_EXEC_IGNORE_USER_CONFIG": "1"},
        clear=False,
    )
    @patch("mastermind_bridge.executor._stop_codex_thread_open_watcher")
    @patch("mastermind_bridge.executor._start_codex_thread_open_watcher", return_value=None)
    @patch("mastermind_bridge.executor.subprocess.run")
    def test_execute_codex_prompt_can_ignore_user_config_for_plugin_auth_recovery(
        self,
        run_mock,
        _start_watcher_mock,
        _stop_watcher_mock,
    ):
        run_mock.return_value = subprocess.CompletedProcess(
            args=["codex", "exec"],
            returncode=0,
            stdout="",
            stderr="",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            prompt_path = tmp_path / "prompt.md"
            prompt_path.write_text("hello\n", encoding="utf-8")
            workdir = tmp_path / "workspace"
            workdir.mkdir()
            artifacts_root = tmp_path / "artifacts"

            execute_codex_prompt(
                prompt_path=prompt_path,
                workdir=workdir,
                artifacts_root=artifacts_root,
                thread_id="session-123",
                codex_bin="codex",
                model="gpt-5.3-codex-spark",
                reasoning_effort="xhigh",
            )

        command = run_mock.call_args.kwargs["args"]
        self.assertIn("--ignore-user-config", command)
        self.assertIn("-m", command)
        self.assertIn("gpt-5.3-codex-spark", command)

    def test_snapshot_workspace_files_respects_time_budget(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            workdir = Path(tmp_dir)
            (workdir / "first.txt").write_text("one\n", encoding="utf-8")
            (workdir / "second.txt").write_text("two\n", encoding="utf-8")

            monotonic_values = iter([0.0, 0.0, 10.0])

            with patch("mastermind_bridge.executor_reporting.time.monotonic", side_effect=lambda: next(monotonic_values)):
                snapshot = _snapshot_workspace_files(workdir, max_duration_seconds=1.0)

        self.assertEqual(list(snapshot.keys()), ["first.txt"])

    @patch("mastermind_bridge.executor.socket.getaddrinfo")
    @patch("mastermind_bridge.executor._stop_codex_thread_open_watcher")
    @patch("mastermind_bridge.executor._start_codex_thread_open_watcher", return_value=None)
    @patch("mastermind_bridge.executor.subprocess.run")
    def test_execute_codex_prompt_short_circuits_when_openai_dns_is_unavailable(
        self,
        run_mock,
        _start_watcher_mock,
        _stop_watcher_mock,
        getaddrinfo_mock,
    ):
        getaddrinfo_mock.side_effect = socket.gaierror("nodename nor servname provided, or not known")

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            prompt_path = tmp_path / "prompt.md"
            prompt_path.write_text("hello\n", encoding="utf-8")
            workdir = tmp_path / "workspace"
            workdir.mkdir()
            artifacts_root = tmp_path / "artifacts"

            report, execution = execute_codex_prompt(
                prompt_path=prompt_path,
                workdir=workdir,
                artifacts_root=artifacts_root,
                thread_id="session-123",
                codex_bin="codex",
                preflight_openai_reachability=True,
            )

        self.assertEqual(run_mock.call_count, 1)
        self.assertEqual(run_mock.call_args.args[0][:3], ["git", "-C", str(workdir)])
        self.assertEqual(report.exit_code, 1)
        self.assertEqual(
            report.summary,
            "Codex could not reach the OpenAI API because network or DNS access was unavailable from this process.",
        )
        self.assertIn("failed to lookup address information", report.blockers[1])
        self.assertEqual(execution["exit_code"], 1)


if __name__ == "__main__":
    unittest.main()
