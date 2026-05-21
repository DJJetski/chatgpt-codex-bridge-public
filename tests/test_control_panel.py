import json
import os
import subprocess
import tempfile
import threading
import unittest
import http.client
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from pathlib import Path
from unittest.mock import patch

from mastermind_bridge.models import repo_root
from mastermind_bridge.orchestrator.state import save_session, session_path


class _FakeSupervisorManager:
    def __init__(self):
        self.started = []
        self.stopped = []

    def ensure_session(self, session_id: str):
        self.started.append(session_id)
        return {"session_id": session_id, "status": "running"}

    def stop_session(self, session_id: str):
        self.stopped.append(session_id)

    def snapshot(self):
        return {}


class _StaticSupervisorManager:
    def __init__(self, snapshot_payload):
        self.snapshot_payload = snapshot_payload

    def ensure_session(self, session_id: str):
        return {"session_id": session_id, "status": "running"}

    def stop_session(self, session_id: str):
        return None

    def snapshot(self):
        return self.snapshot_payload


class _FakePreviewAdapter:
    def __init__(self):
        self.opened_urls = []
        self.closed = False

    def open_chat(self, binding):
        self.opened_urls.append(binding.chat_url)

    def close(self):
        self.closed = True


class _FailingSupervisorManager:
    def ensure_session(self, session_id: str):
        raise RuntimeError("Playwright is not installed. Install the optional browser dependency to use run-loop.")

    def stop_session(self, session_id: str):
        return None

    def snapshot(self):
        return {}


