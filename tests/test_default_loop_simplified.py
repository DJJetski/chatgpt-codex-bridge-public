import json
import os
import tempfile
import time
import unittest
from hashlib import sha1
from pathlib import Path
from unittest.mock import patch

import mastermind_bridge.cli as cli_module
from mastermind_bridge.cli import _execute_session_prompt
from mastermind_bridge.models import RunReport
from mastermind_bridge.orchestrator.loop import LoopRunner
from mastermind_bridge.orchestrator.loop_support import (
    _OUTBOUND_USER_MESSAGE_TIMEOUT_SECONDS,
    _assistant_looks_like_thinking_disclosure,
    _parse_thinking_disclosure_elapsed_seconds,
)
from mastermind_bridge.orchestrator.models import ChatBinding, OrchestratorSession
from mastermind_bridge.orchestrator.state import load_session, save_chat_bindings, save_session, session_path


class _FakeAdapter:
    def __init__(
        self,
        assistant_text: str,
        *,
        assistant_in_progress: bool = False,
        assistant_anchor: str = "msg-assistant-1",
        prepare_results: list[dict] | None = None,
        post_results: list[dict] | None = None,
        current_chat_urls: list[str] | None = None,
        visible_message_ids: set[str] | None = None,
        assistant_retry_result: bool = False,
        assistant_error_text: str = "",
        assistant_read_error: str = "",
        reload_result: bool = False,
    ) -> None:
        self.assistant_text = assistant_text
        self.assistant_in_progress = assistant_in_progress
        self.assistant_anchor = assistant_anchor
        self.prepare_results = list(prepare_results or [{"status": "ready"}])
        self.post_results = list(post_results or [{"status": "delivered", "message_anchor": "msg-user-2"}])
        self.current_urls = list(current_chat_urls or [])
        self.current_url = ""
        self.visible_message_ids = set(visible_message_ids or set())
        self.assistant_retry_result = assistant_retry_result
        self.assistant_error_text = assistant_error_text
        self.assistant_read_error = assistant_read_error
        self.reload_result = reload_result

        self.open_calls = 0
        self.prepare_calls = 0
        self.assistant_retry_calls = 0
        self.reload_calls = 0
        self.posted_messages: list[str] = []
        self.visibility_checks: list[str] = []

    def open_chat(self, binding):
        self.open_calls += 1
        self.current_url = str(binding.chat_url)

    def read_latest_assistant_message(self, session):
        if self.assistant_read_error:
            raise RuntimeError(self.assistant_read_error)
        return {
            "message_id": self.assistant_anchor,
            "message_anchor": self.assistant_anchor,
            "text": self.assistant_text,
        }

    def assistant_response_in_progress(self, session):
        return self.assistant_in_progress

    def retry_latest_assistant_response(self, session):
        self.assistant_retry_calls += 1
        return self.assistant_retry_result

    def reload_chat(self, session):
        self.reload_calls += 1
        return self.reload_result

    def latest_assistant_response_error(self, session):
        return self.assistant_error_text

    def prepare_return_packet_delivery(self, session):
        self.prepare_calls += 1
        if self.prepare_results:
            return dict(self.prepare_results.pop(0))
        return {"status": "ready"}

    def post_user_message(self, session, text: str, return_packet_id: str):
        self.posted_messages.append(text)
        if self.post_results:
            payload = dict(self.post_results.pop(0))
        else:
            payload = {"status": "delivered", "message_anchor": "msg-user-fallback"}
        payload.setdefault("return_packet_id", return_packet_id)
        if payload.get("current_chat_url"):
            self.current_url = str(payload["current_chat_url"])
        return payload

    def return_packet_visible(self, session, return_packet_id: str) -> bool:
        self.visibility_checks.append(return_packet_id)
        return return_packet_id in self.visible_message_ids

    def current_chat_url(self, session):
        if self.current_urls:
            self.current_url = str(self.current_urls.pop(0))
        return self.current_url

    def poll_stop_command(self, session, stop_phrases):
        return None


class _FakeExecutor:
    def __init__(self, report: RunReport) -> None:
        self.report = report
        self.calls: list[dict] = []

    def __call__(self, *, prompt: str, thread_action: str, session, binding, instructions: list[str]):
        self.calls.append(
            {
                "prompt": prompt,
                "thread_action": thread_action,
                "instructions": list(instructions),
                "session_id": session.session_id,
                "binding_id": binding.binding_id,
            }
        )
        return self.report


class _ControlMutatingExecutor(_FakeExecutor):
    def __init__(self, report: RunReport, *, sessions_dir: Path, command: str) -> None:
        super().__init__(report)
        self.sessions_dir = sessions_dir
        self.command = command

    def __call__(self, *, prompt: str, thread_action: str, session, binding, instructions: list[str]):
        result = super().__call__(
            prompt=prompt,
            thread_action=thread_action,
            session=session,
            binding=binding,
            instructions=instructions,
        )
        path = session_path(self.sessions_dir, session.session_id)
        updated = load_session(path)
        updated.latest_user_control_command = self.command
        if self.command == "pause":
            updated.status = "paused"
            updated.auto_run_enabled = False
            updated.supervisor_status = "paused"
        elif self.command == "stop":
            updated.status = "completed"
            updated.auto_run_enabled = False
            updated.supervisor_status = "stopped"
            updated.stop_after_cycle_requested = True
        save_session(path, updated)
        return result


class _RaisingExecutor:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls: list[dict] = []

    def __call__(self, *, prompt: str, thread_action: str, session, binding, instructions: list[str]):
        self.calls.append(
            {
                "prompt": prompt,
                "thread_action": thread_action,
                "instructions": list(instructions),
            }
        )
        raise self.error


