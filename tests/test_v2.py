import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


class V2CliTests(unittest.TestCase):
    def _run_cli(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        effective_env = os.environ.copy()
        if env:
            effective_env.update(env)
        return subprocess.run(
            [sys.executable, "-m", "mastermind_bridge.cli", "v2", *args],
            capture_output=True,
            text=True,
            check=False,
            env=effective_env,
        )

    def test_session_create_and_status(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            repo_path = tmp_path / "repo"
            repo_path.mkdir()
            db_path = tmp_path / "state" / "supervisor_v2.sqlite3"

            created = self._run_cli(
                "session",
                "create",
                "--db",
                str(db_path),
                "--repo-path",
                str(repo_path),
                "--workspace-path",
                str(repo_path),
                "--operator-goal",
                "Ship Supervisor V2",
            )

            self.assertEqual(created.returncode, 0, msg=created.stderr)
            created_payload = json.loads(created.stdout)
            self.assertEqual(created_payload["session"]["status"], "manual_bootstrap")
            self.assertEqual(created_payload["session"]["chatgpt_model"], "gpt-5.5")
            self.assertEqual(created_payload["session"]["chatgpt_reasoning_effort"], "xhigh")
            self.assertEqual(created_payload["session"]["codex_model"], "gpt-5.5")
            self.assertEqual(created_payload["session"]["codex_reasoning_effort"], "xhigh")
            session_id = created_payload["session"]["session_id"]

            status = self._run_cli(
                "session",
                "status",
                "--db",
                str(db_path),
                "--session-id",
                session_id,
            )

            self.assertEqual(status.returncode, 0, msg=status.stderr)
            status_payload = json.loads(status.stdout)
            self.assertEqual(status_payload["session"]["session_id"], session_id)
            self.assertEqual(status_payload["session"]["status"], "manual_bootstrap")
            self.assertEqual(status_payload["turns"]["queued_count"], 0)
            self.assertEqual(status_payload["turns"]["running_count"], 0)

    def test_custom_db_defaults_artifacts_next_to_database(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            repo_path = tmp_path / "repo"
            repo_path.mkdir()
            db_path = tmp_path / "state" / "supervisor_v2.sqlite3"
            session_id = "session-cli-artifacts-test"
            repo_artifact_dir = Path.cwd() / "artifacts" / "v2" / session_id
            self.addCleanup(lambda: shutil.rmtree(repo_artifact_dir, ignore_errors=True))

            created = self._run_cli(
                "session",
                "create",
                "--db",
                str(db_path),
                "--repo-path",
                str(repo_path),
                "--workspace-path",
                str(repo_path),
                "--operator-goal",
                "Keep custom-db artifacts isolated",
                "--session-id",
                session_id,
            )
            self.assertEqual(created.returncode, 0, msg=created.stderr)

            bootstrapped = self._run_cli(
                "session",
                "bootstrap",
                "chatgpt",
                "--db",
                str(db_path),
                "--session-id",
                session_id,
            )
            self.assertEqual(bootstrapped.returncode, 0, msg=bootstrapped.stderr)

            fake_chatgpt_response = json.dumps(
                {
                    "decision": "pause",
                    "codex_thread_mode": "resume_current",
                    "codex_prompt": "",
                    "summary": "Wait for the operator.",
                    "reasoning": "No code turn is needed.",
                    "needs_human_reason": "",
                }
            )
            started = self._run_cli(
                "session",
                "start",
                "--db",
                str(db_path),
                "--session-id",
                session_id,
                "--max-turns",
                "1",
                env={"BRIDGE_V2_FAKE_CHATGPT_RESPONSE": fake_chatgpt_response},
            )
            self.assertEqual(started.returncode, 0, msg=started.stderr)

            artifact_dir = tmp_path / "artifacts" / "v2" / session_id / "0001-chatgpt"
            self.assertTrue((artifact_dir / "worker_input.json").exists())
            self.assertTrue((artifact_dir / "worker_result.json").exists())
            self.assertFalse(repo_artifact_dir.exists())

    def test_session_create_and_configure_persist_execution_settings_and_context_files(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            repo_path = tmp_path / "repo"
            repo_path.mkdir()
            context_path = repo_path / "MASTER_PLAN.md"
            context_path.write_text("# Master Plan\nKeep the loop stable.\n", encoding="utf-8")
            db_path = tmp_path / "state" / "supervisor_v2.sqlite3"

            created = self._run_cli(
                "session",
                "create",
                "--db",
                str(db_path),
                "--repo-path",
                str(repo_path),
                "--workspace-path",
                str(repo_path),
                "--operator-goal",
                "Ship Supervisor V2",
                "--chatgpt-model",
                "gpt-5.4",
                "--chatgpt-reasoning-effort",
                "high",
                "--codex-model",
                "gpt-5.4-mini",
                "--codex-reasoning-effort",
                "medium",
                "--codex-execution-mode",
                "cli_only",
                "--context-file",
                str(context_path),
            )

            self.assertEqual(created.returncode, 0, msg=created.stderr)
            session_id = json.loads(created.stdout)["session"]["session_id"]

            configured = self._run_cli(
                "session",
                "configure",
                "--db",
                str(db_path),
                "--session-id",
                session_id,
                "--chatgpt-model",
                "gpt-5.4-thinking",
                "--chatgpt-reasoning-effort",
                "xhigh",
                "--codex-model",
                "gpt-5.4",
                "--codex-reasoning-effort",
                "high",
            )
            self.assertEqual(configured.returncode, 0, msg=configured.stderr)

            status = self._run_cli(
                "session",
                "status",
                "--db",
                str(db_path),
                "--session-id",
                session_id,
            )
            self.assertEqual(status.returncode, 0, msg=status.stderr)
            payload = json.loads(status.stdout)
            self.assertEqual(payload["session"]["chatgpt_model"], "gpt-5.4-thinking")
            self.assertEqual(payload["session"]["chatgpt_reasoning_effort"], "xhigh")
            self.assertEqual(payload["session"]["codex_model"], "gpt-5.4")
            self.assertEqual(payload["session"]["codex_reasoning_effort"], "high")
            self.assertEqual(payload["session"]["codex_execution_mode"], "cli_only")
            self.assertEqual(payload["session"]["context_files"], [str(context_path)])

    def test_allow_app_execution_mode_requires_macos_app_profile(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            repo_path = tmp_path / "repo"
            repo_path.mkdir()
            db_path = tmp_path / "state" / "supervisor_v2.sqlite3"

            blocked = self._run_cli(
                "session",
                "create",
                "--db",
                str(db_path),
                "--repo-path",
                str(repo_path),
                "--workspace-path",
                str(repo_path),
                "--operator-goal",
                "Try app-server mode.",
                "--codex-execution-mode",
                "allow_app",
                env={"BRIDGE_PROFILE": "core-safe"},
            )
            self.assertEqual(blocked.returncode, 2)
            self.assertEqual(json.loads(blocked.stderr)["required_profile"], "macos-app")

            allowed = self._run_cli(
                "session",
                "create",
                "--db",
                str(db_path),
                "--repo-path",
                str(repo_path),
                "--workspace-path",
                str(repo_path),
                "--operator-goal",
                "Use app-server mode explicitly.",
                "--codex-execution-mode",
                "allow_app",
                env={"BRIDGE_PROFILE": "macos-app"},
            )
            self.assertEqual(allowed.returncode, 0, msg=allowed.stderr)
            allowed_payload = json.loads(allowed.stdout)
            self.assertEqual(allowed_payload["session"]["codex_execution_mode"], "allow_app")

            blocked_start = self._run_cli(
                "session",
                "start",
                "--db",
                str(db_path),
                "--session-id",
                allowed_payload["session"]["session_id"],
                "--max-turns",
                "1",
                env={"BRIDGE_PROFILE": "core-safe"},
            )
            self.assertEqual(blocked_start.returncode, 2)
            self.assertEqual(json.loads(blocked_start.stderr)["error"], "codex_execution_mode_profile_required")

    def test_configure_rejects_allow_app_execution_mode_without_macos_app_profile(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            repo_path = tmp_path / "repo"
            repo_path.mkdir()
            db_path = tmp_path / "state" / "supervisor_v2.sqlite3"

            created = self._run_cli(
                "session",
                "create",
                "--db",
                str(db_path),
                "--repo-path",
                str(repo_path),
                "--workspace-path",
                str(repo_path),
                "--operator-goal",
                "Keep V2 core-safe.",
            )
            self.assertEqual(created.returncode, 0, msg=created.stderr)
            session_id = json.loads(created.stdout)["session"]["session_id"]

            configured = self._run_cli(
                "session",
                "configure",
                "--db",
                str(db_path),
                "--session-id",
                session_id,
                "--codex-execution-mode",
                "allow_app",
                env={"BRIDGE_PROFILE": "core-safe"},
            )
            self.assertEqual(configured.returncode, 2)
            self.assertEqual(json.loads(configured.stderr)["error"], "codex_execution_mode_profile_required")

    def test_store_rejects_unknown_codex_execution_mode(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)

            from mastermind_bridge.v2.store import V2Store

            store = V2Store(tmp_path / "state" / "supervisor_v2.sqlite3")
            with self.assertRaisesRegex(ValueError, "invalid codex_execution_mode"):
                store.create_session(
                    repo_path=tmp_path,
                    workspace_path=tmp_path,
                    operator_goal="Reject unsafe execution modes.",
                    codex_execution_mode="surprise-app-mode",
                )

    def test_bootstrap_chatgpt_payload_includes_context_files_and_session_settings(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            repo_path = tmp_path / "repo"
            repo_path.mkdir()
            context_path = repo_path / "MASTER_PLAN.md"
            context_path.write_text("# Plan\nDo not lose the thread.\n", encoding="utf-8")
            db_path = tmp_path / "state" / "supervisor_v2.sqlite3"

            created = self._run_cli(
                "session",
                "create",
                "--db",
                str(db_path),
                "--repo-path",
                str(repo_path),
                "--workspace-path",
                str(repo_path),
                "--operator-goal",
                "Feed stable context into ChatGPT",
                "--chatgpt-model",
                "gpt-5.4",
                "--chatgpt-reasoning-effort",
                "high",
                "--context-file",
                str(context_path),
            )
            self.assertEqual(created.returncode, 0, msg=created.stderr)
            session_id = json.loads(created.stdout)["session"]["session_id"]

            bootstrap = self._run_cli(
                "session",
                "bootstrap",
                "chatgpt",
                "--db",
                str(db_path),
                "--session-id",
                session_id,
            )
            self.assertEqual(bootstrap.returncode, 0, msg=bootstrap.stderr)

            status = self._run_cli(
                "session",
                "status",
                "--db",
                str(db_path),
                "--session-id",
                session_id,
            )
            self.assertEqual(status.returncode, 0, msg=status.stderr)
            payload = json.loads(status.stdout)
            pending_payload = payload["pending_turn"]["payload"]
            self.assertEqual(pending_payload["chatgpt_model"], "gpt-5.4")
            self.assertEqual(pending_payload["chatgpt_reasoning_effort"], "high")
            self.assertEqual(
                pending_payload["context_files"],
                [
                    {
                        "path": str(context_path),
                        "content": "# Plan\nDo not lose the thread.\n",
                    }
                ],
            )

    def test_manual_bootstrap_chatgpt_turn_commits_without_autorun(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            repo_path = tmp_path / "repo"
            repo_path.mkdir()
            db_path = tmp_path / "state" / "supervisor_v2.sqlite3"

            create = self._run_cli(
                "session",
                "create",
                "--db",
                str(db_path),
                "--repo-path",
                str(repo_path),
                "--workspace-path",
                str(repo_path),
                "--operator-goal",
                "Prepare a coding plan",
            )
            self.assertEqual(create.returncode, 0, msg=create.stderr)
            session_id = json.loads(create.stdout)["session"]["session_id"]

            bootstrap = self._run_cli(
                "session",
                "bootstrap",
                "chatgpt",
                "--db",
                str(db_path),
                "--session-id",
                session_id,
            )
            self.assertEqual(bootstrap.returncode, 0, msg=bootstrap.stderr)

            fake_chatgpt_response = json.dumps(
                {
                    "decision": "run_codex",
                    "codex_thread_mode": "start_fresh",
                    "codex_prompt": "Implement the new kernel.",
                    "summary": "Run Codex once with a fresh thread.",
                    "reasoning": "Manual bootstrap should stop after this turn.",
                    "needs_human_reason": "",
                }
            )
            started = self._run_cli(
                "session",
                "start",
                "--db",
                str(db_path),
                "--session-id",
                session_id,
                "--max-turns",
                "1",
                env={"BRIDGE_V2_FAKE_CHATGPT_RESPONSE": fake_chatgpt_response},
            )
            self.assertEqual(started.returncode, 0, msg=started.stderr)

            status = self._run_cli(
                "session",
                "status",
                "--db",
                str(db_path),
                "--session-id",
                session_id,
            )
            self.assertEqual(status.returncode, 0, msg=status.stderr)
            payload = json.loads(status.stdout)
            self.assertEqual(payload["session"]["status"], "manual_bootstrap")
            self.assertEqual(payload["turns"]["queued_count"], 0)
            self.assertEqual(payload["last_committed_turn"]["worker"], "chatgpt")
            self.assertEqual(payload["last_committed_turn"]["result"]["decision"], "run_codex")

    def test_arm_and_start_runs_codex_follow_up_from_last_chatgpt_turn(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            repo_path = tmp_path / "repo"
            repo_path.mkdir()
            db_path = tmp_path / "state" / "supervisor_v2.sqlite3"

            create = self._run_cli(
                "session",
                "create",
                "--db",
                str(db_path),
                "--repo-path",
                str(repo_path),
                "--workspace-path",
                str(repo_path),
                "--operator-goal",
                "Implement the supervisor kernel",
            )
            self.assertEqual(create.returncode, 0, msg=create.stderr)
            session_id = json.loads(create.stdout)["session"]["session_id"]

            self.assertEqual(
                self._run_cli(
                    "session",
                    "bootstrap",
                    "chatgpt",
                    "--db",
                    str(db_path),
                    "--session-id",
                    session_id,
                ).returncode,
                0,
            )

            fake_chatgpt_response = json.dumps(
                {
                    "decision": "run_codex",
                    "codex_thread_mode": "start_fresh",
                    "codex_prompt": "Build the SQLite-backed runtime store.",
                    "summary": "Codex should create the store.",
                    "reasoning": "We need a concrete implementation step.",
                    "needs_human_reason": "",
                }
            )
            self.assertEqual(
                self._run_cli(
                    "session",
                    "start",
                    "--db",
                    str(db_path),
                    "--session-id",
                    session_id,
                    "--max-turns",
                    "1",
                    env={"BRIDGE_V2_FAKE_CHATGPT_RESPONSE": fake_chatgpt_response},
                ).returncode,
                0,
            )

            armed = self._run_cli(
                "session",
                "arm",
                "--db",
                str(db_path),
                "--session-id",
                session_id,
            )
            self.assertEqual(armed.returncode, 0, msg=armed.stderr)

            fake_codex_result = json.dumps(
                {
                    "status": "completed",
                    "summary": "Implemented the runtime store.",
                    "final_output": "The runtime store is in place.",
                    "observed_thread_id": "codex-thread-1",
                    "exit_code": 0,
                    "files_touched": ["mastermind_bridge/v2/store.py"],
                    "checks": ["python3 -m unittest tests.test_v2"],
                    "blockers": [],
                    "estimated_context_remaining_percent": 88,
                    "artifacts_dir": "",
                }
            )
            started = self._run_cli(
                "session",
                "start",
                "--db",
                str(db_path),
                "--session-id",
                session_id,
                "--max-turns",
                "1",
                env={"BRIDGE_V2_FAKE_CODEX_RESULT": fake_codex_result},
            )
            self.assertEqual(started.returncode, 0, msg=started.stderr)

            status = self._run_cli(
                "session",
                "status",
                "--db",
                str(db_path),
                "--session-id",
                session_id,
            )
            self.assertEqual(status.returncode, 0, msg=status.stderr)
            payload = json.loads(status.stdout)
            self.assertEqual(payload["session"]["status"], "running")
            self.assertEqual(payload["session"]["current_codex_thread_id"], "codex-thread-1")
            self.assertEqual(payload["last_committed_turn"]["worker"], "codex")

    def test_recovery_commits_finished_worker_result_after_kernel_restart(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            db_path = tmp_path / "state" / "supervisor_v2.sqlite3"
            artifacts_root = tmp_path / "artifacts"

            from mastermind_bridge.v2.kernel import V2Kernel
            from mastermind_bridge.v2.store import V2Store

            store = V2Store(db_path)
            session = store.create_session(
                repo_path=tmp_path,
                workspace_path=tmp_path,
                operator_goal="Recover an interrupted turn.",
            )
            queued_turn = store.queue_turn(
                session.session_id,
                worker="chatgpt",
                payload={"operator_goal": "Recover an interrupted turn."},
                idempotency_key="chatgpt:bootstrap",
            )
            artifact_dir = artifacts_root / session.session_id / queued_turn.turn_id
            artifact_dir.mkdir(parents=True)
            result_path = artifact_dir / "worker_result.json"
            result_path.write_text(
                json.dumps(
                    {
                        "decision": "pause",
                        "codex_thread_mode": "resume_current",
                        "codex_prompt": "",
                        "summary": "Human review required after restart.",
                        "reasoning": "Recovered worker output is valid.",
                        "needs_human_reason": "Recovered from crash.",
                    }
                ),
                encoding="utf-8",
            )
            claimed_turn, _lease = store.claim_queued_turn(
                session.session_id,
                worker_pid=999999,
                artifact_path=result_path,
            )
            self.assertEqual(claimed_turn.turn_id, queued_turn.turn_id)

            kernel = V2Kernel(db_path=db_path, artifacts_root=artifacts_root)
            reconciled = kernel.reconcile_session(session.session_id)
            self.assertTrue(reconciled)

            recovered_turn = store.get_turn(queued_turn.turn_id)
            self.assertEqual(recovered_turn.status, "committed")
            self.assertEqual(store.get_session(session.session_id).status, "paused")

    def test_abort_turn_kills_active_worker_and_marks_session_blocked(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            repo_path = tmp_path / "repo"
            repo_path.mkdir()
            db_path = tmp_path / "state" / "supervisor_v2.sqlite3"

            create = self._run_cli(
                "session",
                "create",
                "--db",
                str(db_path),
                "--repo-path",
                str(repo_path),
                "--workspace-path",
                str(repo_path),
                "--operator-goal",
                "Abort an in-flight worker cleanly",
            )
            self.assertEqual(create.returncode, 0, msg=create.stderr)
            session_id = json.loads(create.stdout)["session"]["session_id"]

            bootstrap = self._run_cli(
                "session",
                "bootstrap",
                "chatgpt",
                "--db",
                str(db_path),
                "--session-id",
                session_id,
            )
            self.assertEqual(bootstrap.returncode, 0, msg=bootstrap.stderr)

            env = os.environ.copy()
            env["BRIDGE_V2_FAKE_CHATGPT_RESPONSE"] = json.dumps(
                {
                    "decision": "pause",
                    "codex_thread_mode": "resume_current",
                    "codex_prompt": "",
                    "summary": "Paused.",
                    "reasoning": "This should be killed before it finishes.",
                    "needs_human_reason": "Aborted by operator.",
                }
            )
            env["BRIDGE_V2_FAKE_CHATGPT_SLEEP"] = "20"
            start_process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "mastermind_bridge.cli",
                    "v2",
                    "session",
                    "start",
                    "--db",
                    str(db_path),
                    "--session-id",
                    session_id,
                    "--poll-interval-seconds",
                    "0.1",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            self.addCleanup(start_process.kill)

            deadline = time.time() + 10
            while time.time() < deadline:
                status = self._run_cli(
                    "session",
                    "status",
                    "--db",
                    str(db_path),
                    "--session-id",
                    session_id,
                )
                self.assertEqual(status.returncode, 0, msg=status.stderr)
                payload = json.loads(status.stdout)
                if payload["active_turn"]:
                    break
                time.sleep(0.1)
            else:
                self.fail("worker never entered running state")

            aborted = self._run_cli(
                "session",
                "abort-turn",
                "--db",
                str(db_path),
                "--session-id",
                session_id,
            )
            self.assertEqual(aborted.returncode, 0, msg=aborted.stderr)

            stdout, stderr = start_process.communicate(timeout=10)
            self.assertEqual(start_process.returncode, 0, msg=stderr or stdout)

            status = self._run_cli(
                "session",
                "status",
                "--db",
                str(db_path),
                "--session-id",
                session_id,
            )
            self.assertEqual(status.returncode, 0, msg=status.stderr)
            payload = json.loads(status.stdout)
            self.assertEqual(payload["session"]["status"], "blocked_human")
            self.assertIsNone(payload["active_turn"])
            self.assertEqual(payload["last_terminal_turn"]["status"], "aborted")

    def test_second_start_returns_immediately_while_kernel_runner_is_active(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            repo_path = tmp_path / "repo"
            repo_path.mkdir()
            db_path = tmp_path / "state" / "supervisor_v2.sqlite3"

            create = self._run_cli(
                "session",
                "create",
                "--db",
                str(db_path),
                "--repo-path",
                str(repo_path),
                "--workspace-path",
                str(repo_path),
                "--operator-goal",
                "Prevent duplicate kernel runners",
            )
            self.assertEqual(create.returncode, 0, msg=create.stderr)
            session_id = json.loads(create.stdout)["session"]["session_id"]

            bootstrap = self._run_cli(
                "session",
                "bootstrap",
                "chatgpt",
                "--db",
                str(db_path),
                "--session-id",
                session_id,
            )
            self.assertEqual(bootstrap.returncode, 0, msg=bootstrap.stderr)

            env = os.environ.copy()
            env["BRIDGE_V2_FAKE_CHATGPT_RESPONSE"] = json.dumps(
                {
                    "decision": "pause",
                    "codex_thread_mode": "resume_current",
                    "codex_prompt": "",
                    "summary": "Still running.",
                    "reasoning": "Keep the first kernel busy.",
                    "needs_human_reason": "",
                }
            )
            env["BRIDGE_V2_FAKE_CHATGPT_SLEEP"] = "20"

            first_start = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "mastermind_bridge.cli",
                    "v2",
                    "session",
                    "start",
                    "--db",
                    str(db_path),
                    "--session-id",
                    session_id,
                    "--poll-interval-seconds",
                    "0.1",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            self.addCleanup(first_start.kill)

            deadline = time.time() + 10
            active_turn_id = ""
            while time.time() < deadline:
                status = self._run_cli(
                    "session",
                    "status",
                    "--db",
                    str(db_path),
                    "--session-id",
                    session_id,
                )
                self.assertEqual(status.returncode, 0, msg=status.stderr)
                payload = json.loads(status.stdout)
                if payload["active_turn"]:
                    active_turn_id = payload["active_turn"]["turn_id"]
                    break
                time.sleep(0.1)
            else:
                self.fail("first kernel runner never entered running state")

            second_start = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mastermind_bridge.cli",
                    "v2",
                    "session",
                    "start",
                    "--db",
                    str(db_path),
                    "--session-id",
                    session_id,
                    "--max-turns",
                    "1",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            self.assertEqual(second_start.returncode, 0, msg=second_start.stderr)
            second_payload = json.loads(second_start.stdout)
            self.assertEqual(second_payload["active_turn"]["turn_id"], active_turn_id)
            self.assertEqual(second_payload["turns"]["running_count"], 1)

            aborted = self._run_cli(
                "session",
                "abort-turn",
                "--db",
                str(db_path),
                "--session-id",
                session_id,
            )
            self.assertEqual(aborted.returncode, 0, msg=aborted.stderr)
            first_start.communicate(timeout=10)

    def test_chatgpt_timeout_marks_turn_failed_and_blocks_session(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            repo_path = tmp_path / "repo"
            repo_path.mkdir()
            db_path = tmp_path / "state" / "supervisor_v2.sqlite3"

            create = self._run_cli(
                "session",
                "create",
                "--db",
                str(db_path),
                "--repo-path",
                str(repo_path),
                "--workspace-path",
                str(repo_path),
                "--operator-goal",
                "Fail closed on worker timeout",
            )
            self.assertEqual(create.returncode, 0, msg=create.stderr)
            session_id = json.loads(create.stdout)["session"]["session_id"]

            bootstrap = self._run_cli(
                "session",
                "bootstrap",
                "chatgpt",
                "--db",
                str(db_path),
                "--session-id",
                session_id,
            )
            self.assertEqual(bootstrap.returncode, 0, msg=bootstrap.stderr)

            timed_out = self._run_cli(
                "session",
                "start",
                "--db",
                str(db_path),
                "--session-id",
                session_id,
                "--max-turns",
                "1",
                "--chatgpt-timeout-seconds",
                "0.2",
                env={
                    "BRIDGE_V2_FAKE_CHATGPT_RESPONSE": json.dumps(
                        {
                            "decision": "pause",
                            "codex_thread_mode": "resume_current",
                            "codex_prompt": "",
                            "summary": "This should never commit.",
                            "reasoning": "The kernel should time out first.",
                            "needs_human_reason": "",
                        }
                    ),
                    "BRIDGE_V2_FAKE_CHATGPT_SLEEP": "5",
                },
            )
            self.assertEqual(timed_out.returncode, 0, msg=timed_out.stderr)

            status = self._run_cli(
                "session",
                "status",
                "--db",
                str(db_path),
                "--session-id",
                session_id,
            )
            self.assertEqual(status.returncode, 0, msg=status.stderr)
            payload = json.loads(status.stdout)
            self.assertEqual(payload["session"]["status"], "blocked_human")
            self.assertEqual(payload["last_terminal_turn"]["status"], "failed")
            self.assertIn("timed out", payload["last_terminal_turn"]["error_text"])

    def test_chatgpt_stop_marks_session_completed(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            repo_path = tmp_path / "repo"
            repo_path.mkdir()
            db_path = tmp_path / "state" / "supervisor_v2.sqlite3"

            create = self._run_cli(
                "session",
                "create",
                "--db",
                str(db_path),
                "--repo-path",
                str(repo_path),
                "--workspace-path",
                str(repo_path),
                "--operator-goal",
                "Complete the autonomous session cleanly",
            )
            self.assertEqual(create.returncode, 0, msg=create.stderr)
            session_id = json.loads(create.stdout)["session"]["session_id"]

            self.assertEqual(
                self._run_cli(
                    "session",
                    "bootstrap",
                    "chatgpt",
                    "--db",
                    str(db_path),
                    "--session-id",
                    session_id,
                ).returncode,
                0,
            )
            self.assertEqual(
                self._run_cli(
                    "session",
                    "arm",
                    "--db",
                    str(db_path),
                    "--session-id",
                    session_id,
                ).returncode,
                0,
            )

            stopped = self._run_cli(
                "session",
                "start",
                "--db",
                str(db_path),
                "--session-id",
                session_id,
                "--max-turns",
                "1",
                env={
                    "BRIDGE_V2_FAKE_CHATGPT_RESPONSE": json.dumps(
                        {
                            "decision": "stop",
                            "codex_thread_mode": "resume_current",
                            "codex_prompt": "",
                            "summary": "The session is complete.",
                            "reasoning": "No further Codex work is needed.",
                            "needs_human_reason": "",
                        }
                    )
                },
            )
            self.assertEqual(stopped.returncode, 0, msg=stopped.stderr)

            status = self._run_cli(
                "session",
                "status",
                "--db",
                str(db_path),
                "--session-id",
                session_id,
            )
            self.assertEqual(status.returncode, 0, msg=status.stderr)
            payload = json.loads(status.stdout)
            self.assertEqual(payload["session"]["status"], "completed")
            self.assertFalse(payload["session"]["stop_requested"])

    def test_blocked_human_session_can_reenter_manual_bootstrap(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            repo_path = tmp_path / "repo"
            repo_path.mkdir()
            db_path = tmp_path / "state" / "supervisor_v2.sqlite3"

            create = self._run_cli(
                "session",
                "create",
                "--db",
                str(db_path),
                "--repo-path",
                str(repo_path),
                "--workspace-path",
                str(repo_path),
                "--operator-goal",
                "Recover from a blocked human gate",
            )
            self.assertEqual(create.returncode, 0, msg=create.stderr)
            session_id = json.loads(create.stdout)["session"]["session_id"]

            self.assertEqual(
                self._run_cli(
                    "session",
                    "bootstrap",
                    "chatgpt",
                    "--db",
                    str(db_path),
                    "--session-id",
                    session_id,
                ).returncode,
                0,
            )
            self.assertEqual(
                self._run_cli(
                    "session",
                    "start",
                    "--db",
                    str(db_path),
                    "--session-id",
                    session_id,
                    "--max-turns",
                    "1",
                    env={
                        "BRIDGE_V2_FAKE_CHATGPT_RESPONSE": json.dumps(
                            {
                                "decision": "require_human",
                                "codex_thread_mode": "resume_current",
                                "codex_prompt": "",
                                "summary": "Human input is required.",
                                "reasoning": "The worker cannot continue autonomously.",
                                "needs_human_reason": "A human must resolve the ambiguity.",
                            }
                        )
                    },
                ).returncode,
                0,
            )

            status = self._run_cli(
                "session",
                "status",
                "--db",
                str(db_path),
                "--session-id",
                session_id,
            )
            self.assertEqual(status.returncode, 0, msg=status.stderr)
            self.assertEqual(json.loads(status.stdout)["session"]["status"], "blocked_human")

            bootstrap = self._run_cli(
                "session",
                "bootstrap",
                "chatgpt",
                "--db",
                str(db_path),
                "--session-id",
                session_id,
            )
            self.assertEqual(bootstrap.returncode, 0, msg=bootstrap.stderr)

            status = self._run_cli(
                "session",
                "status",
                "--db",
                str(db_path),
                "--session-id",
                session_id,
            )
            self.assertEqual(status.returncode, 0, msg=status.stderr)
            payload = json.loads(status.stdout)
            self.assertEqual(payload["session"]["status"], "manual_bootstrap")
            self.assertEqual(payload["turns"]["queued_count"], 1)

    def test_status_summary_format_surfaces_settings_and_last_output(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            repo_path = tmp_path / "repo"
            repo_path.mkdir()
            context_path = repo_path / "MASTER_PLAN.md"
            context_path.write_text("# Stable Plan\n", encoding="utf-8")
            db_path = tmp_path / "state" / "supervisor_v2.sqlite3"

            create = self._run_cli(
                "session",
                "create",
                "--db",
                str(db_path),
                "--repo-path",
                str(repo_path),
                "--workspace-path",
                str(repo_path),
                "--operator-goal",
                "Render a readable status summary",
                "--chatgpt-model",
                "gpt-5.5",
                "--chatgpt-reasoning-effort",
                "xhigh",
                "--codex-model",
                "gpt-5.5",
                "--codex-reasoning-effort",
                "xhigh",
                "--context-file",
                str(context_path),
            )
            self.assertEqual(create.returncode, 0, msg=create.stderr)
            session_id = json.loads(create.stdout)["session"]["session_id"]

            self.assertEqual(
                self._run_cli(
                    "session",
                    "bootstrap",
                    "chatgpt",
                    "--db",
                    str(db_path),
                    "--session-id",
                    session_id,
                ).returncode,
                0,
            )
            self.assertEqual(
                self._run_cli(
                    "session",
                    "start",
                    "--db",
                    str(db_path),
                    "--session-id",
                    session_id,
                    "--max-turns",
                    "1",
                    env={
                        "BRIDGE_V2_FAKE_CHATGPT_RESPONSE": json.dumps(
                            {
                                "decision": "pause",
                                "codex_thread_mode": "resume_current",
                                "codex_prompt": "",
                                "summary": "Wait for the operator.",
                                "reasoning": "The session reached a stable handoff point.",
                                "needs_human_reason": "",
                            }
                        )
                    },
                ).returncode,
                0,
            )

            status = self._run_cli(
                "session",
                "status",
                "--db",
                str(db_path),
                "--session-id",
                session_id,
                "--format",
                "summary",
            )
            self.assertEqual(status.returncode, 0, msg=status.stderr)
            self.assertIn("ChatGPT model: gpt-5.5 (reasoning=xhigh)", status.stdout)
            self.assertIn("Codex model: gpt-5.5 (reasoning=xhigh, execution=cli_only)", status.stdout)
            self.assertIn(f"Context files: {context_path}", status.stdout)
            self.assertIn("Last turn: chatgpt -> Wait for the operator.", status.stdout)


class V2StoreTests(unittest.TestCase):
    def test_queue_turn_refuses_second_nonterminal_turn(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)

            from mastermind_bridge.v2.store import V2Store

            store = V2Store(tmp_path / "state" / "supervisor_v2.sqlite3")
            session = store.create_session(
                repo_path=tmp_path,
                workspace_path=tmp_path,
                operator_goal="Keep single-flight guarantees.",
            )

            store.queue_turn(
                session.session_id,
                worker="chatgpt",
                payload={"operator_goal": "Keep single-flight guarantees."},
                idempotency_key="chatgpt:1",
            )
            with self.assertRaises(RuntimeError):
                store.queue_turn(
                    session.session_id,
                    worker="codex",
                    payload={"codex_prompt": "Should not queue."},
                    idempotency_key="codex:2",
                )

    def test_commit_turn_refuses_double_commit(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)

            from mastermind_bridge.v2.store import V2Store

            store = V2Store(tmp_path / "state" / "supervisor_v2.sqlite3")
            session = store.create_session(
                repo_path=tmp_path,
                workspace_path=tmp_path,
                operator_goal="Allow each turn to commit only once.",
            )
            turn = store.queue_turn(
                session.session_id,
                worker="chatgpt",
                payload={"operator_goal": "Commit once."},
                idempotency_key="chatgpt:commit-once",
            )
            claimed_turn, _lease = store.claim_queued_turn(
                session.session_id,
                worker_pid=12345,
                artifact_path=tmp_path / "result.json",
            )
            self.assertEqual(claimed_turn.turn_id, turn.turn_id)
            store.mark_turn_completed(
                turn.turn_id,
                result={
                    "decision": "pause",
                    "codex_thread_mode": "resume_current",
                    "codex_prompt": "",
                    "summary": "Committed once.",
                    "reasoning": "First commit should succeed.",
                    "needs_human_reason": "",
                },
                artifact_path=tmp_path / "result.json",
            )
            store.commit_turn(turn.turn_id)

            with self.assertRaises(RuntimeError):
                store.commit_turn(turn.turn_id)

    def test_reconcile_expired_worker_lease_aborts_running_turn(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)

            from mastermind_bridge.v2.kernel import V2Kernel
            from mastermind_bridge.v2.store import V2Store

            db_path = tmp_path / "state" / "supervisor_v2.sqlite3"
            artifacts_root = tmp_path / "artifacts"
            store = V2Store(db_path)
            session = store.create_session(
                repo_path=tmp_path,
                workspace_path=tmp_path,
                operator_goal="Recover from a stale worker lease.",
            )
            turn = store.queue_turn(
                session.session_id,
                worker="chatgpt",
                payload={"operator_goal": "Recover from a stale worker lease."},
                idempotency_key="chatgpt:stale-lease",
            )
            claimed_turn, _lease = store.claim_queued_turn(
                session.session_id,
                worker_pid=os.getpid(),
                artifact_path=artifacts_root / "missing-worker-result.json",
                lease_ttl_seconds=-1,
            )
            self.assertEqual(claimed_turn.turn_id, turn.turn_id)

            kernel = V2Kernel(db_path=db_path, artifacts_root=artifacts_root)
            self.assertTrue(kernel.reconcile_session(session.session_id))
            self.assertEqual(store.get_turn(turn.turn_id).status, "aborted")
            self.assertEqual(store.get_session(session.session_id).status, "blocked_human")

    def test_resume_current_requires_existing_thread(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)

            from mastermind_bridge.v2.kernel import V2Kernel

            kernel = V2Kernel(
                db_path=tmp_path / "state" / "supervisor_v2.sqlite3",
                artifacts_root=tmp_path / "artifacts",
            )
            session = kernel.create_session(
                repo_path=tmp_path,
                workspace_path=tmp_path,
                operator_goal="Fail closed when no resumable thread exists.",
            )

            with self.assertRaises(RuntimeError):
                kernel.bootstrap_turn(
                    session.session_id,
                    worker="codex",
                    prompt="Resume work without a thread id.",
                    thread_mode="resume_current",
                )

    def test_autonomous_resume_current_without_thread_blocks_session(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)

            from mastermind_bridge.v2.kernel import V2Kernel

            kernel = V2Kernel(
                db_path=tmp_path / "state" / "supervisor_v2.sqlite3",
                artifacts_root=tmp_path / "artifacts",
            )
            session = kernel.create_session(
                repo_path=tmp_path,
                workspace_path=tmp_path,
                operator_goal="Fail closed when autonomous resume has no thread.",
            )
            chatgpt_turn = kernel.store.queue_turn(
                session.session_id,
                worker="chatgpt",
                payload={"session_id": session.session_id},
                idempotency_key="chatgpt:manual",
            )
            claimed_turn, _lease = kernel.store.claim_queued_turn(
                session.session_id,
                worker_pid=12345,
                artifact_path=tmp_path / "chatgpt-result.json",
            )
            self.assertEqual(claimed_turn.turn_id, chatgpt_turn.turn_id)
            kernel.store.mark_turn_completed(
                chatgpt_turn.turn_id,
                result={
                    "decision": "run_codex",
                    "summary": "Run Codex.",
                    "codex_prompt": "Continue.",
                    "codex_thread_mode": "resume_current",
                },
                artifact_path=tmp_path / "chatgpt-result.json",
            )
            kernel.store.commit_turn(chatgpt_turn.turn_id)
            kernel.store.update_session(session.session_id, status="running")

            snapshot = kernel.start(session.session_id, max_turns=1, poll_interval_seconds=0.01)
            updated = kernel.store.get_session(session.session_id)

            self.assertEqual(updated.status, "blocked_human")
            self.assertIn("resume_current requires", updated.last_error)
            self.assertEqual(snapshot["session"]["status"], "blocked_human")


class V2WorkerTests(unittest.TestCase):
    def test_run_chatgpt_worker_uses_worker_input_model_and_reasoning(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)

            from mastermind_bridge.v2.workers import run_chatgpt_worker

            captured_request: dict[str, object] = {}

            class _FakeResponse:
                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

                def read(self):
                    return json.dumps(
                        {
                            "output_text": json.dumps(
                                {
                                    "decision": "pause",
                                    "codex_thread_mode": "resume_current",
                                    "codex_prompt": "",
                                    "summary": "Pause for review.",
                                    "reasoning": "A higher thinking setting was requested.",
                                    "needs_human_reason": "",
                                }
                            )
                        }
                    ).encode("utf-8")

            def fake_urlopen(request, timeout):
                del timeout
                captured_request.update(json.loads(request.data.decode("utf-8")))
                return _FakeResponse()

            with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=False):
                with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
                    result = run_chatgpt_worker(
                        worker_input={
                            "session_id": "session-1",
                            "repo_path": str(tmp_path),
                            "workspace_path": str(tmp_path),
                            "operator_goal": "Think harder by default.",
                            "session_summary": "",
                            "last_committed_codex_turn": {},
                            "relevant_artifacts_manifest": [],
                            "operator_notes": "",
                            "source": "manual_bootstrap",
                            "chatgpt_model": "gpt-5.4",
                            "chatgpt_reasoning_effort": "high",
                            "context_files": [],
                        },
                        output_path=tmp_path / "worker_result.json",
                    )

            self.assertEqual(result["summary"], "Pause for review.")
            self.assertEqual(captured_request["model"], "gpt-5.4")
            self.assertEqual(captured_request["reasoning"], {"effort": "high"})

    def test_run_codex_worker_forces_cli_only_env_skips_default_compaction_and_uses_session_model(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)

            from mastermind_bridge.v2.workers import run_codex_worker

            captured_call: dict[str, object] = {}

            def fake_execute_codex_prompt(**kwargs):
                captured_call.update(kwargs)
                return (
                    SimpleNamespace(
                        summary="Completed the turn.",
                        final_agent_message="No changes required.",
                        observed_codex_thread_id="codex-thread-42",
                        codex_thread_id="codex-thread-42",
                        exit_code=0,
                        files_touched=[],
                        checks=["./tests/repro_test.sh"],
                        blockers=[],
                        estimated_context_remaining_percent=77,
                        artifacts_dir=str(tmp_path / "artifacts"),
                    ),
                    {},
                )

            with mock.patch.dict(
                os.environ,
                {
                    "BRIDGE_ENABLE_CODEX_APP_INTEGRATION": "1",
                    "BRIDGE_AUTO_OPEN_CODEX_APP_THREADS": "1",
                    "BRIDGE_V2_CODEX_AUTO_COMPACT": "",
                },
                clear=False,
            ):
                with mock.patch("mastermind_bridge.v2.workers.execute_codex_prompt", side_effect=fake_execute_codex_prompt):
                    with mock.patch(
                        "mastermind_bridge.v2.workers.compact_codex_thread_after_turn",
                        return_value={
                            "status": "completed",
                            "thread_id": "codex-thread-42",
                            "method": "thread/compact/start",
                            "completion": "thread/compacted",
                        },
                    ) as compact_mock:
                        result = run_codex_worker(
                            worker_input={
                                "session_id": "session-1",
                                "workspace_path": str(tmp_path),
                                "thread_mode": "start_fresh",
                                "current_codex_thread_id": "",
                                "codex_prompt": "Inspect the repo and report.",
                                "codex_execution_mode": "cli_only",
                            },
                            output_path=tmp_path / "worker_result.json",
                        )

            self.assertEqual(result["observed_thread_id"], "codex-thread-42")
            self.assertEqual(result["codex_compaction"], {})
            compact_mock.assert_not_called()
            self.assertEqual(captured_call["model"], "gpt-5.5")
            self.assertEqual(captured_call["reasoning_effort"], "xhigh")
            prompt_text = Path(captured_call["prompt_path"]).read_text(encoding="utf-8")
            self.assertIn("Browser Use Codex plugin", prompt_text)
            self.assertIn("Computer Use Codex plugin", prompt_text)
            self.assertIn("required escalation surface for real macOS GUI blockers", prompt_text)
            self.assertIn("Codex exec capability notes:", prompt_text)
            self.assertIn("Skill path hints for codex exec on this machine:", prompt_text)
            self.assertIn(
                "Do not try paths like ${CODEX_HOME:-$HOME/.codex}/skills/r0/<skill>/SKILL.md",
                prompt_text,
            )
            self.assertNotIn(str(Path.home()), prompt_text)
            self.assertIn("Messages for one-time codes", prompt_text)
            self.assertIn("Touch ID, hardware security-key taps", prompt_text)
            self.assertIn("# Task Prompt\n\nInspect the repo and report.", prompt_text)
            self.assertEqual(captured_call["env"]["BRIDGE_ENABLE_CODEX_APP_INTEGRATION"], "0")
            self.assertEqual(captured_call["env"]["BRIDGE_AUTO_OPEN_CODEX_APP_THREADS"], "0")

    def test_run_codex_worker_compacts_when_explicitly_enabled(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)

            from mastermind_bridge.v2.workers import run_codex_worker

            def fake_execute_codex_prompt(**_kwargs):
                return (
                    SimpleNamespace(
                        summary="Completed the turn.",
                        final_agent_message="No changes required.",
                        observed_codex_thread_id="codex-thread-42",
                        codex_thread_id="codex-thread-42",
                        exit_code=0,
                        files_touched=[],
                        checks=[],
                        blockers=[],
                        estimated_context_remaining_percent=77,
                        artifacts_dir=str(tmp_path / "artifacts"),
                    ),
                    {},
                )

            with mock.patch.dict(os.environ, {"BRIDGE_V2_CODEX_AUTO_COMPACT": "1"}, clear=False):
                with mock.patch("mastermind_bridge.v2.workers.execute_codex_prompt", side_effect=fake_execute_codex_prompt):
                    with mock.patch(
                        "mastermind_bridge.v2.workers.compact_codex_thread_after_turn",
                        return_value={
                            "status": "completed",
                            "thread_id": "codex-thread-42",
                            "method": "thread/compact/start",
                            "completion": "thread/compacted",
                        },
                    ) as compact_mock:
                        result = run_codex_worker(
                            worker_input={
                                "session_id": "session-1",
                                "workspace_path": str(tmp_path),
                                "thread_mode": "start_fresh",
                                "current_codex_thread_id": "",
                                "codex_prompt": "Inspect the repo and report.",
                                "codex_execution_mode": "cli_only",
                            },
                            output_path=tmp_path / "worker_result.json",
                        )

            self.assertEqual(result["codex_compaction"]["completion"], "thread/compacted")
            compact_mock.assert_called_once()

    def test_run_codex_worker_rejects_allow_app_without_macos_app_profile(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)

            from mastermind_bridge.v2.workers import run_codex_worker

            with mock.patch.dict(
                os.environ,
                {
                    "BRIDGE_PROFILE": "core-safe",
                    "BRIDGE_V2_FAKE_CODEX_RESULT": json.dumps(
                        {
                            "status": "completed",
                            "summary": "Should not run.",
                            "final_output": "",
                            "observed_thread_id": "codex-thread-42",
                            "exit_code": 0,
                            "files_touched": [],
                            "checks": [],
                            "blockers": [],
                            "estimated_context_remaining_percent": 77,
                            "artifacts_dir": str(tmp_path / "artifacts"),
                        }
                    ),
                },
                clear=False,
            ):
                with self.assertRaisesRegex(PermissionError, "requires BRIDGE_PROFILE=macos-app"):
                    run_codex_worker(
                        worker_input={
                            "session_id": "session-1",
                            "workspace_path": str(tmp_path),
                            "thread_mode": "start_fresh",
                            "current_codex_thread_id": "",
                            "codex_prompt": "Inspect the repo and report.",
                            "codex_execution_mode": "allow_app",
                        },
                        output_path=tmp_path / "worker_result.json",
                    )

    def test_auto_compaction_defaults_only_for_allow_app_mode(self):
        from mastermind_bridge.v2.workers import _codex_auto_compact_enabled

        with mock.patch.dict(os.environ, {"BRIDGE_V2_CODEX_AUTO_COMPACT": ""}, clear=False):
            self.assertFalse(_codex_auto_compact_enabled("cli_only"))
            self.assertFalse(_codex_auto_compact_enabled("surprise-app-mode"))
            self.assertTrue(_codex_auto_compact_enabled("allow_app"))