class ControlPanelServiceTests(unittest.TestCase):
    def setUp(self):
        self._terminal_open_patcher = patch(
            "mastermind_bridge.orchestrator.control_panel._open_terminal_with_command"
        )
        self._terminal_open_patcher.start()

    def tearDown(self):
        self._terminal_open_patcher.stop()

    def test_snapshot_sorts_newest_sessions_first_and_renders_muted_completed_cards(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            sessions_dir.mkdir(parents=True)
            (sessions_dir / "session-old.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "session": {
                            "session_id": "session-old",
                            "binding_id": "binding-1",
                            "repo_path": "/tmp/repo",
                            "workspace_path": "/tmp/repo",
                            "chat_url": "https://chatgpt.com/c/project/binding-1",
                            "status": "completed",
                            "supervisor_status": "stopped",
                            "updated_at": "2026-04-15T20:00:00+00:00",
                        },
                    }
                )
            )
            (sessions_dir / "session-new.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "session": {
                            "session_id": "session-new",
                            "binding_id": "binding-1",
                            "repo_path": "/tmp/repo",
                            "workspace_path": "/tmp/repo",
                            "chat_url": "https://chatgpt.com/c/project/binding-1",
                            "status": "active",
                            "supervisor_status": "running",
                            "updated_at": "2026-04-15T21:00:00+00:00",
                        },
                    }
                )
            )
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=_FakeSupervisorManager(),
            )

            snapshot = service.snapshot()
            html = service.render_dashboard()

            self.assertEqual([item["session_id"] for item in snapshot["sessions"]], ["session-new", "session-old"])
            self.assertLess(html.index("session-new"), html.index("session-old"))
            self.assertIn('class="session session-muted"', html)

    def test_render_dashboard_includes_forms_and_controls(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "bindings": [
                            {
                                "binding_id": "binding-1",
                                "project_name": "Bridge",
                                "repo_path": "/tmp/repo",
                                "workspace_path": "/tmp/repo",
                                "chat_url": "https://chatgpt.com/c/project/binding-1",
                            }
                        ],
                    }
                )
            )
            policy_path.write_text(json.dumps({"version": 1}))
            sessions_dir.mkdir(parents=True)
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
                            "loop_state": "waiting_for_chatgpt",
                            "time_budget_minutes": 90,
                            "budget_remaining_minutes": 80,
                            "auto_run_enabled": True,
                            "supervisor_status": "running",
                        },
                    }
                )
            )
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=_FakeSupervisorManager(),
            )

            html = service.render_dashboard()

            self.assertIn("Bridge Control Panel", html)
            self.assertIn("Session", html)
            self.assertIn("Create Session", html)
            self.assertIn('name="codex_model"', html)
            self.assertIn('name="codex_reasoning_effort"', html)
            self.assertIn('value="gpt-5.3-codex-spark"', html)
            self.assertIn("ChatGPT 5.3 Spark Codex", html)
            self.assertIn("Extra High", html)
            self.assertIn("You never need to create a binding by hand here.", html)
            self.assertIn("Open Chat", html)
            self.assertIn("Artifacts", html)
            self.assertIn(">Start</button>", html)
            self.assertNotIn(">Send Once</button>", html)
            self.assertNotIn(">Send Twice</button>", html)
            self.assertNotIn(">Go Auto</button>", html)
            self.assertNotIn("controlSession('${session.session_id}', 'start-twice')", html)
            self.assertNotIn("if (action === 'start-twice') await postJson(`/api/sessions/${sessionId}/start`, { single_cycle: true });", html)
            self.assertIn("Apply Execution Settings", html)
            self.assertNotIn("Advanced Setup", html)
            self.assertNotIn("Create Binding", html)
            self.assertNotIn("Create Session from Binding", html)
            self.assertNotIn("Open Chat in Bridge Browser", html)
            self.assertNotIn("Open Latest Run", html)
            self.assertNotIn("Open Codex Thread (CLI)", html)
            self.assertNotIn("Continue in Terminal", html)
            self.assertNotIn("Open in Codex App (experimental)", html)

    def test_render_dashboard_preserves_open_details_and_disables_refresh_animation(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            sessions_dir.mkdir(parents=True)
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
                            "loop_state": "waiting_for_chatgpt_response",
                            "time_budget_minutes": 90,
                            "budget_remaining_minutes": 80,
                            "auto_run_enabled": True,
                            "supervisor_status": "running",
                        },
                    }
                )
            )
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=_FakeSupervisorManager(),
            )

            html = service.render_dashboard()

            self.assertIn("const sessionUiState = new Map();", html)
            self.assertIn("function captureSessionUiState()", html)
            self.assertIn("function restoreSessionUiState(container)", html)
            self.assertIn("session-no-animate", html)
            self.assertIn("data-session-id", html)

    def test_create_session_persists_execution_preferences(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService
        from mastermind_bridge.orchestrator.state import load_session, session_path

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "bindings": [
                            {
                                "binding_id": "binding-1",
                                "project_name": "Bridge",
                                "repo_path": "/tmp/repo",
                                "workspace_path": "/tmp/repo",
                                "chat_url": "https://chatgpt.com/c/project/binding-1",
                            }
                        ],
                    }
                )
            )
            policy_path.write_text(json.dumps({"version": 1}))
            sessions_dir.mkdir(parents=True)
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=_FakeSupervisorManager(),
            )

            session = service.create_session(
                {
                    "session_id": "session-1",
                    "binding_id": "binding-1",
                    "time_budget_minutes": 120,
                    "codex_model": "gpt-5.3-codex-spark",
                    "codex_reasoning_effort": "xhigh",
                }
            )
            reloaded = load_session(session_path(sessions_dir, "session-1"))

            self.assertEqual(session.codex_model, "gpt-5.3-codex-spark")
            self.assertEqual(session.codex_reasoning_effort, "xhigh")
            self.assertEqual(reloaded.codex_model, "gpt-5.3-codex-spark")
            self.assertEqual(reloaded.codex_reasoning_effort, "xhigh")

    def test_update_session_execution_settings_persists_for_running_session(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService
        from mastermind_bridge.orchestrator.state import load_session, session_path

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            sessions_dir.mkdir(parents=True)
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
                            "loop_state": "waiting_for_chatgpt_response",
                            "auto_run_enabled": True,
                            "supervisor_status": "running",
                            "codex_model": "gpt-5.4-mini",
                            "codex_reasoning_effort": "medium",
                        },
                    }
                )
            )
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=_FakeSupervisorManager(),
            )

            payload = service.update_session_execution_settings(
                "session-1",
                {
                    "codex_model": "gpt-5.5",
                    "codex_reasoning_effort": "xhigh",
                },
            )
            reloaded = load_session(session_path(sessions_dir, "session-1"))

            self.assertEqual(payload["codex_model"], "gpt-5.5")
            self.assertEqual(payload["codex_reasoning_effort"], "xhigh")
            self.assertEqual(reloaded.codex_model, "gpt-5.5")
            self.assertEqual(reloaded.codex_reasoning_effort, "xhigh")

    def test_render_dashboard_uses_contextual_actions_for_running_session(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            sessions_dir.mkdir(parents=True)
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
                            "loop_state": "waiting_for_chatgpt_response",
                            "time_budget_minutes": 90,
                            "budget_remaining_minutes": 80,
                            "auto_run_enabled": True,
                            "supervisor_status": "running",
                            "current_codex_thread_id": "codex-thread-123",
                        },
                    }
                )
            )
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=_FakeSupervisorManager(),
            )

            html = service.render_dashboard()

            self.assertIn("controlSession('session-1', 'pause')", html)
            self.assertIn("controlSession('session-1', 'stop')", html)
            self.assertIn("controlSession('session-1', 'open-codex-thread')", html)
            self.assertIn("Open Live Monitor", html)
            self.assertNotIn("controlSession('session-1', 'start')", html)
            self.assertNotIn("controlSession('session-1', 'start-twice')", html)
            self.assertNotIn("controlSession('session-1', 'resume')", html)

    def test_render_dashboard_shows_resume_for_blocked_session(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            sessions_dir.mkdir(parents=True)
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
                            "status": "blocked",
                            "loop_state": "requires_human",
                            "time_budget_minutes": 90,
                            "budget_remaining_minutes": 80,
                            "auto_run_enabled": False,
                            "supervisor_status": "blocked",
                            "human_attention_reason": "DNS failed.",
                        },
                    }
                )
            )
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=_FakeSupervisorManager(),
            )

            html = service.render_dashboard()

            self.assertIn("controlSession('session-1', 'resume')", html)
            self.assertIn(">Start</button>", html)
            self.assertNotIn("Continue Auto", html)
            self.assertNotIn("Continue Once", html)

    def test_render_dashboard_shows_delete_for_inactive_session_and_disables_it_for_running_session(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            sessions_dir.mkdir(parents=True)
            (sessions_dir / "session-running.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "session": {
                            "session_id": "session-running",
                            "binding_id": "binding-1",
                            "repo_path": "/tmp/repo",
                            "workspace_path": "/tmp/repo",
                            "chat_url": "https://chatgpt.com/c/project/binding-1",
                            "status": "active",
                            "loop_state": "waiting_for_chatgpt_response",
                            "time_budget_minutes": 90,
                            "budget_remaining_minutes": 80,
                            "auto_run_enabled": True,
                            "supervisor_status": "running",
                        },
                    }
                )
            )
            (sessions_dir / "session-old.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "session": {
                            "session_id": "session-old",
                            "binding_id": "binding-1",
                            "repo_path": "/tmp/repo",
                            "workspace_path": "/tmp/repo",
                            "chat_url": "https://chatgpt.com/c/project/binding-1",
                            "status": "completed",
                            "loop_state": "idle",
                            "time_budget_minutes": 90,
                            "budget_remaining_minutes": 0,
                            "auto_run_enabled": False,
                            "supervisor_status": "stopped",
                        },
                    }
                )
            )
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=_FakeSupervisorManager(),
            )

            html = service.render_dashboard()

            self.assertIn("controlSession('session-old', 'delete')", html)
            self.assertIn("Delete session", html)
            self.assertIn("Stop the session before deleting it", html)
            self.assertIn("session-running", html)

    def test_delete_session_removes_inactive_session_and_clears_binding_last_session_id(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService
        from mastermind_bridge.orchestrator.state import load_chat_bindings, save_session, session_path

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            manager = _FakeSupervisorManager()
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=manager,
            )

            service.create_binding(
                {
                    "binding_id": "binding-1",
                    "project_name": "Bridge",
                    "repo_path": "/tmp/repo",
                    "workspace_path": "/tmp/repo",
                    "chat_url": "https://chatgpt.com/c/project/binding-1",
                }
            )
            session = service.create_session(
                {
                    "session_id": "session-old",
                    "binding_id": "binding-1",
                    "time_budget_minutes": 30,
                }
            )
            session.status = "completed"
            session.auto_run_enabled = False
            session.supervisor_status = "stopped"
            save_session(session_path(sessions_dir, session.session_id), session)

            payload = service.delete_session("session-old")

            self.assertEqual(payload["session_id"], "session-old")
            self.assertEqual(payload["status"], "deleted")
            self.assertFalse(session_path(sessions_dir, "session-old").exists())
            self.assertEqual(manager.stopped, ["session-old"])
            self.assertEqual(load_chat_bindings(bindings_path)[0].last_session_id, "")

    def test_delete_session_rejects_path_traversal_session_id(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            sessions_dir.mkdir(parents=True)
            manager = _FakeSupervisorManager()
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=manager,
            )

            with self.assertRaisesRegex(ValueError, "path separators"):
                service.delete_session("../session-old")

            self.assertEqual(manager.stopped, [])

    def test_delete_session_rejects_running_session(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService
        from mastermind_bridge.orchestrator.state import session_path

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            manager = _FakeSupervisorManager()
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=manager,
            )

            service.create_binding(
                {
                    "binding_id": "binding-1",
                    "project_name": "Bridge",
                    "repo_path": "/tmp/repo",
                    "workspace_path": "/tmp/repo",
                    "chat_url": "https://chatgpt.com/c/project/binding-1",
                }
            )
            service.create_session(
                {
                    "session_id": "session-running",
                    "binding_id": "binding-1",
                    "time_budget_minutes": 30,
                }
            )
            with patch(
                "mastermind_bridge.orchestrator.control_panel._open_codex_live_monitor",
                return_value={"session_id": "session-running", "repo_path": "/tmp/repo", "log_path": "/tmp/log"},
            ):
                service.start_session("session-running")

            with self.assertRaisesRegex(ValueError, "Stop the session before deleting it"):
                service.delete_session("session-running")

            self.assertTrue(session_path(sessions_dir, "session-running").exists())
            self.assertEqual(manager.stopped, [])

    def test_render_dashboard_surfaces_last_error_and_human_attention_reason(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            sessions_dir.mkdir(parents=True)
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
                            "status": "paused",
                            "loop_state": "requires_human",
                            "time_budget_minutes": 90,
                            "budget_remaining_minutes": 80,
                            "auto_run_enabled": False,
                            "supervisor_status": "blocked",
                            "human_attention_reason": "Delivery requires human attention.",
                            "last_error": "ChatGPT DOM contract missing `composer` selector match.",
                        },
                    }
                )
            )
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=_FakeSupervisorManager(),
            )

            html = service.render_dashboard()

            self.assertIn("Delivery requires human attention.", html)
            self.assertIn("ChatGPT DOM contract missing", html)

    def test_snapshot_includes_latest_codex_run_metadata(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            run_dir = root / "artifacts" / "runs" / "20260415T230310-session-1"
            run_dir.mkdir(parents=True)
            (run_dir / "run_report.json").write_text(
                json.dumps(
                    {
                        "timestamp": "2026-04-15T23:05:54+02:00",
                        "thread_id": "session-1",
                        "summary": "Codex verified the repo state.",
                        "files_touched": [],
                        "checks": [],
                        "blockers": [],
                        "risks": [],
                        "next_step": "Ask the human whether to switch repos.",
                        "observed_codex_thread_id": "codex-thread-123",
                        "thread_action": "same_thread",
                        "estimated_context_remaining_percent": 39,
                        "context_continuity_percent": 62,
                        "continuity_band": "medium",
                        "delivery_status": "delivered",
                        "return_packet_id": "packet-abc",
                    }
                )
            )
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            sessions_dir.mkdir(parents=True)
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
                            "loop_state": "requires_human",
                            "auto_run_enabled": False,
                            "supervisor_status": "blocked",
                        },
                    }
                )
            )
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=_FakeSupervisorManager(),
            )

            snapshot = service.snapshot()

            session = snapshot["sessions"][0]
            self.assertEqual(session["latest_run"]["artifacts_dir"], str(run_dir))
            self.assertEqual(session["latest_run"]["summary"], "Codex verified the repo state.")
            self.assertEqual(session["latest_run"]["observed_codex_thread_id"], "codex-thread-123")
            self.assertEqual(session["latest_run"]["thread_action"], "same_thread")
            self.assertEqual(session["latest_run"]["estimated_context_remaining_percent"], "39")
            self.assertEqual(session["latest_run"]["context_continuity_percent"], "62")
            self.assertEqual(session["latest_run"]["continuity_band"], "medium")
            self.assertEqual(session["latest_run"]["delivery_status"], "delivered")
            self.assertEqual(session["latest_run"]["return_packet_id"], "packet-abc")

    def test_snapshot_marks_running_compaction_as_post_run_pending(self):
        from datetime import datetime

        from mastermind_bridge.orchestrator.control_panel import ControlPanelService

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            run_dir = root / "artifacts" / "runs" / "20260416T100000-session-1"
            run_dir.mkdir(parents=True)
            (run_dir / "run_report.json").write_text(
                json.dumps(
                    {
                        "exit_code": 0,
                        "summary": "Codex completed the turn.",
                        "observed_codex_thread_id": "codex-thread-123",
                        "codex_compaction": {
                            "status": "running",
                            "thread_id": "codex-thread-123",
                            "started_at": "2026-04-16T10:00:00+00:00",
                        },
                    }
                ),
                encoding="utf-8",
            )
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            sessions_dir.mkdir(parents=True)
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
                            "loop_state": "starting_codex",
                            "auto_run_enabled": True,
                            "supervisor_status": "running",
                            "supervisor_heartbeat_at": "2026-04-16T10:01:00+00:00",
                            "phase_started_at": "2026-04-16T10:00:00+00:00",
                            "last_codex_activity_at": "2026-04-16T10:00:10+00:00",
                        },
                    }
                ),
                encoding="utf-8",
            )
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=_FakeSupervisorManager(),
            )

            with patch(
                "mastermind_bridge.orchestrator.control_panel.time.time",
                return_value=datetime.fromisoformat("2026-04-16T10:02:00+00:00").timestamp(),
            ):
                snapshot = service.snapshot()
                html = service.render_dashboard()

            session = snapshot["sessions"][0]
            self.assertEqual(session["latest_run"]["status"], "compacting")
            self.assertEqual(session["latest_run"]["compaction_status"], "running")
            self.assertEqual(session["health"]["status"], "post_run_pending")
            self.assertIn("post-run compaction is still running", session["health"]["reason"])
            self.assertIn("Post-run pending", html)
            self.assertIn("Compaction", html)

    def test_snapshot_marks_stale_running_compaction_as_suspected_hang(self):
        from datetime import datetime

        from mastermind_bridge.orchestrator.control_panel import ControlPanelService

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            run_dir = root / "artifacts" / "runs" / "20260416T100000-session-1"
            run_dir.mkdir(parents=True)
            (run_dir / "run_report.json").write_text(
                json.dumps(
                    {
                        "exit_code": 0,
                        "summary": "Codex completed the turn.",
                        "observed_codex_thread_id": "codex-thread-123",
                        "codex_compaction": {
                            "status": "running",
                            "thread_id": "codex-thread-123",
                            "started_at": "2026-04-16T10:00:00+00:00",
                        },
                    }
                ),
                encoding="utf-8",
            )
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            sessions_dir.mkdir(parents=True)
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
                            "loop_state": "starting_codex",
                            "auto_run_enabled": True,
                            "supervisor_status": "running",
                            "supervisor_heartbeat_at": "2026-04-16T10:16:00+00:00",
                            "phase_started_at": "2026-04-16T10:00:00+00:00",
                            "last_codex_activity_at": "2026-04-16T10:00:10+00:00",
                        },
                    }
                ),
                encoding="utf-8",
            )
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=_FakeSupervisorManager(),
            )

            with patch(
                "mastermind_bridge.orchestrator.control_panel.time.time",
                return_value=datetime.fromisoformat("2026-04-16T10:16:00+00:00").timestamp(),
            ):
                session = service.snapshot()["sessions"][0]

            self.assertEqual(session["latest_run"]["status"], "compacting")
            self.assertEqual(session["health"]["status"], "suspected_hang")
            self.assertIn("Post-run compaction has been running for over 15 minutes", session["health"]["reason"])

    def test_snapshot_reports_browser_transport_mode(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "bindings": [
                            {
                                "binding_id": "binding-1",
                                "project_name": "Bridge",
                                "repo_path": "/tmp/repo",
                                "workspace_path": "/tmp/repo",
                                "chat_url": "https://chatgpt.com/c/project/binding-1",
                                "browser_profile_path": "/tmp/profile",
                                "browser_session_handle": "default",
                            }
                        ],
                    }
                )
            )
            policy_path.write_text(json.dumps({"version": 1}))
            sessions_dir.mkdir(parents=True)
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
                        },
                    }
                )
            )
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=_FakeSupervisorManager(),
            )

            snapshot = service.snapshot()

            self.assertIn("browser_transport_mode", snapshot["sessions"][0])

    def test_render_dashboard_shows_latest_run_thread_action_delivery_and_packet(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            run_dir = root / "artifacts" / "runs" / "20260415T230310-session-1"
            run_dir.mkdir(parents=True)
            (run_dir / "run_report.json").write_text(
                json.dumps(
                    {
                        "timestamp": "2026-04-15T23:05:54+02:00",
                        "thread_id": "session-1",
                        "summary": "Codex verified the repo state.",
                        "files_touched": [],
                        "checks": [],
                        "blockers": [],
                        "risks": [],
                        "next_step": "Continue in the same thread.",
                        "observed_codex_thread_id": "codex-thread-123",
                        "thread_action": "same_thread",
                        "estimated_context_remaining_percent": 39,
                        "context_continuity_percent": 62,
                        "continuity_band": "medium",
                        "delivery_status": "delivered",
                        "return_packet_id": "packet-abc",
                    }
                )
            )
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            sessions_dir.mkdir(parents=True)
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
                            "loop_state": "waiting_for_chatgpt_response",
                            "auto_run_enabled": True,
                            "supervisor_status": "running",
                        },
                    }
                )
            )
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=_FakeSupervisorManager(),
            )

            html = service.render_dashboard()

            self.assertIn("Thread action:</strong> same_thread", html)
            self.assertIn("Context left:</strong> 39%", html)
            self.assertIn("Continuity:</strong> 62% (medium)", html)
            self.assertIn("Delivery:</strong> delivered", html)
            self.assertIn("Return packet:</strong> packet-abc", html)

    def test_render_dashboard_hides_stale_human_attention_for_active_waiting_session(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            sessions_dir.mkdir(parents=True)
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
                            "loop_state": "waiting_for_chatgpt",
                            "time_budget_minutes": 90,
                            "budget_remaining_minutes": 80,
                            "auto_run_enabled": True,
                            "supervisor_status": "running",
                            "human_attention_reason": "Old launcher failure",
                        },
                    }
                )
            )
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=_FakeSupervisorManager(),
            )

            html = service.render_dashboard()

            self.assertNotIn("<strong>Needs human:</strong> Old launcher failure", html)

    def test_service_can_create_and_start_a_session_without_cli(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            manager = _FakeSupervisorManager()
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=manager,
            )

            binding = service.create_binding(
                {
                    "binding_id": "binding-1",
                    "project_name": "Bridge",
                    "repo_path": "/tmp/repo",
                    "workspace_path": "/tmp/repo",
                    "chat_url": "https://chatgpt.com/c/project/binding-1",
                }
            )
            session = service.create_session(
                {
                    "session_id": "session-1",
                    "binding_id": binding.binding_id,
                    "time_budget_minutes": 120,
                }
            )
            started = service.start_session("session-1")

            self.assertEqual(session.session_id, "session-1")
            self.assertEqual(started["status"], "running")
            self.assertEqual(manager.started, ["session-1"])

    def test_start_session_single_cycle_sets_stop_after_cycle_requested(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService
        from mastermind_bridge.orchestrator.state import load_session, session_path

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            manager = _FakeSupervisorManager()
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=manager,
            )

            service.create_binding(
                {
                    "binding_id": "binding-1",
                    "project_name": "Bridge",
                    "repo_path": "/tmp/repo",
                    "workspace_path": "/tmp/repo",
                    "chat_url": "https://chatgpt.com/c/project/binding-1",
                }
            )
            service.create_session(
                {
                    "session_id": "session-1",
                    "binding_id": "binding-1",
                    "time_budget_minutes": 120,
                }
            )

            started = service.start_session("session-1", single_cycle=True)

            self.assertEqual(started["status"], "running")
            session = load_session(session_path(sessions_dir, "session-1"))
            self.assertTrue(session.stop_after_cycle_requested)
            self.assertEqual(manager.started, ["session-1"])

    def test_start_session_schedules_codex_terminal_auto_open(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            manager = _FakeSupervisorManager()
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=manager,
            )

            service.create_binding(
                {
                    "binding_id": "binding-1",
                    "project_name": "Bridge",
                    "repo_path": "/tmp/repo",
                    "workspace_path": "/tmp/repo",
                    "chat_url": "https://chatgpt.com/c/project/binding-1",
                }
            )
            session = service.create_session(
                {
                    "session_id": "session-1",
                    "binding_id": "binding-1",
                    "time_budget_minutes": 120,
                }
            )
            session.current_codex_run_id = "codex-thread-789"
            from mastermind_bridge.orchestrator.state import save_session, session_path

            save_session(session_path(sessions_dir, session.session_id), session)

            with patch(
                "mastermind_bridge.orchestrator.control_panel._open_codex_live_monitor",
                return_value={"session_id": "session-1", "repo_path": "/tmp/repo", "log_path": "/tmp/log"},
            ) as opener:
                started = service.start_session("session-1")

            self.assertEqual(started["status"], "running")
            opener.assert_called_once_with(
                session_id="session-1",
                repo_path="/tmp/repo",
                artifacts_root=root / "artifacts" / "runs",
            )

    def test_start_session_send_once_stops_before_return_packet(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService
        from mastermind_bridge.orchestrator.state import load_session, session_path

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            manager = _FakeSupervisorManager()
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=manager,
            )

            service.create_binding(
                {
                    "binding_id": "binding-1",
                    "project_name": "Bridge",
                    "repo_path": "/tmp/repo",
                    "workspace_path": "/tmp/repo",
                    "chat_url": "https://chatgpt.com/c/project/binding-1",
                }
            )
            service.create_session(
                {
                    "session_id": "session-1",
                    "binding_id": "binding-1",
                    "time_budget_minutes": 120,
                }
            )

            started = service.start_session("session-1", stop_before_return_packet=True)

            self.assertEqual(started["status"], "running")
            session = load_session(session_path(sessions_dir, "session-1"))
            self.assertTrue(session.stop_before_return_packet_requested)
            self.assertFalse(session.stop_after_cycle_requested)
            self.assertEqual(manager.started, ["session-1"])

    def test_resume_session_single_cycle_sets_stop_after_cycle_requested(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService
        from mastermind_bridge.orchestrator.state import load_session, save_session, session_path

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            manager = _FakeSupervisorManager()
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=manager,
            )

            service.create_binding(
                {
                    "binding_id": "binding-1",
                    "project_name": "Bridge",
                    "repo_path": "/tmp/repo",
                    "workspace_path": "/tmp/repo",
                    "chat_url": "https://chatgpt.com/c/project/binding-1",
                }
            )
            session = service.create_session(
                {
                    "session_id": "session-1",
                    "binding_id": "binding-1",
                    "time_budget_minutes": 120,
                }
            )
            session.status = "paused"
            session.auto_run_enabled = False
            session.supervisor_status = "paused"
            save_session(session_path(sessions_dir, "session-1"), session)

            resumed = service.resume_session("session-1", single_cycle=True)

            self.assertEqual(resumed["status"], "running")
            session = load_session(session_path(sessions_dir, "session-1"))
            self.assertTrue(session.stop_after_cycle_requested)
            self.assertEqual(manager.started, ["session-1"])

    def test_resume_session_send_once_stops_before_return_packet(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService
        from mastermind_bridge.orchestrator.state import load_session, save_session, session_path

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            manager = _FakeSupervisorManager()
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=manager,
            )

            service.create_binding(
                {
                    "binding_id": "binding-1",
                    "project_name": "Bridge",
                    "repo_path": "/tmp/repo",
                    "workspace_path": "/tmp/repo",
                    "chat_url": "https://chatgpt.com/c/project/binding-1",
                }
            )
            session = service.create_session(
                {
                    "session_id": "session-1",
                    "binding_id": "binding-1",
                    "time_budget_minutes": 120,
                }
            )
            session.status = "paused"
            session.auto_run_enabled = False
            session.supervisor_status = "paused"
            save_session(session_path(sessions_dir, "session-1"), session)

            resumed = service.resume_session("session-1", stop_before_return_packet=True)

            self.assertEqual(resumed["status"], "running")
            session = load_session(session_path(sessions_dir, "session-1"))
            self.assertTrue(session.stop_before_return_packet_requested)
            self.assertFalse(session.stop_after_cycle_requested)
            self.assertEqual(manager.started, ["session-1"])

    def test_resume_session_schedules_codex_terminal_auto_open(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService
        from mastermind_bridge.orchestrator.state import save_session, session_path

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            manager = _FakeSupervisorManager()
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=manager,
            )

            service.create_binding(
                {
                    "binding_id": "binding-1",
                    "project_name": "Bridge",
                    "repo_path": "/tmp/repo",
                    "workspace_path": "/tmp/repo",
                    "chat_url": "https://chatgpt.com/c/project/binding-1",
                }
            )
            session = service.create_session(
                {
                    "session_id": "session-1",
                    "binding_id": "binding-1",
                    "time_budget_minutes": 120,
                }
            )
            session.status = "paused"
            session.auto_run_enabled = False
            session.supervisor_status = "paused"
            session.current_codex_run_id = "codex-thread-789"
            save_session(session_path(sessions_dir, session.session_id), session)

            with patch(
                "mastermind_bridge.orchestrator.control_panel._open_codex_live_monitor",
                return_value={"session_id": "session-1", "repo_path": "/tmp/repo", "log_path": "/tmp/log"},
            ) as opener:
                resumed = service.resume_session("session-1")

            self.assertEqual(resumed["status"], "running")
            opener.assert_called_once_with(
                session_id="session-1",
                repo_path="/tmp/repo",
                artifacts_root=root / "artifacts" / "runs",
            )

    def test_resume_session_rearms_latest_assistant_after_paused_codex_run(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService
        from mastermind_bridge.orchestrator.state import load_session, save_session, session_path

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            manager = _FakeSupervisorManager()
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=manager,
            )

            service.create_binding(
                {
                    "binding_id": "binding-1",
                    "project_name": "Bridge",
                    "repo_path": "/tmp/repo",
                    "workspace_path": "/tmp/repo",
                    "chat_url": "https://chatgpt.com/c/project/binding-1",
                }
            )
            session = service.create_session(
                {
                    "session_id": "session-1",
                    "binding_id": "binding-1",
                    "time_budget_minutes": 120,
                }
            )
            session.status = "paused"
            session.loop_state = "paused"
            session.auto_run_enabled = False
            session.supervisor_status = "paused"
            session.current_codex_thread_id = "codex-thread-789"
            session.current_codex_run_id = "codex-thread-789"
            session.last_seen_chat_message_anchor = "assistant-4-abc123"
            session.latest_assistant_message_id = "assistant-message-1"
            session.latest_assistant_message_hash = "abc123"
            session.last_chat_activity_at = "2026-04-21T16:16:11+02:00"
            save_session(session_path(sessions_dir, session.session_id), session)

            resumed = service.resume_session("session-1")

            self.assertEqual(resumed["status"], "running")
            refreshed = load_session(session_path(sessions_dir, "session-1"))
            self.assertEqual(refreshed.last_seen_chat_message_anchor, "")
            self.assertEqual(refreshed.latest_assistant_message_id, "")
            self.assertEqual(refreshed.latest_assistant_message_hash, "")
            self.assertEqual(manager.started, ["session-1"])

    def test_start_session_clears_stale_runtime_error_fields(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService
        from mastermind_bridge.orchestrator.state import load_session, session_path

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            manager = _FakeSupervisorManager()
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=manager,
            )

            service.create_binding(
                {
                    "binding_id": "binding-1",
                    "project_name": "Bridge",
                    "repo_path": "/tmp/repo",
                    "workspace_path": "/tmp/repo",
                    "chat_url": "https://chatgpt.com/c/project/binding-1",
                }
            )
            session = service.create_session(
                {
                    "session_id": "session-1",
                    "binding_id": "binding-1",
                    "time_budget_minutes": 120,
                }
            )
            session.human_attention_reason = "[Errno 2] No such file or directory: 'codex'"
            session.last_error = "old error"
            session.last_seen_chat_message_anchor = "msg-assistant-1"
            session.latest_assistant_message_hash = "hash-1"
            from mastermind_bridge.orchestrator.state import save_session
            save_session(session_path(sessions_dir, session.session_id), session)

            with patch(
                "mastermind_bridge.orchestrator.control_panel._open_codex_live_monitor",
                return_value={"session_id": "session-1", "repo_path": "/tmp/repo", "log_path": "/tmp/log"},
            ):
                service.start_session("session-1")

            session = load_session(session_path(sessions_dir, "session-1"))
            self.assertEqual(session.human_attention_reason, "")
            self.assertEqual(session.last_error, "")
            self.assertEqual(session.last_seen_chat_message_anchor, "")
            self.assertEqual(session.latest_assistant_message_hash, "")
            self.assertTrue(session.auto_run_enabled)
            self.assertEqual(session.supervisor_status, "running")

    def test_open_latest_run_opens_artifact_directory(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            run_dir = root / "artifacts" / "runs" / "20260415T230310-session-1"
            run_dir.mkdir(parents=True)
            (run_dir / "prompt.md").write_text("prompt")
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            sessions_dir.mkdir(parents=True)
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
                        },
                    }
                )
            )
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=_FakeSupervisorManager(),
            )

            with patch("mastermind_bridge.orchestrator.control_panel.subprocess.run") as run:
                payload = service.open_latest_run("session-1")

            run.assert_called_once_with(["open", str(run_dir)], check=True)
            self.assertEqual(payload["artifacts_dir"], str(run_dir))

    @patch("mastermind_bridge.orchestrator.control_panel.subprocess.run")
    def test_open_codex_app_thread_targets_openai_bundle_id(self, run_mock):
        from mastermind_bridge.orchestrator.control_panel import _open_codex_app_thread

        _open_codex_app_thread("thread-123")

        run_mock.assert_called_once()
        self.assertEqual(run_mock.call_args.args[0], ["osascript", "-"])
        script = run_mock.call_args.kwargs["input"]
        self.assertIn('application id "com.openai.codex"', script)
        self.assertIn('open location "codex://threads/thread-123"', script)

    def test_open_latest_codex_thread_opens_live_monitor(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            run_dir = root / "artifacts" / "runs" / "20260415T230310-session-1"
            run_dir.mkdir(parents=True)
            (run_dir / "run_report.json").write_text(
                json.dumps(
                    {
                        "timestamp": "2026-04-15T23:05:54+02:00",
                        "thread_id": "session-1",
                        "summary": "Codex verified the repo state.",
                        "files_touched": [],
                        "checks": [],
                        "blockers": [],
                        "risks": [],
                        "next_step": "Ask the human whether to switch repos.",
                        "observed_codex_thread_id": "codex-thread-123",
                    }
                )
            )
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            sessions_dir.mkdir(parents=True)
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
                        },
                    }
                )
            )
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=_FakeSupervisorManager(),
            )

            with patch("mastermind_bridge.orchestrator.control_panel._open_terminal_with_command") as opener:
                payload = service.open_latest_codex_thread("session-1")

            opener.assert_called_once_with(
                "cd "
                + str(repo_root())
                + " && PYTHONUNBUFFERED=1 PYTHONPATH="
                + str(repo_root() / "src")
                + " python3 -m chatgpt_codex_bridge.control_panel_runtime --session-id session-1 --workspace /tmp/repo --artifacts-root "
                + str(root / "artifacts" / "runs")
                + " --tail-lines 80 --no-initial-prompt"
            )
            self.assertEqual(payload["session_id"], "session-1")
            self.assertEqual(payload["log_path"], str(root / "artifacts" / "session_logs" / "session-1.log"))

    def test_open_latest_codex_thread_does_not_require_existing_thread_id(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            sessions_dir.mkdir(parents=True)
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
                        },
                    }
                )
            )
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=_FakeSupervisorManager(),
            )

            with patch("mastermind_bridge.orchestrator.control_panel._open_terminal_with_command") as opener:
                payload = service.open_latest_codex_thread("session-1")

            opener.assert_called_once_with(
                "cd "
                + str(repo_root())
                + " && PYTHONUNBUFFERED=1 PYTHONPATH="
                + str(repo_root() / "src")
                + " python3 -m chatgpt_codex_bridge.control_panel_runtime --session-id session-1 --workspace /tmp/repo --artifacts-root "
                + str(root / "artifacts" / "runs")
                + " --tail-lines 80 --no-initial-prompt"
            )
            self.assertEqual(payload["session_id"], "session-1")

    def test_terminal_live_monitor_command_runs_from_bridge_repo_and_displays_workspace(self):
        from mastermind_bridge.orchestrator.control_panel import _terminal_live_monitor_command

        command = _terminal_live_monitor_command(
            session_id="session-1",
            repo_path="/tmp/empty-workspace",
            artifacts_root=Path("/tmp/bridge-artifacts"),
        )

        self.assertTrue(command.startswith("cd " + str(repo_root()) + " && "))
        self.assertIn("python3 -m chatgpt_codex_bridge.control_panel_runtime --session-id session-1", command)
        self.assertIn("--workspace /tmp/empty-workspace", command)
        self.assertIn("--artifacts-root /tmp/bridge-artifacts", command)
        self.assertIn("--tail-lines 80", command)
        self.assertIn("--no-initial-prompt", command)

    def test_codex_terminal_open_watcher_opens_live_monitor_once_run_starts(self):
        from mastermind_bridge.orchestrator.control_panel import _start_codex_terminal_open_watcher

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            sessions_dir.mkdir(parents=True)
            session_payload = {
                "version": 1,
                "session": {
                    "session_id": "session-1",
                    "binding_id": "binding-1",
                    "repo_path": "/tmp/repo",
                    "workspace_path": "/tmp/repo",
                    "chat_url": "https://chatgpt.com/c/project/binding-1",
                    "status": "active",
                    "loop_state": "idle",
                    "current_codex_run_id": "codex-thread-789",
                },
            }
            (sessions_dir / "session-1.json").write_text(json.dumps(session_payload))

            with patch("mastermind_bridge.orchestrator.control_panel._open_codex_live_monitor") as opener:
                handle = _start_codex_terminal_open_watcher(
                    sessions_dir=sessions_dir,
                    artifacts_root=root / "artifacts" / "runs",
                    session_id="session-1",
                    repo_path="/tmp/repo",
                    baseline_thread_id="codex-thread-789",
                    baseline_artifacts_dir="",
                )
                self.assertIsNotNone(handle)
                session_payload = json.loads((sessions_dir / "session-1.json").read_text())
                session_payload["session"]["loop_state"] = "starting_codex"
                (sessions_dir / "session-1.json").write_text(json.dumps(session_payload))
                if handle is not None:
                    stop_event, watcher = handle
                    watcher.join(timeout=2.0)
                    stop_event.set()

            opener.assert_called_once_with(
                session_id="session-1",
                repo_path="/tmp/repo",
                artifacts_root=root / "artifacts" / "runs",
            )

    def test_open_latest_codex_app_thread_opens_deeplink(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            run_dir = root / "artifacts" / "runs" / "20260415T230310-session-1"
            run_dir.mkdir(parents=True)
            (run_dir / "run_report.json").write_text(
                json.dumps(
                    {
                        "timestamp": "2026-04-15T23:05:54+02:00",
                        "thread_id": "session-1",
                        "summary": "Codex verified the repo state.",
                        "files_touched": [],
                        "checks": [],
                        "blockers": [],
                        "risks": [],
                        "next_step": "Ask the human whether to switch repos.",
                        "observed_codex_thread_id": "codex-thread-123",
                    }
                )
            )
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            sessions_dir.mkdir(parents=True)
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
                        },
                    }
                )
            )
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=_FakeSupervisorManager(),
            )

            with patch("mastermind_bridge.orchestrator.control_panel._open_codex_app_thread") as opener:
                payload = service.open_latest_codex_app_thread("session-1")

            opener.assert_called_once_with("codex-thread-123")
            self.assertEqual(payload["thread_id"], "codex-thread-123")
            self.assertEqual(payload["deeplink"], "codex://threads/codex-thread-123")

    def test_start_session_does_not_auto_open_existing_codex_thread_in_app_by_default(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            manager = _FakeSupervisorManager()
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=manager,
            )

            service.create_binding(
                {
                    "binding_id": "binding-1",
                    "project_name": "Bridge",
                    "repo_path": "/tmp/repo",
                    "workspace_path": "/tmp/repo",
                    "chat_url": "https://chatgpt.com/c/project/binding-1",
                }
            )
            session = service.create_session(
                {
                    "session_id": "session-1",
                    "binding_id": "binding-1",
                    "time_budget_minutes": 120,
                }
            )
            session.current_codex_run_id = "codex-thread-789"
            from mastermind_bridge.orchestrator.state import save_session, session_path

            save_session(session_path(sessions_dir, session.session_id), session)

            with (
                patch(
                    "mastermind_bridge.orchestrator.control_panel._open_codex_live_monitor",
                    return_value={"session_id": "session-1", "repo_path": "/tmp/repo", "log_path": "/tmp/log"},
                ),
                patch("mastermind_bridge.orchestrator.control_panel._open_codex_app_thread") as opener,
            ):
                started = service.start_session("session-1")

            self.assertEqual(started["status"], "running")
            opener.assert_not_called()

    @patch.dict(os.environ, {"BRIDGE_AUTO_OPEN_CODEX_APP_THREADS": "1"}, clear=False)
    def test_start_session_auto_opens_existing_codex_thread_in_app_when_enabled(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            manager = _FakeSupervisorManager()
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=manager,
            )

            service.create_binding(
                {
                    "binding_id": "binding-1",
                    "project_name": "Bridge",
                    "repo_path": "/tmp/repo",
                    "workspace_path": "/tmp/repo",
                    "chat_url": "https://chatgpt.com/c/project/binding-1",
                }
            )
            session = service.create_session(
                {
                    "session_id": "session-1",
                    "binding_id": "binding-1",
                    "time_budget_minutes": 120,
                }
            )
            session.current_codex_run_id = "codex-thread-789"
            from mastermind_bridge.orchestrator.state import save_session, session_path

            save_session(session_path(sessions_dir, session.session_id), session)

            with (
                patch(
                    "mastermind_bridge.orchestrator.control_panel._open_codex_live_monitor",
                    return_value={"session_id": "session-1", "repo_path": "/tmp/repo", "log_path": "/tmp/log"},
                ),
                patch("mastermind_bridge.orchestrator.control_panel._open_codex_app_thread") as opener,
            ):
                started = service.start_session("session-1")

            self.assertEqual(started["status"], "running")
            opener.assert_called_once_with("codex-thread-789")

    @patch.dict(os.environ, {"BRIDGE_ENABLE_CODEX_APP_INTEGRATION": "1"}, clear=False)
    def test_resume_session_does_not_auto_open_existing_codex_thread_when_only_app_integration_is_enabled(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService
        from mastermind_bridge.orchestrator.state import load_session, save_session, session_path

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            manager = _FakeSupervisorManager()
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=manager,
            )

            service.create_binding(
                {
                    "binding_id": "binding-1",
                    "project_name": "Bridge",
                    "repo_path": "/tmp/repo",
                    "workspace_path": "/tmp/repo",
                    "chat_url": "https://chatgpt.com/c/project/binding-1",
                }
            )
            session = service.create_session(
                {
                    "session_id": "session-1",
                    "binding_id": "binding-1",
                    "time_budget_minutes": 120,
                }
            )
            session.status = "paused"
            session.auto_run_enabled = False
            session.supervisor_status = "paused"
            session.current_codex_run_id = "codex-thread-789"
            save_session(session_path(sessions_dir, session.session_id), session)

            with (
                patch(
                    "mastermind_bridge.orchestrator.control_panel._open_codex_live_monitor",
                    return_value={"session_id": "session-1", "repo_path": "/tmp/repo", "log_path": "/tmp/log"},
                ),
                patch("mastermind_bridge.orchestrator.control_panel._open_codex_app_thread") as opener,
            ):
                resumed = service.resume_session("session-1")

            self.assertEqual(resumed["status"], "running")
            opener.assert_not_called()
            refreshed = load_session(session_path(sessions_dir, "session-1"))
            self.assertEqual(refreshed.loop_state, "idle")
            self.assertEqual(refreshed.latest_user_control_command, "")
            self.assertEqual(refreshed.policy_decision.policy_outcome, "allow")

    def test_pause_session_marks_loop_state_and_policy(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            manager = _FakeSupervisorManager()
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=manager,
            )

            service.create_binding(
                {
                    "binding_id": "binding-1",
                    "project_name": "Bridge",
                    "repo_path": "/tmp/repo",
                    "workspace_path": "/tmp/repo",
                    "chat_url": "https://chatgpt.com/c/project/binding-1",
                }
            )
            paused = service.create_session(
                {
                    "session_id": "session-1",
                    "binding_id": "binding-1",
                    "time_budget_minutes": 120,
                }
            )

            paused = service.pause_session("session-1")

            self.assertEqual(paused.status, "paused")
            self.assertEqual(paused.loop_state, "paused")
            self.assertEqual(paused.supervisor_status, "paused")
            self.assertEqual(paused.policy_decision.policy_outcome, "paused")
            self.assertEqual(manager.stopped, ["session-1"])

    def test_stop_session_terminates_locked_detached_supervisor(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            manager = _FakeSupervisorManager()
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=manager,
            )

            service.create_binding(
                {
                    "binding_id": "binding-1",
                    "project_name": "Bridge",
                    "repo_path": "/tmp/repo",
                    "workspace_path": "/tmp/repo",
                    "chat_url": "https://chatgpt.com/c/project/binding-1",
                }
            )
            service.create_session(
                {
                    "session_id": "session-1",
                    "binding_id": "binding-1",
                    "time_budget_minutes": 120,
                }
            )

            with patch(
                "mastermind_bridge.orchestrator.control_panel.terminate_locked_session_supervisor",
                return_value={"status": "terminated"},
            ) as terminator:
                stopped = service.stop_session("session-1", after_cycle=False)

            self.assertEqual(stopped.status, "completed")
            self.assertEqual(stopped.supervisor_status, "stopped")
            self.assertEqual(manager.stopped, ["session-1"])
            terminator.assert_called_once_with(root / "session_locks", "session-1")

    def test_pause_session_drains_pending_return_packet_retry_state(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            manager = _FakeSupervisorManager()
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=manager,
            )

            service.create_binding(
                {
                    "binding_id": "binding-1",
                    "project_name": "Bridge",
                    "repo_path": "/tmp/repo",
                    "workspace_path": "/tmp/repo",
                    "chat_url": "https://chatgpt.com/c/project/binding-1",
                }
            )
            session = service.create_session(
                {
                    "session_id": "session-1",
                    "binding_id": "binding-1",
                    "time_budget_minutes": 120,
                }
            )
            session.status = "active"
            session.loop_state = "posting_return_packet"
            session.auto_run_enabled = True
            session.supervisor_status = "running"
            session.last_outbound_user_message_anchor = "packet-stuck"
            session.last_outbound_user_message_kind = "return_packet_retry_pending"
            session.degraded_mode = "retrying_return_packet"
            session.degraded_reason = "Message delivery confirmation timed out."
            save_session(session_path(sessions_dir, session.session_id), session)

            paused = service.pause_session("session-1")

            self.assertEqual(paused.status, "active")
            self.assertEqual(paused.loop_state, "posting_return_packet")
            self.assertTrue(paused.auto_run_enabled)
            self.assertEqual(paused.supervisor_status, "running")
            self.assertEqual(paused.latest_user_control_command, "pause")
            self.assertEqual(paused.last_outbound_user_message_anchor, "packet-stuck")
            self.assertEqual(paused.last_outbound_user_message_kind, "return_packet_retry_pending")
            self.assertEqual(paused.degraded_mode, "retrying_return_packet")
            self.assertEqual(paused.degraded_reason, "Message delivery confirmation timed out.")
            self.assertEqual(paused.policy_decision.policy_outcome, "paused")
            self.assertIn("drain", paused.policy_decision.reasons[0])

    def test_quickstart_creates_binding_session_and_bootstrap_prompt(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=_FakeSupervisorManager(),
                default_repo_path="/tmp/repo",
                default_workspace_path="/tmp/repo",
                default_browser_profile_path="/tmp/repo/state/playwright-profile",
                default_browser_channel="chrome",
            )

            payload = service.quickstart_session(
                {
                    "chat_url": "https://chatgpt.com/c/project/test-chat",
                    "time_budget_minutes": 45,
                }
            )

            self.assertEqual(payload["session"]["time_budget_minutes"], 45)
            self.assertEqual(payload["binding"]["repo_path"], "/tmp/repo")
            self.assertEqual(payload["binding"]["browser_profile_path"], "/tmp/repo/state/playwright-profile")
            self.assertEqual(payload["binding"]["browser_channel"], "chrome")
            self.assertEqual(payload["binding"]["browser_session_handle"], "default")
            self.assertIn(payload["session"]["session_id"], payload["bootstrap_prompt"])
            self.assertTrue(payload["bootstrap_prompt"].startswith(f"Session id: {payload['session']['session_id']}\n"))
            self.assertIn("refresh your understanding of the current project sources and plan", payload["bootstrap_prompt"])

    def test_quickstart_default_browser_profile_uses_bridge_home(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bridge_home = root / "bridge-home"
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            os.environ["BRIDGE_HOME"] = str(bridge_home)
            try:
                service = ControlPanelService(
                    bindings_path=bindings_path,
                    policy_path=policy_path,
                    sessions_dir=sessions_dir,
                    supervisor_manager=_FakeSupervisorManager(),
                    default_repo_path="/tmp/repo",
                    default_workspace_path="/tmp/repo",
                    default_browser_channel="chrome",
                )
            finally:
                os.environ.pop("BRIDGE_HOME", None)

            payload = service.quickstart_session(
                {
                    "chat_url": "https://chatgpt.com/c/project/test-chat",
                    "time_budget_minutes": 45,
                }
            )

            self.assertEqual(
                payload["binding"]["browser_profile_path"],
                str(bridge_home / "state" / "playwright-profile"),
            )
            self.assertIn("analyze everything Codex returned deeply", payload["bootstrap_prompt"])
            self.assertIn("long-form execution brief", payload["bootstrap_prompt"])
            self.assertIn("keep durable doc work secondary to real progress", payload["bootstrap_prompt"])
            self.assertIn("if I pasted the latest relevant Codex thread or output below this message", payload["bootstrap_prompt"])
            self.assertIn("Use this session id exactly:", payload["bootstrap_prompt"])
            self.assertNotIn("bridge-control", payload["bootstrap_prompt"])
            self.assertNotIn("supervisor will choose same_thread versus new_thread locally", payload["bootstrap_prompt"])

    def test_quickstart_reuses_existing_binding_for_same_chat_url(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "bindings": [
                            {
                                "binding_id": "binding-existing",
                                "project_name": "personal-assistant-bridge",
                                "repo_path": str(root / "personal-assistant-bridge"),
                                "workspace_path": str(root / "personal-assistant-bridge"),
                                "chat_url": "https://chatgpt.com/g/g-p-123-personal-assistant-bridge/c/old-chat",
                                "browser_channel": "chrome",
                                "browser_session_handle": "default",
                            }
                        ],
                    }
                )
            )
            policy_path.write_text(json.dumps({"version": 1}))
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=_FakeSupervisorManager(),
                default_repo_path=str(root / "chatgpt-codex-bridge"),
                default_workspace_path=str(root / "chatgpt-codex-bridge"),
                default_browser_profile_path=str(root / "chatgpt-codex-bridge" / "state" / "playwright-profile"),
                default_browser_channel="chrome",
            )

            payload = service.quickstart_session(
                {
                    "chat_url": "https://chatgpt.com/g/g-p-123-personal-assistant-bridge/c/old-chat?model=gpt-5",
                    "time_budget_minutes": 45,
                }
            )

            self.assertEqual(payload["binding"]["binding_id"], "binding-existing")
            self.assertEqual(payload["binding"]["repo_path"], str(root / "personal-assistant-bridge"))
            bindings_payload = json.loads(bindings_path.read_text())
            self.assertEqual(len(bindings_payload["bindings"]), 1)

    def test_quickstart_explicit_repo_override_updates_binding_and_bootstrap_prompt(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "bindings": [
                            {
                                "binding_id": "binding-existing",
                                "project_name": "personal-assistant-bridge",
                                "repo_path": str(root / "personal-assistant-bridge"),
                                "workspace_path": str(root / "personal-assistant-bridge"),
                                "chat_url": "https://chatgpt.com/g/g-p-123-personal-assistant-bridge/c/old-chat",
                                "browser_channel": "chrome",
                                "browser_session_handle": "default",
                            }
                        ],
                    }
                )
            )
            policy_path.write_text(json.dumps({"version": 1}))
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=_FakeSupervisorManager(),
                default_repo_path=str(root / "chatgpt-codex-bridge"),
                default_workspace_path=str(root / "chatgpt-codex-bridge"),
                default_browser_profile_path=str(root / "chatgpt-codex-bridge" / "state" / "playwright-profile"),
                default_browser_channel="chrome",
            )

            payload = service.quickstart_session(
                {
                    "chat_url": "https://chatgpt.com/g/g-p-123-personal-assistant-bridge/c/old-chat?model=gpt-5",
                    "time_budget_minutes": 45,
                    "repo_path": str(root / "Test Repo"),
                    "workspace_path": str(root / "Test Repo"),
                }
            )

            self.assertEqual(payload["binding"]["repo_path"], str(root / "Test Repo"))
            self.assertEqual(payload["binding"]["workspace_path"], str(root / "Test Repo"))
            self.assertNotIn(str(root / "Test Repo"), payload["bootstrap_prompt"])

    def test_quickstart_infers_repo_from_chat_slug_when_unique_local_match_exists(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "chatgpt-codex-bridge").mkdir()
            (root / "personal-assistant-bridge").mkdir()
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=_FakeSupervisorManager(),
                default_repo_path=str(root / "chatgpt-codex-bridge"),
                default_workspace_path=str(root / "chatgpt-codex-bridge"),
                default_browser_profile_path=str(root / "chatgpt-codex-bridge" / "state" / "playwright-profile"),
                default_browser_channel="chrome",
            )

            payload = service.quickstart_session(
                {
                    "chat_url": "https://chatgpt.com/g/g-p-fixture-personal-assistant-bridge/c/fixture-chat",
                    "time_budget_minutes": 45,
                }
            )

            self.assertEqual(payload["binding"]["repo_path"], str(root / "personal-assistant-bridge"))
            self.assertEqual(payload["binding"]["workspace_path"], str(root / "personal-assistant-bridge"))
            self.assertEqual(payload["binding"]["project_name"], "personal-assistant-bridge")

    def test_quickstart_strips_shell_quotes_from_repo_and_workspace_overrides(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "chatgpt-codex-bridge").mkdir()
            (root / "Test Repo").mkdir()
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=_FakeSupervisorManager(),
                default_repo_path=str(root / "chatgpt-codex-bridge"),
                default_workspace_path=str(root / "chatgpt-codex-bridge"),
                default_browser_profile_path=str(root / "chatgpt-codex-bridge" / "state" / "playwright-profile"),
                default_browser_channel="chrome",
            )

            payload = service.quickstart_session(
                {
                    "chat_url": "https://chatgpt.com/c/test-quoted-paths",
                    "time_budget_minutes": 30,
                    "repo_path": "'/tmp/ignored'",
                    "workspace_path": f"'{root / 'Test Repo'}'",
                }
            )

            self.assertEqual(payload["binding"]["repo_path"], "/tmp/ignored")
            self.assertEqual(payload["binding"]["workspace_path"], str(root / "Test Repo"))
            self.assertEqual(payload["binding"]["project_name"], "ignored")

    def test_quickstart_prefers_repo_inference_over_family_binding_repo(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "chatgpt-codex-bridge").mkdir()
            (root / "personal-assistant-bridge").mkdir()
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "bindings": [
                            {
                                "binding_id": "binding-existing-family",
                                "project_name": "chatgpt-codex-bridge",
                                "repo_path": str(root / "chatgpt-codex-bridge"),
                                "workspace_path": str(root / "chatgpt-codex-bridge"),
                                "chat_url": "https://chatgpt.com/g/g-p-fixture-personal-assistant-bridge/c/old-chat",
                                "browser_channel": "chrome",
                                "browser_session_handle": "default",
                            }
                        ],
                    }
                )
            )
            policy_path.write_text(json.dumps({"version": 1}))
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=_FakeSupervisorManager(),
                default_repo_path=str(root / "chatgpt-codex-bridge"),
                default_workspace_path=str(root / "chatgpt-codex-bridge"),
                default_browser_profile_path=str(root / "chatgpt-codex-bridge" / "state" / "playwright-profile"),
                default_browser_channel="chrome",
            )

            payload = service.quickstart_session(
                {
                    "chat_url": "https://chatgpt.com/g/g-p-fixture-personal-assistant-bridge/c/new-chat",
                    "time_budget_minutes": 45,
                }
            )

            self.assertEqual(payload["binding"]["repo_path"], str(root / "personal-assistant-bridge"))
            self.assertEqual(payload["binding"]["browser_session_handle"], "default")

    def test_create_session_seeds_current_codex_thread_from_latest_repo_session(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService
        from mastermind_bridge.orchestrator.state import save_session, session_path
        from mastermind_bridge.orchestrator.models import OrchestratorSession

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "bindings": [
                            {
                                "binding_id": "binding-1",
                                "project_name": "Bridge",
                                "repo_path": "/tmp/repo",
                                "workspace_path": "/tmp/repo",
                                "chat_url": "https://chatgpt.com/c/project/binding-1",
                            }
                        ],
                    }
                )
            )
            policy_path.write_text(json.dumps({"version": 1}))
            sessions_dir.mkdir(parents=True)
            prior_session = OrchestratorSession(
                session_id="session-old",
                binding_id="binding-1",
                repo_path="/tmp/repo",
                workspace_path="/tmp/repo",
                chat_url="https://chatgpt.com/c/project/binding-1",
                current_codex_thread_id="codex-thread-123",
                current_codex_run_id="codex-thread-123",
            )
            save_session(session_path(sessions_dir, "session-old"), prior_session)
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=_FakeSupervisorManager(),
            )

            session = service.create_session(
                {
                    "session_id": "session-new",
                    "binding_id": "binding-1",
                    "time_budget_minutes": 120,
                }
            )

            self.assertEqual(session.current_codex_thread_id, "codex-thread-123")
            self.assertEqual(session.current_codex_run_id, "codex-thread-123")

    def test_snapshot_reports_health_timestamps_and_suspected_hang(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            sessions_dir.mkdir(parents=True)
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
                            "loop_state": "waiting_for_chatgpt_response",
                            "auto_run_enabled": True,
                            "supervisor_status": "running",
                            "supervisor_heartbeat_at": "2026-04-16T10:00:00+00:00",
                            "phase_started_at": "2026-04-16T09:00:00+00:00",
                            "last_chat_activity_at": "2026-04-16T09:30:00+00:00",
                            "last_codex_activity_at": "2026-04-16T08:45:00+00:00",
                            "last_delivery_at": "2026-04-16T08:50:00+00:00",
                        },
                    }
                )
            )
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=_FakeSupervisorManager(),
            )

            with patch("mastermind_bridge.orchestrator.control_panel.time.time", return_value=1_776_335_100.0):
                snapshot = service.snapshot()
                html = service.render_dashboard()

            session = snapshot["sessions"][0]
            self.assertIn("health", session)
            self.assertEqual(session["health"]["status"], "suspected_hang")
            self.assertIn("supervisor_heartbeat_at", session)
            self.assertIn("phase_started_at", session)
            self.assertIn("Heartbeat", html)
            self.assertIn("Suspected hang", html)
            self.assertIn("setInterval(refreshSessions", html)

    def test_snapshot_tolerates_slow_chatgpt_wait_window(self):
        from datetime import datetime

        from mastermind_bridge.orchestrator.control_panel import ControlPanelService

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            sessions_dir.mkdir(parents=True)
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
                            "loop_state": "waiting_for_chatgpt_response",
                            "auto_run_enabled": True,
                            "supervisor_status": "running",
                            "supervisor_heartbeat_at": "2026-04-16T10:00:00+00:00",
                            "phase_started_at": "2026-04-16T10:00:00+00:00",
                            "last_chat_activity_at": "2026-04-16T10:00:00+00:00",
                            "last_codex_activity_at": "2026-04-16T10:00:00+00:00",
                            "last_delivery_at": "2026-04-16T10:01:45+00:00",
                        },
                    }
                )
            )
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=_FakeSupervisorManager(),
            )

            with patch(
                "mastermind_bridge.orchestrator.control_panel.time.time",
                return_value=datetime.fromisoformat("2026-04-16T10:02:00+00:00").timestamp(),
            ):
                session = service.snapshot()["sessions"][0]

            self.assertEqual(session["health"]["status"], "healthy")
            self.assertLess(session["health"]["heartbeat_age_seconds"], 180)

    def test_snapshot_marks_recent_delivered_packet_as_waiting_despite_stale_heartbeat(self):
        from datetime import datetime

        from mastermind_bridge.orchestrator.control_panel import ControlPanelService

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            sessions_dir.mkdir(parents=True)
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
                            "loop_state": "waiting_for_chatgpt_response",
                            "auto_run_enabled": True,
                            "supervisor_status": "running",
                            "supervisor_heartbeat_at": "2026-04-16T10:00:00+00:00",
                            "phase_started_at": "2026-04-16T10:38:36+00:00",
                            "last_chat_activity_at": "2026-04-16T10:21:46+00:00",
                            "last_codex_activity_at": "2026-04-16T10:38:07+00:00",
                            "last_delivery_at": "2026-04-16T10:38:36+00:00",
                            "last_outbound_user_message_kind": "return_packet",
                        },
                    }
                )
            )
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=_FakeSupervisorManager(),
            )

            with patch(
                "mastermind_bridge.orchestrator.control_panel.time.time",
                return_value=datetime.fromisoformat("2026-04-16T11:15:00+00:00").timestamp(),
            ):
                snapshot = service.snapshot()
                html = service.render_dashboard()

            session = snapshot["sessions"][0]
            self.assertEqual(session["health"]["status"], "waiting_for_chatgpt")
            self.assertIn("Return packet was delivered", session["health"]["reason"])
            self.assertIn("Waiting for ChatGPT", html)

    def test_snapshot_marks_starting_codex_without_output_as_running_quiet(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService
        from datetime import datetime

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            sessions_dir.mkdir(parents=True)
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
                            "loop_state": "starting_codex",
                            "auto_run_enabled": True,
                            "supervisor_status": "running",
                            "supervisor_heartbeat_at": "2026-04-16T10:00:55+00:00",
                            "phase_started_at": "2026-04-16T10:00:00+00:00",
                            "last_chat_activity_at": "2026-04-16T10:00:00+00:00",
                            "last_codex_activity_at": "2026-04-16T09:59:30+00:00",
                            "last_delivery_at": "2026-04-16T09:50:00+00:00",
                        },
                    }
                )
            )
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=_FakeSupervisorManager(),
            )

            with patch(
                "mastermind_bridge.orchestrator.control_panel.time.time",
                return_value=datetime.fromisoformat("2026-04-16T10:01:20+00:00").timestamp(),
            ):
                snapshot = service.snapshot()
                html = service.render_dashboard()

            session = snapshot["sessions"][0]
            self.assertEqual(session["health"]["status"], "running_quiet")
            self.assertIn("no new Codex output has been recorded yet", session["health"]["reason"])
            self.assertGreaterEqual(session["health"]["phase_age_seconds"], 60)
            self.assertIn("Running quietly", html)

    def test_snapshot_keeps_starting_codex_with_recent_output_healthy(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            sessions_dir.mkdir(parents=True)
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
                            "loop_state": "starting_codex",
                            "auto_run_enabled": True,
                            "supervisor_status": "running",
                            "supervisor_heartbeat_at": "2026-04-16T10:01:50+00:00",
                            "phase_started_at": "2026-04-16T10:00:00+00:00",
                            "last_chat_activity_at": "2026-04-16T10:00:00+00:00",
                            "last_codex_activity_at": "2026-04-16T10:01:40+00:00",
                            "last_delivery_at": "2026-04-16T09:50:00+00:00",
                        },
                    }
                )
            )
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=_FakeSupervisorManager(),
            )

            with patch("mastermind_bridge.orchestrator.control_panel.time.time", return_value=1_776_331_260.0):
                snapshot = service.snapshot()

            session = snapshot["sessions"][0]
            self.assertEqual(session["health"]["status"], "healthy")
            self.assertLessEqual(session["health"]["codex_progress_age_seconds"], 20)

    def test_snapshot_marks_blocked_session_with_old_progress_as_stalled(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            sessions_dir.mkdir(parents=True)
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
                            "status": "requires_human",
                            "loop_state": "requires_human",
                            "auto_run_enabled": False,
                            "supervisor_status": "blocked",
                            "supervisor_heartbeat_at": "2026-04-16T10:25:00+00:00",
                            "phase_started_at": "2026-04-16T09:00:00+00:00",
                            "last_chat_activity_at": "2026-04-16T09:20:00+00:00",
                            "last_codex_activity_at": "2026-04-16T09:10:00+00:00",
                            "last_delivery_at": "2026-04-16T09:15:00+00:00",
                            "human_attention_reason": "Browser automation requires manual attention.",
                        },
                    }
                )
            )
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=_FakeSupervisorManager(),
            )

            with patch("mastermind_bridge.orchestrator.control_panel.time.time", return_value=1_776_335_100.0):
                snapshot = service.snapshot()
                html = service.render_dashboard()

            session = snapshot["sessions"][0]
            self.assertEqual(session["health"]["status"], "stalled")
            self.assertGreaterEqual(session["health"]["latest_progress_age_seconds"], 1800)
            self.assertIn("Active intervention is required", session["health"]["reason"])
            self.assertIn("Stalled", html)

    def test_snapshot_marks_active_session_with_no_recent_progress_as_stalled(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            sessions_dir.mkdir(parents=True)
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
                            "loop_state": "waiting_for_codex",
                            "auto_run_enabled": True,
                            "supervisor_status": "running",
                            "supervisor_heartbeat_at": "2026-04-16T10:34:00+00:00",
                            "phase_started_at": "2026-04-16T09:00:00+00:00",
                            "last_chat_activity_at": "2026-04-16T09:30:00+00:00",
                            "last_codex_activity_at": "2026-04-16T09:31:00+00:00",
                            "last_delivery_at": "2026-04-16T09:32:00+00:00",
                        },
                    }
                )
            )
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=_FakeSupervisorManager(),
            )

            with patch("mastermind_bridge.orchestrator.control_panel.time.time", return_value=1_776_335_100.0):
                snapshot = service.snapshot()

            session = snapshot["sessions"][0]
            self.assertEqual(session["health"]["status"], "stalled")
            self.assertGreaterEqual(session["health"]["latest_progress_age_seconds"], 1200)
            self.assertIn("no new ChatGPT, Codex, or delivery progress", session["health"]["reason"])

    def test_snapshot_marks_dead_supervisor_lock_as_stalled(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            sessions_dir.mkdir(parents=True)
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
                            "loop_state": "waiting_for_chatgpt_response",
                            "auto_run_enabled": True,
                            "supervisor_status": "running",
                            "supervisor_heartbeat_at": "2026-04-16T10:34:45+00:00",
                            "last_chat_activity_at": "2026-04-16T10:34:45+00:00",
                        },
                    }
                )
            )
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=_StaticSupervisorManager(
                    {
                        "session-1": {
                            "alive": False,
                            "lock": {
                                "session_id": "session-1",
                                "pid": 4242,
                                "pid_alive": False,
                                "path": str(root / "session_locks" / "session-1.json"),
                            },
                        }
                    }
                ),
            )

            with patch("mastermind_bridge.orchestrator.control_panel.time.time", return_value=1_776_335_100.0):
                snapshot = service.snapshot()

            session = snapshot["sessions"][0]
            self.assertEqual(session["health"]["status"], "stalled")
            self.assertIn("dead pid", session["health"]["reason"])
            self.assertFalse(session["session_lock"]["pid_alive"])

    def test_open_chat_preview_uses_visible_bridge_browser_and_start_closes_preview(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            manager = _FakeSupervisorManager()
            preview_adapter = _FakePreviewAdapter()
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=manager,
                default_repo_path="/tmp/repo",
                default_workspace_path="/tmp/repo",
                default_browser_profile_path="/tmp/repo/state/playwright-profile",
                preview_adapter_factory=lambda: preview_adapter,
            )

            payload = service.quickstart_session(
                {
                    "chat_url": "https://chatgpt.com/c/project/test-chat",
                    "time_budget_minutes": 45,
                    "session_id": "session-1",
                }
            )

            opened = service.open_chat_preview("session-1")
            with patch(
                "mastermind_bridge.orchestrator.control_panel._open_codex_live_monitor",
                return_value={"session_id": "session-1", "repo_path": "/tmp/repo", "log_path": "/tmp/log"},
            ):
                started = service.start_session("session-1")

            self.assertEqual(opened["status"], "opened")
            self.assertEqual(preview_adapter.opened_urls, ["https://chatgpt.com/c/project/test-chat"])
            self.assertTrue(preview_adapter.closed)
            self.assertEqual(started["status"], "running")

    def test_start_session_failure_marks_session_blocked_and_surfaces_error(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelService
        from mastermind_bridge.orchestrator.state import load_session, session_path

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=_FailingSupervisorManager(),
                default_repo_path="/tmp/repo",
                default_workspace_path="/tmp/repo",
                default_browser_profile_path="/tmp/repo/state/playwright-profile",
            )

            payload = service.quickstart_session(
                {
                    "chat_url": "https://chatgpt.com/c/project/test-chat",
                    "time_budget_minutes": 45,
                    "session_id": "session-1",
                }
            )

            with (
                patch(
                    "mastermind_bridge.orchestrator.control_panel._open_codex_live_monitor",
                    return_value={"session_id": "session-1", "repo_path": "/tmp/repo", "log_path": "/tmp/log"},
                ),
                self.assertRaises(ValueError),
            ):
                service.start_session("session-1")

            session = load_session(session_path(sessions_dir, payload["session"]["session_id"]))
            self.assertEqual(session.status, "blocked")
            self.assertEqual(session.supervisor_status, "blocked")
            self.assertEqual(session.loop_state, "requires_human")
            self.assertFalse(session.auto_run_enabled)
            self.assertIn("Playwright is not installed", session.last_error)

    def test_http_server_serves_dashboard_and_state_json(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelServer, ControlPanelService

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            sessions_dir.mkdir(parents=True)
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=_FakeSupervisorManager(),
            )
            server = ControlPanelServer(service=service, host="127.0.0.1", port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            try:
                base_url = f"http://127.0.0.1:{server.server_address[1]}"
                dashboard_response = urlopen(f"{base_url}/")
                dashboard_html = dashboard_response.read().decode("utf-8")
                state_response = urlopen(f"{base_url}/api/state")
                state_payload = json.loads(state_response.read().decode("utf-8"))

                self.assertIn("Bridge Control Panel", dashboard_html)
                self.assertIn('http-equiv="Cache-Control"', dashboard_html)
                self.assertIn("cache: 'no-store'", dashboard_html)
                self.assertIn("sessions", state_payload)
                self.assertIn("bindings", state_payload)
                self.assertIn("server_fingerprint", state_payload)
                self.assertIn("no-store", dashboard_response.headers["Cache-Control"])
                self.assertIn("no-store", state_response.headers["Cache-Control"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=1)

    def test_http_server_accepts_shutdown_request(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelServer, ControlPanelService

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            sessions_dir.mkdir(parents=True)
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=_FakeSupervisorManager(),
            )
            server = ControlPanelServer(service=service, host="127.0.0.1", port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            try:
                base_url = f"http://127.0.0.1:{server.server_address[1]}"
                request = Request(f"{base_url}/api/control/shutdown", method="POST")
                payload = json.loads(urlopen(request).read().decode("utf-8"))

                self.assertEqual(payload["status"], "shutting_down")
                thread.join(timeout=1)
                self.assertFalse(thread.is_alive())
            finally:
                server.server_close()

    def test_http_server_rejects_malformed_post_json_without_crashing_handler(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelServer, ControlPanelService

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            sessions_dir.mkdir(parents=True)
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=_FakeSupervisorManager(),
            )
            server = ControlPanelServer(service=service, host="127.0.0.1", port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            try:
                base_url = f"http://127.0.0.1:{server.server_address[1]}"
                request = Request(
                    f"{base_url}/api/sessions",
                    data=b"{",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(HTTPError) as raised:
                    urlopen(request)

                error_response = raised.exception
                self.assertEqual(error_response.code, 400)
                payload = json.loads(error_response.read().decode("utf-8"))
                error_response.close()
                self.assertIn("error", payload)
                state_payload = json.loads(urlopen(f"{base_url}/api/state").read().decode("utf-8"))
                self.assertIn("sessions", state_payload)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=1)

    def test_http_server_rejects_oversized_post_body(self):
        from mastermind_bridge.orchestrator.control_panel import ControlPanelServer, ControlPanelService

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path = root / "CHAT_BINDINGS.json"
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            sessions_dir = root / "sessions"
            bindings_path.write_text(json.dumps({"version": 1, "bindings": []}))
            policy_path.write_text(json.dumps({"version": 1}))
            sessions_dir.mkdir(parents=True)
            service = ControlPanelService(
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
                supervisor_manager=_FakeSupervisorManager(),
            )
            server = ControlPanelServer(service=service, host="127.0.0.1", port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            connection = None

            try:
                connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=2)
                connection.putrequest("POST", "/api/sessions")
                connection.putheader("Content-Type", "application/json")
                connection.putheader("Content-Length", "1048577")
                connection.endheaders()
                response = connection.getresponse()

                self.assertEqual(response.status, 400)
                payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual(payload["error"], "Request body is too large")
            finally:
                if connection is not None:
                    connection.close()
                server.shutdown()
                server.server_close()
                thread.join(timeout=1)

    def test_cli_parser_registers_control_panel_command(self):
        from mastermind_bridge.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["control-panel", "--host", "127.0.0.1", "--port", "8765"])

        self.assertEqual(args.command, "control-panel")
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 8765)