class SimplifiedDefaultLoopTests(unittest.TestCase):
    def _write_state(self, root: Path, *, extra_session_fields: dict | None = None) -> tuple[Path, Path, Path]:
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
                    "browser_profile_path": "/tmp/profile",
                    "browser_session_handle": "default",
                }
            ],
        )
        policy_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "stop_phrases": ["stop", "pause", "stop after this cycle"],
                    "project_instruction_updates": [],
                    "delivery_retry": {
                        "enabled": True,
                        "transport_direction": "codex_to_chatgpt_only",
                        "max_attempts": 2,
                        "known_error_signatures": ["Reasoning failed"],
                    },
                }
            ),
            encoding="utf-8",
        )
        sessions_dir.mkdir(parents=True, exist_ok=True)
        session_payload = {
            "session_id": "session-1",
            "binding_id": "binding-1",
            "repo_path": "/tmp/repo",
            "workspace_path": "/tmp/repo",
            "chat_url": "https://chatgpt.com/c/project/binding-1",
            "status": "active",
            "loop_state": "idle",
            "time_budget_minutes": 90,
            "budget_remaining_minutes": 90,
            "current_codex_run_id": "exec-123",
            "instruction_updates": [],
        }
        if extra_session_fields:
            session_payload.update(extra_session_fields)
        (sessions_dir / "session-1.json").write_text(
            json.dumps({"version": 1, "session": session_payload}),
            encoding="utf-8",
        )
        return bindings_path, policy_path, sessions_dir

    def test_german_thinking_disclosure_is_treated_as_in_progress_assistant_status(self):
        self.assertTrue(_assistant_looks_like_thinking_disclosure("Nachgedacht für 41s"))
        self.assertEqual(_parse_thinking_disclosure_elapsed_seconds("Nachgedacht für 2m 3s"), 123.0)

    def test_run_once_uses_latest_complete_assistant_text_and_resumes_existing_thread(self):
        assistant_text = "Inspect the default loop and implement the next safe simplification."
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-19T16:00:00+02:00",
                "thread_id": "thread-2",
                "summary": "Codex completed the next safe slice.",
                "files_touched": ["mastermind_bridge/orchestrator/loop.py"],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "Wait for the next ChatGPT answer.",
                "observed_codex_thread_id": "exec-new-1",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(root)
            adapter = _FakeAdapter(assistant_text)
            executor = _FakeExecutor(report)
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1")

            self.assertEqual(result["policy_outcome"], "allow")
            self.assertEqual(result["runner_action"], "cycle_completed")
            self.assertEqual(executor.calls[0]["prompt"], assistant_text)
            self.assertEqual(executor.calls[0]["thread_action"], "same_thread")
            self.assertEqual(executor.calls[0]["instructions"], [])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text(encoding="utf-8"))["session"]
            self.assertEqual(session_payload["last_thread_action"], "same_thread")
            self.assertEqual(session_payload["last_productive_prompt"], assistant_text)
            self.assertEqual(session_payload["loop_state"], "waiting_for_chatgpt_response")

    def test_run_once_wraps_blocked_lane_churn_prompt_before_codex(self):
        assistant_text = "\n".join(
            [
                "Your first job is always to check whether the external backoff has cleared.",
                "If it has not cleared, do not idle.",
                "Instead, harden the same catch-up readiness path and blocker explanation again.",
            ]
        )
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-19T16:00:00+02:00",
                "thread_id": "thread-2",
                "summary": "Codex pivoted away from the blocked lane.",
                "files_touched": ["scripts/inventory.py"],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "Continue the alternate plan lane.",
                "observed_codex_thread_id": "exec-new-1",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(root)
            adapter = _FakeAdapter(assistant_text)
            executor = _FakeExecutor(report)
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1")

            self.assertEqual(result["policy_outcome"], "allow")
            self.assertEqual(result["runner_action"], "cycle_completed")
            self.assertEqual(len(executor.calls), 1)
            codex_prompt = executor.calls[0]["prompt"]
            self.assertIn("Blocked-lane anti-churn override.", codex_prompt)
            self.assertIn("run at most that bounded check", codex_prompt)
            self.assertIn("choose a substantial alternate lane", codex_prompt)
            self.assertIn("media/OCR/transcription derivation", codex_prompt)
            self.assertIn("actual data population is product work", codex_prompt)
            self.assertIn("messages, reminders, notes, tasks, files, media metadata, transcripts", codex_prompt)
            self.assertIn("canonical stores, inventories, indexes, memory, and brain/search surfaces", codex_prompt)
            self.assertIn("do not spend repeated cycles only improving runners, prompts, policy gates", codex_prompt)
            self.assertIn("do not avoid OCR, audio/video transcription", codex_prompt)
            self.assertIn("may take hours", codex_prompt)
            self.assertIn("Original ChatGPT prompt to treat as context", codex_prompt)
            self.assertIn("Your first job is always", codex_prompt)
            session_payload = json.loads((sessions_dir / "session-1.json").read_text(encoding="utf-8"))["session"]
            self.assertEqual(session_payload["last_productive_task_label"], "blocked_lane_churn_redirect")
            self.assertIn("Blocked-lane anti-churn override.", session_payload["last_productive_prompt"])

    def test_run_once_recovers_planner_idle_reply_without_starting_codex(self):
        assistant_text = "\n".join(
            [
                "No Codex prompt.",
                "",
                "Latest packet is No-op. No actionable state change.",
                "",
                "State remains paused: no repo work, no file changes, no new finding, no repo-local next step.",
            ]
        )
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-19T16:00:00+02:00",
                "thread_id": "thread-unused",
                "summary": "Should not run.",
                "files_touched": [],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "",
                "observed_codex_thread_id": "exec-unused",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(root)
            adapter = _FakeAdapter(assistant_text)
            executor = _FakeExecutor(report)
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1")

            self.assertEqual(result["policy_outcome"], "allow")
            self.assertEqual(result["runner_action"], "wait_for_chatgpt")
            self.assertEqual(executor.calls, [])
            self.assertEqual(len(adapter.posted_messages), 1)
            self.assertIn("planner_idle_recovery:", adapter.posted_messages[0])
            self.assertIn("choose another safe substantial task", adapter.posted_messages[0])
            self.assertIn("actual information completion is first-class product work", adapter.posted_messages[0])
            self.assertIn("messages, reminders, notes, tasks, files, media metadata, transcripts", adapter.posted_messages[0])
            self.assertIn("data population, source inventory, memory/search/brain indexing", adapter.posted_messages[0])
            self.assertIn("Do not spend repeated cycles only improving runners, prompts, policy gates", adapter.posted_messages[0])
            self.assertIn("Bound repo path", adapter.posted_messages[0])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text(encoding="utf-8"))["session"]
            self.assertEqual(session_payload["loop_state"], "waiting_for_chatgpt_response")
            self.assertEqual(session_payload["degraded_mode"], "planner_idle_recovery")
            self.assertEqual(session_payload["last_productive_task_label"], "planner_idle_reply")
            self.assertEqual(session_payload["last_outbound_user_message_kind"], "recovery")
            self.assertEqual(session_payload["productive_rewind_attempts"], 1)

    def test_run_once_uses_last_resort_plan_prompt_after_repeated_planner_idle_reply(self):
        assistant_text = "\n".join(
            [
                "Keinen weiteren Codex-Lauf starten.",
                "",
                "Der letzte Codex-Output ist korrekt: Codex bleibt pausiert.",
                "Ohne diese Entscheidung waere jeder weitere Codex-Prompt nur ein No-op-Loop.",
            ]
        )
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-19T16:00:00+02:00",
                "thread_id": "thread-unused",
                "summary": "Should not run.",
                "files_touched": [],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "",
                "observed_codex_thread_id": "exec-unused",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={"productive_rewind_attempts": 2},
            )
            adapter = _FakeAdapter(assistant_text)
            executor = _FakeExecutor(report)
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1")

            self.assertEqual(result["policy_outcome"], "allow")
            self.assertEqual(result["runner_action"], "cycle_completed")
            self.assertEqual(len(executor.calls), 1)
            self.assertIn("Planner-idle emergency fallback.", executor.calls[0]["prompt"])
            self.assertIn("start by reading the repo-local guidance", executor.calls[0]["prompt"])
            self.assertIn("canonical plan/backlog sources", executor.calls[0]["prompt"])
            self.assertIn("folder or code structure improvement", executor.calls[0]["prompt"])
            self.assertEqual(len(adapter.posted_messages), 1)
            self.assertIn("Here is what Codex wrote:", adapter.posted_messages[0])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text(encoding="utf-8"))["session"]
            self.assertEqual(session_payload["status"], "active")
            self.assertEqual(session_payload["loop_state"], "waiting_for_chatgpt_response")
            self.assertEqual(session_payload["degraded_mode"], "")
            self.assertEqual(session_payload["last_productive_task_label"], "planner_idle_fallback_codex_prompt")
            self.assertEqual(session_payload["productive_rewind_attempts"], 0)

    def test_run_once_drains_pause_requested_during_codex_run_after_return_delivery(self):
        assistant_text = "Inspect the default loop and implement the next safe simplification."
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-19T16:00:00+02:00",
                "thread_id": "thread-2",
                "summary": "Codex completed the current slice.",
                "files_touched": [],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "Wait for the next ChatGPT answer.",
                "observed_codex_thread_id": "exec-new-1",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(root)
            adapter = _FakeAdapter(assistant_text)
            executor = _ControlMutatingExecutor(report, sessions_dir=sessions_dir, command="pause")
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1")

            self.assertEqual(result["policy_outcome"], "paused")
            self.assertEqual(result["runner_action"], "paused")
            self.assertEqual(len(adapter.posted_messages), 1)
            session_payload = json.loads((sessions_dir / "session-1.json").read_text(encoding="utf-8"))["session"]
            self.assertEqual(session_payload["status"], "paused")
            self.assertEqual(session_payload["loop_state"], "paused")
            self.assertFalse(session_payload["auto_run_enabled"])
            self.assertEqual(session_payload["supervisor_status"], "paused")
            self.assertEqual(session_payload["cycles_completed"], 1)
            self.assertTrue(session_payload["last_posted_return_packet_id"])

    def test_run_once_drains_stop_requested_during_codex_run_after_return_delivery(self):
        assistant_text = "Inspect the default loop and implement the next safe simplification."
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-19T16:00:00+02:00",
                "thread_id": "thread-2",
                "summary": "Codex completed the current slice.",
                "files_touched": [],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "Wait for the next ChatGPT answer.",
                "observed_codex_thread_id": "exec-new-1",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(root)
            adapter = _FakeAdapter(assistant_text)
            executor = _ControlMutatingExecutor(report, sessions_dir=sessions_dir, command="stop")
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1")

            self.assertEqual(result["policy_outcome"], "stopped")
            self.assertEqual(result["runner_action"], "stopped")
            self.assertEqual(len(adapter.posted_messages), 1)
            session_payload = json.loads((sessions_dir / "session-1.json").read_text(encoding="utf-8"))["session"]
            self.assertEqual(session_payload["status"], "completed")
            self.assertEqual(session_payload["loop_state"], "completed")
            self.assertFalse(session_payload["auto_run_enabled"])
            self.assertEqual(session_payload["supervisor_status"], "stopped")
            self.assertEqual(session_payload["cycles_completed"], 1)
            self.assertTrue(session_payload["last_posted_return_packet_id"])

    def test_run_once_uses_new_thread_when_no_codex_thread_exists(self):
        assistant_text = "Inspect the default loop and implement the next safe simplification."
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-19T16:00:00+02:00",
                "thread_id": "thread-2",
                "summary": "Codex completed the first safe slice.",
                "files_touched": ["mastermind_bridge/orchestrator/loop.py"],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "Wait for the next ChatGPT answer.",
                "observed_codex_thread_id": "exec-new-1",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={"current_codex_run_id": "", "current_codex_thread_id": ""},
            )
            adapter = _FakeAdapter(assistant_text)
            executor = _FakeExecutor(report)
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1")

            self.assertEqual(result["policy_outcome"], "allow")
            self.assertEqual(executor.calls[0]["thread_action"], "new_thread")
            session_payload = json.loads((sessions_dir / "session-1.json").read_text(encoding="utf-8"))["session"]
            self.assertEqual(session_payload["last_thread_action"], "new_thread")

    def test_run_once_injects_persisted_session_instruction_for_freeform_followup(self):
        assistant_text = "Inspect the default loop and implement the next safe simplification."
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-19T16:00:00+02:00",
                "thread_id": "thread-2",
                "summary": "Codex completed the next safe slice.",
                "files_touched": ["mastermind_bridge/orchestrator/loop.py"],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "Wait for the next ChatGPT answer.",
                "observed_codex_thread_id": "exec-new-1",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "instruction_updates": [
                        {
                            "scope": "session",
                            "mode": "replace",
                            "text": "Break stale recovery loops and move to the real frontier.",
                        }
                    ]
                },
            )
            adapter = _FakeAdapter(assistant_text)
            executor = _FakeExecutor(report)
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1")

            self.assertEqual(result["policy_outcome"], "allow")
            self.assertEqual(executor.calls[0]["thread_action"], "same_thread")
            self.assertEqual(
                executor.calls[0]["instructions"],
                ["Break stale recovery loops and move to the real frontier."],
            )

    def test_run_once_marks_completed_truncated_recovery_prompt_as_stale_context(self):
        run_id = "20260430T010203-session-1"
        assistant_text = (
            "Recover the latest truncated run "
            f"/tmp/bridge/artifacts/runs/{run_id}/last_message.md and continue."
        )
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-19T16:00:00+02:00",
                "thread_id": "thread-2",
                "summary": "Codex completed the next safe slice.",
                "files_touched": [],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "Wait for the next ChatGPT answer.",
                "observed_codex_thread_id": "exec-new-1",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(root)
            run_dir = root / "artifacts" / "runs" / run_id
            run_dir.mkdir(parents=True)
            (run_dir / "last_message.md").write_text("Completed final report.", encoding="utf-8")
            (run_dir / "run_report.json").write_text(
                json.dumps(
                    {
                        "timestamp": "2026-04-19T15:59:00+02:00",
                        "thread_id": "thread-previous",
                        "summary": "Previous run completed.",
                        "files_touched": [],
                        "checks": [],
                        "blockers": [],
                        "risks": [],
                        "next_step": "",
                        "artifacts_dir": str(run_dir),
                        "exit_code": 0,
                        "interruption_reason": "",
                        "final_agent_message": "Completed final report.",
                    }
                ),
                encoding="utf-8",
            )
            adapter = _FakeAdapter(assistant_text)
            executor = _FakeExecutor(report)
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1")

            self.assertEqual(result["policy_outcome"], "allow")
            self.assertEqual(len(executor.calls), 1)
            self.assertEqual(executor.calls[0]["prompt"], assistant_text)
            self.assertEqual(len(executor.calls[0]["instructions"]), 1)
            self.assertIn("Bridge completion proof", executor.calls[0]["instructions"][0])
            self.assertIn(run_id, executor.calls[0]["instructions"][0])
            self.assertIn("do not recover or re-verify", executor.calls[0]["instructions"][0])

    def test_run_once_delivers_completed_codex_report_left_in_starting_state(self):
        run_id = "20260430T010203-session-1"
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "loop_state": "starting_codex",
                    "last_seen_chat_message_anchor": "msg-assistant-1",
                    "latest_assistant_message_hash": sha1(b"already processed").hexdigest(),
                    "current_codex_run_id": "exec-new-1",
                },
            )
            run_dir = root / "artifacts" / "runs" / run_id
            run_dir.mkdir(parents=True)
            (run_dir / "last_message.md").write_text("Completed final report.", encoding="utf-8")
            report = RunReport.from_dict(
                {
                    "timestamp": "2026-04-19T16:00:00+02:00",
                    "thread_id": "thread-2",
                    "summary": "Codex completed while the supervisor was interrupted.",
                    "files_touched": ["Sources/App.swift"],
                    "checks": ["swift test"],
                    "blockers": [],
                    "risks": [],
                    "next_step": "Wait for ChatGPT.",
                    "artifacts_dir": str(run_dir),
                    "last_message_path": str(run_dir / "last_message.md"),
                    "exit_code": 0,
                    "interruption_reason": "",
                    "observed_codex_thread_id": "exec-new-1",
                    "final_agent_message": "Completed final report.",
                }
            )
            (run_dir / "run_report.json").write_text(json.dumps(report.as_dict()), encoding="utf-8")
            adapter = _FakeAdapter("already processed")
            executor = _FakeExecutor(report)
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1")

            self.assertEqual(result["policy_outcome"], "allow")
            self.assertEqual(result["runner_action"], "wait_for_chatgpt")
            self.assertEqual(executor.calls, [])
            self.assertEqual(len(adapter.posted_messages), 1)
            session_payload = json.loads((sessions_dir / "session-1.json").read_text(encoding="utf-8"))["session"]
            self.assertEqual(session_payload["loop_state"], "waiting_for_chatgpt_response")
            self.assertEqual(session_payload["last_outbound_user_message_kind"], "return_packet")
            self.assertEqual(session_payload["cycles_completed"], 1)
            reloaded_report = json.loads((run_dir / "run_report.json").read_text(encoding="utf-8"))
            self.assertEqual(reloaded_report["delivery_status"], "delivered")

    def test_run_once_recovers_completed_codex_artifacts_without_run_report(self):
        run_id = "20260506T024449-session-1"
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "loop_state": "starting_codex",
                    "last_seen_chat_message_anchor": "msg-assistant-1",
                    "latest_assistant_message_hash": sha1(b"already processed").hexdigest(),
                    "current_codex_run_id": "exec-new-1",
                    "current_codex_thread_id": "thread-recovered-1",
                },
            )
            run_dir = root / "artifacts" / "runs" / run_id
            run_dir.mkdir(parents=True)
            final_message = "# Run Report\n\nCompleted from artifact-only recovery.\n\nSuggested Next Step: Wait for ChatGPT."
            (run_dir / "last_message.md").write_text(final_message, encoding="utf-8")
            (run_dir / "stdout.jsonl").write_text(
                '{"type":"turn.completed","usage":{"input_tokens":12,"output_tokens":5}}\n',
                encoding="utf-8",
            )
            adapter = _FakeAdapter("already processed")
            executor = _FakeExecutor(
                RunReport.from_dict(
                    {
                        "timestamp": "2026-04-19T16:00:00+02:00",
                        "thread_id": "unused",
                        "summary": "Should not run executor.",
                        "files_touched": [],
                        "checks": [],
                        "blockers": [],
                        "risks": [],
                        "next_step": "",
                    }
                )
            )
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1")

            self.assertEqual(result["policy_outcome"], "allow")
            self.assertEqual(result["runner_action"], "wait_for_chatgpt")
            self.assertEqual(executor.calls, [])
            self.assertEqual(len(adapter.posted_messages), 1)
            report_payload = json.loads((run_dir / "run_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report_payload["delivery_status"], "delivered")
            self.assertEqual(report_payload["summary"], "Run Report")
            self.assertEqual(report_payload["final_agent_message"], final_message)
            self.assertEqual(report_payload["event_types"], ["turn.completed"])
            self.assertEqual(report_payload["usage"]["input_tokens"], 12)
            session_payload = json.loads((sessions_dir / "session-1.json").read_text(encoding="utf-8"))["session"]
            self.assertEqual(session_payload["loop_state"], "waiting_for_chatgpt_response")
            self.assertEqual(session_payload["last_outbound_user_message_kind"], "return_packet")

    def test_run_once_prefers_latest_completed_artifacts_over_older_run_report(self):
        latest_run_id = "20260506T024449-session-1"
        older_run_id = "20260506T014449-session-1"
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "loop_state": "starting_codex",
                    "last_seen_chat_message_anchor": "msg-assistant-1",
                    "latest_assistant_message_hash": sha1(b"already processed").hexdigest(),
                    "current_codex_run_id": "exec-new-1",
                    "current_codex_thread_id": "thread-recovered-1",
                },
            )
            older_run_dir = root / "artifacts" / "runs" / older_run_id
            older_run_dir.mkdir(parents=True)
            older_report = RunReport.from_dict(
                {
                    "timestamp": "2026-05-06T01:44:49+02:00",
                    "thread_id": "old-thread",
                    "summary": "Older completed run.",
                    "files_touched": [],
                    "checks": [],
                    "blockers": [],
                    "risks": [],
                    "next_step": "",
                    "artifacts_dir": str(older_run_dir),
                    "return_packet_id": "packet-old",
                    "delivery_status": "delivered",
                }
            )
            (older_run_dir / "run_report.json").write_text(json.dumps(older_report.as_dict()), encoding="utf-8")
            latest_run_dir = root / "artifacts" / "runs" / latest_run_id
            latest_run_dir.mkdir(parents=True)
            latest_message = "# Latest Artifact Report\n\nRecovered newest run."
            (latest_run_dir / "last_message.md").write_text(latest_message, encoding="utf-8")
            (latest_run_dir / "stdout.jsonl").write_text('{"type":"turn.completed"}\n', encoding="utf-8")
            adapter = _FakeAdapter("already processed")
            executor = _FakeExecutor(older_report)
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1")

            self.assertEqual(result["policy_outcome"], "allow")
            self.assertNotEqual(result["return_packet_id"], "packet-old")
            self.assertTrue((latest_run_dir / "run_report.json").exists())
            self.assertEqual(executor.calls, [])
            self.assertEqual(len(adapter.posted_messages), 1)
            latest_payload = json.loads((latest_run_dir / "run_report.json").read_text(encoding="utf-8"))
            self.assertEqual(latest_payload["summary"], "Latest Artifact Report")
            self.assertEqual(latest_payload["final_agent_message"], latest_message)

    def test_run_once_honors_completed_session_without_opening_chat_or_restarting(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "status": "completed",
                    "auto_run_enabled": False,
                    "supervisor_status": "stopped",
                    "loop_state": "waiting_for_chatgpt_response",
                    "last_posted_return_packet_id": "packet-final",
                },
            )
            adapter = _FakeAdapter("Fresh-looking assistant text that must not restart Codex.")
            executor = _FakeExecutor(
                RunReport.from_dict(
                    {
                        "timestamp": "2026-05-06T02:00:00+02:00",
                        "thread_id": "unused",
                        "summary": "Should not run.",
                        "files_touched": [],
                        "checks": [],
                        "blockers": [],
                        "risks": [],
                        "next_step": "",
                    }
                )
            )
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1")

            self.assertEqual(result["policy_outcome"], "stopped")
            self.assertEqual(result["runner_action"], "stopped")
            self.assertEqual(adapter.open_calls, 0)
            self.assertEqual(executor.calls, [])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text(encoding="utf-8"))["session"]
            self.assertEqual(session_payload["status"], "completed")
            self.assertFalse(session_payload["auto_run_enabled"])
            self.assertEqual(session_payload["supervisor_status"], "stopped")

    def test_run_once_blocks_binding_change_approval_request_without_starting_codex(self):
        assistant_text = "\n".join(
            [
                "Der Codex-Rueckkanal ist gerade nicht vertrauenswuerdig.",
                "Der naechste sichere Mini-Step ist: Bridge-/Packet-Ausgabe reparieren.",
                "Dafuer brauche ich aber deine explizite Freigabe, Codex einmal in das Bridge-Repo zu schicken:",
                "/tmp/test-home/chatgpt-codex-bridge",
                "Schreib bitte nur:",
                "Binding wechseln auf /tmp/test-home/chatgpt-codex-bridge ist erlaubt.",
            ]
        )
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-19T16:00:00+02:00",
                "thread_id": "thread-unused",
                "summary": "Should not run.",
                "files_touched": [],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "last_outbound_user_message_kind": "return_packet",
                    "last_outbound_user_message_anchor": "packet-previous",
                    "last_posted_return_packet_id": "packet-previous",
                },
            )
            adapter = _FakeAdapter(assistant_text, assistant_anchor="msg-assistant-binding-gate")
            executor = _FakeExecutor(report)
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1")

            self.assertEqual(result["policy_outcome"], "require_human")
            self.assertEqual(result["runner_action"], "blocked")
            self.assertEqual(executor.calls, [])
            self.assertEqual(adapter.posted_messages, [])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text(encoding="utf-8"))["session"]
            self.assertEqual(session_payload["status"], "blocked")
            self.assertEqual(session_payload["loop_state"], "requires_human")
            self.assertFalse(session_payload["auto_run_enabled"])
            self.assertEqual(session_payload["policy_decision"]["human_gate_category"], "binding_change_required")
            self.assertEqual(session_payload["last_seen_chat_message_anchor"], "msg-assistant-binding-gate")
            self.assertEqual(session_payload["last_productive_prompt"], "")

    def test_run_once_blocks_new_codex_session_request_without_starting_codex(self):
        assistant_text = "\n".join(
            [
                "Das ist kein PAB-Arbeitsproblem mehr, sondern ein Codex/Bridge-Resume-Problem.",
                "Starte eine neue Codex-Session/frischen Prozess.",
                "Use this as a fresh Codex run, not a resume of the previous noisy thread.",
                "New session id:",
                "session-fresh-pab-smoke-truth",
            ]
        )
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-19T16:00:00+02:00",
                "thread_id": "thread-unused",
                "summary": "Should not run.",
                "files_touched": [],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(root)
            adapter = _FakeAdapter(assistant_text, assistant_anchor="msg-assistant-new-session")
            executor = _FakeExecutor(report)
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1")

            self.assertEqual(result["policy_outcome"], "require_human")
            self.assertEqual(result["runner_action"], "blocked")
            self.assertEqual(executor.calls, [])
            self.assertEqual(adapter.posted_messages, [])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text(encoding="utf-8"))["session"]
            self.assertEqual(session_payload["status"], "blocked")
            self.assertEqual(session_payload["loop_state"], "requires_human")
            self.assertFalse(session_payload["auto_run_enabled"])
            self.assertEqual(session_payload["policy_decision"]["human_gate_category"], "new_codex_session_requested")
            self.assertEqual(session_payload["last_seen_chat_message_anchor"], "msg-assistant-new-session")
            self.assertEqual(session_payload["last_productive_prompt"], "")

    def test_run_once_accepts_fresh_prompt_wording_for_same_session(self):
        assistant_text = "\n".join(
            [
                "Session id: session-1",
                "return_packet_id: packet-1",
                "Now reply once with a fresh complete plain-language next prompt for Codex.",
                "Continue in the same bound session and current Codex thread.",
                "Es soll die gleiche Session bleiben, keinen frischen Prozess starten.",
                "Ignore older text that says New session id: from history; this packet is for the same session.",
            ]
        )
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-19T16:00:00+02:00",
                "thread_id": "thread-current",
                "summary": "Ran.",
                "files_touched": [],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(root)
            adapter = _FakeAdapter(assistant_text, assistant_anchor="msg-assistant-same-session-fresh-prompt")
            executor = _FakeExecutor(report)
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1")

            self.assertNotEqual(result["runner_action"], "blocked")
            self.assertEqual(len(executor.calls), 1)
            session_payload = json.loads((sessions_dir / "session-1.json").read_text(encoding="utf-8"))["session"]
            self.assertNotEqual(session_payload["policy_decision"]["human_gate_category"], "new_codex_session_requested")

    def test_run_once_retries_executor_failure_instead_of_blocking(self):
        assistant_text = "Continue with the next implementation step."

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "last_seen_chat_message_anchor": "msg-assistant-previous",
                    "latest_assistant_message_hash": "old-hash",
                },
            )
            adapter = _FakeAdapter(assistant_text, assistant_anchor="msg-assistant-new")
            executor = _RaisingExecutor(RuntimeError("Codex launcher failed."))
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1")

            self.assertEqual(result["policy_outcome"], "allow")
            self.assertEqual(result["runner_action"], "retry_codex_run")
            session_payload = json.loads((sessions_dir / "session-1.json").read_text(encoding="utf-8"))["session"]
            self.assertEqual(session_payload["status"], "active")
            self.assertEqual(session_payload["loop_state"], "starting_codex")
            self.assertTrue(session_payload["auto_run_enabled"])

    def test_run_once_retries_progress_stall_instead_of_blocking(self):
        assistant_text = "Continue with the next implementation step."
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-19T16:00:00+02:00",
                "thread_id": "thread-2",
                "summary": "Codex run stalled without new output and was terminated for automatic retry.",
                "files_touched": [],
                "checks": [],
                "blockers": ["Codex stalled without new output for 300.0 seconds."],
                "risks": [],
                "next_step": "The bridge should rearm the same assistant turn and retry automatically.",
                "exit_code": 124,
                "interruption_reason": "progress_stall",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "last_seen_chat_message_anchor": "msg-assistant-previous",
                    "latest_assistant_message_hash": "old-hash",
                },
            )
            adapter = _FakeAdapter(assistant_text, assistant_anchor="msg-assistant-new")
            executor = _FakeExecutor(report)
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1")

            self.assertEqual(result["policy_outcome"], "allow")
            self.assertEqual(result["runner_action"], "retry_codex_run")
            session_payload = json.loads((sessions_dir / "session-1.json").read_text(encoding="utf-8"))["session"]
            self.assertEqual(session_payload["status"], "active")
            self.assertEqual(session_payload["loop_state"], "starting_codex")
            self.assertTrue(session_payload["auto_run_enabled"])
            self.assertIn("stalled without new output", session_payload["last_error"])

    def test_run_once_retries_retryable_codex_runtime_failure_instead_of_blocking(self):
        assistant_text = "Continue with the next implementation step."
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-25T04:20:19+02:00",
                "thread_id": "thread-2",
                "summary": "I am still working on the implementation.",
                "files_touched": [],
                "checks": [],
                "blockers": [
                    "codex exec exited with code 1",
                    "2026-04-25T02:23:05.032046Z ERROR codex_core::tools::router: error=failed to parse function arguments: EOF while parsing an object at line 75892 column 0",
                ],
                "risks": [],
                "next_step": "The bridge should retry the same assistant turn automatically.",
                "exit_code": 1,
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "last_seen_chat_message_anchor": "msg-assistant-previous",
                    "latest_assistant_message_id": "msg-assistant-previous",
                    "latest_assistant_message_hash": "old-hash",
                },
            )
            adapter = _FakeAdapter(assistant_text, assistant_anchor="msg-assistant-new")
            executor = _FakeExecutor(report)
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1")

            self.assertEqual(result["policy_outcome"], "allow")
            self.assertEqual(result["runner_action"], "retry_codex_run")
            session_payload = json.loads((sessions_dir / "session-1.json").read_text(encoding="utf-8"))["session"]
            self.assertEqual(session_payload["status"], "active")
            self.assertEqual(session_payload["loop_state"], "starting_codex")
            self.assertTrue(session_payload["auto_run_enabled"])
            self.assertEqual(session_payload["last_seen_chat_message_anchor"], "msg-assistant-previous")
            self.assertEqual(session_payload["latest_assistant_message_id"], "msg-assistant-previous")
            self.assertEqual(session_payload["latest_assistant_message_hash"], "old-hash")
            self.assertIn("failed to parse function arguments", session_payload["last_error"])
            self.assertEqual(session_payload["degraded_mode"], "retrying_codex_runtime_failure")

    def test_run_once_does_not_reactivate_paused_pending_return_packet_retry(self):
        assistant_text = "Continue with the next implementation step."

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "status": "paused",
                    "loop_state": "paused",
                    "auto_run_enabled": False,
                    "supervisor_status": "paused",
                    "last_outbound_user_message_kind": "return_packet_retry_pending",
                    "last_outbound_user_message_anchor": "packet-stuck",
                    "last_outbound_user_message_sent_at": time.time(),
                    "degraded_mode": "retrying_return_packet",
                    "degraded_reason": "Message delivery confirmation timed out.",
                },
            )
            adapter = _FakeAdapter(assistant_text)
            executor = _FakeExecutor(
                RunReport.from_dict(
                    {
                        "timestamp": "2026-04-19T16:00:00+02:00",
                        "thread_id": "thread-2",
                        "summary": "Paused session should stay paused.",
                        "files_touched": [],
                        "checks": [],
                        "blockers": [],
                        "risks": [],
                        "next_step": "",
                    }
                )
            )
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1")

            self.assertEqual(result["policy_outcome"], "paused")
            self.assertEqual(result["runner_action"], "paused")
            session_payload = json.loads((sessions_dir / "session-1.json").read_text(encoding="utf-8"))["session"]
            self.assertEqual(session_payload["status"], "paused")
            self.assertEqual(session_payload["loop_state"], "paused")
            self.assertFalse(session_payload["auto_run_enabled"])
            self.assertEqual(session_payload["last_outbound_user_message_kind"], "")
            self.assertEqual(session_payload["last_outbound_user_message_anchor"], "")
            self.assertEqual(session_payload["degraded_mode"], "")
            self.assertEqual(adapter.open_calls, 0)
            self.assertEqual(executor.calls, [])

    def test_run_once_allows_same_indexed_assistant_turn_hash_change_for_ready_packet(self):
        expected_text = "Continue the implementation."
        observed_text = "Continue the implementation.\n\nAdditional rendered detail."
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-19T16:00:00+02:00",
                "thread_id": "thread-2",
                "summary": "Codex completed the next safe slice.",
                "files_touched": ["mastermind_bridge/orchestrator/loop.py"],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "Wait for the next ChatGPT answer.",
                "observed_codex_thread_id": "exec-new-1",
                "return_packet_id": "packet-ready-1",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "last_outbound_user_message_anchor": "packet-ready-1",
                    "last_outbound_user_message_kind": "return_packet_ready",
                    "last_seen_chat_message_anchor": "assistant-106-f735da568ac4",
                    "latest_assistant_message_hash": sha1(expected_text.encode("utf-8")).hexdigest(),
                },
            )
            run_dir = root / "artifacts" / "runs" / "20260419T160000-session-1"
            run_dir.mkdir(parents=True, exist_ok=True)
            report.artifacts_dir = str(run_dir)
            report.delivery_status = "ready_to_post"
            (run_dir / "run_report.json").write_text(
                json.dumps(report.as_dict()),
                encoding="utf-8",
            )
            adapter = _FakeAdapter(
                observed_text,
                assistant_anchor="assistant-106-9ac8bbbdc2be",
            )
            executor = _FakeExecutor(report)
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1")

            self.assertEqual(result["policy_outcome"], "allow")
            self.assertEqual(result["runner_action"], "wait_for_chatgpt")
            self.assertEqual(result["return_packet_id"], "packet-ready-1")
            self.assertEqual(executor.calls, [])
            self.assertEqual(len(adapter.posted_messages), 1)
            persisted_report = RunReport.from_dict(json.loads((run_dir / "run_report.json").read_text(encoding="utf-8")))
            self.assertEqual(persisted_report.delivery_status, "delivered")
            session_payload = json.loads((sessions_dir / "session-1.json").read_text(encoding="utf-8"))["session"]
            self.assertEqual(session_payload["loop_state"], "waiting_for_chatgpt_response")
            self.assertEqual(session_payload["last_outbound_user_message_kind"], "return_packet")
            self.assertEqual(session_payload["last_posted_return_packet_id"], "packet-ready-1")

    def test_deliver_packet_accepts_visible_packet_after_assistant_turn_shifted(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "last_seen_chat_message_anchor": "assistant-before-codex",
                    "latest_assistant_message_hash": sha1("Prompt that started Codex.".encode("utf-8")).hexdigest(),
                },
            )
            adapter = _FakeAdapter(
                "ChatGPT has already answered the visible return packet.",
                assistant_anchor="assistant-after-packet",
                visible_message_ids={"packet-visible-after-shift"},
                current_chat_urls=["https://chatgpt.com/c/project/binding-1"],
            )
            executor = _FakeExecutor(
                RunReport.from_dict(
                    {
                        "timestamp": "2026-04-19T16:00:00+02:00",
                        "thread_id": "thread-2",
                        "summary": "Unused.",
                        "files_touched": [],
                        "checks": [],
                        "blockers": [],
                        "risks": [],
                        "next_step": "",
                    }
                )
            )
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            delivery = runner._deliver_packet(
                load_session(sessions_dir / "session-1.json"),
                {
                    "delivery_retry": {
                        "max_attempts": 2,
                        "known_error_signatures": ["Reasoning failed"],
                    }
                },
                "packet-visible-after-shift",
                "return packet text",
            )

            self.assertEqual(delivery["status"], "delivered")
            self.assertEqual(delivery["attempt_count"], 0)
            self.assertEqual(adapter.prepare_calls, 0)
            self.assertEqual(adapter.posted_messages, [])
            self.assertEqual(adapter.visibility_checks, ["packet-visible-after-shift"])

    def test_run_once_recovers_report_with_delivered_attempt_but_stale_delivery_status(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "loop_state": "posting_return_packet",
                    "last_outbound_user_message_anchor": "packet-manual-1",
                    "last_outbound_user_message_kind": "return_packet_retry_pending",
                    "last_outbound_user_message_sent_at": time.time() - 600.0,
                    "last_seen_chat_message_anchor": "assistant-before-codex",
                    "latest_assistant_message_hash": sha1("Prompt that started Codex.".encode("utf-8")).hexdigest(),
                },
            )
            run_dir = root / "artifacts" / "runs" / "20260419T160000-session-1"
            run_dir.mkdir(parents=True, exist_ok=True)
            report = RunReport.from_dict(
                {
                    "timestamp": "2026-04-19T16:00:00+02:00",
                    "thread_id": "thread-2",
                    "session_id": "session-1",
                    "summary": "Codex completed the next safe slice.",
                    "files_touched": ["mastermind_bridge/orchestrator/loop.py"],
                    "checks": [],
                    "blockers": [],
                    "risks": [],
                    "next_step": "Wait for the next ChatGPT answer.",
                    "artifacts_dir": str(run_dir),
                    "return_packet_id": "packet-manual-1",
                    "delivery_status": "preflight_failed",
                    "delivery_attempt_count": 3,
                    "delivery_attempts": [
                        {
                            "attempt_number": 1,
                            "status": "failed",
                            "transport": "chatgpt_browser",
                            "return_packet_id": "packet-manual-1",
                            "error_signature": "Message delivery confirmation timed out.",
                        },
                        {
                            "attempt_number": 2,
                            "status": "delivered",
                            "transport": "manual_real_chrome_gui",
                            "return_packet_id": "packet-manual-1",
                            "error_signature": "",
                        },
                    ],
                }
            )
            (run_dir / "run_report.json").write_text(json.dumps(report.as_dict()), encoding="utf-8")
            adapter = _FakeAdapter(
                "ChatGPT already started a later assistant turn.",
                assistant_anchor="assistant-after-packet",
            )
            executor = _FakeExecutor(report)
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1")

            self.assertEqual(result["policy_outcome"], "allow")
            self.assertEqual(result["runner_action"], "wait_for_chatgpt")
            self.assertEqual(result["return_packet_id"], "packet-manual-1")
            self.assertEqual(adapter.posted_messages, [])
            persisted_report = RunReport.from_dict(json.loads((run_dir / "run_report.json").read_text(encoding="utf-8")))
            self.assertEqual(persisted_report.delivery_status, "delivered")
            self.assertEqual(persisted_report.delivery_attempt_count, 2)
            session_payload = json.loads((sessions_dir / "session-1.json").read_text(encoding="utf-8"))["session"]
            self.assertEqual(session_payload["loop_state"], "waiting_for_chatgpt_response")
            self.assertEqual(session_payload["last_outbound_user_message_kind"], "return_packet")
            self.assertEqual(session_payload["last_posted_return_packet_id"], "packet-manual-1")

    def test_run_once_recovers_blocked_session_when_latest_report_was_already_delivered(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "status": "blocked",
                    "auto_run_enabled": False,
                    "supervisor_status": "blocked",
                    "loop_state": "requires_human",
                    "last_outbound_user_message_anchor": "packet-manual-2",
                    "last_outbound_user_message_kind": "return_packet",
                    "last_posted_return_packet_id": "packet-manual-2",
                    "cycles_completed": 7,
                    "human_attention_reason": "Return packet delivery was marked failed after manual recovery.",
                    "last_error": "Return packet delivery was marked failed after manual recovery.",
                    "degraded_mode": "stale_return_packet",
                    "degraded_reason": "Return packet delivery was marked failed after manual recovery.",
                },
            )
            run_dir = root / "artifacts" / "runs" / "20260419T170000-session-1"
            run_dir.mkdir(parents=True, exist_ok=True)
            report = RunReport.from_dict(
                {
                    "timestamp": "2026-04-19T17:00:00+02:00",
                    "thread_id": "thread-2",
                    "session_id": "session-1",
                    "summary": "Codex completed the next safe slice.",
                    "files_touched": [],
                    "checks": [],
                    "blockers": [],
                    "risks": [],
                    "next_step": "Wait for the next ChatGPT answer.",
                    "artifacts_dir": str(run_dir),
                    "return_packet_id": "packet-manual-2",
                    "delivery_status": "preflight_failed",
                    "delivery_attempts": [
                        {
                            "attempt_number": 1,
                            "status": "delivered",
                            "transport": "manual_real_chrome_gui",
                            "return_packet_id": "packet-manual-2",
                            "error_signature": "",
                        },
                    ],
                }
            )
            (run_dir / "run_report.json").write_text(json.dumps(report.as_dict()), encoding="utf-8")
            adapter = _FakeAdapter(
                "ChatGPT is still waiting after the packet.",
                assistant_anchor="assistant-after-packet",
            )
            executor = _FakeExecutor(report)
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1")

            self.assertEqual(result["policy_outcome"], "allow")
            self.assertEqual(result["runner_action"], "wait_for_chatgpt")
            self.assertEqual(adapter.posted_messages, [])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text(encoding="utf-8"))["session"]
            self.assertEqual(session_payload["status"], "active")
            self.assertTrue(session_payload["auto_run_enabled"])
            self.assertEqual(session_payload["supervisor_status"], "running")
            self.assertEqual(session_payload["loop_state"], "waiting_for_chatgpt_response")
            self.assertEqual(session_payload["last_outbound_user_message_kind"], "return_packet")
            self.assertEqual(session_payload["last_posted_return_packet_id"], "packet-manual-2")
            self.assertEqual(session_payload["cycles_completed"], 7)
            self.assertEqual(session_payload["human_attention_reason"], "")
            self.assertEqual(session_payload["last_error"], "")

    def test_run_once_records_retry_required_when_return_packet_delivery_fails(self):
        assistant_text = "Ship the next safe patch."
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-19T16:00:00+02:00",
                "thread_id": "thread-2",
                "summary": "Codex completed the next safe slice.",
                "files_touched": ["mastermind_bridge/orchestrator/loop.py"],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "Wait for the next ChatGPT answer.",
                "observed_codex_thread_id": "exec-new-1",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(root)
            run_dir = root / "artifacts" / "runs" / "20260419T160000-session-1"
            run_dir.mkdir(parents=True, exist_ok=True)
            report.artifacts_dir = str(run_dir)
            adapter = _FakeAdapter(
                assistant_text,
                post_results=[{"status": "failed", "error_signature": "Syntax error: browser transport failed."}],
            )
            executor = _FakeExecutor(report)
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1")

            self.assertEqual(result["policy_outcome"], "allow")
            self.assertEqual(result["runner_action"], "wait_for_chatgpt")
            persisted_report = RunReport.from_dict(json.loads((run_dir / "run_report.json").read_text(encoding="utf-8")))
            self.assertEqual(persisted_report.delivery_status, "retry_required")
            session_payload = json.loads((sessions_dir / "session-1.json").read_text(encoding="utf-8"))["session"]
            self.assertEqual(session_payload["status"], "active")
            self.assertEqual(session_payload["loop_state"], "posting_return_packet")
            self.assertEqual(session_payload["degraded_mode"], "retrying_return_packet")

    def test_run_once_blocks_stale_return_packet_preflight_without_retry_cooldown(self):
        assistant_text = "Ship the next safe patch."
        stale_signature = (
            "ChatGPT is showing a different assistant turn than the one that started this Codex run. "
            "The bridge refused to post the return packet into a shifted chat state."
        )
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-19T16:00:00+02:00",
                "thread_id": "thread-2",
                "summary": "Codex completed the next safe slice.",
                "files_touched": ["mastermind_bridge/orchestrator/loop.py"],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "Wait for the next ChatGPT answer.",
                "observed_codex_thread_id": "exec-new-1",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(root)
            run_dir = root / "artifacts" / "runs" / "20260419T160000-session-1"
            run_dir.mkdir(parents=True, exist_ok=True)
            report.artifacts_dir = str(run_dir)
            adapter = _FakeAdapter(
                assistant_text,
                prepare_results=[
                    {"status": "failed", "error_signature": stale_signature},
                    {"status": "failed", "error_signature": stale_signature},
                ],
            )
            executor = _FakeExecutor(report)
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1")

            self.assertEqual(result["policy_outcome"], "require_human")
            self.assertEqual(result["runner_action"], "blocked")
            self.assertEqual(len(executor.calls), 1)
            self.assertEqual(adapter.posted_messages, [])
            persisted_report = RunReport.from_dict(json.loads((run_dir / "run_report.json").read_text(encoding="utf-8")))
            self.assertEqual(persisted_report.delivery_status, "stale_chat_state")
            self.assertEqual(persisted_report.policy_outcome, "require_human")
            self.assertEqual(persisted_report.delivery_attempt_count, 2)
            session_payload = json.loads((sessions_dir / "session-1.json").read_text(encoding="utf-8"))["session"]
            self.assertEqual(session_payload["status"], "blocked")
            self.assertEqual(session_payload["loop_state"], "requires_human")
            self.assertFalse(session_payload["auto_run_enabled"])
            self.assertEqual(session_payload["last_outbound_user_message_anchor"], persisted_report.return_packet_id)
            self.assertEqual(session_payload["last_outbound_user_message_kind"], "return_packet_retry_pending")
            self.assertEqual(session_payload["last_posted_return_packet_id"], "")
            self.assertEqual(session_payload["degraded_mode"], "stale_return_packet")
            self.assertEqual(
                session_payload["policy_decision"]["human_gate_category"],
                "stale_return_packet_chat_state",
            )

    def test_run_once_blocks_stale_pending_return_packet_before_next_codex_turn(self):
        next_assistant_text = "Continue the Google Drive lane and make the next concrete repo change."
        pending_report = RunReport.from_dict(
            {
                "timestamp": "2026-04-19T16:00:00+02:00",
                "thread_id": "thread-old",
                "summary": "Older Codex result waiting to be posted.",
                "files_touched": ["docs/status/CURRENT_STATE.md"],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "Post the return packet back into ChatGPT.",
                "observed_codex_thread_id": "exec-old-1",
                "return_packet_id": "packet-retry-1",
            }
        )
        next_report = RunReport.from_dict(
            {
                "timestamp": "2026-04-19T17:00:00+02:00",
                "thread_id": "thread-new",
                "summary": "Codex completed the next safe slice.",
                "files_touched": ["mastermind_bridge/orchestrator/loop.py"],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "Wait for the next ChatGPT answer.",
                "observed_codex_thread_id": "exec-new-2",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "last_outbound_user_message_anchor": "packet-retry-1",
                    "last_outbound_user_message_kind": "return_packet_retry_pending",
                    "last_outbound_user_message_sent_at": 0.0,
                    "last_seen_chat_message_anchor": "msg-assistant-old",
                    "latest_assistant_message_hash": sha1("Older assistant text.".encode("utf-8")).hexdigest(),
                },
            )
            pending_run_dir = root / "artifacts" / "runs" / "20260419T160000-session-1"
            pending_run_dir.mkdir(parents=True, exist_ok=True)
            pending_report.artifacts_dir = str(pending_run_dir)
            pending_report.delivery_status = "retry_required"
            pending_report.delivery_attempts = [
                {
                    "attempt_number": 1,
                    "status": "failed",
                    "transport": "chatgpt_browser",
                    "return_packet_id": "packet-retry-1",
                    "error_signature": "Message delivery confirmation timed out.",
                }
            ]
            pending_report.delivery_attempt_count = 1
            (pending_run_dir / "run_report.json").write_text(
                json.dumps(pending_report.as_dict()),
                encoding="utf-8",
            )
            adapter = _FakeAdapter(
                next_assistant_text,
                assistant_anchor="msg-assistant-new",
                prepare_results=[
                    {
                        "status": "failed",
                        "error_signature": (
                            "ChatGPT is showing a different assistant turn than the one that started this Codex run. "
                            "The bridge refused to post the return packet into a shifted chat state."
                        ),
                    }
                ],
            )
            executor = _FakeExecutor(next_report)
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1")

            self.assertEqual(result["policy_outcome"], "require_human")
            self.assertEqual(result["runner_action"], "blocked")
            self.assertEqual(executor.calls, [])
            self.assertEqual(adapter.posted_messages, [])
            stale_report = RunReport.from_dict(
                json.loads((pending_run_dir / "run_report.json").read_text(encoding="utf-8"))
            )
            self.assertEqual(stale_report.delivery_status, "stale_chat_state")
            self.assertEqual(stale_report.policy_outcome, "require_human")
            session_payload = json.loads((sessions_dir / "session-1.json").read_text(encoding="utf-8"))["session"]
            self.assertEqual(session_payload["status"], "blocked")
            self.assertEqual(session_payload["loop_state"], "requires_human")
            self.assertFalse(session_payload["auto_run_enabled"])
            self.assertEqual(session_payload["last_seen_chat_message_anchor"], "msg-assistant-old")
            self.assertEqual(session_payload["last_outbound_user_message_anchor"], "packet-retry-1")
            self.assertEqual(session_payload["last_outbound_user_message_kind"], "return_packet_retry_pending")
            self.assertEqual(
                session_payload["policy_decision"]["human_gate_category"],
                "stale_return_packet_chat_state",
            )

    def test_run_once_retries_visible_network_error_while_waiting_for_chatgpt_response(self):
        assistant_text = "Previous assistant turn."
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-19T16:00:00+02:00",
                "thread_id": "thread-2",
                "summary": "Unused.",
                "files_touched": [],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "last_outbound_user_message_kind": "return_packet",
                    "last_outbound_user_message_anchor": "packet-123",
                    "last_outbound_user_message_sent_at": time.time(),
                    "loop_state": "waiting_for_chatgpt_response",
                },
            )
            adapter = _FakeAdapter(
                assistant_text,
                assistant_error_text="Network error\nErneut versuchen",
                assistant_retry_result=True,
            )
            executor = _FakeExecutor(report)
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1")

            self.assertEqual(result["policy_outcome"], "allow")
            self.assertEqual(result["runner_action"], "wait_for_chatgpt")
            self.assertEqual(adapter.assistant_retry_calls, 1)
            self.assertEqual(executor.calls, [])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text(encoding="utf-8"))["session"]
            self.assertEqual(session_payload["loop_state"], "waiting_for_chatgpt_response")
            self.assertEqual(session_payload["degraded_mode"], "retrying_chatgpt_reply")
            self.assertEqual(session_payload["last_error"], "")
            self.assertIn("Network error", session_payload["policy_decision"]["reasons"][0])

    def test_run_once_marks_visible_network_error_as_retrying_not_honest_wait_when_retry_fails(self):
        assistant_text = "Previous assistant turn."
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-19T16:00:00+02:00",
                "thread_id": "thread-2",
                "summary": "Unused.",
                "files_touched": [],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "last_outbound_user_message_kind": "return_packet",
                    "last_outbound_user_message_anchor": "packet-123",
                    "last_outbound_user_message_sent_at": time.time(),
                    "loop_state": "waiting_for_chatgpt_response",
                },
            )
            adapter = _FakeAdapter(
                assistant_text,
                assistant_error_text="Network error\nErneut versuchen",
                assistant_retry_result=False,
            )
            executor = _FakeExecutor(report)
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1")

            self.assertEqual(result["policy_outcome"], "allow")
            self.assertEqual(result["runner_action"], "wait_for_chatgpt")
            self.assertEqual(adapter.assistant_retry_calls, 1)
            self.assertEqual(executor.calls, [])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text(encoding="utf-8"))["session"]
            self.assertEqual(session_payload["loop_state"], "waiting_for_chatgpt_response")
            self.assertEqual(session_payload["degraded_mode"], "retrying_chatgpt_reply")
            self.assertEqual(session_payload["last_error"], "ChatGPT in-page send failed.")
            self.assertIn("Network error", session_payload["policy_decision"]["reasons"][0])

    def test_run_once_retries_when_retryable_error_text_is_rendered_as_assistant_message(self):
        assistant_text = "A network error occurred. Please check your connection and try again.\n\nErneut versuchen"
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-19T16:00:00+02:00",
                "thread_id": "thread-2",
                "summary": "Unused.",
                "files_touched": [],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "last_outbound_user_message_kind": "return_packet",
                    "last_outbound_user_message_anchor": "packet-123",
                    "last_outbound_user_message_sent_at": time.time(),
                    "loop_state": "waiting_for_chatgpt_response",
                },
            )
            adapter = _FakeAdapter(
                assistant_text,
                assistant_retry_result=True,
            )
            executor = _FakeExecutor(report)
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1")

            self.assertEqual(result["policy_outcome"], "allow")
            self.assertEqual(result["runner_action"], "wait_for_chatgpt")
            self.assertEqual(adapter.assistant_retry_calls, 1)
            self.assertEqual(executor.calls, [])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text(encoding="utf-8"))["session"]
            self.assertEqual(session_payload["loop_state"], "waiting_for_chatgpt_response")
            self.assertEqual(session_payload["degraded_mode"], "retrying_chatgpt_reply")
            self.assertEqual(session_payload["last_error"], "")

    def test_run_once_recovers_from_previous_bad_error_prompt_after_resume(self):
        assistant_text = "A network error occurred. Please check your connection and try again.\n\nErneut versuchen"
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-19T16:00:00+02:00",
                "thread_id": "thread-2",
                "summary": "Unused.",
                "files_touched": [],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "loop_state": "idle",
                    "last_seen_chat_message_anchor": "msg-assistant-1",
                    "latest_assistant_message_hash": sha1(assistant_text.encode("utf-8")).hexdigest(),
                    "last_productive_prompt": assistant_text,
                    "last_productive_task_label": "accepted_assistant_text",
                },
            )
            adapter = _FakeAdapter(
                assistant_text,
                assistant_retry_result=True,
            )
            executor = _FakeExecutor(report)
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1")

            self.assertEqual(result["policy_outcome"], "allow")
            self.assertEqual(result["runner_action"], "wait_for_chatgpt")
            self.assertEqual(adapter.assistant_retry_calls, 1)
            self.assertEqual(executor.calls, [])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text(encoding="utf-8"))["session"]
            self.assertEqual(session_payload["loop_state"], "waiting_for_chatgpt_response")
            self.assertEqual(session_payload["degraded_mode"], "retrying_chatgpt_reply")

    def test_run_once_reloads_bound_chat_when_return_packet_is_visible_but_no_new_assistant_starts(self):
        assistant_text = "Already processed assistant reply."
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-19T16:00:00+02:00",
                "thread_id": "thread-2",
                "summary": "Unused.",
                "files_touched": [],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "last_seen_chat_message_anchor": "msg-assistant-1",
                    "latest_assistant_message_hash": sha1(assistant_text.encode("utf-8")).hexdigest(),
                    "last_outbound_user_message_anchor": "packet-123",
                    "last_outbound_user_message_kind": "return_packet",
                    "last_outbound_user_message_sent_at": time.time()
                    - (_OUTBOUND_USER_MESSAGE_TIMEOUT_SECONDS + 1.0),
                    "last_posted_return_packet_id": "packet-123",
                    "auto_run_enabled": True,
                    "supervisor_status": "running",
                },
            )
            adapter = _FakeAdapter(assistant_text, reload_result=True)
            executor = _FakeExecutor(report)
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1", require_new_message=True)

            self.assertEqual(result["policy_outcome"], "allow")
            self.assertEqual(result["runner_action"], "wait_for_chatgpt")
            self.assertEqual(adapter.reload_calls, 1)
            self.assertEqual(adapter.posted_messages, [])
            self.assertEqual(executor.calls, [])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text(encoding="utf-8"))["session"]
            self.assertEqual(session_payload["loop_state"], "waiting_for_chatgpt_response")
            self.assertEqual(session_payload["productive_rewind_attempts"], 1)
            self.assertEqual(session_payload["degraded_mode"], "retrying_chatgpt_reply")
            self.assertIn("reloaded the bound chat", session_payload["degraded_reason"])

    def test_run_once_posts_same_chat_recovery_prompt_after_reload_fails_to_unstick_silent_stall(self):
        assistant_text = "Already processed assistant reply."
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-19T16:00:00+02:00",
                "thread_id": "thread-2",
                "summary": "Unused.",
                "files_touched": [],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "last_seen_chat_message_anchor": "msg-assistant-1",
                    "latest_assistant_message_hash": sha1(assistant_text.encode("utf-8")).hexdigest(),
                    "last_outbound_user_message_anchor": "packet-123",
                    "last_outbound_user_message_kind": "return_packet",
                    "last_outbound_user_message_sent_at": time.time()
                    - (_OUTBOUND_USER_MESSAGE_TIMEOUT_SECONDS + 1.0),
                    "last_posted_return_packet_id": "packet-123",
                    "productive_rewind_attempts": 1,
                    "auto_run_enabled": True,
                    "supervisor_status": "running",
                },
            )
            adapter = _FakeAdapter(
                assistant_text,
                reload_result=False,
                post_results=[{"status": "delivered", "message_anchor": "msg-user-recovery"}],
            )
            executor = _FakeExecutor(report)
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1", require_new_message=True)

            self.assertEqual(result["policy_outcome"], "allow")
            self.assertEqual(result["runner_action"], "wait_for_chatgpt")
            self.assertEqual(adapter.reload_calls, 0)
            self.assertEqual(len(adapter.posted_messages), 1)
            self.assertIn("stalled_assistant_recovery:", adapter.posted_messages[0])
            self.assertIn("packet-123", adapter.posted_messages[0])
            self.assertEqual(executor.calls, [])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text(encoding="utf-8"))["session"]
            self.assertEqual(session_payload["loop_state"], "waiting_for_chatgpt_response")
            self.assertEqual(session_payload["last_outbound_user_message_kind"], "return_packet")
            self.assertTrue(
                session_payload["last_outbound_user_message_anchor"].startswith("stalled-assistant-recovery-")
            )
            self.assertEqual(session_payload["productive_rewind_attempts"], 2)
            self.assertEqual(session_payload["degraded_mode"], "retrying_chatgpt_reply")
            self.assertIn("same-chat recovery prompt", session_payload["degraded_reason"])

    def test_run_once_treats_same_anchor_changed_text_as_new_assistant_message(self):
        previous_text = "Continue the previous task."
        next_text = "Continue with the newly retried task."
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-19T16:00:00+02:00",
                "thread_id": "thread-2",
                "summary": "Unused.",
                "files_touched": [],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "last_seen_chat_message_anchor": "msg-assistant-1",
                    "latest_assistant_message_hash": sha1(previous_text.encode("utf-8")).hexdigest(),
                },
            )
            adapter = _FakeAdapter(next_text, assistant_anchor="msg-assistant-1")
            executor = _FakeExecutor(report)
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1")

            self.assertEqual(result["policy_outcome"], "allow")
            self.assertEqual(executor.calls[0]["prompt"], next_text)

    def test_run_once_does_not_forward_retryable_error_surface_as_next_codex_prompt(self):
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-19T16:00:00+02:00",
                "thread_id": "thread-2",
                "summary": "Unused.",
                "files_touched": [],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "last_outbound_user_message_kind": "return_packet",
                    "last_outbound_user_message_anchor": "packet-123",
                    "last_outbound_user_message_sent_at": time.time(),
                    "loop_state": "waiting_for_chatgpt_response",
                },
            )
            adapter = _FakeAdapter(
                "A network error occurred. Please check your connection and try again.\n\nErneut versuchen",
                assistant_retry_result=True,
            )
            executor = _FakeExecutor(report)
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1")

            self.assertEqual(result["policy_outcome"], "allow")
            self.assertEqual(result["runner_action"], "wait_for_chatgpt")
            self.assertEqual(adapter.assistant_retry_calls, 1)
            self.assertEqual(executor.calls, [])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text(encoding="utf-8"))["session"]
            self.assertEqual(session_payload["loop_state"], "waiting_for_chatgpt_response")
            self.assertEqual(session_payload["degraded_mode"], "retrying_chatgpt_reply")
            self.assertEqual(session_payload["last_error"], "")
            self.assertIn("network error", session_payload["policy_decision"]["reasons"][0].casefold())

    def test_run_once_resumes_waiting_for_followup_after_pause_via_last_posted_packet(self):
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-19T16:00:00+02:00",
                "thread_id": "thread-2",
                "summary": "Unused.",
                "files_touched": [],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "last_posted_return_packet_id": "packet-123",
                    "last_delivery_at": "2026-04-22T18:02:31+02:00",
                    "last_chat_activity_at": "2026-04-22T18:02:31+02:00",
                    "last_outbound_user_message_kind": "",
                    "last_outbound_user_message_anchor": "",
                    "loop_state": "idle",
                },
            )
            adapter = _FakeAdapter(
                "",
                assistant_read_error="ChatGPT DOM contract missing `assistant_message` selector match.",
                assistant_error_text="Network error\nErneut versuchen",
                assistant_retry_result=True,
            )
            executor = _FakeExecutor(report)
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1")

            self.assertEqual(result["policy_outcome"], "allow")
            self.assertEqual(result["runner_action"], "wait_for_chatgpt")
            self.assertEqual(adapter.assistant_retry_calls, 1)
            self.assertEqual(executor.calls, [])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text(encoding="utf-8"))["session"]
            self.assertEqual(session_payload["loop_state"], "waiting_for_chatgpt_response")
            self.assertEqual(session_payload["degraded_mode"], "retrying_chatgpt_reply")
            self.assertEqual(session_payload["last_error"], "")
            self.assertIn("Network error", session_payload["policy_decision"]["reasons"][0])

    def test_run_once_retries_stalled_followup_after_pause_without_outbound_anchor(self):
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-19T16:00:00+02:00",
                "thread_id": "thread-2",
                "summary": "Unused.",
                "files_touched": [],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "last_posted_return_packet_id": "packet-123",
                    "last_delivery_at": "2026-04-22T18:02:31+02:00",
                    "last_chat_activity_at": "2026-04-22T18:02:31+02:00",
                    "last_outbound_user_message_kind": "",
                    "last_outbound_user_message_anchor": "",
                    "last_outbound_user_message_sent_at": 0.0,
                    "loop_state": "waiting_for_chatgpt_response",
                },
            )
            adapter = _FakeAdapter(
                "",
                assistant_read_error="ChatGPT DOM contract missing `assistant_message` selector match.",
                assistant_retry_result=True,
            )
            executor = _FakeExecutor(report)
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1")

            self.assertEqual(result["policy_outcome"], "allow")
            self.assertEqual(result["runner_action"], "wait_for_chatgpt")
            self.assertEqual(adapter.assistant_retry_calls, 1)
            self.assertEqual(executor.calls, [])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text(encoding="utf-8"))["session"]
            self.assertEqual(session_payload["loop_state"], "waiting_for_chatgpt_response")
            self.assertEqual(session_payload["degraded_mode"], "retrying_chatgpt_reply")
            self.assertGreater(session_payload["last_outbound_user_message_sent_at"], 0.0)
            self.assertEqual(session_payload["last_error"], "")

    def test_run_once_reposts_missing_visible_packet_when_waiting_after_delivery_without_outbound_anchor(self):
        pending_report = RunReport.from_dict(
            {
                "timestamp": "2026-04-22T18:02:31+02:00",
                "thread_id": "thread-2",
                "summary": "Codex completed the next safe slice.",
                "files_touched": ["mastermind_bridge/orchestrator/loop.py"],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "Wait for the next ChatGPT answer.",
                "observed_codex_thread_id": "exec-new-1",
                "return_packet_id": "packet-123",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "last_posted_return_packet_id": "packet-123",
                    "last_delivery_at": "2026-04-22T18:02:31+02:00",
                    "last_chat_activity_at": "2026-04-22T18:02:31+02:00",
                    "last_outbound_user_message_kind": "",
                    "last_outbound_user_message_anchor": "",
                    "loop_state": "waiting_for_chatgpt_response",
                },
            )
            pending_run_dir = root / "artifacts" / "runs" / "20260422T180231-session-1"
            pending_run_dir.mkdir(parents=True, exist_ok=True)
            pending_report.artifacts_dir = str(pending_run_dir)
            (pending_run_dir / "run_report.json").write_text(
                json.dumps(pending_report.as_dict()),
                encoding="utf-8",
            )
            adapter = _FakeAdapter("Previous assistant turn still visible.")
            executor = _FakeExecutor(pending_report)
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1")

            self.assertEqual(result["policy_outcome"], "allow")
            self.assertEqual(result["runner_action"], "wait_for_chatgpt")
            self.assertEqual(len(adapter.posted_messages), 1)
            self.assertEqual(executor.calls, [])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text(encoding="utf-8"))["session"]
            self.assertEqual(session_payload["last_outbound_user_message_kind"], "return_packet")
            self.assertEqual(session_payload["last_outbound_user_message_anchor"], "packet-123")
            self.assertEqual(session_payload["last_posted_return_packet_id"], "packet-123")
            self.assertEqual(session_payload["loop_state"], "waiting_for_chatgpt_response")

    def test_run_once_blocks_when_codex_exits_nonzero_before_return_packet(self):
        assistant_text = "Ship the next safe patch."
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-19T16:00:00+02:00",
                "thread_id": "thread-2",
                "summary": "codex exec exited with code 2",
                "files_touched": [],
                "checks": [],
                "blockers": ["codex exec exited with code 2", "error: unexpected argument '-a' found"],
                "risks": [],
                "next_step": "Fix the Codex launch configuration before retrying.",
                "exit_code": 2,
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "last_seen_chat_message_anchor": "msg-assistant-previous",
                    "latest_assistant_message_id": "msg-assistant-previous",
                    "latest_assistant_message_hash": "old-hash",
                },
            )
            adapter = _FakeAdapter(assistant_text, assistant_anchor="msg-assistant-new")
            executor = _FakeExecutor(report)
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1")

            self.assertEqual(result["policy_outcome"], "require_human")
            self.assertEqual(result["runner_action"], "blocked")
            session_payload = json.loads((sessions_dir / "session-1.json").read_text(encoding="utf-8"))["session"]
            self.assertEqual(session_payload["status"], "blocked")
            self.assertEqual(session_payload["loop_state"], "requires_human")
            self.assertFalse(session_payload["auto_run_enabled"])
            self.assertEqual(session_payload["supervisor_status"], "blocked")
            self.assertEqual(session_payload["last_seen_chat_message_anchor"], "msg-assistant-previous")
            self.assertEqual(session_payload["latest_assistant_message_id"], "msg-assistant-previous")
            self.assertEqual(session_payload["latest_assistant_message_hash"], "old-hash")
            self.assertIn("unexpected argument '-a'", session_payload["last_error"])

    def test_run_once_refocuses_bound_chat_and_retries_return_packet_preflight(self):
        assistant_text = "Ship the next safe patch."
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-19T16:00:00+02:00",
                "thread_id": "thread-2",
                "summary": "Codex completed the next safe slice.",
                "files_touched": ["mastermind_bridge/orchestrator/loop.py"],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "Wait for the next ChatGPT answer.",
                "observed_codex_thread_id": "exec-new-1",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(root)
            adapter = _FakeAdapter(
                assistant_text,
                current_chat_urls=["https://chatgpt.com/c/project/different-chat"],
            )
            executor = _FakeExecutor(report)
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1")

            self.assertEqual(result["policy_outcome"], "allow")
            self.assertEqual(result["runner_action"], "cycle_completed")
            self.assertGreaterEqual(adapter.open_calls, 2)
            self.assertEqual(len(adapter.posted_messages), 1)
            session_payload = json.loads((sessions_dir / "session-1.json").read_text(encoding="utf-8"))["session"]
            self.assertEqual(session_payload["status"], "active")
            self.assertEqual(session_payload["last_posted_return_packet_id"], result["return_packet_id"])

    def test_run_once_waits_for_fragmentary_assistant_text(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(root)
            adapter = _FakeAdapter("bridge-control")
            executor = _FakeExecutor(
                RunReport.from_dict(
                    {
                        "timestamp": "2026-04-19T16:00:00+02:00",
                        "thread_id": "thread-2",
                        "summary": "Unused.",
                        "files_touched": [],
                        "checks": [],
                        "blockers": [],
                        "risks": [],
                        "next_step": "",
                    }
                )
            )
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1")

            self.assertEqual(result["policy_outcome"], "allow")
            self.assertEqual(result["runner_action"], "wait_for_chatgpt")
            self.assertEqual(executor.calls, [])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text(encoding="utf-8"))["session"]
            self.assertEqual(session_payload["loop_state"], "waiting_for_chatgpt")

    def test_run_once_does_not_rerun_the_same_assistant_turn(self):
        assistant_text = "Continue with the next safe step."
        assistant_hash = sha1(assistant_text.encode("utf-8")).hexdigest()

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "last_seen_chat_message_anchor": "msg-assistant-1",
                    "latest_assistant_message_hash": assistant_hash,
                },
            )
            adapter = _FakeAdapter(assistant_text, assistant_anchor="msg-assistant-1")
            executor = _FakeExecutor(
                RunReport.from_dict(
                    {
                        "timestamp": "2026-04-19T16:00:00+02:00",
                        "thread_id": "thread-2",
                        "summary": "Unused.",
                        "files_touched": [],
                        "checks": [],
                        "blockers": [],
                        "risks": [],
                        "next_step": "",
                    }
                )
            )
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1")

            self.assertEqual(result["policy_outcome"], "allow")
            self.assertEqual(result["runner_action"], "wait_for_chatgpt")
            self.assertEqual(executor.calls, [])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text(encoding="utf-8"))["session"]
            self.assertEqual(session_payload["loop_state"], "waiting_for_chatgpt")

    def test_run_once_treats_control_stop_during_codex_run_as_stopped_not_blocked(self):
        assistant_text = "Continue with the next safe slice."
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-21T10:59:58+02:00",
                "thread_id": "thread-2",
                "summary": "Codex run was stopped by control request.",
                "exit_code": 130,
                "files_touched": [],
                "checks": [],
                "blockers": ["Codex run was stopped by control request."],
                "risks": ["Partial changes may need review."],
                "next_step": "Inspect the partial artifacts before resuming or starting a fresh run.",
                "interruption_reason": "stop_requested",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(root)
            adapter = _FakeAdapter(assistant_text)
            executor = _FakeExecutor(report)
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1")

            self.assertEqual(result["policy_outcome"], "stopped")
            self.assertEqual(result["runner_action"], "stopped")
            self.assertEqual(adapter.posted_messages, [])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text(encoding="utf-8"))["session"]
            self.assertEqual(session_payload["status"], "completed")
            self.assertEqual(session_payload["loop_state"], "completed")
            self.assertEqual(session_payload["supervisor_status"], "stopped")


