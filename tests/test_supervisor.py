import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mastermind_bridge.orchestrator.state import load_session, save_chat_bindings, save_session


class _FakeRunner:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def run_once(self, session_id: str, require_new_message: bool = False):
        self.calls.append({"session_id": session_id, "require_new_message": require_new_message})
        if self.results:
            return dict(self.results.pop(0))
        return {
            "session_id": session_id,
            "policy_outcome": "allow",
            "loop_state": "waiting_for_chatgpt",
            "runner_action": "wait_for_chatgpt",
            "return_packet_id": "",
        }


class _MalformedRunner:
    def run_once(self, session_id: str, require_new_message: bool = False):
        return []


class _RaisesOnceRunner:
    def __init__(self, exc: Exception, fallback: dict[str, str]):
        self.exc = exc
        self.fallback = dict(fallback)
        self.calls = 0

    def run_once(self, session_id: str, require_new_message: bool = False):
        self.calls += 1
        if self.calls == 1:
            raise self.exc
        return dict(self.fallback)


class SessionSupervisorTests(unittest.TestCase):
    def _write_state(self, root: Path) -> tuple[Path, Path, Path]:
        bindings_path = root / "CHAT_BINDINGS.json"
        policy_path = root / "ORCHESTRATOR_POLICY.json"
        sessions_dir = root / "sessions"
        save_chat_bindings(
            bindings_path,
            [
                {
                    "binding_id": "binding-1",
                    "project_name": "bridge",
                    "repo_path": "/tmp/repo",
                    "workspace_path": "/tmp/repo",
                    "chat_url": "https://chatgpt.com/c/project/binding-1",
                }
            ],
        )
        policy_path.write_text(json.dumps({"version": 1}))
        sessions_dir.mkdir(parents=True, exist_ok=True)
        (sessions_dir / "session-1.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "session": {
                        "session_id": "session-1",
                        "binding_id": "binding-1",
                        "repo_path": "/tmp/repo",
                        "workspace_path": "/tmp/repo",
                        "chat_url": "https://chatgpt.com/c/project/binding-1",
                        "status": "active",
                        "loop_state": "idle",
                        "time_budget_minutes": 90,
                        "budget_remaining_minutes": 90,
                        "auto_run_enabled": True,
                        "supervisor_status": "idle",
                    },
                }
            )
        )
        return bindings_path, policy_path, sessions_dir

    def test_supervisor_repeats_until_human_attention_is_required(self):
        from mastermind_bridge.orchestrator.supervisor import SessionSupervisor

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            _bindings_path, _policy_path, sessions_dir = self._write_state(root)
            runner = _FakeRunner(
                [
                    {
                        "session_id": "session-1",
                        "policy_outcome": "allow",
                        "loop_state": "waiting_for_chatgpt",
                        "runner_action": "wait_for_chatgpt",
                        "return_packet_id": "",
                    },
                    {
                        "session_id": "session-1",
                        "policy_outcome": "allow",
                        "loop_state": "waiting_for_chatgpt_response",
                        "runner_action": "cycle_completed",
                        "return_packet_id": "packet-1",
                    },
                    {
                        "session_id": "session-1",
                        "policy_outcome": "require_human",
                        "loop_state": "requires_human",
                        "runner_action": "blocked",
                        "return_packet_id": "",
                    },
                ]
            )
            supervisor = SessionSupervisor(
                session_id="session-1",
                sessions_dir=sessions_dir,
                runner=runner,
                poll_interval_seconds=0.01,
            )

            supervisor.start()
            supervisor.join(timeout=1)

            self.assertFalse(supervisor.is_alive())
            self.assertGreaterEqual(len(runner.calls), 3)
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["supervisor_status"], "blocked")
            self.assertFalse(session_payload["auto_run_enabled"])
            self.assertEqual(session_payload["cycles_completed"], 1)

    def test_supervisor_stops_when_session_is_paused(self):
        from mastermind_bridge.orchestrator.supervisor import SessionSupervisor

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            _bindings_path, _policy_path, sessions_dir = self._write_state(root)
            session_path = sessions_dir / "session-1.json"
            paused_session = load_session(session_path)
            paused_session.status = "paused"
            paused_session.auto_run_enabled = False
            save_session(session_path, paused_session)
            runner = _FakeRunner(
                [
                    {
                        "session_id": "session-1",
                        "policy_outcome": "allow",
                        "loop_state": "waiting_for_chatgpt",
                        "runner_action": "wait_for_chatgpt",
                        "return_packet_id": "",
                    }
                ]
            )
            supervisor = SessionSupervisor(
                session_id="session-1",
                sessions_dir=sessions_dir,
                runner=runner,
                poll_interval_seconds=0.01,
            )

            supervisor.start()
            supervisor.join(timeout=1)
            session_payload = json.loads(session_path.read_text())["session"]
            self.assertEqual(session_payload["supervisor_status"], "paused")
            self.assertFalse(supervisor.is_alive())
            self.assertEqual(runner.calls, [])

    def test_load_session_retries_when_file_is_temporarily_empty(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            session_file = root / "session-1.json"
            session_file.write_text("")

            def _finish_write() -> None:
                time.sleep(0.02)
                session_file.write_text(
                    json.dumps(
                        {
                            "version": 1,
                            "session": {
                                "session_id": "session-1",
                                "binding_id": "binding-1",
                                "repo_path": "/tmp/repo",
                                "workspace_path": "/tmp/repo",
                                "chat_url": "https://chatgpt.com/c/project/binding-1",
                                "status": "active",
                                "loop_state": "idle",
                                "time_budget_minutes": 90,
                                "budget_remaining_minutes": 90,
                            },
                        }
                    )
                )

            writer = threading.Thread(target=_finish_write)
            writer.start()
            session = load_session(session_file)
            writer.join(timeout=1)

            self.assertEqual(session.session_id, "session-1")

    def test_supervisor_manager_rejects_duplicate_session_lock(self):
        from mastermind_bridge.orchestrator.supervisor import SupervisorManager

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            _bindings_path, _policy_path, sessions_dir = self._write_state(root)
            lock_dir = root / "session_locks"
            lock_dir.mkdir(parents=True, exist_ok=True)
            (lock_dir / "session-1.json").write_text(
                json.dumps(
                    {
                        "session_id": "session-1",
                        "pid": os.getpid(),
                        "token": "foreign-lock",
                        "hostname": "localhost",
                        "thread_name": "other-runner",
                        "acquired_at": time.time(),
                    }
                )
            )
            manager = SupervisorManager(
                sessions_dir=sessions_dir,
                runner_factory=lambda: _FakeRunner([]),
                poll_interval_seconds=0.01,
                lock_dir=lock_dir,
            )

            with self.assertRaisesRegex(RuntimeError, "already supervised"):
                manager.ensure_session("session-1")

    def test_supervisor_manager_reclaims_stale_session_lock(self):
        from mastermind_bridge.orchestrator.supervisor import SupervisorManager

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            _bindings_path, _policy_path, sessions_dir = self._write_state(root)
            lock_dir = root / "session_locks"
            lock_dir.mkdir(parents=True, exist_ok=True)
            (lock_dir / "session-1.json").write_text(
                json.dumps(
                    {
                        "session_id": "session-1",
                        "pid": 999999,
                        "token": "stale-lock",
                        "hostname": "localhost",
                        "thread_name": "dead-runner",
                        "acquired_at": time.time(),
                    }
                )
            )
            manager = SupervisorManager(
                sessions_dir=sessions_dir,
                runner_factory=lambda: _FakeRunner(
                    [
                        {
                            "session_id": "session-1",
                            "policy_outcome": "require_human",
                            "loop_state": "requires_human",
                            "runner_action": "blocked",
                            "return_packet_id": "",
                        }
                    ]
                ),
                poll_interval_seconds=0.01,
                lock_dir=lock_dir,
            )

    def test_supervisor_manager_does_not_start_paused_session(self):
        from mastermind_bridge.orchestrator.supervisor import SupervisorManager

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            _bindings_path, _policy_path, sessions_dir = self._write_state(root)
            session_file = sessions_dir / "session-1.json"
            paused_session = load_session(session_file)
            paused_session.status = "paused"
            paused_session.loop_state = "paused"
            paused_session.auto_run_enabled = False
            paused_session.supervisor_status = "paused"
            save_session(session_file, paused_session)
            manager = SupervisorManager(
                sessions_dir=sessions_dir,
                runner_factory=lambda: _FakeRunner([]),
                poll_interval_seconds=0.01,
                lock_dir=root / "session_locks",
            )

            result = manager.ensure_session("session-1")

            self.assertEqual(result["status"], "paused")
            self.assertEqual(manager.snapshot(), {})

    def test_describe_session_lock_treats_permission_denied_pid_probe_as_alive(self):
        from mastermind_bridge.orchestrator.supervisor import describe_session_lock

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            lock_dir = root / "session_locks"
            lock_dir.mkdir(parents=True, exist_ok=True)
            (lock_dir / "session-1.json").write_text(
                json.dumps(
                    {
                        "session_id": "session-1",
                        "pid": 4242,
                        "token": "sandboxed-lock",
                        "hostname": "localhost",
                        "thread_name": "runner",
                        "acquired_at": time.time(),
                    }
                )
            )

            with patch("mastermind_bridge.orchestrator.supervisor.os.kill", side_effect=PermissionError()):
                lock = describe_session_lock(lock_dir, "session-1")

            self.assertIsNotNone(lock)
            self.assertTrue(lock["pid_alive"])

    def test_describe_session_lock_treats_zombie_pid_as_stale(self):
        from mastermind_bridge.orchestrator.supervisor import describe_session_lock

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            lock_dir = root / "session_locks"
            lock_dir.mkdir(parents=True, exist_ok=True)
            (lock_dir / "session-1.json").write_text(
                json.dumps(
                    {
                        "session_id": "session-1",
                        "pid": 4242,
                        "token": "zombie-lock",
                        "hostname": "localhost",
                        "thread_name": "runner",
                        "acquired_at": time.time(),
                    }
                )
            )

            with (
                patch("mastermind_bridge.orchestrator.supervisor.os.kill", return_value=None),
                patch(
                    "mastermind_bridge.orchestrator.supervisor.subprocess.run",
                    return_value=type(
                        "_CompletedProcess",
                        (),
                        {"returncode": 0, "stdout": "Z+\n"},
                    )(),
                ),
            ):
                lock = describe_session_lock(lock_dir, "session-1")

            self.assertIsNotNone(lock)
            self.assertFalse(lock["pid_alive"])

    def test_supervisor_manager_releases_lock_when_runner_factory_raises(self):
        from mastermind_bridge.orchestrator.supervisor import SupervisorManager

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            _bindings_path, _policy_path, sessions_dir = self._write_state(root)
            lock_dir = root / "session_locks"
            manager = SupervisorManager(
                sessions_dir=sessions_dir,
                runner_factory=lambda: (_ for _ in ()).throw(RuntimeError("runner factory exploded")),
                poll_interval_seconds=0.01,
                lock_dir=lock_dir,
            )

            with self.assertRaisesRegex(RuntimeError, "runner factory exploded"):
                manager.ensure_session("session-1")

            self.assertFalse((lock_dir / "session-1.json").exists())

    def test_supervisor_manager_reclaims_corrupt_session_lock(self):
        from mastermind_bridge.orchestrator.supervisor import SupervisorManager

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            _bindings_path, _policy_path, sessions_dir = self._write_state(root)
            lock_dir = root / "session_locks"
            lock_dir.mkdir(parents=True, exist_ok=True)
            (lock_dir / "session-1.json").write_text("{not valid json")
            manager = SupervisorManager(
                sessions_dir=sessions_dir,
                runner_factory=lambda: _FakeRunner(
                    [
                        {
                            "session_id": "session-1",
                            "policy_outcome": "require_human",
                            "loop_state": "requires_human",
                            "runner_action": "blocked",
                            "return_packet_id": "",
                        }
                    ]
                ),
                poll_interval_seconds=0.01,
                lock_dir=lock_dir,
            )

            result = manager.ensure_session("session-1")

            self.assertEqual(result["status"], "running")
            time.sleep(0.05)
            snapshot = manager.snapshot()
            self.assertIn("session-1", snapshot)

    def test_terminate_locked_session_supervisor_kills_process_tree_and_removes_lock(self):
        import signal
        import socket

        from mastermind_bridge.orchestrator.supervisor import terminate_locked_session_supervisor

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            lock_dir = root / "session_locks"
            lock_dir.mkdir()
            lock_path = lock_dir / "session-1.json"
            lock_path.write_text(
                json.dumps(
                    {
                        "session_id": "session-1",
                        "pid": 123,
                        "token": "lock-token",
                        "hostname": socket.gethostname(),
                        "thread_name": "session-supervisor-session-1",
                        "acquired_at": time.time(),
                    }
                )
            )

            with (
                patch("mastermind_bridge.orchestrator.supervisor._pid_is_alive", side_effect=[True, False]),
                patch(
                    "mastermind_bridge.orchestrator.supervisor._process_command",
                    return_value="python3 -m mastermind_bridge.cli supervise-session --session-id session-1",
                ),
                patch("mastermind_bridge.orchestrator.supervisor._descendant_process_ids", return_value=[124]),
                patch("mastermind_bridge.orchestrator.supervisor._wait_for_process_exit", return_value=[]),
                patch("mastermind_bridge.orchestrator.supervisor._signal_processes") as signaler,
            ):
                result = terminate_locked_session_supervisor(lock_dir, "session-1")

            self.assertEqual(result["status"], "terminated")
            self.assertEqual(result["descendant_pids"], [124])
            self.assertTrue(result["lock_removed"])
            self.assertFalse(lock_path.exists())
            signaler.assert_called_once_with([124, 123], signal.SIGTERM)

    def test_terminate_locked_session_supervisor_refuses_unrecognized_process(self):
        import socket

        from mastermind_bridge.orchestrator.supervisor import terminate_locked_session_supervisor

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            lock_dir = root / "session_locks"
            lock_dir.mkdir()
            lock_path = lock_dir / "session-1.json"
            lock_path.write_text(
                json.dumps(
                    {
                        "session_id": "session-1",
                        "pid": 123,
                        "token": "lock-token",
                        "hostname": socket.gethostname(),
                        "thread_name": "session-supervisor-session-1",
                        "acquired_at": time.time(),
                    }
                )
            )

            with (
                patch("mastermind_bridge.orchestrator.supervisor._pid_is_alive", return_value=True),
                patch("mastermind_bridge.orchestrator.supervisor._process_command", return_value="python3 other.py"),
                patch("mastermind_bridge.orchestrator.supervisor._signal_processes") as signaler,
            ):
                result = terminate_locked_session_supervisor(lock_dir, "session-1")

            self.assertEqual(result["status"], "refused_unrecognized_process")
            self.assertTrue(lock_path.exists())
            signaler.assert_not_called()

    def test_supervisor_marks_session_failed_when_runner_returns_malformed_payload(self):
        from mastermind_bridge.orchestrator.supervisor import SessionSupervisor

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            _bindings_path, _policy_path, sessions_dir = self._write_state(root)
            supervisor = SessionSupervisor(
                session_id="session-1",
                sessions_dir=sessions_dir,
                runner=_MalformedRunner(),
                poll_interval_seconds=0.01,
            )

            supervisor.start()
            supervisor.join(timeout=1)

            self.assertFalse(supervisor.is_alive())
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["supervisor_status"], "failed")
            self.assertFalse(session_payload["auto_run_enabled"])
            self.assertIn("invalid", session_payload["human_attention_reason"].lower())

    def test_supervisor_retries_transient_host_browser_transport_failure(self):
        from mastermind_bridge.orchestrator.supervisor import SessionSupervisor

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            _bindings_path, _policy_path, sessions_dir = self._write_state(root)
            runner = _RaisesOnceRunner(
                RuntimeError(
                    "macOS browser Apple Events automation is not functioning on this host. "
                    "The Bridge reached the configured browser tab, but Chrome did not answer the Apple Event "
                    "before macOS timed the call out (`-1712`)."
                ),
                {
                    "session_id": "session-1",
                    "policy_outcome": "require_human",
                    "loop_state": "requires_human",
                    "runner_action": "blocked",
                    "return_packet_id": "",
                },
            )
            supervisor = SessionSupervisor(
                session_id="session-1",
                sessions_dir=sessions_dir,
                runner=runner,
                poll_interval_seconds=0.01,
            )

            supervisor.start()
            supervisor.join(timeout=1)

            self.assertFalse(supervisor.is_alive())
            self.assertEqual(runner.calls, 2)
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["supervisor_status"], "blocked")
            self.assertFalse(session_payload["auto_run_enabled"])
            self.assertIn("host_browser_transport_retry", session_payload["degraded_mode"])
