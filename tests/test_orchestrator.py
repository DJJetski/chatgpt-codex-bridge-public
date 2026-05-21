import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mastermind_bridge.orchestrator.models import BridgeControlEnvelope, InstructionScopeUpdate, OrchestratorSession
from mastermind_bridge.orchestrator.loop import _delivery_attempts_need_foreground_browser_reopen
from mastermind_bridge.orchestrator.state import (
    load_chat_bindings,
    load_orchestrator_policy,
    load_session,
    read_orchestrator_policy,
    save_chat_bindings,
    save_session,
)


class OrchestratorStateTests(unittest.TestCase):
    def test_chat_bindings_round_trip_through_state_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            bindings_path = tmp_path / "CHAT_BINDINGS.json"

            save_chat_bindings(
                bindings_path,
                [
                    {
                        "binding_id": "binding-1",
                        "project_name": "bridge",
                        "repo_path": "/tmp/repo",
                        "workspace_path": "/tmp/repo",
                        "chat_url": "https://chatgpt.com/c/project/binding-1",
                        "browser_profile_path": "/tmp/profile",
                        "browser_session_handle": "default",
                    }
                ],
            )

            bindings = load_chat_bindings(bindings_path)

            self.assertEqual(len(bindings), 1)
            self.assertEqual(bindings[0].binding_id, "binding-1")
            self.assertEqual(bindings[0].workspace_path, "/tmp/repo")
            payload = json.loads(bindings_path.read_text())
            self.assertEqual(payload["version"], 1)
            self.assertEqual(payload["bindings"][0]["chat_url"], "https://chatgpt.com/c/project/binding-1")

    def test_bridge_control_envelope_parses_nested_instruction_updates(self):
        envelope = BridgeControlEnvelope.from_dict(
            {
                "protocol_version": "1",
                "session_id": "session-1",
                "decision": "run_codex",
                "codex_thread_action": "same_thread",
                "prompt": "Continue implementation.",
                "task_label": "foundation-scaffolding",
                "human_gate": {
                    "required": True,
                    "reason": "Need approval for spend.",
                    "category": "paid_spend",
                },
                "instruction_updates": [
                    {
                        "scope": "session",
                        "mode": "append",
                        "text": "Keep state local.",
                    }
                ],
                "time_budget_remaining_hint": "45m",
                "notes_for_audit": ["ChatGPT requested the next Codex run."],
            }
        )

        self.assertEqual(envelope.session_id, "session-1")
        self.assertEqual(envelope.human_gate_category, "paid_spend")
        self.assertEqual(envelope.instruction_updates[0].scope, "session")
        self.assertEqual(envelope.notes_for_audit, ["ChatGPT requested the next Codex run."])

    def test_load_orchestrator_policy_returns_default_policy_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            policy_path = tmp_path / "ORCHESTRATOR_POLICY.json"

            policy = load_orchestrator_policy(policy_path)

            self.assertEqual(policy["version"], 2)
            self.assertTrue(policy["require_explicit_budget"])
            self.assertFalse(policy["allow_branch_worktree_creation"])
            self.assertFalse(policy["allow_commit_push_pr"])
            self.assertFalse(policy["allow_deployments"])
            self.assertFalse(policy["allow_existing_local_secrets"])
            self.assertFalse(policy["allow_operator_provided_secrets"])
            self.assertFalse(policy["allow_keychain_access"])
            self.assertFalse(policy["prefer_full_local_codex_environment"])
            self.assertFalse(policy["allow_browser_and_screen_tools"])
            self.assertFalse(policy["prefer_installed_mcp_tools"])
            self.assertFalse(policy["prefer_installed_apps_plugins_and_clis"])
            self.assertEqual(policy["delivery_retry"]["transport_direction"], "codex_to_chatgpt_only")
            self.assertIn(
                "Message delivery confirmation timed out.",
                policy["delivery_retry"]["known_error_signatures"],
            )
            self.assertTrue(policy_path.exists())

    def test_load_orchestrator_policy_migrates_legacy_permissive_defaults(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            policy_path = tmp_path / "ORCHESTRATOR_POLICY.json"
            policy_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "allow_commit_push_pr": True,
                        "allow_deployments": True,
                        "allow_existing_local_secrets": True,
                        "allow_operator_provided_secrets": True,
                        "allow_keychain_access": True,
                        "allow_browser_and_screen_tools": True,
                    }
                ),
                encoding="utf-8",
            )

            policy = load_orchestrator_policy(policy_path)
            persisted = json.loads(policy_path.read_text(encoding="utf-8"))

            self.assertEqual(policy["version"], 2)
            self.assertFalse(policy["allow_commit_push_pr"])
            self.assertFalse(policy["allow_deployments"])
            self.assertFalse(policy["allow_existing_local_secrets"])
            self.assertFalse(policy["allow_operator_provided_secrets"])
            self.assertFalse(policy["allow_keychain_access"])
            self.assertFalse(policy["allow_browser_and_screen_tools"])
            self.assertEqual(persisted["version"], 2)
            self.assertFalse(persisted["allow_commit_push_pr"])

    def test_read_orchestrator_policy_does_not_persist_defaults_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            policy_path = tmp_path / "ORCHESTRATOR_POLICY.json"

            policy = read_orchestrator_policy(policy_path)

            self.assertTrue(policy["require_explicit_budget"])
            self.assertFalse(policy_path.exists())

    def test_session_round_trip_preserves_policy_decision_and_budget(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            session_path = tmp_path / "sessions" / "session-1.json"
            session_path.parent.mkdir(parents=True, exist_ok=True)
            session_path.write_text(
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
                            "policy_decision": {
                                "policy_outcome": "allow",
                                "reasons": ["Explicit time budget provided."],
                            },
                            "instruction_updates": [
                                {
                                    "scope": "project",
                                    "mode": "append",
                                    "text": "Persist durable instructions locally.",
                                }
                            ],
                        },
                    }
                )
            )

            session = load_session(session_path)

            self.assertEqual(session.session_id, "session-1")
            self.assertEqual(session.time_budget_minutes, 90)
            self.assertEqual(session.policy_decision.policy_outcome, "allow")
            self.assertEqual(session.instruction_updates[0].scope, "project")

    def test_session_round_trip_preserves_current_codex_thread_and_budget_semantics(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            session_path = tmp_path / "sessions" / "session-1.json"
            session_path.parent.mkdir(parents=True, exist_ok=True)
            session_path.write_text(
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
                            "budget_remaining_minutes": 88,
                            "budget_semantics": "elapsed_active_wall_clock_minutes",
                            "budget_consumed_seconds": 120.0,
                            "current_codex_thread_id": "thread-xyz",
                            "degraded_mode": "cli_fresh_exec",
                        },
                    }
                )
            )

            session = load_session(session_path)

            self.assertEqual(session.current_codex_thread_id, "thread-xyz")
            self.assertEqual(session.current_codex_run_id, "thread-xyz")
            self.assertEqual(session.budget_semantics, "elapsed_active_wall_clock_minutes")
            self.assertEqual(session.degraded_mode, "cli_fresh_exec")

    def test_load_session_and_bindings_strip_shell_quotes_from_persisted_paths(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            bindings_path = tmp_path / "CHAT_BINDINGS.json"
            session_path = tmp_path / "sessions" / "session-1.json"
            session_path.parent.mkdir(parents=True, exist_ok=True)
            bindings_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "bindings": [
                            {
                                "binding_id": "binding-1",
                                "project_name": "Test Repo'",
                                "repo_path": "'/tmp/Test Repo'",
                                "workspace_path": "'/tmp/Test Repo'",
                                "chat_url": "https://chatgpt.com/c/project/binding-1",
                            }
                        ],
                    }
                )
            )
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "session": {
                            "session_id": "session-1",
                            "binding_id": "binding-1",
                            "repo_path": "'/tmp/Test Repo'",
                            "workspace_path": "'/tmp/Test Repo'",
                            "chat_url": "https://chatgpt.com/c/project/binding-1",
                        },
                    }
                )
            )

            binding = load_chat_bindings(bindings_path)[0]
            session = load_session(session_path)

            self.assertEqual(binding.repo_path, "/tmp/Test Repo")
            self.assertEqual(binding.workspace_path, "/tmp/Test Repo")
            self.assertEqual(binding.project_name, "Test Repo")
            self.assertEqual(session.repo_path, "/tmp/Test Repo")
            self.assertEqual(session.workspace_path, "/tmp/Test Repo")

    def test_save_session_refreshes_updated_at_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            session_path = tmp_path / "sessions" / "session-1.json"
            session_path.parent.mkdir(parents=True, exist_ok=True)
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "session": {
                            "session_id": "session-1",
                            "binding_id": "binding-1",
                            "repo_path": "/tmp/repo",
                            "workspace_path": "/tmp/repo",
                            "chat_url": "https://chatgpt.com/c/project/binding-1",
                            "updated_at": "2026-04-15T00:00:00+00:00",
                        },
                    }
                )
            )

            session = load_session(session_path)
            self.assertEqual(session.updated_at, "2026-04-15T00:00:00+00:00")

            save_session(session_path, session)

            reloaded = load_session(session_path)
            self.assertNotEqual(reloaded.updated_at, "2026-04-15T00:00:00+00:00")

    def test_save_session_preserves_concurrent_instruction_updates(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            session_path = tmp_path / "sessions" / "session-1.json"
            session_path.parent.mkdir(parents=True, exist_ok=True)
            session_path.write_text(
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

            stale_session = load_session(session_path)
            concurrent_session = load_session(session_path)
            concurrent_session.instruction_updates = [
                InstructionScopeUpdate(
                    scope="next_run",
                    mode="append",
                    text="Keep the operator-provided clarification for the next run.",
                )
            ]
            save_session(session_path, concurrent_session)

            stale_session.supervisor_status = "running"
            save_session(session_path, stale_session)

            reloaded = load_session(session_path)
            self.assertEqual(reloaded.supervisor_status, "running")
            self.assertEqual(len(reloaded.instruction_updates), 1)
            self.assertEqual(reloaded.instruction_updates[0].scope, "next_run")
            self.assertEqual(
                reloaded.instruction_updates[0].text,
                "Keep the operator-provided clarification for the next run.",
            )

    def test_save_session_keeps_newest_replace_instruction_after_stale_save(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            session_path = tmp_path / "sessions" / "session-1.json"
            session_path.parent.mkdir(parents=True, exist_ok=True)
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "session": {
                            "session_id": "session-1",
                            "binding_id": "binding-1",
                            "repo_path": "/tmp/repo",
                            "workspace_path": "/tmp/repo",
                            "chat_url": "https://chatgpt.com/c/project/binding-1",
                            "instruction_updates": [
                                {
                                    "scope": "session",
                                    "mode": "replace",
                                    "text": "Old session steering.",
                                    "created_at": "2026-04-30T10:00:00+00:00",
                                }
                            ],
                        },
                    }
                )
            )

            stale_session = load_session(session_path)
            concurrent_session = load_session(session_path)
            concurrent_session.instruction_updates = [
                InstructionScopeUpdate(
                    scope="session",
                    mode="replace",
                    text="New session steering.",
                    created_at="2026-04-30T10:05:00+00:00",
                )
            ]
            save_session(session_path, concurrent_session)

            stale_session.supervisor_status = "running"
            save_session(session_path, stale_session)

            reloaded = load_session(session_path)
            self.assertEqual(reloaded.supervisor_status, "running")
            self.assertEqual(len(reloaded.instruction_updates), 1)
            self.assertEqual(reloaded.instruction_updates[0].scope, "session")
            self.assertEqual(reloaded.instruction_updates[0].mode, "replace")
            self.assertEqual(reloaded.instruction_updates[0].text, "New session steering.")

    def test_save_session_preserves_newer_execution_settings_from_concurrent_state(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            session_path = tmp_path / "sessions" / "session-1.json"
            session_path.parent.mkdir(parents=True, exist_ok=True)
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "session": {
                            "session_id": "session-1",
                            "binding_id": "binding-1",
                            "repo_path": "/tmp/repo",
                            "workspace_path": "/tmp/repo",
                            "chat_url": "https://chatgpt.com/c/project/binding-1",
                            "updated_at": "2026-04-15T00:00:00+00:00",
                        },
                    }
                )
            )

            stale_session = load_session(session_path)
            concurrent_session = load_session(session_path)
            concurrent_session.codex_model = "gpt-5.4-mini"
            concurrent_session.codex_reasoning_effort = "medium"
            save_session(session_path, concurrent_session)

            stale_session.supervisor_status = "running"
            save_session(session_path, stale_session)

            reloaded = load_session(session_path)
            self.assertEqual(reloaded.supervisor_status, "running")
            self.assertEqual(reloaded.codex_model, "gpt-5.4-mini")
            self.assertEqual(reloaded.codex_reasoning_effort, "medium")

    def test_delivery_attempts_request_foreground_reopen_for_live_chat_surface_failures(self):
        self.assertTrue(
            _delivery_attempts_need_foreground_browser_reopen(
                [
                    {
                        "status": "failed",
                        "error_signature": "ChatGPT DOM contract missing `composer` selector match.",
                    }
                ]
            )
        )
        self.assertFalse(
            _delivery_attempts_need_foreground_browser_reopen(
                [{"status": "failed", "error_signature": "Message delivery confirmation timed out."}]
            )
        )

    def test_save_session_does_not_restore_cleared_repair_tracking_from_concurrent_state(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            session_path = tmp_path / "sessions" / "session-1.json"
            session_path.parent.mkdir(parents=True, exist_ok=True)
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "session": {
                            "session_id": "session-1",
                            "binding_id": "binding-1",
                            "repo_path": "/tmp/repo",
                            "workspace_path": "/tmp/repo",
                            "chat_url": "https://chatgpt.com/c/project/binding-1",
                            "loop_state": "waiting_for_chatgpt",
                            "last_seen_chat_message_anchor": "assistant-1",
                            "latest_assistant_message_hash": "hash-1",
                            "last_outbound_user_message_anchor": "repair-session-1-hash-1-1",
                            "last_outbound_user_message_kind": "repair",
                            "last_outbound_user_message_sent_at": 123.0,
                        },
                    }
                )
            )

            stale_session = load_session(session_path)
            concurrent_session = load_session(session_path)
            concurrent_session.last_outbound_user_message_anchor = ""
            concurrent_session.last_outbound_user_message_kind = ""
            concurrent_session.last_outbound_user_message_sent_at = 0.0
            save_session(session_path, concurrent_session)

            stale_session.supervisor_status = "running"
            save_session(session_path, stale_session)

            reloaded = load_session(session_path)
            self.assertEqual(reloaded.supervisor_status, "running")
            self.assertEqual(reloaded.last_outbound_user_message_anchor, "")
            self.assertEqual(reloaded.last_outbound_user_message_kind, "")
            self.assertEqual(reloaded.last_outbound_user_message_sent_at, 0.0)

    def test_save_session_preserves_concurrent_outbound_tracking_while_waiting(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            session_path = tmp_path / "sessions" / "session-1.json"
            session_path.parent.mkdir(parents=True, exist_ok=True)
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "session": {
                            "session_id": "session-1",
                            "binding_id": "binding-1",
                            "repo_path": "/tmp/repo",
                            "workspace_path": "/tmp/repo",
                            "chat_url": "https://chatgpt.com/c/project/binding-1",
                            "loop_state": "waiting_for_chatgpt_response",
                        },
                    }
                )
            )

            stale_session = load_session(session_path)
            concurrent_session = load_session(session_path)
            concurrent_session.last_outbound_user_message_anchor = "user-1-anchor"
            concurrent_session.last_outbound_user_message_kind = "operator_manual"
            concurrent_session.last_outbound_user_message_sent_at = 123.0
            concurrent_session.last_delivery_at = "2026-04-18T00:35:00+02:00"
            save_session(session_path, concurrent_session)

            stale_session.supervisor_status = "running"
            save_session(session_path, stale_session)

            reloaded = load_session(session_path)
            self.assertEqual(reloaded.supervisor_status, "running")
            self.assertEqual(reloaded.last_outbound_user_message_anchor, "user-1-anchor")
            self.assertEqual(reloaded.last_outbound_user_message_kind, "operator_manual")
            self.assertEqual(reloaded.last_outbound_user_message_sent_at, 123.0)
            self.assertEqual(reloaded.last_delivery_at, "2026-04-18T00:35:00+02:00")