class ExecuteSessionPromptTests(unittest.TestCase):
    def test_execute_session_prompt_does_not_call_repo_scope_guard(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            runtime_prompts_dir = root / "runtime_prompts"
            sessions_dir = root / "sessions"
            artifacts_root = root / "artifacts"
            sessions_dir.mkdir(parents=True, exist_ok=True)
            artifacts_root.mkdir(parents=True, exist_ok=True)
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            policy_path.write_text(
                json.dumps({"version": 1, "stop_phrases": ["stop", "pause"]}),
                encoding="utf-8",
            )

            session = OrchestratorSession(
                session_id="session-1",
                binding_id="binding-1",
                repo_path="/tmp/repo",
                workspace_path="/tmp/repo",
                chat_url="https://chatgpt.com/c/project/binding-1",
                time_budget_minutes=90,
                budget_remaining_minutes=90,
            )
            save_session(session_path(sessions_dir, session.session_id), session)
            binding = ChatBinding(
                binding_id="binding-1",
                project_name="bridge",
                repo_path="/tmp/repo",
                workspace_path="/tmp/repo",
                chat_url="https://chatgpt.com/c/project/binding-1",
            )
            report = RunReport.from_dict(
                {
                    "timestamp": "2026-04-19T16:00:00+02:00",
                    "thread_id": "thread-2",
                    "summary": "Completed.",
                    "files_touched": [],
                    "checks": [],
                    "blockers": [],
                    "risks": [],
                    "next_step": "",
                }
            )

            self.assertFalse(hasattr(cli_module, "ensure_prompt_repo_scope"))
            with (
                patch("mastermind_bridge.cli.codex_app_integration_enabled", return_value=False),
                patch("mastermind_bridge.cli.execute_codex_prompt", return_value=(report, {"exit_code": 0})),
            ):
                returned_report = _execute_session_prompt(
                    prompt="Inspect /tmp/test-home/other-repo but keep going in the current binding.",
                    thread_action="new_thread",
                    session=session,
                    binding=binding,
                    instructions=[],
                    runtime_prompts_dir=runtime_prompts_dir,
                    sessions_dir=sessions_dir,
                    policy_path=policy_path,
                    artifacts_root=artifacts_root,
                    log_file=None,
                    registry_path=None,
                    codex_bin="codex",
                    model=None,
                    reasoning_effort=None,
                    sandbox=None,
                    profile=None,
                    env=None,
                    adapter=None,
                )

            prompt_file = runtime_prompts_dir / "session-1" / "NEXT_PROMPT.md"
            self.assertTrue(prompt_file.exists())
            self.assertIn("/tmp/test-home/other-repo", prompt_file.read_text(encoding="utf-8"))
            self.assertEqual(returned_report.summary, "Completed.")

    def test_execute_session_prompt_passes_default_hard_timeout_to_codex_executor(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            runtime_prompts_dir = root / "runtime_prompts"
            sessions_dir = root / "sessions"
            artifacts_root = root / "artifacts"
            sessions_dir.mkdir(parents=True, exist_ok=True)
            artifacts_root.mkdir(parents=True, exist_ok=True)
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            policy_path.write_text(
                json.dumps({"version": 1, "stop_phrases": ["stop", "pause"]}),
                encoding="utf-8",
            )

            session = OrchestratorSession(
                session_id="session-1",
                binding_id="binding-1",
                repo_path="/tmp/repo",
                workspace_path="/tmp/repo",
                chat_url="https://chatgpt.com/c/project/binding-1",
                time_budget_minutes=90,
                budget_remaining_minutes=90,
            )
            save_session(session_path(sessions_dir, session.session_id), session)
            binding = ChatBinding(
                binding_id="binding-1",
                project_name="bridge",
                repo_path="/tmp/repo",
                workspace_path="/tmp/repo",
                chat_url="https://chatgpt.com/c/project/binding-1",
            )
            report = RunReport.from_dict(
                {
                    "timestamp": "2026-04-19T16:00:00+02:00",
                    "thread_id": "thread-2",
                    "summary": "Completed.",
                    "files_touched": [],
                    "checks": [],
                    "blockers": [],
                    "risks": [],
                    "next_step": "",
                }
            )

            with (
                patch.dict(
                    os.environ,
                    {
                        "BRIDGE_ORCHESTRATOR_CODEX_TIMEOUT_SECONDS": "",
                        "BRIDGE_ORCHESTRATOR_CODEX_PROGRESS_STALL_SECONDS": "",
                    },
                    clear=False,
                ),
                patch("mastermind_bridge.cli.codex_app_integration_enabled", return_value=False),
                patch("mastermind_bridge.cli.execute_codex_prompt", return_value=(report, {"exit_code": 0})) as execute_mock,
            ):
                _execute_session_prompt(
                    prompt="Continue safely.",
                    thread_action="new_thread",
                    session=session,
                    binding=binding,
                    instructions=[],
                    runtime_prompts_dir=runtime_prompts_dir,
                    sessions_dir=sessions_dir,
                    policy_path=policy_path,
                    artifacts_root=artifacts_root,
                    log_file=None,
                    registry_path=None,
                    codex_bin="codex",
                    model=None,
                    reasoning_effort=None,
                    sandbox=None,
                    profile=None,
                    env=None,
                    adapter=None,
                )

            self.assertEqual(execute_mock.call_args.kwargs["timeout_seconds"], 1800.0)
            self.assertEqual(execute_mock.call_args.kwargs["progress_stall_seconds"], 300.0)

    def test_execute_session_prompt_allows_timeout_override_via_environment(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            runtime_prompts_dir = root / "runtime_prompts"
            sessions_dir = root / "sessions"
            artifacts_root = root / "artifacts"
            sessions_dir.mkdir(parents=True, exist_ok=True)
            artifacts_root.mkdir(parents=True, exist_ok=True)
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            policy_path.write_text(
                json.dumps({"version": 1, "stop_phrases": ["stop", "pause"]}),
                encoding="utf-8",
            )

            session = OrchestratorSession(
                session_id="session-1",
                binding_id="binding-1",
                repo_path="/tmp/repo",
                workspace_path="/tmp/repo",
                chat_url="https://chatgpt.com/c/project/binding-1",
                time_budget_minutes=90,
                budget_remaining_minutes=90,
            )
            save_session(session_path(sessions_dir, session.session_id), session)
            binding = ChatBinding(
                binding_id="binding-1",
                project_name="bridge",
                repo_path="/tmp/repo",
                workspace_path="/tmp/repo",
                chat_url="https://chatgpt.com/c/project/binding-1",
            )
            report = RunReport.from_dict(
                {
                    "timestamp": "2026-04-19T16:00:00+02:00",
                    "thread_id": "thread-2",
                    "summary": "Completed.",
                    "files_touched": [],
                    "checks": [],
                    "blockers": [],
                    "risks": [],
                    "next_step": "",
                }
            )

            with (
                patch.dict(
                    os.environ,
                    {
                        "BRIDGE_ORCHESTRATOR_CODEX_TIMEOUT_SECONDS": "42.5",
                        "BRIDGE_ORCHESTRATOR_CODEX_PROGRESS_STALL_SECONDS": "55.5",
                    },
                    clear=False,
                ),
                patch("mastermind_bridge.cli.codex_app_integration_enabled", return_value=False),
                patch("mastermind_bridge.cli.execute_codex_prompt", return_value=(report, {"exit_code": 0})) as execute_mock,
            ):
                _execute_session_prompt(
                    prompt="Continue safely.",
                    thread_action="new_thread",
                    session=session,
                    binding=binding,
                    instructions=[],
                    runtime_prompts_dir=runtime_prompts_dir,
                    sessions_dir=sessions_dir,
                    policy_path=policy_path,
                    artifacts_root=artifacts_root,
                    log_file=None,
                    registry_path=None,
                    codex_bin="codex",
                    model=None,
                    reasoning_effort=None,
                    sandbox=None,
                    profile=None,
                    env=None,
                    adapter=None,
                )

            self.assertEqual(execute_mock.call_args.kwargs["timeout_seconds"], 42.5)
            self.assertEqual(execute_mock.call_args.kwargs["progress_stall_seconds"], 55.5)


if __name__ == "__main__":
    unittest.main()
