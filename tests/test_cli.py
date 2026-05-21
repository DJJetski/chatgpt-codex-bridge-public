import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
import unittest.mock

import mastermind_bridge.cli as cli_module


class CliTests(unittest.TestCase):
    _NOISY_STDERR = "\n".join(
        [
            "2026-04-15T09:18:48.413415Z ERROR codex_core_skills::loader: failed to stat skills entry /tmp/example-home/.codex/skills/bun-runtime (symlink): No such file or directory (os error 2)",
            "2026-04-15T09:38:19.857130Z  WARN codex_core::plugins::manifest: ignoring interface.defaultPrompt: prompt must be at most 128 characters path=/tmp/example-home/.codex/.tmp/plugins/plugins/life-science-research/.codex-plugin/plugin.json",
        ]
    )

    def _write_fake_codex(self, root: Path) -> Path:
        fake_codex = root / "fake_codex.py"
        fake_codex.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import json",
                    "import os",
                    "import sys",
                    "import time",
                    "from pathlib import Path",
                    "",
                    "args = sys.argv[1:]",
                    "last_message = None",
                    "workdir = None",
                    "for index, value in enumerate(args):",
                    "    if value in ('-o', '--output-last-message'):",
                    "        last_message = Path(args[index + 1])",
                    "    if value == '-C':",
                    "        workdir = Path(args[index + 1])",
                    "",
                    "prompt = sys.stdin.read()",
                    "scenario = os.environ.get('FAKE_CODEX_SCENARIO', 'default')",
                    "sleep_seconds = float(os.environ.get('FAKE_CODEX_SLEEP', '0'))",
                    "stderr_text = os.environ.get('FAKE_CODEX_STDERR', '')",
                    "stderr_before_sleep = os.environ.get('FAKE_CODEX_STDERR_BEFORE_SLEEP') == '1'",
                    "exit_code = int(os.environ.get('FAKE_CODEX_EXIT', '0'))",
                    "",
                    "def emit_stderr():",
                    "    if stderr_text:",
                    "        sys.stderr.write(stderr_text)",
                    "        sys.stderr.flush()",
                    "",
                    "if scenario == 'partial_timeout':",
                    "    if last_message is not None:",
                    "        last_message.write_text(os.environ.get('FAKE_CODEX_LAST_MESSAGE_TEXT', ''), encoding='utf-8')",
                    "    print(json.dumps({'event': 'started'}), flush=True)",
                    "    print(json.dumps({'type': 'thread.started', 'thread_id': 'exec-thread-timeout'}), flush=True)",
                    "    print(json.dumps({'type': 'turn.started'}), flush=True)",
                    "    partial_agent_message = os.environ.get('FAKE_CODEX_PARTIAL_AGENT_MESSAGE', '')",
                    "    sys.stdout.write('{\"type\":\"item.completed\",\"item\":{\"id\":\"item_0\",\"type\":\"agent_message\",\"text\":' + json.dumps(partial_agent_message)[:-1])",
                    "    sys.stdout.flush()",
                    "    if stderr_before_sleep:",
                    "        emit_stderr()",
                    "    if sleep_seconds > 0:",
                    "        time.sleep(sleep_seconds)",
                    "    if not stderr_before_sleep:",
                    "        emit_stderr()",
                    "    sys.exit(exit_code)",
                    "",
                    "if scenario == 'inferred_files_touched':",
                    "    if workdir is None:",
                    "        raise SystemExit('missing workdir')",
                    "    (workdir / 'README.md').write_text('updated\\n', encoding='utf-8')",
                    "    (workdir / 'tests').mkdir(exist_ok=True)",
                    "    (workdir / 'tests' / 'test_executor.py').write_text('print(\\'updated\\')\\n', encoding='utf-8')",
                    "    (workdir / '__pycache__').mkdir(exist_ok=True)",
                    "    (workdir / '__pycache__' / 'executor.cpython-314.pyc').write_bytes(b'noise')",
                    "    if last_message is not None:",
                    "        last_message.write_text('Completed Codex wrapper execution without explicit file markers.\\n', encoding='utf-8')",
                    "    print(json.dumps({'event': 'started'}), flush=True)",
                    "    print(json.dumps({'type': 'thread.started', 'thread_id': 'exec-thread-files'}), flush=True)",
                    "    print(json.dumps({'type': 'turn.started'}), flush=True)",
                    "    print(json.dumps({'type': 'item.completed', 'item': {'id': 'item_0', 'type': 'agent_message', 'text': 'Completed Codex wrapper execution without explicit file markers.'}}), flush=True)",
                    "    print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 123, 'cached_input_tokens': 45, 'output_tokens': 67}}), flush=True)",
                    "    if stderr_before_sleep:",
                    "        emit_stderr()",
                    "    if sleep_seconds > 0:",
                    "        time.sleep(sleep_seconds)",
                    "    if not stderr_before_sleep:",
                    "        emit_stderr()",
                    "    sys.exit(exit_code)",
                    "",
                    "if scenario == 'inferred_checks':",
                    "    if last_message is not None:",
                    "        last_message.write_text('Completed Codex wrapper execution without explicit checks.\\n', encoding='utf-8')",
                    "    print(json.dumps({'event': 'started'}), flush=True)",
                    "    print(json.dumps({'type': 'thread.started', 'thread_id': 'exec-thread-inferred'}), flush=True)",
                    "    print(json.dumps({'type': 'turn.started'}), flush=True)",
                    "    print(json.dumps({'type': 'item.completed', 'item': {'id': 'item_cmd_1', 'type': 'command_execution', 'command': '/bin/zsh -lc \"sed -n \\'1,240p\\' tests/test_executor.py\"', 'aggregated_output': '', 'exit_code': 0, 'status': 'completed'}}), flush=True)",
                    "    print(json.dumps({'type': 'item.completed', 'item': {'id': 'item_cmd_2', 'type': 'command_execution', 'command': \"/bin/zsh -lc 'python3 -m unittest tests.test_executor tests.test_cli'\", 'aggregated_output': 'F\\n', 'exit_code': 1, 'status': 'failed'}}), flush=True)",
                    "    print(json.dumps({'type': 'item.completed', 'item': {'id': 'item_cmd_3', 'type': 'command_execution', 'command': \"/bin/zsh -lc 'python3 -m unittest tests.test_executor tests.test_cli'\", 'aggregated_output': '.\\n', 'exit_code': 0, 'status': 'completed'}}), flush=True)",
                    "    print(json.dumps({'type': 'item.completed', 'item': {'id': 'item_0', 'type': 'agent_message', 'text': 'Completed Codex wrapper execution without explicit checks.'}}), flush=True)",
                    "    print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 123, 'cached_input_tokens': 45, 'output_tokens': 67}}), flush=True)",
                    "    if stderr_before_sleep:",
                    "        emit_stderr()",
                    "    if sleep_seconds > 0:",
                    "        time.sleep(sleep_seconds)",
                    "    if not stderr_before_sleep:",
                    "        emit_stderr()",
                    "    sys.exit(exit_code)",
                    "",
                    "if last_message is not None:",
                    "    message = (",
                    "        'Completed Codex wrapper execution.\\n'",
                    "        + f'Prompt characters: {len(prompt)}\\n'",
                    "        + 'Files touched: README.md, mastermind_bridge/cli.py\\n'",
                    "        + 'Checks run: python3 -m unittest discover -s tests\\n'",
                    "    )",
                    "    last_message.write_text(message, encoding='utf-8')",
                    "",
                    "print(json.dumps({'event': 'started'}), flush=True)",
                    "print(json.dumps({'type': 'thread.started', 'thread_id': 'exec-thread-xyz'}), flush=True)",
                    "print(json.dumps({'type': 'turn.started'}), flush=True)",
                    "print(json.dumps({'type': 'item.started', 'item': {'id': 'item_cmd_1', 'type': 'command_execution', 'command': '/bin/zsh -c pwd', 'aggregated_output': '', 'exit_code': None, 'status': 'in_progress'}}), flush=True)",
                    "print(json.dumps({'type': 'item.completed', 'item': {'id': 'item_cmd_1', 'type': 'command_execution', 'command': '/bin/zsh -c pwd', 'aggregated_output': '/tmp/workspace\\n', 'exit_code': 0, 'status': 'completed'}}), flush=True)",
                    "print(json.dumps({'type': 'item.completed', 'item': {'id': 'item_0', 'type': 'agent_message', 'text': 'Completed Codex wrapper execution.'}}), flush=True)",
                    "print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 123, 'cached_input_tokens': 45, 'output_tokens': 67}}), flush=True)",
                    "if stderr_before_sleep:",
                    "    emit_stderr()",
                    "if sleep_seconds > 0:",
                    "    time.sleep(sleep_seconds)",
                    "if not stderr_before_sleep:",
                    "    emit_stderr()",
                    "sys.exit(exit_code)",
                ]
            )
            + "\n"
        )
        fake_codex.chmod(0o755)
        return fake_codex

    def test_run_loop_defaults_to_non_headless_browser_transport(self):
        parser = cli_module.build_parser()

        args = parser.parse_args(["run-loop", "--session-id", "session-1"])

        self.assertFalse(args.headless)

    def test_decide_command_updates_registry(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            registry_path = tmp_path / "THREAD_REGISTRY.json"
            context_path = tmp_path / "decision_context.json"
            registry_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "project": {"name": "bridge"},
                        "threads": [],
                        "decision_log": [],
                    }
                )
            )
            context_path.write_text(
                json.dumps(
                    {
                        "project_name": "bridge",
                        "task_label": "fork-thread",
                        "current_thread_id": "thread-1",
                        "candidate_thread_id": "thread-2",
                        "goal_family": "bridge-core",
                        "work_unit": "implementation",
                        "repo_path": "/tmp/repo",
                        "current_worktree": "/tmp/repo",
                        "target_worktree": "/tmp/repo",
                        "current_branch": "main",
                        "target_branch": "main",
                        "same_goal_family": True,
                        "same_work_unit": False,
                        "same_repo": True,
                        "same_worktree": True,
                        "same_branch": True,
                        "assumptions_stable": True,
                        "rebrief_cost": "medium",
                        "topic_shift": "adjacent",
                        "context_overloaded": True,
                        "parallel_isolation_needed": False,
                        "risky_changes": False,
                        "unrelated_uncommitted_changes": False,
                        "needs_clean_replay": False,
                    }
                )
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mastermind_bridge.cli",
                    "decide",
                    "--context",
                    str(context_path),
                    "--registry",
                    str(registry_path),
                    "--write",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["thread_action"], "fork_thread")

            registry = json.loads(registry_path.read_text())
            self.assertEqual(registry["threads"][0]["thread_id"], "thread-2")
            self.assertEqual(registry["threads"][0]["parent_thread_id"], "thread-1")
            self.assertEqual(registry["threads"][0]["lineage_root_thread_id"], "thread-1")
            self.assertEqual(registry["threads"][0]["lineage_depth"], 1)
            self.assertEqual(registry["threads"][0]["lineage_path"], ["thread-1", "thread-2"])
            self.assertEqual(registry["decision_log"][0]["thread_action"], "fork_thread")
            self.assertEqual(registry["decision_log"][0]["parent_thread_id"], "thread-1")
            self.assertEqual(registry["decision_log"][0]["lineage_root_thread_id"], "thread-1")
            self.assertEqual(registry["decision_log"][0]["lineage_depth"], 1)

    def test_prompt_command_writes_output_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            request_path = tmp_path / "prompt_request.json"
            output_path = tmp_path / "NEXT_PROMPT.md"
            request_path.write_text(
                json.dumps(
                    {
                        "mode": "codex_new_thread",
                        "project_name": "bridge",
                        "thread_id": "thread-2",
                        "goal_family": "bridge-core",
                        "work_unit": "kernel",
                        "repo_path": "/tmp/repo",
                        "worktree_path": "/tmp/repo",
                        "branch": "main",
                        "thread_action": "fork_thread",
                        "objective": "Build the bridge.",
                        "task": "Implement the local kernel.",
                        "read_order": ["README.md"],
                        "durable_state": ["HANDOFF.md"],
                        "constraints": ["Keep state local."],
                        "acceptance_criteria": ["A prompt file is generated."],
                        "recent_results": ["Docs exist."],
                        "required_output": ["A Markdown prompt"],
                    }
                )
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mastermind_bridge.cli",
                    "prompt",
                    "--request",
                    str(request_path),
                    "--output",
                    str(output_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertTrue(output_path.exists())
            prompt = output_path.read_text()
            self.assertIn("Codex New Thread Brief", prompt)
            self.assertIn("Implement the local kernel.", prompt)

    def test_prepare_cycle_picks_rebrief_template_for_fork_thread(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            registry_path = tmp_path / "THREAD_REGISTRY.json"
            context_path = tmp_path / "decision_context.json"
            request_path = tmp_path / "prompt_request.json"
            output_path = tmp_path / "NEXT_PROMPT.md"
            registry_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "project": {"name": "bridge"},
                        "threads": [],
                        "decision_log": [],
                    }
                )
            )
            context_path.write_text(
                json.dumps(
                    {
                        "project_name": "bridge",
                        "task_label": "fork-thread",
                        "current_thread_id": "thread-1",
                        "candidate_thread_id": "thread-2",
                        "goal_family": "bridge-core",
                        "work_unit": "implementation",
                        "repo_path": "/tmp/repo",
                        "current_worktree": "/tmp/repo",
                        "target_worktree": "/tmp/repo",
                        "current_branch": "main",
                        "target_branch": "main",
                        "same_goal_family": True,
                        "same_work_unit": False,
                        "same_repo": True,
                        "same_worktree": True,
                        "same_branch": True,
                        "assumptions_stable": True,
                        "rebrief_cost": "medium",
                        "topic_shift": "adjacent",
                        "context_overloaded": True,
                        "parallel_isolation_needed": False,
                        "risky_changes": False,
                        "unrelated_uncommitted_changes": False,
                        "needs_clean_replay": False,
                    }
                )
            )
            request_path.write_text(
                json.dumps(
                    {
                        "mode": "codex_new_thread",
                        "project_name": "bridge",
                        "thread_id": "thread-1",
                        "goal_family": "bridge-core",
                        "work_unit": "implementation",
                        "repo_path": "/tmp/repo",
                        "worktree_path": "/tmp/repo",
                        "branch": "main",
                        "thread_action": "fork_thread",
                        "objective": "Continue the same project with a cleaner Codex context.",
                        "task": "Implement the next slice.",
                        "decision_summary": "",
                        "delta_summary": "Architecture is done; execution continues.",
                        "read_order": ["README.md"],
                        "durable_state": ["HANDOFF.md"],
                        "constraints": ["Keep state local."],
                        "acceptance_criteria": ["Prompt is generated."],
                        "recent_results": ["Docs exist."],
                        "required_output": ["A Markdown prompt"],
                        "rebrief_reason": "Prior thread is too noisy."
                    }
                )
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mastermind_bridge.cli",
                    "prepare-cycle",
                    "--context",
                    str(context_path),
                    "--request",
                    str(request_path),
                    "--registry",
                    str(registry_path),
                    "--output",
                    str(output_path),
                    "--write",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["thread_action"], "fork_thread")
            self.assertEqual(payload["prompt_mode"], "codex_rebrief")
            self.assertTrue(output_path.exists())
            prompt = output_path.read_text()
            self.assertIn("fresh Codex thread", prompt)
            self.assertIn("Parent thread: thread-1", prompt)

    def test_start_cycle_writes_prompt_and_non_git_launch_briefing(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            repo_path = tmp_path / "repo"
            repo_path.mkdir()
            registry_path = tmp_path / "THREAD_REGISTRY.json"
            context_path = tmp_path / "decision_context.json"
            request_path = tmp_path / "prompt_request.json"
            prompt_output = tmp_path / "NEXT_PROMPT.md"
            launch_output = tmp_path / "START_CYCLE.md"
            registry_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "project": {"name": "bridge"},
                        "threads": [],
                        "decision_log": [],
                    }
                )
            )
            context_path.write_text(
                json.dumps(
                    {
                        "project_name": "bridge",
                        "task_label": "launch-cycle",
                        "current_thread_id": "thread-1",
                        "candidate_thread_id": "thread-2",
                        "goal_family": "bridge-core",
                        "work_unit": "implementation",
                        "repo_path": str(repo_path),
                        "current_worktree": str(repo_path),
                        "target_worktree": str(repo_path),
                        "current_branch": "main",
                        "target_branch": "feature-cycle",
                        "same_goal_family": True,
                        "same_work_unit": False,
                        "same_repo": True,
                        "same_worktree": True,
                        "same_branch": False,
                        "assumptions_stable": True,
                        "rebrief_cost": "medium",
                        "topic_shift": "adjacent",
                        "context_overloaded": True,
                        "parallel_isolation_needed": False,
                        "risky_changes": False,
                        "unrelated_uncommitted_changes": False,
                        "needs_clean_replay": False,
                    }
                )
            )
            request_path.write_text(
                json.dumps(
                    {
                        "mode": "codex_new_thread",
                        "project_name": "bridge",
                        "thread_id": "thread-1",
                        "goal_family": "bridge-core",
                        "work_unit": "implementation",
                        "repo_path": str(repo_path),
                        "worktree_path": str(repo_path),
                        "branch": "main",
                        "thread_action": "fork_thread",
                        "objective": "Continue the same project with a cleaner Codex context.",
                        "task": "Implement the next slice.",
                        "decision_summary": "",
                        "delta_summary": "Architecture is done; execution continues.",
                        "read_order": ["README.md"],
                        "durable_state": ["HANDOFF.md"],
                        "constraints": ["Keep state local."],
                        "acceptance_criteria": ["Prompt is generated."],
                        "recent_results": ["Docs exist."],
                        "required_output": ["A Markdown prompt"],
                        "rebrief_reason": "Prior thread is too noisy.",
                    }
                )
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mastermind_bridge.cli",
                    "start-cycle",
                    "--context",
                    str(context_path),
                    "--request",
                    str(request_path),
                    "--registry",
                    str(registry_path),
                    "--prompt-output",
                    str(prompt_output),
                    "--launch-output",
                    str(launch_output),
                    "--write",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["thread_action"], "fork_thread")
            self.assertEqual(payload["branch_action"], "new_branch")
            self.assertTrue(prompt_output.exists())
            self.assertTrue(launch_output.exists())
            launch_text = launch_output.read_text()
            self.assertIn("Thread action: fork_thread", launch_text)
            self.assertIn("Selected thread: thread-2", launch_text)
            self.assertIn(f"cd {repo_path}", launch_text)
            self.assertIn("not a Git work tree", launch_text)

    def test_start_cycle_writes_git_worktree_add_command(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            repo_path = tmp_path / "repo"
            worktree_path = tmp_path / "feature-worktree"
            repo_path.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(repo_path)], check=True, capture_output=True, text=True)
            subprocess.run(["git", "-C", str(repo_path), "config", "user.name", "Bridge Tester"], check=True)
            subprocess.run(
                ["git", "-C", str(repo_path), "config", "user.email", "bridge@example.com"],
                check=True,
            )
            (repo_path / "README.md").write_text("seed\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo_path), "add", "README.md"], check=True)
            subprocess.run(["git", "-C", str(repo_path), "commit", "-m", "seed"], check=True, capture_output=True, text=True)

            registry_path = tmp_path / "THREAD_REGISTRY.json"
            context_path = tmp_path / "decision_context.json"
            request_path = tmp_path / "prompt_request.json"
            prompt_output = tmp_path / "NEXT_PROMPT.md"
            launch_output = tmp_path / "START_CYCLE.md"
            registry_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "project": {"name": "bridge"},
                        "threads": [],
                        "decision_log": [],
                    }
                )
            )
            context_path.write_text(
                json.dumps(
                    {
                        "project_name": "bridge",
                        "task_label": "launch-isolated-cycle",
                        "current_thread_id": "thread-2",
                        "candidate_thread_id": "thread-3",
                        "goal_family": "bridge-core",
                        "work_unit": "implementation",
                        "repo_path": str(repo_path),
                        "current_worktree": str(repo_path),
                        "target_worktree": str(worktree_path),
                        "current_branch": "main",
                        "target_branch": "feature-cycle",
                        "same_goal_family": True,
                        "same_work_unit": False,
                        "same_repo": True,
                        "same_worktree": False,
                        "same_branch": False,
                        "assumptions_stable": True,
                        "rebrief_cost": "medium",
                        "topic_shift": "adjacent",
                        "context_overloaded": True,
                        "parallel_isolation_needed": True,
                        "risky_changes": True,
                        "unrelated_uncommitted_changes": False,
                        "needs_clean_replay": False,
                    }
                )
            )
            request_path.write_text(
                json.dumps(
                    {
                        "mode": "codex_new_thread",
                        "project_name": "bridge",
                        "thread_id": "thread-2",
                        "goal_family": "bridge-core",
                        "work_unit": "implementation",
                        "repo_path": str(repo_path),
                        "worktree_path": str(repo_path),
                        "branch": "main",
                        "thread_action": "fork_thread",
                        "objective": "Continue the same project with isolated implementation.",
                        "task": "Implement the risky slice in isolation.",
                        "decision_summary": "",
                        "delta_summary": "Need isolated worktree and branch.",
                        "read_order": ["README.md"],
                        "durable_state": ["HANDOFF.md"],
                        "constraints": ["Keep state local."],
                        "acceptance_criteria": ["Prompt is generated."],
                        "recent_results": ["Docs exist."],
                        "required_output": ["A Markdown prompt"],
                        "rebrief_reason": "Prior thread is too noisy.",
                    }
                )
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mastermind_bridge.cli",
                    "start-cycle",
                    "--context",
                    str(context_path),
                    "--request",
                    str(request_path),
                    "--registry",
                    str(registry_path),
                    "--prompt-output",
                    str(prompt_output),
                    "--launch-output",
                    str(launch_output),
                    "--write",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["worktree_action"], "new_worktree")
            self.assertEqual(payload["branch_action"], "new_branch")
            launch_text = launch_output.read_text()
            self.assertIn(
                f"git -C {repo_path} worktree add -b feature-cycle {worktree_path} main",
                launch_text,
            )
            self.assertIn(f"cd {worktree_path}", launch_text)

    def test_start_cycle_applies_git_worktree_when_requested(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            repo_path = tmp_path / "repo"
            worktree_path = tmp_path / "feature-worktree"
            repo_path.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(repo_path)], check=True, capture_output=True, text=True)
            subprocess.run(["git", "-C", str(repo_path), "config", "user.name", "Bridge Tester"], check=True)
            subprocess.run(
                ["git", "-C", str(repo_path), "config", "user.email", "bridge@example.com"],
                check=True,
            )
            (repo_path / "README.md").write_text("seed\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo_path), "add", "README.md"], check=True)
            subprocess.run(["git", "-C", str(repo_path), "commit", "-m", "seed"], check=True, capture_output=True, text=True)

            registry_path = tmp_path / "THREAD_REGISTRY.json"
            context_path = tmp_path / "decision_context.json"
            request_path = tmp_path / "prompt_request.json"
            prompt_output = tmp_path / "NEXT_PROMPT.md"
            launch_output = tmp_path / "START_CYCLE.md"
            registry_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "project": {"name": "bridge"},
                        "threads": [],
                        "decision_log": [],
                    }
                )
            )
            context_path.write_text(
                json.dumps(
                    {
                        "project_name": "bridge",
                        "task_label": "launch-apply-cycle",
                        "current_thread_id": "thread-4",
                        "candidate_thread_id": "thread-5",
                        "goal_family": "bridge-core",
                        "work_unit": "implementation",
                        "repo_path": str(repo_path),
                        "current_worktree": str(repo_path),
                        "target_worktree": str(worktree_path),
                        "current_branch": "main",
                        "target_branch": "feature-apply",
                        "same_goal_family": True,
                        "same_work_unit": False,
                        "same_repo": True,
                        "same_worktree": False,
                        "same_branch": False,
                        "assumptions_stable": True,
                        "rebrief_cost": "medium",
                        "topic_shift": "adjacent",
                        "context_overloaded": True,
                        "parallel_isolation_needed": True,
                        "risky_changes": True,
                        "unrelated_uncommitted_changes": False,
                        "needs_clean_replay": False,
                    }
                )
            )
            request_path.write_text(
                json.dumps(
                    {
                        "mode": "codex_new_thread",
                        "project_name": "bridge",
                        "thread_id": "thread-4",
                        "goal_family": "bridge-core",
                        "work_unit": "implementation",
                        "repo_path": str(repo_path),
                        "worktree_path": str(repo_path),
                        "branch": "main",
                        "thread_action": "fork_thread",
                        "objective": "Continue the same project with isolated implementation.",
                        "task": "Implement the risky slice in isolation.",
                        "decision_summary": "",
                        "delta_summary": "Need isolated worktree and branch.",
                        "read_order": ["README.md"],
                        "durable_state": ["HANDOFF.md"],
                        "constraints": ["Keep state local."],
                        "acceptance_criteria": ["Prompt is generated."],
                        "recent_results": ["Docs exist."],
                        "required_output": ["A Markdown prompt"],
                        "rebrief_reason": "Prior thread is too noisy.",
                    }
                )
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mastermind_bridge.cli",
                    "start-cycle",
                    "--context",
                    str(context_path),
                    "--request",
                    str(request_path),
                    "--registry",
                    str(registry_path),
                    "--prompt-output",
                    str(prompt_output),
                    "--launch-output",
                    str(launch_output),
                    "--write",
                    "--apply-workspace",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["workspace_apply_status"], "applied")
            self.assertTrue(worktree_path.exists())
            branch_result = subprocess.run(
                ["git", "-C", str(worktree_path), "branch", "--show-current"],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(branch_result.stdout.strip(), "feature-apply")
            launch_text = launch_output.read_text()
            self.assertIn("Workspace apply status: applied", launch_text)
            self.assertIn(f"git -C {repo_path} worktree add -b feature-apply {worktree_path} main", launch_text)
            registry = json.loads(registry_path.read_text())
            thread_entry = next(item for item in registry["threads"] if item["thread_id"] == "thread-5")
            self.assertEqual(thread_entry["last_workspace_apply_status"], "applied")
            self.assertIn(
                f"git -C {repo_path} worktree add -b feature-apply {worktree_path} main",
                thread_entry["last_workspace_apply_commands"],
            )

    def test_start_cycle_skips_workspace_apply_for_non_git_repo(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            repo_path = tmp_path / "repo"
            repo_path.mkdir()
            registry_path = tmp_path / "THREAD_REGISTRY.json"
            context_path = tmp_path / "decision_context.json"
            request_path = tmp_path / "prompt_request.json"
            prompt_output = tmp_path / "NEXT_PROMPT.md"
            launch_output = tmp_path / "START_CYCLE.md"
            registry_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "project": {"name": "bridge"},
                        "threads": [],
                        "decision_log": [],
                    }
                )
            )
            context_path.write_text(
                json.dumps(
                    {
                        "project_name": "bridge",
                        "task_label": "launch-skip-cycle",
                        "current_thread_id": "thread-6",
                        "candidate_thread_id": "thread-7",
                        "goal_family": "bridge-core",
                        "work_unit": "implementation",
                        "repo_path": str(repo_path),
                        "current_worktree": str(repo_path),
                        "target_worktree": str(repo_path / "second"),
                        "current_branch": "main",
                        "target_branch": "feature-skip",
                        "same_goal_family": True,
                        "same_work_unit": False,
                        "same_repo": True,
                        "same_worktree": False,
                        "same_branch": False,
                        "assumptions_stable": True,
                        "rebrief_cost": "medium",
                        "topic_shift": "adjacent",
                        "context_overloaded": True,
                        "parallel_isolation_needed": True,
                        "risky_changes": True,
                        "unrelated_uncommitted_changes": False,
                        "needs_clean_replay": False,
                    }
                )
            )
            request_path.write_text(
                json.dumps(
                    {
                        "mode": "codex_new_thread",
                        "project_name": "bridge",
                        "thread_id": "thread-6",
                        "goal_family": "bridge-core",
                        "work_unit": "implementation",
                        "repo_path": str(repo_path),
                        "worktree_path": str(repo_path),
                        "branch": "main",
                        "thread_action": "fork_thread",
                        "objective": "Continue the same project with isolated implementation.",
                        "task": "Implement the risky slice in isolation.",
                        "decision_summary": "",
                        "delta_summary": "Need isolated worktree and branch.",
                        "read_order": ["README.md"],
                        "durable_state": ["HANDOFF.md"],
                        "constraints": ["Keep state local."],
                        "acceptance_criteria": ["Prompt is generated."],
                        "recent_results": ["Docs exist."],
                        "required_output": ["A Markdown prompt"],
                        "rebrief_reason": "Prior thread is too noisy.",
                    }
                )
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mastermind_bridge.cli",
                    "start-cycle",
                    "--context",
                    str(context_path),
                    "--request",
                    str(request_path),
                    "--registry",
                    str(registry_path),
                    "--prompt-output",
                    str(prompt_output),
                    "--launch-output",
                    str(launch_output),
                    "--write",
                    "--apply-workspace",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["workspace_apply_status"], "skipped")
            self.assertIn("not a Git work tree", "\n".join(payload["workspace_apply_warnings"]))
            launch_text = launch_output.read_text()
            self.assertIn("Workspace apply status: skipped", launch_text)

    def test_log_command_appends_markdown_and_updates_registry(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            registry_path = tmp_path / "THREAD_REGISTRY.json"
            log_path = tmp_path / "EXECUTION_LOG.md"
            report_path = tmp_path / "run_report.json"
            registry_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "project": {"name": "bridge"},
                        "threads": [
                            {"thread_id": "thread-1", "status": "archived"},
                            {
                                "thread_id": "thread-2",
                                "status": "active",
                                "parent_thread_id": "thread-1",
                                "last_workspace_apply_status": "applied",
                                "last_workspace_apply_commands": [
                                    "git -C /tmp/repo worktree add -b feature-cycle /tmp/feature-worktree main"
                                ],
                                "last_workspace_apply_warnings": [],
                            },
                        ],
                        "decision_log": [],
                    }
                )
            )
            log_path.write_text("# EXECUTION_LOG\n")
            report_path.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-04-14T23:20:00+02:00",
                        "thread_id": "thread-2",
                        "summary": "Kernel implemented.",
                        "files_touched": ["README.md", "mastermind_bridge/cli.py"],
                        "checks": ["python3 -m unittest discover -s tests"],
                        "blockers": [],
                        "risks": ["SDK adapter not implemented yet."],
                        "next_step": "Run the first real bridge cycle.",
                    }
                )
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mastermind_bridge.cli",
                    "log",
                    "--report",
                    str(report_path),
                    "--log",
                    str(log_path),
                    "--registry",
                    str(registry_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            log_text = log_path.read_text()
            self.assertIn("Kernel implemented.", log_text)
            self.assertIn("SDK adapter not implemented yet.", log_text)
            self.assertIn("Lineage:", log_text)
            self.assertIn("thread-1 -> thread-2", log_text)

            registry = json.loads(registry_path.read_text())
            thread_entry = next(item for item in registry["threads"] if item["thread_id"] == "thread-2")
            self.assertEqual(thread_entry["last_summary"], "Kernel implemented.")
            self.assertEqual(thread_entry["lineage_path"], ["thread-1", "thread-2"])

            report = json.loads(report_path.read_text())
            self.assertEqual(report["parent_thread_id"], "thread-1")
            self.assertEqual(report["lineage_root_thread_id"], "thread-1")

    def test_prepare_return_writes_mastermind_packet(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            report_path = tmp_path / "run_report.json"
            output_path = tmp_path / "RETURN_TO_MASTERMIND.md"
            report_path.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-04-14T23:20:00+02:00",
                        "thread_id": "thread-2",
                        "summary": "Kernel implemented.",
                        "files_touched": ["README.md", "mastermind_bridge/cli.py"],
                        "checks": ["python3 -m unittest discover -s tests"],
                        "blockers": ["No direct Codex capture yet."],
                        "risks": ["SDK adapter not implemented yet."],
                        "next_step": "Run the first real bridge cycle.",
                    }
                )
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mastermind_bridge.cli",
                    "prepare-return",
                    "--report",
                    str(report_path),
                    "--output",
                    str(output_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertTrue(output_path.exists())
            packet = output_path.read_text()
            self.assertTrue(packet.startswith("Session id: none\n"))
            self.assertIn("Here is what Codex wrote:", packet)
            self.assertIn("return_packet_id:", packet)
            self.assertIn("Kernel implemented.", packet)
            self.assertIn("python3 -m unittest discover -s tests", packet)
            self.assertIn("Recommended next step: Run the first real bridge cycle.", packet)

    def test_reflect_writes_mastermind_reflection_prompt(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            report_path = tmp_path / "run_report.json"
            registry_path = tmp_path / "THREAD_REGISTRY.json"
            output_path = tmp_path / "REFLECTION_PROMPT.md"
            report_path.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-04-14T23:20:00+02:00",
                        "thread_id": "thread-2",
                        "summary": "Kernel implemented.",
                        "files_touched": ["README.md", "mastermind_bridge/cli.py"],
                        "checks": ["python3 -m unittest discover -s tests"],
                        "blockers": [],
                        "risks": ["SDK adapter not implemented yet."],
                        "next_step": "Run the first real bridge cycle.",
                    }
                )
            )
            registry_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "project": {
                            "name": "bridge",
                            "canonical_state_files": [
                                "README.md",
                                "docs/private/DECISIONS.md",
                                "docs/ARCHITECTURE.md",
                                "docs/THREAD_POLICY.md",
                            ],
                        },
                        "threads": [
                            {"thread_id": "thread-1", "status": "archived"},
                            {
                                "thread_id": "thread-2",
                                "status": "active",
                                "parent_thread_id": "thread-1",
                            },
                        ],
                        "decision_log": [],
                    }
                )
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mastermind_bridge.cli",
                    "reflect",
                    "--report",
                    str(report_path),
                    "--registry",
                    str(registry_path),
                    "--output",
                    str(output_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertTrue(output_path.exists())
            prompt = output_path.read_text()
            self.assertIn("Mastermind Reflection Prompt", prompt)
            self.assertIn("Durable state file: README.md", prompt)
            self.assertIn("Latest run report:", prompt)
            self.assertIn("Thread lineage: thread-1 -> thread-2", prompt)
            self.assertIn("Latest run summary: Kernel implemented.", prompt)
            self.assertIn("Recommended next step: Run the first real bridge cycle.", prompt)

    def test_execute_codex_captures_artifacts_and_updates_return_files(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            fake_codex = self._write_fake_codex(tmp_path)
            prompt_path = tmp_path / "NEXT_PROMPT.md"
            prompt_path.write_text("# Prompt\nImplement the next step.\n", encoding="utf-8")
            workdir = tmp_path / "workspace"
            workdir.mkdir()
            artifacts_root = tmp_path / "runs"
            log_path = tmp_path / "EXECUTION_LOG.md"
            log_path.write_text("# EXECUTION_LOG\n", encoding="utf-8")
            registry_path = tmp_path / "THREAD_REGISTRY.json"
            registry_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "project": {"name": "bridge"},
                        "threads": [
                            {"thread_id": "thread-1", "status": "archived"},
                            {
                                "thread_id": "thread-2",
                                "status": "active",
                                "parent_thread_id": "thread-1",
                                "last_workspace_apply_status": "applied",
                                "last_workspace_apply_commands": [
                                    "git -C /tmp/repo worktree add -b feature-cycle /tmp/feature-worktree main"
                                ],
                                "last_workspace_apply_warnings": [],
                            },
                        ],
                        "decision_log": [],
                    }
                )
            )
            return_output = tmp_path / "RETURN_TO_MASTERMIND.md"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mastermind_bridge.cli",
                    "execute-codex",
                    "--prompt",
                    str(prompt_path),
                    "--workdir",
                    str(workdir),
                    "--artifacts-root",
                    str(artifacts_root),
                    "--thread-id",
                    "thread-2",
                    "--codex-bin",
                    str(fake_codex),
                    "--log-file",
                    str(log_path),
                    "--registry",
                    str(registry_path),
                    "--return-output",
                    str(return_output),
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            payload = json.loads(result.stdout)
            run_dir = Path(payload["run_dir"])
            self.assertTrue(run_dir.exists())
            self.assertTrue((run_dir / "stdout.jsonl").exists())
            self.assertTrue((run_dir / "last_message.md").exists())
            self.assertTrue((run_dir / "run_report.json").exists())
            self.assertIn("Completed Codex wrapper execution.", (run_dir / "last_message.md").read_text())
            self.assertTrue(return_output.exists())
            self.assertIn("Completed Codex wrapper execution.", return_output.read_text())
            self.assertIn("Completed Codex wrapper execution.", log_path.read_text())
            self.assertIn("Commands observed:", log_path.read_text())
            self.assertIn("/bin/zsh -c pwd", log_path.read_text())
            self.assertIn("Lineage:", log_path.read_text())
            self.assertIn("thread-1 -> thread-2", log_path.read_text())
            report = json.loads((run_dir / "run_report.json").read_text())
            self.assertEqual(report["observed_codex_thread_id"], "exec-thread-xyz")
            self.assertEqual(report["final_agent_message"], "Completed Codex wrapper execution.")
            self.assertEqual(report["usage"]["input_tokens"], 123)
            self.assertEqual(report["context_window_tokens"], 200000)
            self.assertEqual(report["context_used_tokens"], 190)
            self.assertEqual(report["estimated_context_remaining_percent"], 99)
            self.assertEqual(report["context_signal_source"], "default")
            self.assertTrue(report["session_live_log_path"])
            self.assertIn("thread.started", report["event_types"])
            self.assertEqual(report["commands_observed"][0]["command"], "/bin/zsh -c pwd")
            self.assertEqual(report["commands_observed"][0]["exit_code"], 0)
            self.assertEqual(report["commands_observed"][0]["aggregated_output"], "/tmp/workspace\n")
            self.assertEqual(report["files_touched"], ["README.md", "mastermind_bridge/cli.py"])
            self.assertEqual(report["checks"], ["python3 -m unittest discover -s tests"])
            self.assertEqual(report["parent_thread_id"], "thread-1")
            self.assertEqual(report["lineage_root_thread_id"], "thread-1")
            self.assertEqual(report["lineage_depth"], 1)
            self.assertEqual(report["lineage_path"], ["thread-1", "thread-2"])
            self.assertEqual(report["workspace_apply_status"], "applied")
            self.assertEqual(
                report["workspace_apply_commands"],
                ["git -C /tmp/repo worktree add -b feature-cycle /tmp/feature-worktree main"],
            )
            registry = json.loads(registry_path.read_text())
            thread_entry = next(item for item in registry["threads"] if item["thread_id"] == "thread-2")
            self.assertEqual(
                thread_entry["last_summary"],
                "Completed Codex wrapper execution.",
            )
            self.assertEqual(thread_entry["last_exec_thread_id"], "exec-thread-xyz")
            self.assertEqual(thread_entry["lineage_path"], ["thread-1", "thread-2"])
            return_text = return_output.read_text()
            self.assertIn("Here is what Codex wrote:", return_text)
            self.assertIn("Run started:", return_text)
            self.assertIn("Command:", return_text)
            self.assertIn("Completed Codex wrapper execution.", return_text)
            self.assertNotIn("Commands observed:", return_text)
            self.assertNotIn("Workspace apply status: applied", return_text)
            self.assertIn("Workspace apply:", log_path.read_text())
            self.assertIn(
                "git -C /tmp/repo worktree add -b feature-cycle /tmp/feature-worktree main",
                log_path.read_text(),
            )

    def test_execute_codex_failure_still_writes_artifacts_and_report(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            fake_codex = self._write_fake_codex(tmp_path)
            prompt_path = tmp_path / "NEXT_PROMPT.md"
            prompt_path.write_text("# Prompt\nFail intentionally.\n", encoding="utf-8")
            workdir = tmp_path / "workspace"
            workdir.mkdir()
            artifacts_root = tmp_path / "runs"

            env = dict(os.environ)
            env["FAKE_CODEX_EXIT"] = "7"
            env["FAKE_CODEX_STDERR"] = f"{self._NOISY_STDERR}\nrunner failed\n"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mastermind_bridge.cli",
                    "execute-codex",
                    "--prompt",
                    str(prompt_path),
                    "--workdir",
                    str(workdir),
                    "--artifacts-root",
                    str(artifacts_root),
                    "--thread-id",
                    "thread-9",
                    "--codex-bin",
                    str(fake_codex),
                ],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )

            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stdout)
            run_dir = Path(payload["run_dir"])
            self.assertTrue((run_dir / "stderr.txt").exists())
            stderr_text = (run_dir / "stderr.txt").read_text()
            self.assertIn("runner failed", stderr_text)
            self.assertIn("codex_core_skills::loader", stderr_text)
            report = json.loads((run_dir / "run_report.json").read_text())
            self.assertEqual(report["blockers"], ["codex exec exited with code 7", "runner failed"])

    def test_execute_codex_timeout_still_writes_artifacts_and_report(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            fake_codex = self._write_fake_codex(tmp_path)
            prompt_path = tmp_path / "NEXT_PROMPT.md"
            prompt_path.write_text("# Prompt\nHang intentionally.\n", encoding="utf-8")
            workdir = tmp_path / "workspace"
            workdir.mkdir()
            artifacts_root = tmp_path / "runs"

            env = dict(os.environ)
            env["FAKE_CODEX_SLEEP"] = "1.0"
            env["FAKE_CODEX_STDERR"] = f"{self._NOISY_STDERR}\n"
            env["FAKE_CODEX_STDERR_BEFORE_SLEEP"] = "1"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mastermind_bridge.cli",
                    "execute-codex",
                    "--prompt",
                    str(prompt_path),
                    "--workdir",
                    str(workdir),
                    "--artifacts-root",
                    str(artifacts_root),
                    "--thread-id",
                    "thread-timeout",
                    "--codex-bin",
                    str(fake_codex),
                    "--timeout-seconds",
                    "0.5",
                ],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )

            self.assertEqual(result.returncode, 0)
            payload = json.loads(result.stdout)
            run_dir = Path(payload["run_dir"])
            self.assertTrue((run_dir / "stdout.jsonl").exists())
            self.assertTrue((run_dir / "stderr.txt").exists())
            stderr_text = (run_dir / "stderr.txt").read_text()
            self.assertNotIn("timed out after 0.5 seconds", stderr_text)
            report = json.loads((run_dir / "run_report.json").read_text())
            self.assertEqual(report["exit_code"], 0)
            self.assertEqual(report["blockers"], [])
            self.assertEqual(report["interruption_reason"], "")
            metadata = json.loads((run_dir / "run_metadata.json").read_text())
            self.assertEqual(metadata["timeout_seconds"], 0.5)

    def test_execute_codex_timeout_recovers_partial_structured_fields(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            fake_codex = self._write_fake_codex(tmp_path)
            prompt_path = tmp_path / "NEXT_PROMPT.md"
            prompt_path.write_text("# Prompt\nRecover partial timeout artifacts.\n", encoding="utf-8")
            workdir = tmp_path / "workspace"
            workdir.mkdir()
            artifacts_root = tmp_path / "runs"

            env = dict(os.environ)
            env["FAKE_CODEX_SCENARIO"] = "partial_timeout"
            env["FAKE_CODEX_SLEEP"] = "1.0"
            env["FAKE_CODEX_LAST_MESSAGE_TEXT"] = "\n".join(
                [
                    "Implemented partial timeout rec",
                    "Files touched:",
                    "- README.md",
                ]
            )
            env["FAKE_CODEX_PARTIAL_AGENT_MESSAGE"] = "\n".join(
                [
                    "Implemented partial timeout recovery.",
                    "Files touched: README.md, mastermind_bridge/executor.py",
                    "Checks run: python3 -m unittest tests.test_executor",
                ]
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mastermind_bridge.cli",
                    "execute-codex",
                    "--prompt",
                    str(prompt_path),
                    "--workdir",
                    str(workdir),
                    "--artifacts-root",
                    str(artifacts_root),
                    "--thread-id",
                    "thread-timeout-partial",
                    "--codex-bin",
                    str(fake_codex),
                    "--timeout-seconds",
                    "0.5",
                ],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )

            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stdout)
            run_dir = Path(payload["run_dir"])
            report = json.loads((run_dir / "run_report.json").read_text())
            self.assertEqual(report["exit_code"], 124)
            self.assertEqual(report["summary"], "Implemented partial timeout recovery.")
            self.assertEqual(report["files_touched"], ["README.md", "mastermind_bridge/executor.py"])
            self.assertEqual(report["checks"], ["python3 -m unittest tests.test_executor"])
            self.assertEqual(report["observed_codex_thread_id"], "exec-thread-timeout")
            self.assertIn("item.completed", report["event_types"])
            self.assertEqual(
                report["final_agent_message"],
                "\n".join(
                    [
                        "Implemented partial timeout recovery.",
                        "Files touched: README.md, mastermind_bridge/executor.py",
                        "Checks run: python3 -m unittest tests.test_executor",
                    ]
                ),
            )

    def test_execute_codex_timeout_after_turn_completed_is_salvaged_as_success(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            fake_codex = self._write_fake_codex(tmp_path)
            prompt_path = tmp_path / "NEXT_PROMPT.md"
            prompt_path.write_text("# Prompt\nFinish before shutdown timeout.\n", encoding="utf-8")
            workdir = tmp_path / "workspace"
            workdir.mkdir()
            artifacts_root = tmp_path / "runs"

            env = dict(os.environ)
            env["FAKE_CODEX_SLEEP"] = "1.0"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mastermind_bridge.cli",
                    "execute-codex",
                    "--prompt",
                    str(prompt_path),
                    "--workdir",
                    str(workdir),
                    "--artifacts-root",
                    str(artifacts_root),
                    "--thread-id",
                    "thread-timeout-after-complete",
                    "--codex-bin",
                    str(fake_codex),
                    "--timeout-seconds",
                    "0.5",
                ],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            run_dir = Path(payload["run_dir"])
            report = json.loads((run_dir / "run_report.json").read_text())
            self.assertEqual(report["exit_code"], 0)
            self.assertEqual(report["interruption_reason"], "")
            self.assertEqual(report["blockers"], [])
            self.assertEqual(report["summary"], "Completed Codex wrapper execution.")
            self.assertIn("turn.completed", report["event_types"])
            self.assertNotIn("timed out after 0.5 seconds", (run_dir / "stderr.txt").read_text())

    def test_execute_codex_infers_checks_from_observed_test_commands(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            fake_codex = self._write_fake_codex(tmp_path)
            prompt_path = tmp_path / "NEXT_PROMPT.md"
            prompt_path.write_text("# Prompt\nInfer checks from commands.\n", encoding="utf-8")
            workdir = tmp_path / "workspace"
            workdir.mkdir()
            artifacts_root = tmp_path / "runs"

            env = dict(os.environ)
            env["FAKE_CODEX_SCENARIO"] = "inferred_checks"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mastermind_bridge.cli",
                    "execute-codex",
                    "--prompt",
                    str(prompt_path),
                    "--workdir",
                    str(workdir),
                    "--artifacts-root",
                    str(artifacts_root),
                    "--thread-id",
                    "thread-inferred-checks",
                    "--codex-bin",
                    str(fake_codex),
                ],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            payload = json.loads(result.stdout)
            run_dir = Path(payload["run_dir"])
            report = json.loads((run_dir / "run_report.json").read_text())
            self.assertEqual(
                report["checks"],
                ["python3 -m unittest tests.test_executor tests.test_cli"],
            )
            self.assertNotIn("sed -n '1,240p' tests/test_executor.py", report["checks"])
            self.assertEqual(report["observed_codex_thread_id"], "exec-thread-inferred")

    def test_execute_codex_infers_files_touched_from_workspace_delta(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            fake_codex = self._write_fake_codex(tmp_path)
            prompt_path = tmp_path / "NEXT_PROMPT.md"
            prompt_path.write_text("# Prompt\nInfer files touched from workspace delta.\n", encoding="utf-8")
            workdir = tmp_path / "workspace"
            workdir.mkdir()
            (workdir / "README.md").write_text("before\n", encoding="utf-8")
            artifacts_root = workdir / "artifacts"

            env = dict(os.environ)
            env["FAKE_CODEX_SCENARIO"] = "inferred_files_touched"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mastermind_bridge.cli",
                    "execute-codex",
                    "--prompt",
                    str(prompt_path),
                    "--workdir",
                    str(workdir),
                    "--artifacts-root",
                    str(artifacts_root),
                    "--thread-id",
                    "thread-inferred-files",
                    "--codex-bin",
                    str(fake_codex),
                ],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            payload = json.loads(result.stdout)
            run_dir = Path(payload["run_dir"])
            report = json.loads((run_dir / "run_report.json").read_text())
            self.assertEqual(
                report["files_touched"],
                ["README.md", "tests/test_executor.py"],
            )
            self.assertEqual(report["observed_codex_thread_id"], "exec-thread-files")

    def test_status_does_not_create_policy_file_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            bindings_path = tmp_path / "CHAT_BINDINGS.json"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}), encoding="utf-8")
            policy_path = tmp_path / "ORCHESTRATOR_POLICY.json"
            sessions_dir = tmp_path / "sessions"
            sessions_dir.mkdir()

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mastermind_bridge.cli",
                    "status",
                    "--bindings",
                    str(bindings_path),
                    "--policy",
                    str(policy_path),
                    "--sessions-dir",
                    str(sessions_dir),
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertFalse(policy_path.exists())

    def test_runtime_prompt_path_isolated_per_session(self):
        from mastermind_bridge.cli import _runtime_prompt_path

        root = Path("/tmp/runtime-prompts")

        self.assertEqual(
            _runtime_prompt_path(root, "session-a"),
            root / "session-a" / "NEXT_PROMPT.md",
        )
        self.assertEqual(
            _runtime_prompt_path(root, "session-b"),
            root / "session-b" / "NEXT_PROMPT.md",
        )

    @unittest.mock.patch.dict(os.environ, {"BRIDGE_ENABLE_CODEX_APP_INTEGRATION": "1"}, clear=False)
    def test_local_recovery_codex_env_uses_default_codex_home_for_app_threads(self):
        from mastermind_bridge.cli import _local_recovery_codex_env

        self.assertIsNone(_local_recovery_codex_env("session-1", thread_action="new_thread"))

    @unittest.mock.patch.dict(os.environ, {"BRIDGE_ENABLE_CODEX_APP_INTEGRATION": "0"}, clear=False)
    def test_local_recovery_codex_env_isolates_non_app_fresh_recovery_threads(self):
        from mastermind_bridge.cli import _local_recovery_codex_env

        env = _local_recovery_codex_env("session-1", thread_action="new_thread")

        self.assertIsNotNone(env)
        self.assertEqual(env["CODEX_HOME"], "/tmp/bridge-codex-home/session-1")

    @unittest.mock.patch.dict(os.environ, {"BRIDGE_ENABLE_CODEX_APP_INTEGRATION": "0"}, clear=False)
    @unittest.mock.patch("mastermind_bridge.cli.prepare_native_codex_fork_thread")
    @unittest.mock.patch("mastermind_bridge.cli.prepare_native_codex_start_thread")
    @unittest.mock.patch("mastermind_bridge.cli.execute_codex_prompt")
    def test_build_loop_runner_uses_background_fresh_exec_for_new_thread_by_default(
        self,
        execute_mock,
        start_mock,
        fork_mock,
    ):
        from mastermind_bridge.cli import _build_loop_runner
        from mastermind_bridge.models import RunReport
        from mastermind_bridge.orchestrator.models import ChatBinding, OrchestratorSession

        execute_mock.return_value = (
            RunReport.from_dict(
                {
                    "timestamp": "2026-04-16T12:00:00+02:00",
                    "thread_id": "session-1",
                    "summary": "Started fresh thread.",
                    "observed_codex_thread_id": "observed-new-thread-123",
                    "files_touched": [],
                    "checks": [],
                    "blockers": [],
                    "risks": [],
                    "next_step": "",
                }
            ),
            {},
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            policy_path = tmp_path / "ORCHESTRATOR_POLICY.json"
            policy_path.write_text(json.dumps({"version": 1, "stop_phrases": []}), encoding="utf-8")
            sessions_dir = tmp_path / "sessions"
            sessions_dir.mkdir()
            runner = _build_loop_runner(
                bindings_path=tmp_path / "CHAT_BINDINGS.json",
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                artifacts_root=tmp_path / "artifacts",
                log_file=None,
                registry_path=None,
                codex_bin="codex",
                model=None,
                sandbox=None,
                profile=None,
                headless=True,
            )
            session = OrchestratorSession(
                session_id="session-1",
                binding_id="binding-1",
                repo_path="/tmp/repo",
                workspace_path="/tmp/repo",
                chat_url="https://chatgpt.com/c/project/binding-1",
                current_codex_thread_id="parent-thread-ignored",
            )
            binding = ChatBinding(
                binding_id="binding-1",
                project_name="bridge",
                repo_path="/tmp/repo",
                workspace_path="/tmp/repo",
                chat_url="https://chatgpt.com/c/project/binding-1",
            )

            report = runner.executor(
                prompt="Continue.",
                thread_action="new_thread",
                session=session,
                binding=binding,
                instructions=[],
            )

            self.assertEqual(report.thread_operation, "fresh_exec_new_thread")
            self.assertEqual(report.requested_codex_thread_id, "")
            self.assertEqual(report.codex_thread_id, "observed-new-thread-123")
            self.assertIsNone(execute_mock.call_args.kwargs["resume_session_id"])
            self.assertEqual(execute_mock.call_args.kwargs["observed_thread_name_hint"], "")

    @unittest.mock.patch.dict(os.environ, {"BRIDGE_ENABLE_CODEX_APP_INTEGRATION": "0"}, clear=False)
    @unittest.mock.patch("mastermind_bridge.cli.prepare_native_codex_fork_thread")
    @unittest.mock.patch("mastermind_bridge.cli.prepare_native_codex_start_thread")
    @unittest.mock.patch("mastermind_bridge.cli.execute_codex_prompt")
    def test_build_loop_runner_prefers_session_execution_settings_over_global_defaults(
        self,
        execute_mock,
        start_mock,
        fork_mock,
    ):
        from mastermind_bridge.cli import _build_loop_runner
        from mastermind_bridge.models import RunReport
        from mastermind_bridge.orchestrator.models import ChatBinding, OrchestratorSession

        execute_mock.return_value = (
            RunReport.from_dict(
                {
                    "timestamp": "2026-04-16T12:00:00+02:00",
                    "thread_id": "session-1",
                    "summary": "Used session execution settings.",
                    "observed_codex_thread_id": "observed-thread-123",
                    "files_touched": [],
                    "checks": [],
                    "blockers": [],
                    "risks": [],
                    "next_step": "",
                }
            ),
            {},
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            policy_path = tmp_path / "ORCHESTRATOR_POLICY.json"
            policy_path.write_text(json.dumps({"version": 1, "stop_phrases": []}), encoding="utf-8")
            sessions_dir = tmp_path / "sessions"
            sessions_dir.mkdir()
            runner = _build_loop_runner(
                bindings_path=tmp_path / "CHAT_BINDINGS.json",
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                artifacts_root=tmp_path / "artifacts",
                log_file=None,
                registry_path=None,
                codex_bin="codex",
                model="gpt-5.4",
                reasoning_effort="high",
                sandbox=None,
                profile=None,
                headless=True,
            )
            session = OrchestratorSession(
                session_id="session-1",
                binding_id="binding-1",
                repo_path="/tmp/repo",
                workspace_path="/tmp/repo",
                chat_url="https://chatgpt.com/c/project/binding-1",
                codex_model="gpt-5.5",
                codex_reasoning_effort="xhigh",
            )
            binding = ChatBinding(
                binding_id="binding-1",
                project_name="bridge",
                repo_path="/tmp/repo",
                workspace_path="/tmp/repo",
                chat_url="https://chatgpt.com/c/project/binding-1",
            )

            runner.executor(
                prompt="Continue.",
                thread_action="new_thread",
                session=session,
                binding=binding,
                instructions=[],
            )

            self.assertEqual(execute_mock.call_args.kwargs["model"], "gpt-5.5")
            self.assertEqual(execute_mock.call_args.kwargs["reasoning_effort"], "xhigh")
            start_mock.assert_not_called()
            fork_mock.assert_not_called()

    @unittest.mock.patch.dict(os.environ, {"BRIDGE_ENABLE_CODEX_APP_INTEGRATION": "1"}, clear=False)
    @unittest.mock.patch("mastermind_bridge.cli.prepare_native_codex_fork_thread")
    @unittest.mock.patch("mastermind_bridge.cli.prepare_native_codex_start_thread", return_value="new-thread-123")
    @unittest.mock.patch("mastermind_bridge.cli.execute_codex_prompt")
    def test_build_loop_runner_uses_native_start_for_new_thread_when_app_integration_enabled(
        self,
        execute_mock,
        start_mock,
        fork_mock,
    ):
        from mastermind_bridge.cli import _build_loop_runner
        from mastermind_bridge.models import RunReport
        from mastermind_bridge.orchestrator.models import ChatBinding, OrchestratorSession

        execute_mock.return_value = (
            RunReport.from_dict(
                {
                    "timestamp": "2026-04-16T12:00:00+02:00",
                    "thread_id": "session-1",
                    "summary": "Started fresh thread.",
                    "files_touched": [],
                    "checks": [],
                    "blockers": [],
                    "risks": [],
                    "next_step": "",
                }
            ),
            {},
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            policy_path = tmp_path / "ORCHESTRATOR_POLICY.json"
            policy_path.write_text(json.dumps({"version": 1, "stop_phrases": []}), encoding="utf-8")
            sessions_dir = tmp_path / "sessions"
            sessions_dir.mkdir()
            runner = _build_loop_runner(
                bindings_path=tmp_path / "CHAT_BINDINGS.json",
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                artifacts_root=tmp_path / "artifacts",
                log_file=None,
                registry_path=None,
                codex_bin="codex",
                model=None,
                sandbox=None,
                profile=None,
                headless=True,
            )
            session = OrchestratorSession(
                session_id="session-1",
                binding_id="binding-1",
                repo_path="/tmp/repo",
                workspace_path="/tmp/repo",
                chat_url="https://chatgpt.com/c/project/binding-1",
                current_codex_thread_id="parent-thread-ignored",
            )
            from mastermind_bridge.orchestrator.state import load_session, save_session, session_path
            save_session(session_path(sessions_dir, session.session_id), session)
            binding = ChatBinding(
                binding_id="binding-1",
                project_name="bridge",
                repo_path="/tmp/repo",
                workspace_path="/tmp/repo",
                chat_url="https://chatgpt.com/c/project/binding-1",
            )

            report = runner.executor(
                prompt="Continue.",
                thread_action="new_thread",
                session=session,
                binding=binding,
                instructions=[],
            )

            self.assertEqual(report.thread_operation, "app_server_start")
            self.assertEqual(report.requested_codex_thread_id, "new-thread-123")
            self.assertEqual(report.codex_thread_id, "new-thread-123")
            refreshed = load_session(session_path(sessions_dir, session.session_id))
            self.assertEqual(refreshed.current_codex_thread_id, "new-thread-123")
            self.assertEqual(refreshed.current_codex_run_id, "new-thread-123")
            start_mock.assert_called_once()
            fork_mock.assert_not_called()

    @unittest.mock.patch.dict(os.environ, {"BRIDGE_ENABLE_CODEX_APP_INTEGRATION": "1"}, clear=False)
    @unittest.mock.patch("mastermind_bridge.cli.prepare_native_codex_fork_thread")
    @unittest.mock.patch("mastermind_bridge.cli.prepare_native_codex_start_thread", return_value="")
    @unittest.mock.patch("mastermind_bridge.cli.execute_codex_prompt")
    def test_build_loop_runner_marks_new_thread_degraded_when_native_start_is_unavailable(
        self,
        execute_mock,
        start_mock,
        fork_mock,
    ):
        from mastermind_bridge.cli import _build_loop_runner
        from mastermind_bridge.models import RunReport
        from mastermind_bridge.orchestrator.models import ChatBinding, OrchestratorSession

        execute_mock.return_value = (
            RunReport.from_dict(
                {
                    "timestamp": "2026-04-16T12:00:00+02:00",
                    "thread_id": "session-1",
                    "summary": "Started via CLI fallback.",
                    "files_touched": [],
                    "checks": [],
                    "blockers": [],
                    "risks": [],
                    "next_step": "",
                }
            ),
            {},
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            policy_path = tmp_path / "ORCHESTRATOR_POLICY.json"
            policy_path.write_text(json.dumps({"version": 1, "stop_phrases": []}), encoding="utf-8")
            sessions_dir = tmp_path / "sessions"
            sessions_dir.mkdir()
            runner = _build_loop_runner(
                bindings_path=tmp_path / "CHAT_BINDINGS.json",
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                artifacts_root=tmp_path / "artifacts",
                log_file=None,
                registry_path=None,
                codex_bin="codex",
                model=None,
                sandbox=None,
                profile=None,
                headless=True,
            )
            session = OrchestratorSession(
                session_id="session-1",
                binding_id="binding-1",
                repo_path="/tmp/repo",
                workspace_path="/tmp/repo",
                chat_url="https://chatgpt.com/c/project/binding-1",
            )
            binding = ChatBinding(
                binding_id="binding-1",
                project_name="bridge",
                repo_path="/tmp/repo",
                workspace_path="/tmp/repo",
                chat_url="https://chatgpt.com/c/project/binding-1",
            )

            report = runner.executor(
                prompt="Continue.",
                thread_action="new_thread",
                session=session,
                binding=binding,
                instructions=[],
            )

            self.assertEqual(report.thread_operation, "cli_fresh_exec")
            self.assertEqual(report.degraded_mode, "app_server_start_unavailable")
            self.assertEqual(report.requested_codex_thread_id, "")
            start_mock.assert_called_once()
            fork_mock.assert_not_called()

    @unittest.mock.patch.dict(os.environ, {"BRIDGE_ENABLE_CODEX_APP_INTEGRATION": "0"}, clear=False)
    @unittest.mock.patch("mastermind_bridge.cli.prepare_native_codex_fork_thread")
    @unittest.mock.patch("mastermind_bridge.cli.prepare_native_codex_start_thread")
    @unittest.mock.patch("mastermind_bridge.cli.execute_codex_prompt")
    def test_build_loop_runner_uses_background_fresh_exec_for_fork_thread_by_default(
        self,
        execute_mock,
        start_mock,
        fork_mock,
    ):
        from mastermind_bridge.cli import _build_loop_runner
        from mastermind_bridge.models import RunReport
        from mastermind_bridge.orchestrator.models import ChatBinding, OrchestratorSession

        execute_mock.return_value = (
            RunReport.from_dict(
                {
                    "timestamp": "2026-04-16T12:00:00+02:00",
                    "thread_id": "session-1",
                    "summary": "Forked thread via fresh exec.",
                    "observed_codex_thread_id": "observed-fork-thread-123",
                    "files_touched": [],
                    "checks": [],
                    "blockers": [],
                    "risks": [],
                    "next_step": "",
                }
            ),
            {},
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            policy_path = tmp_path / "ORCHESTRATOR_POLICY.json"
            policy_path.write_text(json.dumps({"version": 1, "stop_phrases": []}), encoding="utf-8")
            sessions_dir = tmp_path / "sessions"
            sessions_dir.mkdir()
            runner = _build_loop_runner(
                bindings_path=tmp_path / "CHAT_BINDINGS.json",
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                artifacts_root=tmp_path / "artifacts",
                log_file=None,
                registry_path=None,
                codex_bin="codex",
                model=None,
                sandbox=None,
                profile=None,
                headless=True,
            )
            session = OrchestratorSession(
                session_id="session-1",
                binding_id="binding-1",
                repo_path="/tmp/repo",
                workspace_path="/tmp/repo",
                chat_url="https://chatgpt.com/c/project/binding-1",
                current_codex_thread_id="parent-thread-123",
            )
            binding = ChatBinding(
                binding_id="binding-1",
                project_name="bridge",
                repo_path="/tmp/repo",
                workspace_path="/tmp/repo",
                chat_url="https://chatgpt.com/c/project/binding-1",
            )

            report = runner.executor(
                prompt="Continue.",
                thread_action="fork_thread",
                session=session,
                binding=binding,
                instructions=[],
            )

            self.assertEqual(report.thread_operation, "fresh_exec_fork_thread")
            self.assertEqual(report.parent_thread_id, "parent-thread-123")
            self.assertEqual(report.requested_codex_thread_id, "")
            self.assertEqual(report.codex_thread_id, "observed-fork-thread-123")
            self.assertIsNone(execute_mock.call_args.kwargs["resume_session_id"])
            fork_mock.assert_not_called()
            start_mock.assert_not_called()

    @unittest.mock.patch.dict(os.environ, {"BRIDGE_ENABLE_CODEX_APP_INTEGRATION": "1"}, clear=False)
    @unittest.mock.patch("mastermind_bridge.cli.prepare_native_codex_fork_thread", return_value="forked-thread-123")
    @unittest.mock.patch("mastermind_bridge.cli.prepare_native_codex_start_thread")
    @unittest.mock.patch("mastermind_bridge.cli.execute_codex_prompt")
    def test_build_loop_runner_uses_native_fork_for_fork_thread_when_app_integration_enabled(
        self,
        execute_mock,
        start_mock,
        fork_mock,
    ):
        from mastermind_bridge.cli import _build_loop_runner
        from mastermind_bridge.models import RunReport
        from mastermind_bridge.orchestrator.models import ChatBinding, OrchestratorSession

        execute_mock.return_value = (
            RunReport.from_dict(
                {
                    "timestamp": "2026-04-16T12:00:00+02:00",
                    "thread_id": "session-1",
                    "summary": "Forked thread.",
                    "files_touched": [],
                    "checks": [],
                    "blockers": [],
                    "risks": [],
                    "next_step": "",
                }
            ),
            {},
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            policy_path = tmp_path / "ORCHESTRATOR_POLICY.json"
            policy_path.write_text(json.dumps({"version": 1, "stop_phrases": []}), encoding="utf-8")
            sessions_dir = tmp_path / "sessions"
            sessions_dir.mkdir()
            runner = _build_loop_runner(
                bindings_path=tmp_path / "CHAT_BINDINGS.json",
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                artifacts_root=tmp_path / "artifacts",
                log_file=None,
                registry_path=None,
                codex_bin="codex",
                model=None,
                sandbox=None,
                profile=None,
                headless=True,
            )
            session = OrchestratorSession(
                session_id="session-1",
                binding_id="binding-1",
                repo_path="/tmp/repo",
                workspace_path="/tmp/repo",
                chat_url="https://chatgpt.com/c/project/binding-1",
                current_codex_thread_id="parent-thread-123",
            )
            binding = ChatBinding(
                binding_id="binding-1",
                project_name="bridge",
                repo_path="/tmp/repo",
                workspace_path="/tmp/repo",
                chat_url="https://chatgpt.com/c/project/binding-1",
            )

            report = runner.executor(
                prompt="Continue.",
                thread_action="fork_thread",
                session=session,
                binding=binding,
                instructions=[],
            )

            self.assertEqual(report.thread_operation, "app_server_fork")
            self.assertEqual(report.parent_thread_id, "parent-thread-123")
            self.assertEqual(report.requested_codex_thread_id, "forked-thread-123")
            fork_mock.assert_called_once()
            start_mock.assert_not_called()

    def test_handle_resume_session_rearms_autoloop_and_clears_blockers(self):
        from argparse import Namespace
        from io import StringIO

        from mastermind_bridge.cli import handle_resume_session
        from mastermind_bridge.orchestrator.models import LoopPolicyDecision, OrchestratorSession
        from mastermind_bridge.orchestrator.state import load_session, save_session, session_path

        with tempfile.TemporaryDirectory() as tmp_dir:
            sessions_dir = Path(tmp_dir) / "sessions"
            sessions_dir.mkdir()
            session = OrchestratorSession(
                session_id="session-1",
                binding_id="binding-1",
                repo_path="/tmp/repo",
                workspace_path="/tmp/repo",
                chat_url="https://chatgpt.com/c/project/binding-1",
                status="paused",
                loop_state="requires_human",
                auto_run_enabled=False,
                supervisor_status="blocked",
                human_attention_reason="Browser transport failed.",
                last_error="Browser transport failed.",
                time_budget_minutes=120,
                budget_remaining_minutes=118,
                policy_decision=LoopPolicyDecision(
                    policy_outcome="require_human",
                    reasons=["Browser transport failed."],
                    human_gate_required=True,
                    human_gate_reason="Browser transport failed.",
                    human_gate_category="bridge_control_or_browser_error",
                    time_budget_minutes=120,
                    time_budget_remaining_minutes=118,
                ),
            )
            save_session(session_path(sessions_dir, session.session_id), session)
            stdout = StringIO()
            args = Namespace(session_id="session-1", sessions_dir=sessions_dir)

            with unittest.mock.patch("sys.stdout", stdout):
                result = handle_resume_session(args)

            self.assertEqual(result, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["status"], "active")
            self.assertIn("supervise-session --session-id session-1", payload["next_step"])

            refreshed = load_session(session_path(sessions_dir, session.session_id))
            self.assertEqual(refreshed.status, "active")
            self.assertEqual(refreshed.loop_state, "idle")
            self.assertTrue(refreshed.auto_run_enabled)
            self.assertEqual(refreshed.supervisor_status, "running")
            self.assertEqual(refreshed.latest_user_control_command, "")
            self.assertEqual(refreshed.human_attention_reason, "")
            self.assertEqual(refreshed.last_error, "")

    def test_handle_pause_drains_pending_return_packet_retry_state(self):
        from argparse import Namespace
        from io import StringIO

        from mastermind_bridge.cli import handle_pause
        from mastermind_bridge.orchestrator.models import OrchestratorSession
        from mastermind_bridge.orchestrator.state import load_session, save_session, session_path

        with tempfile.TemporaryDirectory() as tmp_dir:
            sessions_dir = Path(tmp_dir) / "sessions"
            sessions_dir.mkdir()
            session = OrchestratorSession(
                session_id="session-1",
                binding_id="binding-1",
                repo_path="/tmp/repo",
                workspace_path="/tmp/repo",
                chat_url="https://chatgpt.com/c/project/binding-1",
                status="active",
                loop_state="posting_return_packet",
                auto_run_enabled=True,
                supervisor_status="running",
                last_outbound_user_message_anchor="packet-stuck",
                last_outbound_user_message_kind="return_packet_retry_pending",
                degraded_mode="retrying_return_packet",
                degraded_reason="Message delivery confirmation timed out.",
            )
            save_session(session_path(sessions_dir, session.session_id), session)
            stdout = StringIO()
            args = Namespace(session_id="session-1", sessions_dir=sessions_dir)

            with unittest.mock.patch("sys.stdout", stdout):
                result = handle_pause(args)

            self.assertEqual(result, 0)
            refreshed = load_session(session_path(sessions_dir, session.session_id))
            self.assertEqual(refreshed.status, "active")
            self.assertEqual(refreshed.loop_state, "posting_return_packet")
            self.assertTrue(refreshed.auto_run_enabled)
            self.assertEqual(refreshed.supervisor_status, "running")
            self.assertEqual(refreshed.latest_user_control_command, "pause")
            self.assertEqual(refreshed.last_outbound_user_message_anchor, "packet-stuck")
            self.assertEqual(refreshed.last_outbound_user_message_kind, "return_packet_retry_pending")
            self.assertEqual(refreshed.degraded_mode, "retrying_return_packet")
            self.assertEqual(refreshed.degraded_reason, "Message delivery confirmation timed out.")
            self.assertEqual(refreshed.policy_decision.policy_outcome, "paused")
            self.assertIn("drain", refreshed.policy_decision.reasons[0])

    def test_handle_stop_terminates_locked_supervisor_process(self):
        from argparse import Namespace
        from io import StringIO

        from mastermind_bridge.cli import handle_stop
        from mastermind_bridge.orchestrator.models import OrchestratorSession
        from mastermind_bridge.orchestrator.state import load_session, save_session, session_path

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            sessions_dir = root / "sessions"
            sessions_dir.mkdir()
            lock_dir = root / "session_locks"
            lock_dir.mkdir()
            session = OrchestratorSession(
                session_id="session-1",
                binding_id="binding-1",
                repo_path="/tmp/repo",
                workspace_path="/tmp/repo",
                chat_url="https://chatgpt.com/c/project/binding-1",
                status="active",
                loop_state="waiting_for_chatgpt_response",
                auto_run_enabled=True,
                supervisor_status="running",
            )
            save_session(session_path(sessions_dir, session.session_id), session)
            stdout = StringIO()
            args = Namespace(session_id="session-1", sessions_dir=sessions_dir, after_cycle=False)

            with (
                unittest.mock.patch(
                    "mastermind_bridge.cli.terminate_locked_session_supervisor",
                    return_value={"status": "terminated", "pid": 12345, "lock_removed": True},
                ) as terminator,
                unittest.mock.patch("sys.stdout", stdout),
            ):
                result = handle_stop(args)

            self.assertEqual(result, 0)
            terminator.assert_not_called()
            payload = json.loads(stdout.getvalue())
            self.assertNotIn("supervisor_termination", payload)
            refreshed = load_session(session_path(sessions_dir, session.session_id))
            self.assertEqual(refreshed.status, "active")
            self.assertEqual(refreshed.loop_state, "waiting_for_chatgpt_response")
            self.assertTrue(refreshed.stop_after_cycle_requested)
            self.assertTrue(refreshed.auto_run_enabled)
            self.assertEqual(refreshed.supervisor_status, "running")

    def test_handle_resume_session_rearms_latest_assistant_after_paused_codex_run(self):
        from argparse import Namespace
        from io import StringIO

        from mastermind_bridge.cli import handle_resume_session
        from mastermind_bridge.orchestrator.models import LoopPolicyDecision, OrchestratorSession
        from mastermind_bridge.orchestrator.state import load_session, save_session, session_path

        with tempfile.TemporaryDirectory() as tmp_dir:
            sessions_dir = Path(tmp_dir) / "sessions"
            sessions_dir.mkdir()
            session = OrchestratorSession(
                session_id="session-1",
                binding_id="binding-1",
                repo_path="/tmp/repo",
                workspace_path="/tmp/repo",
                chat_url="https://chatgpt.com/c/project/binding-1",
                status="paused",
                loop_state="paused",
                auto_run_enabled=False,
                supervisor_status="paused",
                current_codex_thread_id="codex-thread-789",
                current_codex_run_id="codex-thread-789",
                last_seen_chat_message_anchor="assistant-4-abc123",
                latest_assistant_message_id="assistant-message-1",
                latest_assistant_message_hash="abc123",
                last_chat_activity_at="2026-04-21T16:16:11+02:00",
                time_budget_minutes=120,
                budget_remaining_minutes=118,
                policy_decision=LoopPolicyDecision(
                    policy_outcome="paused",
                    reasons=["Pause requested while Codex was running."],
                    time_budget_minutes=120,
                    time_budget_remaining_minutes=118,
                ),
            )
            save_session(session_path(sessions_dir, session.session_id), session)
            stdout = StringIO()
            args = Namespace(session_id="session-1", sessions_dir=sessions_dir)

            with unittest.mock.patch("sys.stdout", stdout):
                result = handle_resume_session(args)

            self.assertEqual(result, 0)
            refreshed = load_session(session_path(sessions_dir, session.session_id))
            self.assertEqual(refreshed.last_seen_chat_message_anchor, "")
            self.assertEqual(refreshed.latest_assistant_message_id, "")
            self.assertEqual(refreshed.latest_assistant_message_hash, "")

    def test_handle_resume_session_rearms_retryable_error_prompt_state(self):
        from argparse import Namespace
        from io import StringIO

        from mastermind_bridge.cli import handle_resume_session
        from mastermind_bridge.orchestrator.models import LoopPolicyDecision, OrchestratorSession
        from mastermind_bridge.orchestrator.state import load_session, save_session, session_path

        with tempfile.TemporaryDirectory() as tmp_dir:
            sessions_dir = Path(tmp_dir) / "sessions"
            sessions_dir.mkdir()
            assistant_error = "A network error occurred. Please check your connection and try again.\n\nErneut versuchen"
            session = OrchestratorSession(
                session_id="session-1",
                binding_id="binding-1",
                repo_path="/tmp/repo",
                workspace_path="/tmp/repo",
                chat_url="https://chatgpt.com/c/project/binding-1",
                status="paused",
                loop_state="paused",
                auto_run_enabled=False,
                supervisor_status="paused",
                last_seen_chat_message_anchor="assistant-4-abc123",
                latest_assistant_message_id="assistant-message-1",
                latest_assistant_message_hash="abc123",
                last_productive_prompt=assistant_error,
                last_productive_task_label="accepted_assistant_text",
                time_budget_minutes=120,
                budget_remaining_minutes=118,
                policy_decision=LoopPolicyDecision(
                    policy_outcome="paused",
                    reasons=["Pause requested while Codex was running."],
                    time_budget_minutes=120,
                    time_budget_remaining_minutes=118,
                ),
            )
            save_session(session_path(sessions_dir, session.session_id), session)
            stdout = StringIO()
            args = Namespace(session_id="session-1", sessions_dir=sessions_dir)

            with unittest.mock.patch("sys.stdout", stdout):
                result = handle_resume_session(args)

            self.assertEqual(result, 0)
            refreshed = load_session(session_path(sessions_dir, session.session_id))
            self.assertEqual(refreshed.last_seen_chat_message_anchor, "")
            self.assertEqual(refreshed.latest_assistant_message_id, "")
            self.assertEqual(refreshed.latest_assistant_message_hash, "")

    def test_handle_queue_instruction_persists_next_run_update(self):
        from argparse import Namespace
        from io import StringIO

        from mastermind_bridge.cli import handle_queue_instruction
        from mastermind_bridge.orchestrator.models import OrchestratorSession
        from mastermind_bridge.orchestrator.state import load_session, save_session, session_path

        with tempfile.TemporaryDirectory() as tmp_dir:
            sessions_dir = Path(tmp_dir) / "sessions"
            sessions_dir.mkdir()
            session = OrchestratorSession(
                session_id="session-1",
                binding_id="binding-1",
                repo_path="/tmp/repo",
                workspace_path="/tmp/repo",
                chat_url="https://chatgpt.com/c/project/binding-1",
            )
            save_session(session_path(sessions_dir, session.session_id), session)
            stdout = StringIO()
            args = Namespace(
                session_id="session-1",
                sessions_dir=sessions_dir,
                scope="next_run",
                mode="append",
                text="Drive progress must be real and verified against repo-native state.",
            )

            with unittest.mock.patch("sys.stdout", stdout):
                result = handle_queue_instruction(args)

            self.assertEqual(result, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["session_id"], "session-1")
            self.assertEqual(payload["scope"], "next_run")
            self.assertEqual(payload["mode"], "append")
            self.assertEqual(payload["instruction_count"], 1)

            refreshed = load_session(session_path(sessions_dir, session.session_id))
            self.assertEqual(len(refreshed.instruction_updates), 1)
            self.assertEqual(refreshed.instruction_updates[0].scope, "next_run")
            self.assertEqual(refreshed.instruction_updates[0].mode, "append")
            self.assertEqual(
                refreshed.instruction_updates[0].text,
                "Drive progress must be real and verified against repo-native state.",
            )

    @unittest.mock.patch.dict(os.environ, {"BRIDGE_ENABLE_CODEX_APP_INTEGRATION": "0"}, clear=False)
    def test_handle_run_recovery_prefers_queued_instruction_prompt(self):
        from argparse import Namespace
        from io import StringIO

        from mastermind_bridge.cli import handle_run_recovery
        from mastermind_bridge.orchestrator.models import ChatBinding, InstructionScopeUpdate, OrchestratorSession
        from mastermind_bridge.orchestrator.state import (
            load_session,
            save_session,
            session_path,
            upsert_chat_binding,
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            workspace = root / "workspace"
            workspace.mkdir()
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            artifacts_root = root / "artifacts"
            sessions_dir.mkdir()
            fake_codex = self._write_fake_codex(root)
            binding = ChatBinding(
                binding_id="binding-1",
                project_name="pab",
                repo_path=str(workspace),
                workspace_path=str(workspace),
                chat_url="https://chatgpt.com/c/project/binding-1",
            )
            upsert_chat_binding(bindings_path, binding)
            policy_path.write_text(json.dumps({"project_instruction_updates": [], "stop_phrases": []}), encoding="utf-8")
            session = OrchestratorSession(
                session_id="session-1",
                binding_id="binding-1",
                repo_path=str(workspace),
                workspace_path=str(workspace),
                chat_url=binding.chat_url,
                in_progress_assistant_text=(
                    "bridge-control\n"
                    'protocol_version: "1.0"\n'
                    'session_id: "session-1"\n'
                    'decision: "run_codex"\n'
                    'codex_thread_action: "new_thread"\n'
                    'task_label: "stale_prompt"\n'
                    "prompt: |\n"
                    "  This older prompt should not win.\n"
                ),
                instruction_updates=[
                    InstructionScopeUpdate(
                        scope="next_run",
                        mode="replace",
                        text="Reproduce and fix the readonly status path first.",
                    )
                ],
            )
            save_session(session_path(sessions_dir, session.session_id), session)
            stdout = StringIO()
            args = Namespace(
                session_id="session-1",
                bindings=bindings_path,
                policy=policy_path,
                sessions_dir=sessions_dir,
                artifacts_root=artifacts_root,
                log_file=None,
                registry=None,
                codex_bin=str(fake_codex),
                model=None,
                reasoning_effort=None,
                sandbox=None,
                profile=None,
            )

            with unittest.mock.patch("sys.stdout", stdout):
                result = handle_run_recovery(args)

            self.assertEqual(result, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["prompt_source"], "queued_instruction")
            self.assertEqual(payload["thread_action"], "new_thread")
            self.assertEqual(payload["runner_action"], "offline_recovery_executed")
            self.assertEqual(payload["codex_thread_id"], "exec-thread-xyz")

            refreshed = load_session(session_path(sessions_dir, session.session_id))
            self.assertEqual(refreshed.current_codex_thread_id, "exec-thread-xyz")
            self.assertEqual(refreshed.last_thread_action, "new_thread")
            self.assertTrue(refreshed.last_codex_activity_at)
            self.assertEqual(refreshed.loop_state, "starting_codex")
            self.assertTrue(refreshed.auto_run_enabled)

            live_output_path = Path(payload["artifacts_dir"]) / "live_output.log"
            self.assertIn("STDOUT |", live_output_path.read_text(encoding="utf-8"))

            prompt_path = sessions_dir.parent / "runtime_prompts" / "session-1" / "NEXT_PROMPT.md"
            prompt_text = prompt_path.read_text(encoding="utf-8")
            self.assertIn("This is a local recovery Codex execution", prompt_text)
            self.assertIn("Reproduce and fix the readonly status path first.", prompt_text)
            self.assertNotIn("This older prompt should not win.", prompt_text)

    @unittest.mock.patch.dict(os.environ, {"BRIDGE_ENABLE_CODEX_APP_INTEGRATION": "0"}, clear=False)
    @unittest.mock.patch("mastermind_bridge.cli.execute_codex_prompt")
    def test_handle_run_recovery_resumes_recorded_codex_thread_when_available(self, execute_mock):
        from argparse import Namespace
        from io import StringIO

        from mastermind_bridge.cli import handle_run_recovery
        from mastermind_bridge.models import RunReport
        from mastermind_bridge.orchestrator.models import ChatBinding, InstructionScopeUpdate, OrchestratorSession
        from mastermind_bridge.orchestrator.state import (
            load_session,
            save_session,
            session_path,
            upsert_chat_binding,
        )

        execute_mock.return_value = (
            RunReport.from_dict(
                {
                    "timestamp": "2026-05-03T12:00:00+02:00",
                    "thread_id": "session-1",
                    "summary": "Recovered in existing thread.",
                    "observed_codex_thread_id": "codex-thread-existing",
                    "files_touched": [],
                    "checks": [],
                    "blockers": [],
                    "risks": [],
                    "next_step": "",
                }
            ),
            {},
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            workspace = root / "workspace"
            workspace.mkdir()
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            artifacts_root = root / "artifacts"
            sessions_dir.mkdir()
            binding = ChatBinding(
                binding_id="binding-1",
                project_name="pab",
                repo_path=str(workspace),
                workspace_path=str(workspace),
                chat_url="https://chatgpt.com/c/project/binding-1",
            )
            upsert_chat_binding(bindings_path, binding)
            policy_path.write_text(json.dumps({"project_instruction_updates": [], "stop_phrases": []}), encoding="utf-8")
            session = OrchestratorSession(
                session_id="session-1",
                binding_id="binding-1",
                repo_path=str(workspace),
                workspace_path=str(workspace),
                chat_url=binding.chat_url,
                current_codex_thread_id="codex-thread-existing",
                current_codex_run_id="codex-thread-existing",
                instruction_updates=[
                    InstructionScopeUpdate(
                        scope="next_run",
                        mode="replace",
                        text="Continue the current product frontier in the existing Codex thread.",
                    )
                ],
            )
            save_session(session_path(sessions_dir, session.session_id), session)
            stdout = StringIO()
            args = Namespace(
                session_id="session-1",
                bindings=bindings_path,
                policy=policy_path,
                sessions_dir=sessions_dir,
                artifacts_root=artifacts_root,
                log_file=None,
                registry=None,
                codex_bin="codex",
                model=None,
                reasoning_effort=None,
                sandbox=None,
                profile=None,
            )

            with unittest.mock.patch("sys.stdout", stdout):
                result = handle_run_recovery(args)

            self.assertEqual(result, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["prompt_source"], "queued_instruction")
            self.assertEqual(payload["thread_action"], "same_thread")
            self.assertEqual(payload["codex_thread_id"], "codex-thread-existing")
            self.assertEqual(execute_mock.call_args.kwargs["resume_session_id"], "codex-thread-existing")
            self.assertIsNone(execute_mock.call_args.kwargs["env"])

            refreshed = load_session(session_path(sessions_dir, session.session_id))
            self.assertEqual(refreshed.current_codex_thread_id, "codex-thread-existing")
            self.assertEqual(refreshed.last_thread_action, "same_thread")
            self.assertEqual(refreshed.loop_state, "starting_codex")

    @unittest.mock.patch.dict(os.environ, {"BRIDGE_ENABLE_CODEX_APP_INTEGRATION": "0"}, clear=False)
    def test_handle_run_recovery_falls_back_to_stored_assistant_prompt(self):
        from argparse import Namespace
        from io import StringIO

        from mastermind_bridge.cli import handle_run_recovery
        from mastermind_bridge.orchestrator.models import ChatBinding, OrchestratorSession
        from mastermind_bridge.orchestrator.state import save_session, session_path, upsert_chat_binding

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            workspace = root / "workspace"
            workspace.mkdir()
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            artifacts_root = root / "artifacts"
            sessions_dir.mkdir()
            fake_codex = self._write_fake_codex(root)
            binding = ChatBinding(
                binding_id="binding-1",
                project_name="pab",
                repo_path=str(workspace),
                workspace_path=str(workspace),
                chat_url="https://chatgpt.com/c/project/binding-1",
            )
            upsert_chat_binding(bindings_path, binding)
            policy_path.write_text(json.dumps({"project_instruction_updates": [], "stop_phrases": []}), encoding="utf-8")
            session = OrchestratorSession(
                session_id="session-1",
                binding_id="binding-1",
                repo_path=str(workspace),
                workspace_path=str(workspace),
                chat_url=binding.chat_url,
                in_progress_assistant_text=(
                    "bridge-control\n"
                    'protocol_version: "1.0"\n'
                    'session_id: "session-1"\n'
                    'decision: "run_codex"\n'
                    'codex_thread_action: "new_thread"\n'
                    'task_label: "direct_recovery"\n'
                    "prompt: |\n"
                    "  Focus on the concrete repo bug now.\n"
                ),
            )
            save_session(session_path(sessions_dir, session.session_id), session)
            stdout = StringIO()
            args = Namespace(
                session_id="session-1",
                bindings=bindings_path,
                policy=policy_path,
                sessions_dir=sessions_dir,
                artifacts_root=artifacts_root,
                log_file=None,
                registry=None,
                codex_bin=str(fake_codex),
                model=None,
                reasoning_effort=None,
                sandbox=None,
                profile=None,
            )

            with unittest.mock.patch("sys.stdout", stdout):
                result = handle_run_recovery(args)

            self.assertEqual(result, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["prompt_source"], "stored_assistant")
            self.assertEqual(payload["task_label"], "direct_recovery")

            prompt_path = sessions_dir.parent / "runtime_prompts" / "session-1" / "NEXT_PROMPT.md"
            prompt_text = prompt_path.read_text(encoding="utf-8")
            self.assertIn("Focus on the concrete repo bug now.", prompt_text)

    @unittest.mock.patch.dict(os.environ, {"BRIDGE_ENABLE_CODEX_APP_INTEGRATION": "0"}, clear=False)
    def test_handle_run_recovery_ignores_paused_session_state_for_local_run(self):
        from argparse import Namespace
        from io import StringIO

        from mastermind_bridge.cli import handle_run_recovery
        from mastermind_bridge.orchestrator.models import ChatBinding, InstructionScopeUpdate, OrchestratorSession
        from mastermind_bridge.orchestrator.state import save_session, session_path, upsert_chat_binding

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            workspace = root / "workspace"
            workspace.mkdir()
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            artifacts_root = root / "artifacts"
            sessions_dir.mkdir()
            fake_codex = self._write_fake_codex(root)
            binding = ChatBinding(
                binding_id="binding-1",
                project_name="pab",
                repo_path=str(workspace),
                workspace_path=str(workspace),
                chat_url="https://chatgpt.com/c/project/binding-1",
            )
            upsert_chat_binding(bindings_path, binding)
            policy_path.write_text(json.dumps({"project_instruction_updates": [], "stop_phrases": []}), encoding="utf-8")
            session = OrchestratorSession(
                session_id="session-1",
                binding_id="binding-1",
                repo_path=str(workspace),
                workspace_path=str(workspace),
                chat_url=binding.chat_url,
                status="paused",
                loop_state="paused",
                supervisor_status="paused",
                instruction_updates=[
                    InstructionScopeUpdate(
                        scope="next_run",
                        mode="replace",
                        text="Fix the readonly status path first.",
                    )
                ],
            )
            save_session(session_path(sessions_dir, session.session_id), session)
            stdout = StringIO()
            args = Namespace(
                session_id="session-1",
                bindings=bindings_path,
                policy=policy_path,
                sessions_dir=sessions_dir,
                artifacts_root=artifacts_root,
                log_file=None,
                registry=None,
                codex_bin=str(fake_codex),
                model=None,
                reasoning_effort=None,
                sandbox=None,
                profile=None,
            )

            with unittest.mock.patch("sys.stdout", stdout):
                result = handle_run_recovery(args)

            self.assertEqual(result, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["runner_action"], "offline_recovery_executed")
            self.assertEqual(payload["thread_action"], "new_thread")

    @unittest.mock.patch.dict(os.environ, {"BRIDGE_ENABLE_CODEX_APP_INTEGRATION": "0"}, clear=False)
    def test_handle_run_recovery_marks_session_blocked_when_local_codex_fails(self):
        from argparse import Namespace
        from io import StringIO

        from mastermind_bridge.cli import handle_run_recovery
        from mastermind_bridge.orchestrator.models import ChatBinding, InstructionScopeUpdate, OrchestratorSession
        from mastermind_bridge.orchestrator.state import load_session, save_session, session_path, upsert_chat_binding

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            workspace = root / "workspace"
            workspace.mkdir()
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            artifacts_root = root / "artifacts"
            sessions_dir.mkdir()
            fake_codex = self._write_fake_codex(root)
            binding = ChatBinding(
                binding_id="binding-1",
                project_name="pab",
                repo_path=str(workspace),
                workspace_path=str(workspace),
                chat_url="https://chatgpt.com/c/project/binding-1",
            )
            upsert_chat_binding(bindings_path, binding)
            policy_path.write_text(json.dumps({"project_instruction_updates": [], "stop_phrases": []}), encoding="utf-8")
            session = OrchestratorSession(
                session_id="session-1",
                binding_id="binding-1",
                repo_path=str(workspace),
                workspace_path=str(workspace),
                chat_url=binding.chat_url,
                instruction_updates=[
                    InstructionScopeUpdate(
                        scope="next_run",
                        mode="replace",
                        text="Fix the readonly status path first.",
                    )
                ],
            )
            save_session(session_path(sessions_dir, session.session_id), session)
            stdout = StringIO()
            args = Namespace(
                session_id="session-1",
                bindings=bindings_path,
                policy=policy_path,
                sessions_dir=sessions_dir,
                artifacts_root=artifacts_root,
                log_file=None,
                registry=None,
                codex_bin=str(fake_codex),
                model=None,
                reasoning_effort=None,
                sandbox=None,
                profile=None,
            )

            with (
                unittest.mock.patch("sys.stdout", stdout),
                unittest.mock.patch.dict(
                    os.environ,
                    {
                        "FAKE_CODEX_EXIT": "1",
                        "FAKE_CODEX_STDERR": (
                            "2026-04-17T07:59:07.725331Z ERROR codex_api::endpoint::responses_websocket: "
                            "failed to connect to websocket: IO error: failed to lookup address information: "
                            "nodename nor servname provided, or not known, url: wss://api.openai.com/v1/responses\n"
                        ),
                    },
                    clear=False,
                ),
            ):
                result = handle_run_recovery(args)

            self.assertEqual(result, 1)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["runner_action"], "offline_recovery_executed")
            self.assertEqual(payload["exit_code"], 1)

            refreshed = load_session(session_path(sessions_dir, session.session_id))
            self.assertEqual(refreshed.status, "blocked")
            self.assertFalse(refreshed.auto_run_enabled)
            self.assertEqual(refreshed.loop_state, "requires_human")
            self.assertEqual(refreshed.supervisor_status, "blocked")
            self.assertEqual(
                refreshed.human_attention_reason,
                "Codex could not reach the OpenAI API because network or DNS access was unavailable from this process.",
            )
            self.assertEqual(refreshed.policy_decision.policy_outcome, "require_human")


if __name__ == "__main__":
    unittest.main()