class OrchestratorCliTests(unittest.TestCase):
    def test_bind_chat_writes_binding_state(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            bindings_path = tmp_path / "CHAT_BINDINGS.json"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mastermind_bridge.cli",
                    "bind-chat",
                    "--binding-id",
                    "binding-1",
                    "--project-name",
                    "bridge",
                    "--repo-path",
                    "/tmp/repo",
                    "--workspace-path",
                    "/tmp/repo",
                    "--chat-url",
                    "https://chatgpt.com/c/project/binding-1",
                    "--browser-profile-path",
                    "/tmp/profile",
                    "--browser-session-handle",
                    "default",
                    "--bindings",
                    str(bindings_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["binding_id"], "binding-1")
            bindings = json.loads(bindings_path.read_text())
            self.assertEqual(bindings["bindings"][0]["repo_path"], "/tmp/repo")
            self.assertEqual(bindings["bindings"][0]["browser_session_handle"], "default")

    def test_start_session_requires_explicit_budget(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            bindings_path = tmp_path / "CHAT_BINDINGS.json"
            policy_path = tmp_path / "ORCHESTRATOR_POLICY.json"
            sessions_dir = tmp_path / "sessions"
            save_chat_bindings(
                bindings_path,
                [
                    {
                        "binding_id": "binding-1",
                        "project_name": "bridge",
                        "repo_path": "/tmp/repo",
                        "workspace_path": "/tmp/repo",
                        "chat_url": "https://chatgpt.com/c/project/binding-1",
                        "browser_profile_path": "/tmp/profile",
                        "browser_session_handle": "default",
                    }
                ],
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mastermind_bridge.cli",
                    "start-session",
                    "--session-id",
                    "session-1",
                    "--binding-id",
                    "binding-1",
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

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("explicit time budget", result.stderr.lower())
            self.assertFalse((sessions_dir / "session-1.json").exists())

    def test_start_session_writes_session_file_and_default_policy(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            bindings_path = tmp_path / "CHAT_BINDINGS.json"
            policy_path = tmp_path / "ORCHESTRATOR_POLICY.json"
            sessions_dir = tmp_path / "sessions"
            save_chat_bindings(
                bindings_path,
                [
                    {
                        "binding_id": "binding-1",
                        "project_name": "bridge",
                        "repo_path": "/tmp/repo",
                        "workspace_path": "/tmp/repo",
                        "chat_url": "https://chatgpt.com/c/project/binding-1",
                        "browser_profile_path": "/tmp/profile",
                        "browser_session_handle": "default",
                    }
                ],
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mastermind_bridge.cli",
                    "start-session",
                    "--session-id",
                    "session-1",
                    "--binding-id",
                    "binding-1",
                    "--time-budget-minutes",
                    "90",
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
            payload = json.loads(result.stdout)
            self.assertEqual(payload["session_id"], "session-1")
            session = load_session(sessions_dir / "session-1.json")
            self.assertEqual(session.binding_id, "binding-1")
            self.assertEqual(session.time_budget_minutes, 90)
            self.assertEqual(session.policy_decision.policy_outcome, "allow")
            policy = json.loads(policy_path.read_text())
            self.assertTrue(policy["require_explicit_budget"])

    def test_status_reports_binding_and_session(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            bindings_path = tmp_path / "CHAT_BINDINGS.json"
            policy_path = tmp_path / "ORCHESTRATOR_POLICY.json"
            sessions_dir = tmp_path / "sessions"
            save_chat_bindings(
                bindings_path,
                [
                    {
                        "binding_id": "binding-1",
                        "project_name": "bridge",
                        "repo_path": "/tmp/repo",
                        "workspace_path": "/tmp/repo",
                        "chat_url": "https://chatgpt.com/c/project/binding-1",
                        "browser_profile_path": "/tmp/profile",
                        "browser_session_handle": "default",
                    }
                ],
            )

            start_result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mastermind_bridge.cli",
                    "start-session",
                    "--session-id",
                    "session-1",
                    "--binding-id",
                    "binding-1",
                    "--time-budget-minutes",
                    "45",
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
            self.assertEqual(start_result.returncode, 0, msg=start_result.stderr)

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mastermind_bridge.cli",
                    "status",
                    "--binding-id",
                    "binding-1",
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
            payload = json.loads(result.stdout)
            self.assertEqual(payload["binding"]["binding_id"], "binding-1")
            self.assertEqual(payload["session"]["session_id"], "session-1")
            self.assertEqual(payload["session"]["time_budget_minutes"], 45)
            self.assertEqual(payload["sessions"][0]["session_id"], "session-1")
            self.assertEqual(payload["session"]["health"]["status"], "inactive")
            self.assertEqual(payload["policy"]["autonomy_mode"], "balanced_aggressive")

    def test_status_surfaces_dead_supervisor_lock_health(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            bindings_path = tmp_path / "CHAT_BINDINGS.json"
            policy_path = tmp_path / "ORCHESTRATOR_POLICY.json"
            sessions_dir = tmp_path / "sessions"
            lock_dir = tmp_path / "session_locks"
            save_chat_bindings(
                bindings_path,
                [
                    {
                        "binding_id": "binding-1",
                        "project_name": "bridge",
                        "repo_path": "/tmp/repo",
                        "workspace_path": "/tmp/repo",
                        "chat_url": "https://chatgpt.com/c/project/binding-1",
                        "browser_profile_path": "/tmp/profile",
                        "browser_session_handle": "default",
                    }
                ],
            )
            sessions_dir.mkdir(parents=True)
            lock_dir.mkdir(parents=True)
            save_session(
                sessions_dir / "session-1.json",
                OrchestratorSession(
                    session_id="session-1",
                    binding_id="binding-1",
                    repo_path="/tmp/repo",
                    workspace_path="/tmp/repo",
                    chat_url="https://chatgpt.com/c/project/binding-1",
                    status="active",
                    loop_state="waiting_for_chatgpt_response",
                    auto_run_enabled=True,
                    supervisor_status="running",
                    supervisor_heartbeat_at="2026-04-16T10:34:45+00:00",
                    last_chat_activity_at="2026-04-16T10:34:45+00:00",
                    time_budget_minutes=45,
                    budget_remaining_minutes=45,
                ),
            )
            (lock_dir / "session-1.json").write_text(
                json.dumps(
                    {
                        "session_id": "session-1",
                        "pid": 999999,
                        "token": "dead-lock",
                        "hostname": "localhost",
                        "thread_name": "dead-runner",
                        "acquired_at": 1.0,
                    }
                )
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mastermind_bridge.cli",
                    "status",
                    "--binding-id",
                    "binding-1",
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
            payload = json.loads(result.stdout)
            self.assertEqual(payload["session"]["health"]["status"], "stalled")
            self.assertFalse(payload["session"]["session_lock"]["pid_alive"])

    def test_pause_resume_and_stop_update_session_state(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            bindings_path = tmp_path / "CHAT_BINDINGS.json"
            policy_path = tmp_path / "ORCHESTRATOR_POLICY.json"
            sessions_dir = tmp_path / "sessions"
            save_chat_bindings(
                bindings_path,
                [
                    {
                        "binding_id": "binding-1",
                        "project_name": "bridge",
                        "repo_path": "/tmp/repo",
                        "workspace_path": "/tmp/repo",
                        "chat_url": "https://chatgpt.com/c/project/binding-1",
                        "browser_profile_path": "/tmp/profile",
                        "browser_session_handle": "default",
                    }
                ],
            )

            start_result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mastermind_bridge.cli",
                    "start-session",
                    "--session-id",
                    "session-1",
                    "--binding-id",
                    "binding-1",
                    "--time-budget-minutes",
                    "45",
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
            self.assertEqual(start_result.returncode, 0, msg=start_result.stderr)

            pause_result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mastermind_bridge.cli",
                    "pause",
                    "--session-id",
                    "session-1",
                    "--sessions-dir",
                    str(sessions_dir),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(pause_result.returncode, 0, msg=pause_result.stderr)
            session = load_session(sessions_dir / "session-1.json")
            self.assertEqual(session.status, "paused")

            resume_result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mastermind_bridge.cli",
                    "resume-session",
                    "--session-id",
                    "session-1",
                    "--sessions-dir",
                    str(sessions_dir),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(resume_result.returncode, 0, msg=resume_result.stderr)
            session = load_session(sessions_dir / "session-1.json")
            self.assertEqual(session.status, "active")
            self.assertEqual(session.loop_state, "idle")
            self.assertTrue(session.auto_run_enabled)
            self.assertEqual(session.supervisor_status, "running")
            self.assertEqual(session.human_attention_reason, "")
            self.assertEqual(session.last_error, "")

            stop_result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mastermind_bridge.cli",
                    "stop",
                    "--session-id",
                    "session-1",
                    "--after-cycle",
                    "--sessions-dir",
                    str(sessions_dir),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(stop_result.returncode, 0, msg=stop_result.stderr)
            session = load_session(sessions_dir / "session-1.json")
            self.assertTrue(session.stop_after_cycle_requested)


if __name__ == "__main__":
    unittest.main()
