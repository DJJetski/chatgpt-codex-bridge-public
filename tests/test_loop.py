import json
import tempfile
import time
import unittest
from hashlib import sha1
from pathlib import Path
from unittest.mock import patch

from mastermind_bridge.models import RunReport, derive_return_packet_id
from mastermind_bridge.orchestrator.loop import LoopRunner
from mastermind_bridge.orchestrator.loop_support import _observed_recent_recovery_prompt_count
from mastermind_bridge.orchestrator.state import load_session, save_chat_bindings


class _FakeAdapter:
    def __init__(
        self,
        assistant_text: str,
        *,
        user_text: str = "",
        user_messages: list[str] | None = None,
        post_results: list[dict] | None = None,
        stop_commands: list[object] | None = None,
        visible_message_ids: set[str] | None = None,
        assistant_in_progress: bool = False,
        cancel_result: bool = True,
        assistant_anchor: str = "msg-assistant-1",
        user_anchor: str = "msg-user-1",
        prepare_results: list[dict] | None = None,
        current_chat_url: str = "",
        current_chat_urls: list[str] | None = None,
        assistant_retry_result: bool = False,
    ):
        self.assistant_text = assistant_text
        self.user_text = user_text
        self.user_messages = list(user_messages or [])
        self.post_results = list(post_results or [{"status": "delivered", "message_anchor": "msg-user-2"}])
        self.stop_commands = list(stop_commands or [])
        self.visible_message_ids = set(visible_message_ids or set())
        self.assistant_in_progress = assistant_in_progress
        self.cancel_result = cancel_result
        self.assistant_anchor = assistant_anchor
        self.user_anchor = user_anchor
        self.prepare_results = list(prepare_results or [{"status": "ready"}])
        self.current_url = current_chat_url
        self.current_urls = list(current_chat_urls or [])
        self.assistant_retry_result = assistant_retry_result
        self.open_calls = 0
        self.cancel_calls = 0
        self.assistant_retry_calls = 0
        self.prepare_calls = 0
        self.posted_messages: list[str] = []
        self.visibility_checks: list[str] = []
        self.opened_urls: list[str] = []

    def open_chat(self, binding):
        self.open_calls += 1
        self.binding = binding
        self.current_url = str(binding.chat_url)
        self.opened_urls.append(self.current_url)

    def read_latest_assistant_message(self, session):
        return {
            "message_id": self.assistant_anchor,
            "message_anchor": self.assistant_anchor,
            "text": self.assistant_text,
        }

    def read_latest_user_message(self, session):
        return {
            "message_id": self.user_anchor,
            "message_anchor": self.user_anchor,
            "text": self.user_text,
        }

    def read_recent_user_messages(self, session, limit: int = 8):
        messages = self.user_messages[-max(limit, 1) :]
        if not messages and self.user_text:
            messages = [self.user_text]
        return [
            {
                "message_id": f"msg-user-{index + 1}",
                "message_anchor": f"msg-user-{index + 1}",
                "text": text,
            }
            for index, text in enumerate(messages)
        ]

    def assistant_response_in_progress(self, session):
        return self.assistant_in_progress

    def cancel_assistant_response(self, session):
        self.cancel_calls += 1
        self.assistant_in_progress = False if self.cancel_result else self.assistant_in_progress
        return self.cancel_result

    def retry_latest_assistant_response(self, session):
        self.assistant_retry_calls += 1
        return self.assistant_retry_result

    def prepare_return_packet_delivery(self, session):
        self.prepare_calls += 1
        if self.prepare_results:
            return dict(self.prepare_results.pop(0))
        return {"status": "ready"}

    def post_user_message(self, session, text: str, return_packet_id: str):
        self.posted_messages.append(text)
        if self.post_results:
            payload = self.post_results.pop(0)
        else:
            payload = {"status": "delivered", "message_anchor": "msg-user-fallback"}
        payload = dict(payload)
        if payload.get("current_chat_url"):
            self.current_url = str(payload["current_chat_url"])
        payload.setdefault("return_packet_id", return_packet_id)
        return payload

    def return_packet_visible(self, session, return_packet_id: str) -> bool:
        self.visibility_checks.append(return_packet_id)
        return return_packet_id in self.visible_message_ids

    def current_chat_url(self, session):
        if self.current_urls:
            self.current_url = str(self.current_urls.pop(0))
        return self.current_url

    def poll_stop_command(self, session, stop_phrases: list[str]) -> str | None:
        if not self.stop_commands:
            return None
        return self.stop_commands.pop(0)


class _OpenChatFailingAdapter(_FakeAdapter):
    def open_chat(self, binding):
        raise RuntimeError("Browser transport failed while opening chat.")


class _PostMessageRaisingAdapter(_FakeAdapter):
    def post_user_message(self, session, text: str, return_packet_id: str):
        raise OSError(7, "Argument list too long", "/usr/bin/osascript")


class _BrowserAutomationFailingAdapter(_FakeAdapter):
    def open_chat(self, binding):
        raise RuntimeError(
            "macOS browser automation is not functioning on this host. "
            "The normal-browser Apple Events path failed during live tab inspection, and the Playwright "
            "persistent-profile fallback also failed to launch from this Codex process due to host or sandbox browser transport restrictions."
        )


class _FakeExecutor:
    def __init__(self, report: RunReport):
        self.report = report
        self.calls: list[dict] = []

    def __call__(self, *, prompt: str, thread_action: str, session, binding, instructions: list[str]):
        self.calls.append(
            {
                "prompt": prompt,
                "thread_action": thread_action,
                "session_id": session.session_id,
                "binding_id": binding.binding_id,
                "instructions": list(instructions),
                "resume_session_id": session.current_codex_run_id,
            }
        )
        return self.report


class _SequencedExecutor:
    def __init__(self, reports: list[RunReport]):
        self.reports = list(reports)
        self.calls: list[dict] = []

    def __call__(self, *, prompt: str, thread_action: str, session, binding, instructions: list[str]):
        self.calls.append(
            {
                "prompt": prompt,
                "thread_action": thread_action,
                "session_id": session.session_id,
                "binding_id": binding.binding_id,
                "instructions": list(instructions),
                "resume_session_id": session.current_codex_run_id,
            }
        )
        return self.reports.pop(0)


class _RaisingExecutor:
    def __init__(self, error: Exception):
        self.error = error
        self.calls: list[dict] = []

    def __call__(self, *, prompt: str, thread_action: str, session, binding, instructions: list[str]):
        self.calls.append(
            {
                "prompt": prompt,
                "thread_action": thread_action,
                "session_id": session.session_id,
                "binding_id": binding.binding_id,
                "instructions": list(instructions),
            }
        )
        raise self.error


class _MissingAssistantAdapter(_FakeAdapter):
    def read_latest_assistant_message(self, session):
        raise RuntimeError("ChatGPT DOM contract missing `assistant_message` selector match.")


@unittest.skip("Legacy bridge-control loop coverage; use tests/test_default_loop_simplified.py for the current default path.")
class LoopRunnerTests(unittest.TestCase):
    def _write_state(
        self,
        root: Path,
        *,
        budget_minutes: int = 90,
        current_codex_run_id: str = "exec-123",
        extra_session_fields: dict | None = None,
    ) -> tuple[Path, Path, Path]:
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
                    "autonomy_mode": "balanced_aggressive",
                    "require_explicit_budget": True,
                    "stop_phrases": ["stop", "pause", "stop after this cycle"],
                    "project_instruction_updates": ["Keep project-level policy in view."],
                    "delivery_retry": {
                        "enabled": True,
                        "transport_direction": "codex_to_chatgpt_only",
                        "max_attempts": 2,
                        "known_error_signatures": ["Reasoning failed"],
                    },
                }
            )
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
            "time_budget_minutes": budget_minutes,
            "budget_remaining_minutes": budget_minutes,
            "current_codex_run_id": current_codex_run_id,
            "instruction_updates": [
                {"scope": "session", "mode": "append", "text": "Keep the current session coherent."}
            ],
        }
        if extra_session_fields:
            session_payload.update(extra_session_fields)
        (sessions_dir / "session-1.json").write_text(json.dumps({"version": 1, "session": session_payload}))
        return bindings_path, policy_path, sessions_dir

    def test_run_once_executes_same_thread_and_posts_return_packet(self):
        assistant_text = "\n".join(
            [
                "Continue with the same Codex session.",
                "```bridge-control",
                '{"protocol_version":"1","session_id":"session-1","decision":"run_codex","codex_thread_action":"same_thread","prompt":"Continue the loop runner implementation.","task_label":"loop-runner","instruction_updates":[{"scope":"next_run","mode":"append","text":"Surface visible trace excerpts."}]}',
                "```",
            ]
        )
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-2",
                "summary": "Loop runner executed successfully.",
                "files_touched": ["mastermind_bridge/orchestrator/loop.py"],
                "checks": ["python3 -m unittest discover -s tests"],
                "blockers": [],
                "risks": [],
                "next_step": "Wait for the next ChatGPT turn.",
                "observed_codex_thread_id": "exec-123",
                "visible_assistant_trace": ["Implemented the loop runner."],
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
            self.assertEqual(result["loop_state"], "waiting_for_chatgpt_response")
            self.assertEqual(executor.calls[0]["thread_action"], "same_thread")
            self.assertEqual(executor.calls[0]["resume_session_id"], "exec-123")
            self.assertIn("Keep project-level policy in view.", executor.calls[0]["instructions"])
            self.assertIn("Surface visible trace excerpts.", executor.calls[0]["instructions"])
            self.assertEqual(len(adapter.posted_messages), 1)
            self.assertIn("return_packet_id:", adapter.posted_messages[0])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["loop_state"], "waiting_for_chatgpt_response")
            self.assertEqual(session_payload["last_posted_return_packet_id"], result["return_packet_id"])

    def test_run_once_falls_back_to_new_thread_when_same_thread_has_no_resumable_codex_thread(self):
        assistant_text = "\n".join(
            [
                "Continue with the same Codex session.",
                "```bridge-control",
                '{"protocol_version":"1","session_id":"session-1","decision":"run_codex","codex_thread_action":"same_thread","prompt":"Continue the loop runner implementation.","task_label":"loop-runner"}',
                "```",
            ]
        )
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
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
            bindings_path, policy_path, sessions_dir = self._write_state(root, current_codex_run_id="")
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
            self.assertEqual(executor.calls[0]["thread_action"], "new_thread")
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["human_attention_reason"], "")
            self.assertTrue(session_payload["auto_run_enabled"])
            self.assertEqual(session_payload["last_thread_action"], "new_thread")
            self.assertEqual(session_payload["degraded_mode"], "missing_codex_thread_fallback")
            self.assertIn("same_thread", session_payload["degraded_reason"])

    def test_run_once_persists_enriched_run_report_after_delivery(self):
        assistant_text = "\n".join(
            [
                "Continue with the same Codex session.",
                "```bridge-control",
                '{"protocol_version":"1","session_id":"session-1","decision":"run_codex","codex_thread_action":"same_thread","prompt":"Continue the loop runner implementation.","task_label":"loop-runner"}',
                "```",
            ]
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(root)
            run_dir = root / "artifacts" / "runs" / "20260415T140000-session-1"
            run_dir.mkdir(parents=True)
            report = RunReport.from_dict(
                {
                    "timestamp": "2026-04-15T14:00:00+02:00",
                    "thread_id": "thread-2",
                    "summary": "Loop runner executed successfully.",
                    "files_touched": ["mastermind_bridge/orchestrator/loop.py"],
                    "checks": ["python3 -m unittest discover -s tests"],
                    "blockers": [],
                    "risks": [],
                    "next_step": "Wait for the next ChatGPT turn.",
                    "observed_codex_thread_id": "exec-123",
                    "artifacts_dir": str(run_dir),
                }
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

            report_payload = json.loads((run_dir / "run_report.json").read_text())
            self.assertEqual(report_payload["delivery_status"], "delivered")
            self.assertEqual(report_payload["delivery_attempt_count"], 1)
            self.assertEqual(
                report_payload["delivery_attempts"],
                [
                    {
                        "attempt_number": 1,
                        "status": "delivered",
                        "transport": "chatgpt_browser",
                        "return_packet_id": result["return_packet_id"],
                        "error_signature": "",
                    }
                ],
            )
            self.assertEqual(report_payload["return_packet_id"], result["return_packet_id"])
            self.assertEqual(report_payload["session_id"], "session-1")

    def test_run_once_keeps_next_run_instructions_when_delivery_retry_is_pending(self):
        assistant_text = "\n".join(
            [
                "Continue with the same Codex session.",
                "```bridge-control",
                '{"protocol_version":"1","session_id":"session-1","decision":"run_codex","codex_thread_action":"same_thread","prompt":"Continue the loop runner implementation.","task_label":"loop-runner","instruction_updates":[{"scope":"next_run","mode":"append","text":"Preserve this steering until delivery succeeds."}]}',
                "```",
            ]
        )
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-2",
                "summary": "Loop runner executed successfully.",
                "files_touched": ["mastermind_bridge/orchestrator/loop.py"],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "Wait for the next ChatGPT turn.",
                "observed_codex_thread_id": "exec-123",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(root)
            adapter = _FakeAdapter(
                assistant_text,
                post_results=[
                    {
                        "status": "failed",
                        "error_signature": "Browser transport still blocked.",
                    }
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

            self.assertEqual(result["policy_outcome"], "allow")
            self.assertEqual(result["runner_action"], "wait_for_chatgpt")
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["loop_state"], "posting_return_packet")
            self.assertTrue(session_payload["auto_run_enabled"])
            self.assertEqual(session_payload["supervisor_status"], "running")
            self.assertEqual(session_payload["last_outbound_user_message_kind"], "return_packet_retry_pending")
            updates = [
                {key: value for key, value in item.items() if key != "created_at"}
                for item in session_payload["instruction_updates"]
            ]
            self.assertEqual(
                updates,
                [
                    {"scope": "session", "mode": "append", "text": "Keep the current session coherent."},
                    {
                        "scope": "next_run",
                        "mode": "append",
                        "text": "Preserve this steering until delivery succeeds.",
                    },
                ],
            )

    def test_run_once_keeps_session_active_when_post_user_message_raises(self):
        assistant_text = "\n".join(
            [
                "Continue with the same Codex session.",
                "```bridge-control",
                '{"protocol_version":"1","session_id":"session-1","decision":"run_codex","codex_thread_action":"same_thread","prompt":"Continue the loop runner implementation.","task_label":"loop-runner"}',
                "```",
            ]
        )
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-2",
                "summary": "Loop runner executed successfully.",
                "files_touched": ["mastermind_bridge/orchestrator/loop.py"],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "Wait for the next ChatGPT turn.",
                "observed_codex_thread_id": "exec-123",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(root)
            adapter = _PostMessageRaisingAdapter(assistant_text)
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
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["loop_state"], "posting_return_packet")
            self.assertTrue(session_payload["auto_run_enabled"])
            self.assertEqual(session_payload["supervisor_status"], "running")
            self.assertEqual(session_payload["last_outbound_user_message_kind"], "return_packet_retry_pending")
            self.assertIn("Argument list too long", session_payload["last_error"])

    def test_run_once_pauses_before_return_packet_when_send_once_is_requested(self):
        assistant_text = "\n".join(
            [
                "Continue with the same Codex session.",
                "```bridge-control",
                '{"protocol_version":"1","session_id":"session-1","decision":"run_codex","codex_thread_action":"same_thread","prompt":"Continue the loop runner implementation.","task_label":"loop-runner"}',
                "```",
            ]
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={"stop_before_return_packet_requested": True},
            )
            run_dir = root / "artifacts" / "runs" / "20260415T140000-session-1"
            run_dir.mkdir(parents=True)
            report = RunReport.from_dict(
                {
                    "timestamp": "2026-04-15T14:00:00+02:00",
                    "thread_id": "thread-2",
                    "summary": "Loop runner executed successfully.",
                    "files_touched": ["mastermind_bridge/orchestrator/loop.py"],
                    "checks": [],
                    "blockers": [],
                    "risks": [],
                    "next_step": "Inspect the first Codex handoff before posting back.",
                    "observed_codex_thread_id": "exec-123",
                    "artifacts_dir": str(run_dir),
                }
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

            self.assertEqual(result["policy_outcome"], "paused")
            self.assertEqual(result["loop_state"], "codex_completed_waiting_to_post")
            self.assertEqual(result["runner_action"], "paused_before_return_packet")
            self.assertEqual(adapter.posted_messages, [])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["status"], "paused")
            self.assertEqual(session_payload["supervisor_status"], "paused")
            self.assertFalse(session_payload["auto_run_enabled"])
            self.assertEqual(session_payload["last_outbound_user_message_kind"], "return_packet_ready")
            self.assertEqual(session_payload["last_outbound_user_message_anchor"], result["return_packet_id"])
            self.assertFalse(session_payload["stop_before_return_packet_requested"])
            report_payload = json.loads((run_dir / "run_report.json").read_text())
            self.assertEqual(report_payload["delivery_status"], "ready_to_post")
            self.assertEqual(report_payload["return_packet_id"], result["return_packet_id"])

    def test_run_once_retries_pending_return_packet_delivery_before_rerunning_codex(self):
        assistant_text = "\n".join(
            [
                "Continue with the same Codex session.",
                "```bridge-control",
                '{"protocol_version":"1","session_id":"session-1","decision":"run_codex","codex_thread_action":"same_thread","prompt":"Continue the loop runner implementation.","task_label":"loop-runner"}',
                "```",
            ]
        )
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-2",
                "summary": "Loop runner executed successfully.",
                "files_touched": ["mastermind_bridge/orchestrator/loop.py"],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "Wait for the next ChatGPT turn.",
                "observed_codex_thread_id": "exec-123",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "last_outbound_user_message_anchor": "packet-retry-1",
                    "last_outbound_user_message_kind": "return_packet_retry_pending",
                    "last_outbound_user_message_sent_at": time.time() - 60.0,
                    "last_seen_chat_message_anchor": "msg-assistant-1",
                    "latest_assistant_message_hash": sha1(assistant_text.encode("utf-8")).hexdigest(),
                },
            )
            run_dir = root / "artifacts" / "runs" / "20260415T140000-session-1"
            run_dir.mkdir(parents=True)
            report.return_packet_id = "packet-retry-1"
            report.delivery_status = "human_attention_required"
            report.delivery_attempts = [
                {
                    "attempt_number": 1,
                    "status": "failed",
                    "transport": "chatgpt_browser",
                    "return_packet_id": "packet-retry-1",
                    "error_signature": "Browser transport still blocked.",
                }
            ]
            report.delivery_attempt_count = 1
            (run_dir / "run_report.json").write_text(json.dumps(report.as_dict()))
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
            self.assertEqual(result["return_packet_id"], "packet-retry-1")
            self.assertEqual(executor.calls, [])
            self.assertEqual(len(adapter.posted_messages), 1)
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["loop_state"], "waiting_for_chatgpt_response")
            self.assertEqual(session_payload["last_outbound_user_message_kind"], "return_packet")
            self.assertEqual(session_payload["last_posted_return_packet_id"], "packet-retry-1")
            self.assertEqual(session_payload["cycles_completed"], 1)

    def test_run_once_posts_ready_return_packet_without_rerunning_codex(self):
        assistant_text = "\n".join(
            [
                "Continue with the same Codex session.",
                "```bridge-control",
                '{"protocol_version":"1","session_id":"session-1","decision":"run_codex","codex_thread_action":"same_thread","prompt":"Continue the loop runner implementation.","task_label":"loop-runner"}',
                "```",
            ]
        )
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-2",
                "summary": "Loop runner executed successfully.",
                "files_touched": ["mastermind_bridge/orchestrator/loop.py"],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "Wait for the next ChatGPT turn.",
                "observed_codex_thread_id": "exec-123",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "last_outbound_user_message_anchor": "packet-ready-1",
                    "last_outbound_user_message_kind": "return_packet_ready",
                },
            )
            run_dir = root / "artifacts" / "runs" / "20260415T140000-session-1"
            run_dir.mkdir(parents=True)
            report.return_packet_id = "packet-ready-1"
            report.delivery_status = "ready_to_post"
            (run_dir / "run_report.json").write_text(json.dumps(report.as_dict()))
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
            self.assertEqual(result["return_packet_id"], "packet-ready-1")
            self.assertEqual(executor.calls, [])
            self.assertEqual(len(adapter.posted_messages), 1)
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["loop_state"], "waiting_for_chatgpt_response")
            self.assertEqual(session_payload["last_outbound_user_message_kind"], "return_packet")
            self.assertEqual(session_payload["last_posted_return_packet_id"], "packet-ready-1")
            self.assertEqual(session_payload["cycles_completed"], 1)

    def test_run_once_blocks_ready_return_packet_when_latest_assistant_turn_changed(self):
        assistant_text = "\n".join(
            [
                "Continue with the same Codex session.",
                "```bridge-control",
                '{"protocol_version":"1","session_id":"session-1","decision":"run_codex","codex_thread_action":"same_thread","prompt":"Continue the loop runner implementation.","task_label":"loop-runner"}',
                "```",
            ]
        )
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-2",
                "summary": "Loop runner executed successfully.",
                "files_touched": ["mastermind_bridge/orchestrator/loop.py"],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "Wait for the next ChatGPT turn.",
                "observed_codex_thread_id": "exec-123",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "last_outbound_user_message_anchor": "packet-ready-1",
                    "last_outbound_user_message_kind": "return_packet_ready",
                    "last_seen_chat_message_anchor": "msg-assistant-1",
                    "latest_assistant_message_hash": sha1(assistant_text.encode("utf-8")).hexdigest(),
                },
            )
            run_dir = root / "artifacts" / "runs" / "20260415T140000-session-1"
            run_dir.mkdir(parents=True)
            report.return_packet_id = "packet-ready-1"
            report.delivery_status = "ready_to_post"
            (run_dir / "run_report.json").write_text(json.dumps(report.as_dict()))
            adapter = _FakeAdapter(assistant_text, assistant_anchor="msg-assistant-2")
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
            self.assertEqual(adapter.prepare_calls, 0)
            self.assertEqual(adapter.posted_messages, [])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["status"], "blocked")
            self.assertEqual(session_payload["loop_state"], "requires_human")
            self.assertIn("different assistant turn", session_payload["human_attention_reason"])
            self.assertEqual(session_payload["last_posted_return_packet_id"], "")

    def test_run_once_blocks_ready_return_packet_when_latest_assistant_text_changed_under_same_anchor(self):
        assistant_text = "\n".join(
            [
                "Continue with the same Codex session.",
                "```bridge-control",
                '{"protocol_version":"1","session_id":"session-1","decision":"run_codex","codex_thread_action":"same_thread","prompt":"Continue the loop runner implementation.","task_label":"loop-runner"}',
                "```",
            ]
        )
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-2",
                "summary": "Loop runner executed successfully.",
                "files_touched": ["mastermind_bridge/orchestrator/loop.py"],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "Wait for the next ChatGPT turn.",
                "observed_codex_thread_id": "exec-123",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "last_outbound_user_message_anchor": "packet-ready-1",
                    "last_outbound_user_message_kind": "return_packet_ready",
                    "last_seen_chat_message_anchor": "msg-assistant-1",
                    "latest_assistant_message_hash": sha1(assistant_text.encode("utf-8")).hexdigest(),
                },
            )
            run_dir = root / "artifacts" / "runs" / "20260415T140000-session-1"
            run_dir.mkdir(parents=True)
            report.return_packet_id = "packet-ready-1"
            report.delivery_status = "ready_to_post"
            (run_dir / "run_report.json").write_text(json.dumps(report.as_dict()))
            adapter = _FakeAdapter("A different prompt on the same anchor.")
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
            self.assertEqual(adapter.prepare_calls, 0)
            self.assertEqual(adapter.posted_messages, [])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["status"], "blocked")
            self.assertEqual(session_payload["loop_state"], "requires_human")
            self.assertIn("different assistant turn", session_payload["human_attention_reason"])

    def test_run_once_blocks_ready_return_packet_when_active_chat_url_shifted(self):
        assistant_text = "\n".join(
            [
                "Continue with the same Codex session.",
                "```bridge-control",
                '{"protocol_version":"1","session_id":"session-1","decision":"run_codex","codex_thread_action":"same_thread","prompt":"Continue the loop runner implementation.","task_label":"loop-runner"}',
                "```",
            ]
        )
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-2",
                "summary": "Loop runner executed successfully.",
                "files_touched": ["mastermind_bridge/orchestrator/loop.py"],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "Wait for the next ChatGPT turn.",
                "observed_codex_thread_id": "exec-123",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "chat_url": "https://chatgpt.com/c/project/binding-1",
                    "last_outbound_user_message_anchor": "packet-ready-1",
                    "last_outbound_user_message_kind": "return_packet_ready",
                    "last_seen_chat_message_anchor": "msg-assistant-1",
                    "latest_assistant_message_hash": sha1(assistant_text.encode("utf-8")).hexdigest(),
                },
            )
            run_dir = root / "artifacts" / "runs" / "20260415T140000-session-1"
            run_dir.mkdir(parents=True)
            report.return_packet_id = "packet-ready-1"
            report.delivery_status = "ready_to_post"
            (run_dir / "run_report.json").write_text(json.dumps(report.as_dict()))
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

            self.assertEqual(result["policy_outcome"], "require_human")
            self.assertEqual(result["runner_action"], "blocked")
            self.assertEqual(adapter.prepare_calls, 0)
            self.assertEqual(adapter.posted_messages, [])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["status"], "blocked")
            self.assertEqual(session_payload["loop_state"], "requires_human")
            self.assertIn("different chat url", session_payload["human_attention_reason"].casefold())
            self.assertEqual(session_payload["last_posted_return_packet_id"], "")

    def test_run_once_blocks_retry_pending_return_packet_when_composer_not_empty_after_clear(self):
        assistant_text = "\n".join(
            [
                "Continue with the same Codex session.",
                "```bridge-control",
                '{"protocol_version":"1","session_id":"session-1","decision":"run_codex","codex_thread_action":"same_thread","prompt":"Continue the loop runner implementation.","task_label":"loop-runner"}',
                "```",
            ]
        )
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-2",
                "summary": "Loop runner executed successfully.",
                "files_touched": ["mastermind_bridge/orchestrator/loop.py"],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "Wait for the next ChatGPT turn.",
                "observed_codex_thread_id": "exec-123",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "last_outbound_user_message_anchor": "packet-retry-1",
                    "last_outbound_user_message_kind": "return_packet_retry_pending",
                    "last_outbound_user_message_sent_at": time.time() - 60.0,
                    "last_seen_chat_message_anchor": "msg-assistant-1",
                    "latest_assistant_message_hash": sha1(assistant_text.encode("utf-8")).hexdigest(),
                },
            )
            run_dir = root / "artifacts" / "runs" / "20260415T140000-session-1"
            run_dir.mkdir(parents=True)
            report.return_packet_id = "packet-retry-1"
            report.delivery_status = "human_attention_required"
            report.delivery_attempts = [
                {
                    "attempt_number": 1,
                    "status": "failed",
                    "transport": "chatgpt_browser",
                    "return_packet_id": "packet-retry-1",
                    "error_signature": "ChatGPT composer still contains draft text after clear verification.",
                }
            ]
            report.delivery_attempt_count = 1
            (run_dir / "run_report.json").write_text(json.dumps(report.as_dict()))
            adapter = _FakeAdapter(
                assistant_text,
                prepare_results=[
                    {
                        "status": "failed",
                        "error_signature": "ChatGPT composer still contains draft text after clear verification.",
                    }
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
            self.assertEqual(adapter.prepare_calls, 1)
            self.assertEqual(adapter.posted_messages, [])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["status"], "blocked")
            self.assertEqual(session_payload["loop_state"], "requires_human")
            self.assertIn("composer still contains draft text", session_payload["human_attention_reason"])
            self.assertEqual(session_payload["last_posted_return_packet_id"], "")

    def test_run_once_can_stop_after_delivering_ready_return_packet(self):
        assistant_text = "\n".join(
            [
                "Continue with the same Codex session.",
                "```bridge-control",
                '{"protocol_version":"1","session_id":"session-1","decision":"run_codex","codex_thread_action":"same_thread","prompt":"Continue the loop runner implementation.","task_label":"loop-runner"}',
                "```",
            ]
        )
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-2",
                "summary": "Loop runner executed successfully.",
                "files_touched": ["mastermind_bridge/orchestrator/loop.py"],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "Wait for the next ChatGPT turn.",
                "observed_codex_thread_id": "exec-123",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "last_outbound_user_message_anchor": "packet-ready-1",
                    "last_outbound_user_message_kind": "return_packet_ready",
                    "stop_after_cycle_requested": True,
                },
            )
            run_dir = root / "artifacts" / "runs" / "20260415T140000-session-1"
            run_dir.mkdir(parents=True)
            report.return_packet_id = "packet-ready-1"
            report.delivery_status = "ready_to_post"
            (run_dir / "run_report.json").write_text(json.dumps(report.as_dict()))
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
            self.assertEqual(executor.calls, [])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["status"], "completed")
            self.assertEqual(session_payload["loop_state"], "completed")
            self.assertFalse(session_payload["stop_after_cycle_requested"])

    def test_run_once_cools_down_pending_return_packet_retry_after_recent_browser_failure(self):
        assistant_text = "\n".join(
            [
                "Continue with the same Codex session.",
                "```bridge-control",
                '{"protocol_version":"1","session_id":"session-1","decision":"run_codex","codex_thread_action":"same_thread","prompt":"Continue the loop runner implementation.","task_label":"loop-runner"}',
                "```",
            ]
        )
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-2",
                "summary": "Loop runner executed successfully.",
                "files_touched": ["mastermind_bridge/orchestrator/loop.py"],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "Wait for the next ChatGPT turn.",
                "observed_codex_thread_id": "exec-123",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "last_outbound_user_message_anchor": "packet-retry-1",
                    "last_outbound_user_message_kind": "return_packet_retry_pending",
                    "last_outbound_user_message_sent_at": 1000.0,
                    "last_error": "198:199: syntax error: Zeilenende, etc. erwartet, aber „\\\"“ gefunden. (-2741)",
                    "degraded_reason": "198:199: syntax error: Zeilenende, etc. erwartet, aber „\\\"“ gefunden. (-2741)",
                },
            )
            run_dir = root / "artifacts" / "runs" / "20260415T140000-session-1"
            run_dir.mkdir(parents=True)
            report.return_packet_id = "packet-retry-1"
            report.delivery_status = "human_attention_required"
            report.delivery_attempts = [
                {
                    "attempt_number": 1,
                    "status": "failed",
                    "transport": "chatgpt_browser",
                    "return_packet_id": "packet-retry-1",
                    "error_signature": "198:199: syntax error: Zeilenende, etc. erwartet, aber „\\\"“ gefunden. (-2741)",
                }
            ]
            report.delivery_attempt_count = 1
            (run_dir / "run_report.json").write_text(json.dumps(report.as_dict()))
            adapter = _FakeAdapter(assistant_text)
            executor = _FakeExecutor(report)
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            with patch("mastermind_bridge.orchestrator.loop.time.time", return_value=1010.0):
                result = runner.run_once("session-1")

            self.assertEqual(result["policy_outcome"], "allow")
            self.assertEqual(result["runner_action"], "wait_for_chatgpt")
            self.assertEqual(result["return_packet_id"], "packet-retry-1")
            self.assertEqual(executor.calls, [])
            self.assertEqual(adapter.posted_messages, [])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["loop_state"], "posting_return_packet")
            self.assertEqual(session_payload["last_outbound_user_message_kind"], "return_packet_retry_pending")
            self.assertEqual(session_payload["last_posted_return_packet_id"], "")
            self.assertIn("cooling down", session_payload["policy_decision"]["reasons"][0])

    def test_run_once_rewinds_to_last_productive_prompt_after_no_task_report(self):
        assistant_text = "\n".join(
            [
                "Schick Codex jetzt genau das:",
                "",
                "```text",
                "Dein Audit reicht. Jetzt wechselst du von Inventur in Ausfuehrung.",
                "Arbeite die drei priorisierten Roots ab.",
                "```",
            ]
        )
        no_task_report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-empty",
                "summary": "No task was provided under `Task from ChatGPT:`.",
                "final_agent_message": "No task was provided under `Task from ChatGPT:`.",
                "files_touched": [],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "Paste the raw output back into ChatGPT.",
                "observed_codex_thread_id": "exec-empty",
            }
        )
        recovered_report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:05:00+02:00",
                "thread_id": "thread-recovered",
                "summary": "Recovered tracked roots execution.",
                "final_agent_message": "Recovered tracked roots execution.",
                "files_touched": ["Sources/PABFilesFeature/FilesFeature.swift"],
                "checks": ["swift test --filter FilesFeatureTests"],
                "blockers": [],
                "risks": [],
                "next_step": "Wait for the next ChatGPT turn.",
                "observed_codex_thread_id": "exec-recovered",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(root)
            adapter = _FakeAdapter(assistant_text)
            executor = _SequencedExecutor([no_task_report, recovered_report])
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
            self.assertEqual(len(executor.calls), 2)
            self.assertEqual(executor.calls[0]["prompt"], executor.calls[1]["prompt"])
            self.assertIn("Dein Audit reicht.", executor.calls[1]["prompt"])
            self.assertEqual(len(adapter.posted_messages), 1)
            self.assertIn("Recovered tracked roots execution.", adapter.posted_messages[0])
            self.assertNotIn("No task was provided", adapter.posted_messages[0])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertTrue(session_payload["last_productive_task_label"].startswith("dein_audit_reicht_jetzt_wechselst_du_von_inve"))
            self.assertEqual(session_payload["productive_rewind_attempts"], 0)
            self.assertEqual(session_payload["degraded_mode"], "last_productive_prompt_rewind")

    def test_run_once_never_delivers_no_task_reply_to_chatgpt(self):
        assistant_text = "\n".join(
            [
                "Schick Codex jetzt genau das:",
                "",
                "```text",
                "Dein Audit reicht. Jetzt wechselst du von Inventur in Ausfuehrung.",
                "Arbeite die drei priorisierten Roots ab.",
                "```",
            ]
        )
        no_task_report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-empty",
                "summary": "No task was provided under `Task from ChatGPT:`.",
                "final_agent_message": "No task was provided under `Task from ChatGPT:`.",
                "files_touched": [],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "Paste the raw output back into ChatGPT.",
                "observed_codex_thread_id": "exec-empty",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(root)
            adapter = _FakeAdapter(assistant_text)
            executor = _SequencedExecutor([no_task_report, no_task_report])
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
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["loop_state"], "waiting_for_chatgpt")
            self.assertEqual(session_payload["degraded_mode"], "missing_task_delivery_suppressed")
            self.assertEqual(session_payload["productive_rewind_attempts"], 1)

    def test_deliver_packet_does_not_retry_when_packet_is_already_visible(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(root)
            adapter = _FakeAdapter(
                "bridge",
                post_results=[
                    {
                        "status": "failed",
                        "error_signature": "Reasoning failed",
                    }
                ],
                visible_message_ids={"packet-visible-1"},
                current_chat_url="https://chatgpt.com/c/project/binding-1",
            )
            executor = _FakeExecutor(
                RunReport.from_dict(
                    {
                        "timestamp": "2026-04-15T14:00:00+02:00",
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
                "packet-visible-1",
                "return packet text",
            )

            self.assertEqual(delivery["status"], "delivered")
            self.assertEqual(delivery["attempt_count"], 0)
            self.assertEqual(adapter.prepare_calls, 0)
            self.assertEqual(len(adapter.posted_messages), 0)
            self.assertEqual(adapter.visibility_checks, ["packet-visible-1"])

    def test_deliver_packet_blocks_visible_packet_when_active_chat_url_shifted(self):
        assistant_text = "bridge"

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "chat_url": "https://chatgpt.com/c/project/binding-1",
                    "last_seen_chat_message_anchor": "msg-assistant-1",
                    "latest_assistant_message_hash": sha1(assistant_text.encode("utf-8")).hexdigest(),
                },
            )
            adapter = _FakeAdapter(
                assistant_text,
                visible_message_ids={"packet-visible-1"},
                current_chat_url="https://chatgpt.com/c/project/different-chat",
            )
            executor = _FakeExecutor(
                RunReport.from_dict(
                    {
                        "timestamp": "2026-04-15T14:00:00+02:00",
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
                "packet-visible-1",
                "return packet text",
            )

            self.assertEqual(delivery["status"], "preflight_failed")
            self.assertEqual(delivery["attempt_count"], 1)
            self.assertEqual(adapter.prepare_calls, 0)
            self.assertEqual(len(adapter.posted_messages), 0)
            self.assertEqual(adapter.visibility_checks, [])
            self.assertIn("different chat url", delivery["attempts"][0]["error_signature"].casefold())

    def test_deliver_packet_accepts_late_visible_packet_after_retryable_timeouts(self):
        class _LateVisibleAdapter(_FakeAdapter):
            def __init__(self):
                super().__init__(
                    "bridge",
                    post_results=[
                        {"status": "failed", "error_signature": "Message delivery confirmation timed out."},
                        {"status": "failed", "error_signature": "Message delivery confirmation timed out."},
                    ],
                    current_chat_url="https://chatgpt.com/c/project/binding-1",
                )
                self._visibility_probe_count = 0

            def return_packet_visible(self, session, return_packet_id: str) -> bool:
                self.visibility_checks.append(return_packet_id)
                self._visibility_probe_count += 1
                return self._visibility_probe_count >= 3

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(root)
            adapter = _LateVisibleAdapter()
            executor = _FakeExecutor(
                RunReport.from_dict(
                    {
                        "timestamp": "2026-04-15T14:00:00+02:00",
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

            with patch("mastermind_bridge.orchestrator.loop.time.sleep", return_value=None):
                delivery = runner._deliver_packet(
                    load_session(sessions_dir / "session-1.json"),
                    {
                        "delivery_retry": {
                            "max_attempts": 2,
                            "known_error_signatures": ["Message delivery confirmation timed out."],
                        }
                    },
                    "packet-visible-late",
                    "return packet text",
                )

            self.assertEqual(delivery["status"], "delivered")
            self.assertEqual(delivery["attempt_count"], 1)
            self.assertEqual(len(adapter.posted_messages), 1)
            self.assertEqual(
                adapter.visibility_checks,
                ["packet-visible-late", "packet-visible-late", "packet-visible-late"],
            )

    def test_deliver_packet_does_not_immediately_retry_browser_transport_syntax_errors(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(root)
            adapter = _FakeAdapter(
                "bridge",
                post_results=[
                    {
                        "status": "failed",
                        "error_signature": '198:199: syntax error: Zeilenende, etc. erwartet, aber „"“ gefunden. (-2741)',
                    },
                    {
                        "status": "failed",
                        "error_signature": "This second attempt should never run.",
                    },
                ],
                current_chat_url="https://chatgpt.com/c/project/binding-1",
            )
            executor = _FakeExecutor(
                RunReport.from_dict(
                    {
                        "timestamp": "2026-04-15T14:00:00+02:00",
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
                "packet-syntax-1",
                "return packet text",
            )

            self.assertEqual(delivery["status"], "human_attention_required")
            self.assertEqual(delivery["attempt_count"], 1)
            self.assertEqual(len(adapter.posted_messages), 1)
            self.assertEqual(adapter.visibility_checks, ["packet-syntax-1", "packet-syntax-1"])

    def test_run_once_keeps_session_active_when_open_chat_fails(self):
        assistant_text = "bridge-control"
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
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
            bindings_path, policy_path, sessions_dir = self._write_state(root)
            adapter = _OpenChatFailingAdapter(assistant_text)
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
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["loop_state"], "waiting_for_chatgpt")
            self.assertEqual(session_payload["human_attention_reason"], "")
            self.assertTrue(session_payload["auto_run_enabled"])
            self.assertEqual(session_payload["supervisor_status"], "running")
            self.assertEqual(session_payload["degraded_mode"], "browser_transport_retry_pending")
            self.assertIn("Browser transport failed while opening chat.", session_payload["last_error"])

    def test_run_once_enriches_browser_transport_blocker_with_host_probe_context(self):
        assistant_text = "bridge-control"
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
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
            bindings_path, policy_path, sessions_dir = self._write_state(root)
            adapter = _BrowserAutomationFailingAdapter(assistant_text)
            executor = _FakeExecutor(report)
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            with patch(
                "mastermind_bridge.orchestrator.loop.enrich_browser_blocker_reason",
                return_value="macOS browser automation is not functioning on this host. Host probes: system_events=-10827; screencapture=display_capture_failed.",
            ) as enrich_mock:
                result = runner.run_once("session-1")

            self.assertEqual(result["policy_outcome"], "allow")
            enrich_mock.assert_called_once()
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["human_attention_reason"], "")
            self.assertEqual(session_payload["degraded_mode"], "browser_transport_retry_pending")
            self.assertIn("screencapture=display_capture_failed", session_payload["last_error"])

    def test_run_once_exhausts_elapsed_time_budget_before_executor_starts(self):
        assistant_text = "\n".join(
            [
                "Continue with the same Codex session.",
                "```bridge-control",
                '{"protocol_version":"1","session_id":"session-1","decision":"run_codex","codex_thread_action":"same_thread","prompt":"Continue.","task_label":"loop-runner"}',
                "```",
            ]
        )
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
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
                budget_minutes=1,
                extra_session_fields={
                    "auto_run_enabled": True,
                    "budget_consumed_seconds": 59.0,
                    "budget_clock_started_at": 1.0,
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

            with patch("mastermind_bridge.orchestrator.models.time.time", return_value=3.0), patch(
                "mastermind_bridge.orchestrator.state.time.time", return_value=3.0
            ):
                result = runner.run_once("session-1")

            self.assertEqual(result["policy_outcome"], "budget_exhausted")
            self.assertEqual(result["runner_action"], "budget_exhausted")
            self.assertEqual(executor.calls, [])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["budget_remaining_minutes"], 0)

    def test_run_once_requires_human_when_human_gate_is_requested(self):
        assistant_text = "\n".join(
            [
                "Human approval is required.",
                "```bridge-control",
                '{"protocol_version":"1","session_id":"session-1","decision":"run_codex","codex_thread_action":"new_thread","prompt":"Do not run yet.","task_label":"human-gate","human_gate":{"required":true,"reason":"Need approval for paid spend.","category":"paid_spend"}}',
                "```",
            ]
        )
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-2",
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

            self.assertEqual(result["policy_outcome"], "require_human")
            self.assertEqual(result["loop_state"], "requires_human")
            self.assertEqual(executor.calls, [])
            self.assertEqual(adapter.posted_messages, [])

    def test_run_once_waits_when_fresh_chat_has_no_assistant_message_yet(self):
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
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
                    "last_outbound_user_message_anchor": "packet-123",
                    "last_outbound_user_message_kind": "return_packet",
                },
            )
            adapter = _MissingAssistantAdapter("")
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
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["loop_state"], "waiting_for_chatgpt_response")
            self.assertTrue(session_payload["auto_run_enabled"])
            self.assertEqual(session_payload["supervisor_status"], "running")
            self.assertEqual(session_payload["human_attention_reason"], "")
            self.assertEqual(session_payload["last_error"], "")
            self.assertEqual(executor.calls, [])

    def test_run_once_retries_same_chat_when_no_assistant_response_is_visible_after_timeout(self):
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-2",
                "summary": "Recovered run completed.",
                "final_agent_message": "Here is the latest Codex result.",
                "files_touched": ["mastermind_bridge/orchestrator/loop.py"],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "Continue in a fresh ChatGPT conversation.",
                "observed_codex_thread_id": "exec-999",
                "thread_action": "same_thread",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "last_outbound_user_message_anchor": "packet-123",
                    "last_outbound_user_message_kind": "return_packet",
                    "last_outbound_user_message_sent_at": time.time() - 120.0,
                    "chat_url": "https://chatgpt.com/c/current-fresh-chat",
                    "auto_run_enabled": True,
                    "supervisor_status": "running",
                },
            )
            save_chat_bindings(
                bindings_path,
                [
                    {
                        "binding_id": "binding-1",
                        "project_name": "bridge",
                        "repo_path": "/tmp/repo",
                        "workspace_path": "/tmp/repo",
                        "chat_url": "https://chatgpt.com/c/current-fresh-chat",
                        "browser_profile_path": "/tmp/profile",
                        "browser_session_handle": "default",
                    }
                ],
            )
            run_dir = root / "artifacts" / "runs" / "20260415T140000-session-1"
            run_dir.mkdir(parents=True)
            (run_dir / "run_report.json").write_text(json.dumps(report.as_dict()))
            adapter = _MissingAssistantAdapter(
                "",
                current_chat_url="https://chatgpt.com/c/current-fresh-chat",
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
            self.assertEqual(adapter.posted_messages, [])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["chat_url"], "https://chatgpt.com/c/current-fresh-chat")
            self.assertEqual(session_payload["loop_state"], "waiting_for_chatgpt_response")
            self.assertEqual(session_payload["last_outbound_user_message_kind"], "return_packet")
            self.assertEqual(session_payload["degraded_mode"], "chatgpt_same_turn_retry")
            self.assertEqual(session_payload["supervisor_status"], "running")
            self.assertTrue(session_payload["auto_run_enabled"])
            bindings_payload = json.loads(bindings_path.read_text())
            self.assertEqual(bindings_payload["bindings"][0]["chat_url"], "https://chatgpt.com/c/current-fresh-chat")

    def test_run_once_retries_same_chat_when_latest_assistant_reply_stalls_after_return_packet(self):
        assistant_text = "Already processed assistant reply."
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
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
                    "last_outbound_user_message_sent_at": time.time() - 120.0,
                    "auto_run_enabled": True,
                    "supervisor_status": "running",
                },
            )
            adapter = _FakeAdapter(assistant_text, assistant_retry_result=True)
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
            self.assertEqual(adapter.assistant_retry_calls, 1)
            self.assertEqual(executor.calls, [])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["loop_state"], "waiting_for_chatgpt_response")
            self.assertEqual(session_payload["last_outbound_user_message_kind"], "return_packet")
            self.assertEqual(session_payload["degraded_mode"], "chatgpt_same_turn_retry")

    def test_run_once_retries_known_delivery_errors(self):
        assistant_text = "\n".join(
            [
                "Continue with the next run.",
                "```bridge-control",
                '{"protocol_version":"1","session_id":"session-1","decision":"run_codex","codex_thread_action":"fork_thread","prompt":"Rebrief in a clean Codex session.","task_label":"retry-delivery"}',
                "```",
            ]
        )
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-3",
                "summary": "Forked run finished.",
                "files_touched": ["mastermind_bridge/orchestrator/control.py"],
                "checks": ["python3 -m unittest discover -s tests"],
                "blockers": [],
                "risks": [],
                "next_step": "Wait for the next ChatGPT turn.",
                "observed_codex_thread_id": "exec-999",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(root)
            adapter = _FakeAdapter(
                assistant_text,
                post_results=[
                    {"status": "failed", "error_signature": "Reasoning failed"},
                    {"status": "delivered", "message_anchor": "msg-user-2"},
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

            self.assertEqual(result["delivery_status"], "delivered")
            self.assertEqual(result["delivery_attempt_count"], 2)
            self.assertEqual(len(adapter.posted_messages), 2)

    def test_run_once_retries_delivery_confirmation_timeout(self):
        assistant_text = "\n".join(
            [
                "Continue with the next run.",
                "```bridge-control",
                '{"protocol_version":"1","session_id":"session-1","decision":"run_codex","codex_thread_action":"same_thread","prompt":"Continue after the slow delivery confirmation.","task_label":"retry-timeout"}',
                "```",
            ]
        )
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-4",
                "summary": "Timeout retry finished.",
                "files_touched": ["mastermind_bridge/orchestrator/loop.py"],
                "checks": ["python3 -m unittest tests.test_loop -q"],
                "blockers": [],
                "risks": [],
                "next_step": "Wait for the next ChatGPT turn.",
                "observed_codex_thread_id": "exec-1000",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(root)
            adapter = _FakeAdapter(
                assistant_text,
                post_results=[
                    {"status": "failed", "error_signature": "Message delivery confirmation timed out."},
                    {"status": "delivered", "message_anchor": "msg-user-2"},
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

            self.assertEqual(result["delivery_status"], "delivered")
            self.assertEqual(result["delivery_attempt_count"], 2)
            self.assertEqual(len(adapter.posted_messages), 2)

    def test_run_once_requests_automatic_repair_when_bridge_control_block_is_missing(self):
        assistant_text = "I forgot the control block."
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-3",
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
            self.assertEqual(result["loop_state"], "waiting_for_chatgpt_response")
            self.assertEqual(len(executor.calls), 1)
            self.assertEqual(executor.calls[0]["prompt"], assistant_text)
            self.assertEqual(len(adapter.posted_messages), 1)
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["degraded_mode"], "assistant_freeform_followup")
            self.assertEqual(session_payload["bridge_control_failure_streak"], 0)
            self.assertEqual(session_payload["loop_state"], "waiting_for_chatgpt_response")
            self.assertEqual(session_payload["human_attention_reason"], "")
            self.assertNotEqual(session_payload["latest_assistant_message_hash"], "")

    def test_run_once_chooses_new_thread_locally_for_freeform_followup_when_context_is_low(self):
        assistant_text = "Write the next Codex prompt in plain prose and continue the tracked roots work."
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:10:00+02:00",
                "thread_id": "thread-4",
                "summary": "Continued the next slice.",
                "files_touched": [],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "",
                "observed_codex_thread_id": "exec-999",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(root, current_codex_run_id="exec-123")
            run_dir = root / "artifacts" / "runs" / "20260418T020000-session-1"
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "run_report.json").write_text(
                json.dumps(
                    {
                        "timestamp": "2026-04-15T14:00:00+02:00",
                        "thread_id": "thread-low-context",
                        "summary": "Earlier run",
                        "files_touched": [],
                        "checks": [],
                        "blockers": [],
                        "risks": [],
                        "next_step": "",
                        "estimated_context_remaining_percent": 35,
                    }
                )
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
            self.assertEqual(executor.calls[0]["thread_action"], "new_thread")
            self.assertEqual(executor.calls[0]["prompt"], assistant_text)

    def test_run_once_respects_explicit_new_thread_hint_in_freeform_followup(self):
        assistant_text = "\n".join(
            [
                "The last Codex run fixed the right layers first.",
                "",
                "bridge-control",
                "thread_action: new_thread",
                "prompt: |",
                "  Continue work in /tmp/repo only.",
                "  Start a fresh Codex thread for the next filesystem frontier run.",
            ]
        )
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:10:00+02:00",
                "thread_id": "thread-4",
                "summary": "Continued the next slice on a fresh thread.",
                "files_touched": [],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "",
                "observed_codex_thread_id": "exec-999",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(root, current_codex_run_id="exec-123")
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
            self.assertEqual(executor.calls[0]["thread_action"], "new_thread")
            self.assertEqual(executor.calls[0]["prompt"], assistant_text)

    def test_run_once_waits_while_assistant_response_is_still_in_progress(self):
        assistant_text = "bridge-control"
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-3",
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
            bindings_path, policy_path, sessions_dir = self._write_state(root)
            adapter = _FakeAdapter(assistant_text, assistant_in_progress=True)
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
            self.assertEqual(executor.calls, [])
            self.assertEqual(adapter.posted_messages, [])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["loop_state"], "waiting_for_chatgpt_response")
            self.assertEqual(session_payload["human_attention_reason"], "")

    def test_run_once_waits_for_complete_bridge_control_block_even_if_partial_block_is_already_visible(self):
        assistant_text = "\n".join(
            [
                "bridge-control",
                'protocol_version: "1.0"',
                'session_id: "session-1"',
                'decision: "run_codex"',
                'codex_thread_action: "new_thread"',
                'task_label: "continue_cycle"',
                "prompt: |",
                "  Stay in /tmp/repo only.",
                "  Continue with the next safe step.",
            ]
        )
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-3",
                "summary": "Override executed successfully.",
                "files_touched": [],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "Wait for the next ChatGPT turn.",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(root)
            adapter = _FakeAdapter(assistant_text, assistant_in_progress=True)
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
            self.assertEqual(executor.calls, [])
            self.assertEqual(adapter.posted_messages, [])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["loop_state"], "waiting_for_chatgpt_response")

    def test_run_once_waits_for_stale_in_progress_assistant_response_without_repair(self):
        assistant_text = "Ich"
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-3",
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
            stale_time = time.time() - 180.0
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "in_progress_assistant_anchor": "msg-assistant-1",
                    "in_progress_assistant_hash": sha1(assistant_text.encode("utf-8")).hexdigest(),
                    "in_progress_assistant_text": assistant_text,
                    "in_progress_assistant_started_at": stale_time,
                    "in_progress_assistant_last_progress_at": stale_time,
                },
            )
            adapter = _FakeAdapter(assistant_text, assistant_in_progress=True, cancel_result=True)
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
            self.assertEqual(adapter.cancel_calls, 0)
            self.assertEqual(executor.calls, [])
            self.assertEqual(adapter.posted_messages, [])
            self.assertIn("still in progress", json.loads((sessions_dir / "session-1.json").read_text())["session"]["policy_decision"]["reasons"][0])

    def test_run_once_waits_for_short_pathological_fragment_while_assistant_is_still_in_progress(self):
        assistant_text = "bridge"
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-3",
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
            stale_time = time.time() - 30.0
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "in_progress_assistant_anchor": "msg-assistant-1",
                    "in_progress_assistant_hash": sha1(assistant_text.encode("utf-8")).hexdigest(),
                    "in_progress_assistant_text": assistant_text,
                    "in_progress_assistant_started_at": stale_time,
                    "in_progress_assistant_last_progress_at": stale_time,
                },
            )
            adapter = _FakeAdapter(assistant_text, assistant_in_progress=True, cancel_result=True)
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
            self.assertEqual(adapter.cancel_calls, 0)
            self.assertEqual(adapter.posted_messages, [])

    def test_run_once_processes_new_user_override_even_when_latest_assistant_turn_is_already_processed(self):
        assistant_text = "bridge-control"
        user_override = "\n".join(
            [
                "bridge-control",
                'protocol_version: "1.0"',
                'session_id: "session-1"',
                'decision: "run_codex"',
                'codex_thread_action: "new_thread"',
                'task_label: "manual_recovery"',
                "prompt: |",
                "  Stay in /tmp/repo only.",
                "  Start a fresh Codex thread for the next workstream.",
            ]
        )
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-3",
                "summary": "Recovered from manual override.",
                "files_touched": ["mastermind_bridge/orchestrator/loop.py"],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "Wait for the next ChatGPT turn.",
                "observed_codex_thread_id": "exec-999",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            assistant_hash = sha1(assistant_text.encode("utf-8")).hexdigest()
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "last_seen_chat_message_anchor": "msg-assistant-1",
                    "latest_assistant_message_hash": assistant_hash,
                    "loop_state": "requires_human",
                    "auto_run_enabled": False,
                    "supervisor_status": "blocked",
                    "human_attention_reason": "Old malformed reply.",
                    "last_error": "Old malformed reply.",
                },
            )
            adapter = _FakeAdapter(assistant_text, user_messages=[user_override])
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
            self.assertEqual(result["runner_action"], "cycle_completed")
            self.assertEqual(executor.calls[0]["thread_action"], "new_thread")
            self.assertIn("fresh Codex thread", executor.calls[0]["prompt"])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertTrue(session_payload["auto_run_enabled"])
            self.assertEqual(session_payload["supervisor_status"], "running")
            self.assertEqual(session_payload["human_attention_reason"], "")
            self.assertEqual(session_payload["last_error"], "")

    def test_run_once_waits_for_pathological_fragment_even_with_prior_recovery_outstanding(self):
        assistant_text = "bridge"
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-3",
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
            stale_time = time.time() - 60.0
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "in_progress_assistant_anchor": "msg-assistant-1",
                    "in_progress_assistant_hash": sha1(assistant_text.encode("utf-8")).hexdigest(),
                    "in_progress_assistant_text": assistant_text,
                    "in_progress_assistant_started_at": stale_time,
                    "in_progress_assistant_last_progress_at": stale_time,
                    "last_outbound_user_message_anchor": "recovery-session-1-deadbeef-1",
                    "last_outbound_user_message_kind": "recovery",
                    "last_outbound_user_message_sent_at": stale_time,
                },
            )
            adapter = _FakeAdapter(assistant_text, assistant_in_progress=True, cancel_result=False)
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
            self.assertEqual(adapter.cancel_calls, 0)
            self.assertEqual(adapter.posted_messages, [])
            self.assertIn("still in progress", json.loads((sessions_dir / "session-1.json").read_text())["session"]["policy_decision"]["reasons"][0])

    def test_observed_recent_recovery_prompt_count_counts_matching_plain_text_recovery_prompts(self):
        adapter = _FakeAdapter(
            "bridge",
            user_messages=[
                "[recovery-session-1-deadbeef-1]\nThen reply once with one fresh complete plain-language next prompt for Codex for this same session.",
                "[recovery-session-1-deadbeef-2]\nThen reply once with one fresh complete plain-language next prompt for Codex for this same session.",
                "[recovery-session-1-deadbeef-3]\nThen reply once with one fresh complete plain-language next prompt for Codex for this same session.",
            ],
        )

        count = _observed_recent_recovery_prompt_count(
            adapter,
            object(),
            required_signature="fresh complete plain-language next prompt for codex",
        )

        self.assertEqual(count, 3)

    def test_observed_recent_recovery_prompt_count_scopes_to_matching_anchor_prefix(self):
        adapter = _FakeAdapter(
            "bridge",
            user_messages=[
                "[recovery-session-1-otherhash0001-1]\nThen reply once with one fresh complete plain-language next prompt for Codex for this same session.",
                "[recovery-session-1-targethash001-1]\nThen reply once with one fresh complete plain-language next prompt for Codex for this same session.",
                "[recovery-session-1-targethash001-2]\nThen reply once with one fresh complete plain-language next prompt for Codex for this same session.",
            ],
        )

        count = _observed_recent_recovery_prompt_count(
            adapter,
            object(),
            required_signature="fresh complete plain-language next prompt for codex",
            required_anchor_prefix="[recovery-session-1-targethash001-",
        )

        self.assertEqual(count, 2)

    def test_run_once_waits_on_thinking_disclosure_before_long_stall_threshold(self):
        assistant_text = "Thought for 21s"
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-3",
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
            stale_time = time.time() - 60.0
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "in_progress_assistant_anchor": "msg-assistant-1",
                    "in_progress_assistant_hash": sha1(assistant_text.encode("utf-8")).hexdigest(),
                    "in_progress_assistant_text": assistant_text,
                    "in_progress_assistant_started_at": stale_time,
                    "in_progress_assistant_last_progress_at": stale_time,
                },
            )
            adapter = _FakeAdapter(assistant_text, assistant_in_progress=True, cancel_result=False)
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
            self.assertEqual(adapter.cancel_calls, 0)
            self.assertEqual(adapter.posted_messages, [])

    def test_run_once_waits_for_long_thinking_disclosure_without_repair(self):
        assistant_text = "Thought for 21s"
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-3",
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
            stale_time = time.time() - 120.0
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "in_progress_assistant_anchor": "msg-assistant-1",
                    "in_progress_assistant_hash": sha1(assistant_text.encode("utf-8")).hexdigest(),
                    "in_progress_assistant_text": assistant_text,
                    "in_progress_assistant_started_at": stale_time,
                    "in_progress_assistant_last_progress_at": stale_time,
                },
            )
            adapter = _FakeAdapter(assistant_text, assistant_in_progress=True, cancel_result=False)
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
            self.assertEqual(adapter.cancel_calls, 0)
            self.assertEqual(adapter.posted_messages, [])

    def test_run_once_waits_for_thinking_disclosure_when_display_seconds_change(self):
        assistant_text = "Thought for 56s"
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-3",
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
            stale_time = time.time() - 120.0
            previous_text = "Thought for 21s"
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "in_progress_assistant_anchor": "msg-assistant-1",
                    "in_progress_assistant_hash": sha1(previous_text.encode("utf-8")).hexdigest(),
                    "in_progress_assistant_text": previous_text,
                    "in_progress_assistant_started_at": stale_time,
                    "in_progress_assistant_last_progress_at": stale_time,
                },
            )
            adapter = _FakeAdapter(assistant_text, assistant_in_progress=True, cancel_result=False)
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
            self.assertEqual(adapter.cancel_calls, 0)
            self.assertEqual(adapter.posted_messages, [])

    def test_run_once_waits_for_thinking_disclosure_even_after_prior_repair(self):
        assistant_text = "Thought for 21s"
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-3",
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
            stale_time = time.time() - 40.0
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "in_progress_assistant_anchor": "msg-assistant-1",
                    "in_progress_assistant_hash": sha1(assistant_text.encode("utf-8")).hexdigest(),
                    "in_progress_assistant_text": assistant_text,
                    "in_progress_assistant_started_at": stale_time,
                    "in_progress_assistant_last_progress_at": stale_time,
                    "bridge_control_failure_streak": 1,
                    "last_outbound_user_message_anchor": "repair-session-1-thinking-1",
                    "last_outbound_user_message_kind": "repair",
                    "last_outbound_user_message_sent_at": stale_time,
                },
            )
            adapter = _FakeAdapter(assistant_text, assistant_in_progress=True, cancel_result=False)
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
            self.assertEqual(adapter.cancel_calls, 0)
            self.assertEqual(adapter.posted_messages, [])

    def test_run_once_waits_for_chatgpt_label_fragment_while_in_progress(self):
        assistant_text = "ChatGPT:"
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-3",
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
            stale_time = time.time() - 30.0
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "in_progress_assistant_anchor": "msg-assistant-1",
                    "in_progress_assistant_hash": sha1(assistant_text.encode("utf-8")).hexdigest(),
                    "in_progress_assistant_text": assistant_text,
                    "in_progress_assistant_started_at": stale_time,
                    "in_progress_assistant_last_progress_at": stale_time,
                },
            )
            adapter = _FakeAdapter(assistant_text, assistant_in_progress=True, cancel_result=False)
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
            self.assertEqual(adapter.cancel_calls, 0)
            self.assertEqual(adapter.posted_messages, [])

    def test_run_once_waits_for_multi_unit_thinking_disclosure_even_when_elapsed_exceeds_threshold(self):
        assistant_text = "Thought for 2m 33s"
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-3",
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
            bindings_path, policy_path, sessions_dir = self._write_state(root)
            adapter = _FakeAdapter(assistant_text, assistant_in_progress=False, cancel_result=True)
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
            self.assertEqual(adapter.cancel_calls, 0)
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["last_outbound_user_message_kind"], "")

    def test_run_once_waits_for_short_thinking_disclosure_even_when_adapter_does_not_flag_in_progress(self):
        assistant_text = "Thought for 6s"
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-3",
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
            bindings_path, policy_path, sessions_dir = self._write_state(root)
            adapter = _FakeAdapter(assistant_text, assistant_in_progress=False, cancel_result=True)
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
            self.assertEqual(adapter.cancel_calls, 0)
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["in_progress_assistant_anchor"], "msg-assistant-1")
            self.assertEqual(session_payload["in_progress_assistant_text"], "thinking")

    def test_run_once_waits_for_mixed_reply_with_trailing_thinking_disclosure(self):
        assistant_text = (
            "I’m pulling in the recent chat context first so I can ground the Codex handoff.\n\n"
            "Denke nach…"
        )
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-3",
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
            assistant_hash = sha1(assistant_text.encode("utf-8")).hexdigest()
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "bridge_control_failure_streak": 1,
                    "in_progress_assistant_anchor": "msg-assistant-1",
                    "in_progress_assistant_hash": assistant_hash,
                    "in_progress_assistant_text": assistant_text,
                    "in_progress_assistant_started_at": time.time() - 35.0,
                    "in_progress_assistant_last_progress_at": time.time() - 35.0,
                },
            )
            adapter = _FakeAdapter(assistant_text, assistant_in_progress=True, cancel_result=False)
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
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["last_outbound_user_message_kind"], "")

    def test_run_once_cancels_stale_in_progress_turn_and_requests_recovery_after_repair_timeout(self):
        assistant_text = "I’ve got direct GitHub access available here."
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-3",
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
            assistant_hash = sha1(assistant_text.encode("utf-8")).hexdigest()
            base_hash = assistant_hash[:12]
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "bridge_control_failure_streak": 1,
                    "last_posted_return_packet_id": "packet-123",
                    "last_seen_chat_message_anchor": "msg-assistant-1",
                    "latest_assistant_message_hash": assistant_hash,
                    "last_outbound_user_message_anchor": f"repair-session-1-{base_hash}-1",
                    "last_outbound_user_message_kind": "repair",
                    "last_outbound_user_message_sent_at": time.time() - 90.0,
                },
            )
            prior_repair = f"[repair-session-1-{base_hash}-1]\nEarlier repair attempt."
            adapter = _FakeAdapter(
                assistant_text,
                assistant_in_progress=True,
                cancel_result=True,
                user_messages=[prior_repair],
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
            self.assertEqual(adapter.cancel_calls, 1)
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["bridge_control_failure_streak"], 1)
            self.assertEqual(session_payload["last_outbound_user_message_kind"], "")
            self.assertEqual(session_payload["loop_state"], "waiting_for_chatgpt")
            self.assertEqual(session_payload["in_progress_assistant_anchor"], "")

    def test_run_once_waits_when_stale_in_progress_repair_turn_cannot_be_cancelled(self):
        assistant_text = "I’ve got direct GitHub access available here."
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-3",
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
            assistant_hash = sha1(assistant_text.encode("utf-8")).hexdigest()
            base_hash = assistant_hash[:12]
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "bridge_control_failure_streak": 1,
                    "last_posted_return_packet_id": "packet-123",
                    "last_seen_chat_message_anchor": "msg-assistant-1",
                    "latest_assistant_message_hash": assistant_hash,
                    "last_outbound_user_message_anchor": f"repair-session-1-{base_hash}-1",
                    "last_outbound_user_message_kind": "repair",
                    "last_outbound_user_message_sent_at": time.time() - 90.0,
                },
            )
            adapter = _FakeAdapter(assistant_text, assistant_in_progress=True, cancel_result=False)
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
            self.assertEqual(adapter.cancel_calls, 1)
            self.assertEqual(adapter.posted_messages, [])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["bridge_control_failure_streak"], 1)
            self.assertEqual(session_payload["last_outbound_user_message_kind"], "repair")

    def test_run_once_failovers_to_fresh_chat_when_no_new_assistant_response_arrives_after_recovery(self):
        assistant_text = "The connected repo search"
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-3",
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
            assistant_hash = sha1(assistant_text.encode("utf-8")).hexdigest()
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "bridge_control_failure_streak": 2,
                    "last_posted_return_packet_id": "packet-123",
                    "last_seen_chat_message_anchor": "msg-assistant-1",
                    "latest_assistant_message_id": "msg-assistant-1",
                    "latest_assistant_message_hash": assistant_hash,
                    "last_outbound_user_message_anchor": "recovery-session-1-deadbeef-1",
                    "last_outbound_user_message_kind": "recovery",
                    "last_outbound_user_message_sent_at": time.time() - 90.0,
                    "auto_run_enabled": True,
                    "supervisor_status": "running",
                    "chat_url": "https://chatgpt.com/g/g-p-test/c/old-chat",
                },
            )
            save_chat_bindings(
                bindings_path,
                [
                    {
                        "binding_id": "binding-1",
                        "project_name": "bridge",
                        "repo_path": "/tmp/repo",
                        "workspace_path": "/tmp/repo",
                        "chat_url": "https://chatgpt.com/g/g-p-test/c/old-chat",
                        "browser_profile_path": "/tmp/profile",
                        "browser_session_handle": "default",
                    }
                ],
            )
            run_dir = root / "artifacts" / "runs" / "20260415T140000-session-1"
            run_dir.mkdir(parents=True)
            (run_dir / "run_report.json").write_text(json.dumps(report.as_dict()))
            adapter = _FakeAdapter(
                assistant_text,
                assistant_in_progress=False,
                current_chat_url="https://chatgpt.com/g/g-p-test/c/old-chat",
                post_results=[
                    {
                        "status": "failed",
                        "error_signature": "ChatGPT DOM contract missing `composer` selector match.",
                    },
                    {
                        "status": "delivered",
                        "message_anchor": "msg-user-failover",
                        "current_chat_url": "https://chatgpt.com/c/new-chat",
                    },
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

            self.assertEqual(result["policy_outcome"], "allow")
            self.assertEqual(result["runner_action"], "fresh_chat_failover")
            self.assertEqual(
                adapter.opened_urls,
                [
                    "https://chatgpt.com/g/g-p-test/c/old-chat",
                    "https://chatgpt.com/g/g-p-test/new",
                    "https://chatgpt.com/new",
                ],
            )
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["chat_url"], "https://chatgpt.com/c/new-chat")
            self.assertEqual(session_payload["loop_state"], "waiting_for_chatgpt_response")
            self.assertEqual(session_payload["last_outbound_user_message_kind"], "return_packet")

    def test_run_once_prioritizes_new_user_override_over_in_progress_assistant(self):
        assistant_text = "bridge"
        user_override = "\n".join(
            [
                "bridge-control",
                'protocol_version: "1.0"',
                'session_id: "session-1"',
                'decision: "run_codex"',
                'codex_thread_action: "new_thread"',
                'task_label: "continue_cycle"',
                "prompt: |",
                "  Stay in /tmp/repo only.",
                "  Continue with the next safe step.",
            ]
        )
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-3",
                "summary": "Override executed successfully.",
                "files_touched": [],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "Wait for the next ChatGPT turn.",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(root)
            adapter = _FakeAdapter(
                assistant_text,
                assistant_in_progress=True,
                cancel_result=True,
                user_messages=[user_override],
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
            self.assertEqual(result["runner_action"], "cycle_completed")
            self.assertEqual(adapter.cancel_calls, 1)
            self.assertEqual(executor.calls[0]["thread_action"], "new_thread")

    def test_run_once_accepts_identical_override_text_when_message_anchor_is_new(self):
        assistant_text = "bridge"
        user_override = "\n".join(
            [
                "bridge-control",
                'protocol_version: "1.0"',
                'session_id: "session-1"',
                'decision: "run_codex"',
                'codex_thread_action: "new_thread"',
                'task_label: "continue_cycle"',
                "prompt: |",
                "  Stay in /tmp/repo only.",
                "  Continue with the next safe step.",
            ]
        )
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-3",
                "summary": "Override executed successfully.",
                "files_touched": [],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "Wait for the next ChatGPT turn.",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "last_seen_user_control_anchor": "msg-user-1",
                    "latest_user_control_message_hash": sha1(user_override.encode("utf-8")).hexdigest(),
                },
            )
            adapter = _FakeAdapter(assistant_text, user_messages=["older", user_override])
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
            self.assertEqual(result["runner_action"], "cycle_completed")
            self.assertEqual(executor.calls[0]["thread_action"], "new_thread")

    def test_run_once_retries_repair_when_no_assistant_response_arrives(self):
        assistant_text = "bridge"
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-3",
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
            assistant_hash = sha1(assistant_text.encode("utf-8")).hexdigest()
            base_hash = assistant_hash[:12]
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "last_seen_chat_message_anchor": "msg-assistant-1",
                    "latest_assistant_message_hash": assistant_hash,
                    "last_outbound_user_message_anchor": f"repair-session-1-{base_hash}-1",
                    "last_outbound_user_message_kind": "repair",
                    "last_outbound_user_message_sent_at": time.time() - 180.0,
                },
            )
            prior_repair = f"[repair-session-1-{base_hash}-1]\nEarlier repair attempt."
            adapter = _FakeAdapter(assistant_text, user_messages=[prior_repair])
            executor = _FakeExecutor(report)
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1", require_new_message=True)

            self.assertEqual(result["runner_action"], "wait_for_chatgpt")
            self.assertEqual(adapter.posted_messages, [])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["last_outbound_user_message_kind"], "")
            self.assertEqual(session_payload["bridge_control_failure_streak"], 0)
            self.assertEqual(session_payload["loop_state"], "waiting_for_chatgpt")

    def test_run_once_escalates_to_recovery_rebrief_after_repeated_invalid_replies(self):
        assistant_text = "bridge-control"
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-3",
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
                    "bridge_control_failure_streak": 3,
                    "last_posted_return_packet_id": "packet-123",
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

            self.assertEqual(result["runner_action"], "wait_for_chatgpt")
            self.assertEqual(adapter.posted_messages, [])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["bridge_control_failure_streak"], 3)
            self.assertEqual(session_payload["last_outbound_user_message_kind"], "")

    def test_run_once_reconstructs_failure_streak_from_recent_repair_history(self):
        assistant_text = "bridge-control"
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-3",
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
            bindings_path, policy_path, sessions_dir = self._write_state(root)
            base_hash = sha1(assistant_text.encode("utf-8")).hexdigest()[:12]
            prior_repairs = [
                f"[repair-session-1-{base_hash}-1]\nEarlier repair attempt.",
                f"[repair-session-1-{base_hash}-2]\nSecond repair attempt.",
                f"[repair-session-1-{base_hash}-3]\nThird repair attempt.",
            ]
            adapter = _FakeAdapter(assistant_text, user_messages=prior_repairs)
            executor = _FakeExecutor(report)
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1")

            self.assertEqual(result["runner_action"], "wait_for_chatgpt")
            self.assertEqual(adapter.posted_messages, [])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["bridge_control_failure_streak"], 0)

    def test_run_once_blocks_after_repeated_recovery_rebriefs(self):
        assistant_text = "bridge"
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-3",
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
                    "bridge_control_failure_streak": 6,
                    "last_posted_return_packet_id": "packet-123",
                },
            )
            base_hash = sha1(assistant_text.encode("utf-8")).hexdigest()[:12]
            recent_messages = [
                f"[recovery-session-1-{base_hash}-1]\nThen reply once with one fresh complete plain-language next prompt for Codex for this same session.",
                f"[recovery-session-1-{base_hash}-2]\nThen reply once with one fresh complete plain-language next prompt for Codex for this same session.",
                f"[recovery-session-1-{base_hash}-3]\nThen reply once with one fresh complete plain-language next prompt for Codex for this same session.",
            ]
            adapter = _FakeAdapter(assistant_text, user_messages=recent_messages)
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
            self.assertEqual(adapter.posted_messages, [])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["human_attention_reason"], "")
            self.assertEqual(session_payload["loop_state"], "waiting_for_chatgpt")

    def test_run_once_fails_over_to_fresh_chat_after_single_recovery_rebrief(self):
        assistant_text = "bridge"
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-3",
                "summary": "Recovered run completed.",
                "final_agent_message": "Here is the latest Codex result.",
                "files_touched": ["mastermind_bridge/orchestrator/loop.py"],
                "checks": ["python3 -m unittest discover -s tests"],
                "blockers": [],
                "risks": [],
                "next_step": "Continue in a fresh ChatGPT conversation.",
                "observed_codex_thread_id": "exec-999",
                "thread_action": "same_thread",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "bridge_control_failure_streak": 6,
                    "last_posted_return_packet_id": "packet-123",
                    "auto_run_enabled": True,
                    "supervisor_status": "running",
                    "chat_url": "https://chatgpt.com/g/g-p-test/c/old-chat",
                },
            )
            save_chat_bindings(
                bindings_path,
                [
                    {
                        "binding_id": "binding-1",
                        "project_name": "bridge",
                        "repo_path": "/tmp/repo",
                        "workspace_path": "/tmp/repo",
                        "chat_url": "https://chatgpt.com/g/g-p-test/c/old-chat",
                        "browser_profile_path": "/tmp/profile",
                        "browser_session_handle": "default",
                    }
                ],
            )
            run_dir = root / "artifacts" / "runs" / "20260415T140000-session-1"
            run_dir.mkdir(parents=True)
            run_report_path = run_dir / "run_report.json"
            run_report_path.write_text(json.dumps(report.as_dict()))
            base_hash = sha1(assistant_text.encode("utf-8")).hexdigest()[:12]
            recent_messages = [
                f"[recovery-session-1-{base_hash}-1]\nThen reply once with one fresh complete plain-language next prompt for Codex for this same session.",
            ]
            adapter = _FakeAdapter(
                assistant_text,
                user_messages=recent_messages,
                current_chat_url="https://chatgpt.com/g/g-p-test/c/old-chat",
                post_results=[
                    {
                        "status": "failed",
                        "error_signature": "ChatGPT DOM contract missing `composer` selector match.",
                    },
                    {
                        "status": "delivered",
                        "message_anchor": "msg-user-failover",
                        "current_chat_url": "https://chatgpt.com/c/new-chat",
                    }
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

            self.assertEqual(result["policy_outcome"], "allow")
            self.assertEqual(result["runner_action"], "wait_for_chatgpt")
            self.assertEqual(executor.calls, [])
            self.assertEqual(adapter.posted_messages, [])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["loop_state"], "waiting_for_chatgpt")
            self.assertEqual(session_payload["supervisor_status"], "running")
            self.assertEqual(session_payload["chat_url"], "https://chatgpt.com/g/g-p-test/c/old-chat")
            self.assertEqual(session_payload["bridge_control_failure_streak"], 6)
            self.assertEqual(session_payload["human_attention_reason"], "")
            self.assertEqual(session_payload["last_posted_return_packet_id"], "packet-123")
            bindings_payload = json.loads(bindings_path.read_text())
            self.assertEqual(
                bindings_payload["bindings"][0]["chat_url"],
                "https://chatgpt.com/g/g-p-test/c/old-chat",
            )

    def test_run_once_keeps_canonical_binding_url_after_fresh_chat_failover(self):
        assistant_text = "bridge"
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-3",
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
                    "bridge_control_failure_streak": 6,
                    "last_posted_return_packet_id": "packet-123",
                    "auto_run_enabled": True,
                    "supervisor_status": "running",
                    "chat_url": "https://chatgpt.com/g/g-p-test/c/old-chat",
                },
            )
            save_chat_bindings(
                bindings_path,
                [
                    {
                        "binding_id": "binding-1",
                        "project_name": "bridge",
                        "repo_path": "/tmp/repo",
                        "workspace_path": "/tmp/repo",
                        "chat_url": "https://chatgpt.com/g/g-p-test/c/old-chat",
                        "browser_profile_path": "/tmp/profile",
                        "browser_session_handle": "default",
                    }
                ],
            )
            run_dir = root / "artifacts" / "runs" / "20260415T140000-session-1"
            run_dir.mkdir(parents=True)
            run_report_path = run_dir / "run_report.json"
            run_report_path.write_text(json.dumps(report.as_dict()))
            base_hash = sha1(assistant_text.encode("utf-8")).hexdigest()[:12]
            adapter = _FakeAdapter(
                assistant_text,
                user_messages=[
                    f"[recovery-session-1-{base_hash}-1]\nThen reply once with one fresh complete plain-language next prompt for Codex for this same session.",
                ],
                current_chat_url="https://chatgpt.com/g/g-p-test/c/old-chat",
                current_chat_urls=[
                    "https://chatgpt.com/g/g-p-test/c/old-chat",
                    "https://chatgpt.com/new",
                    "https://chatgpt.com/c/redirected-chat",
                ],
                post_results=[
                    {
                        "status": "failed",
                        "error_signature": "ChatGPT DOM contract missing `composer` selector match.",
                    },
                    {
                        "status": "delivered",
                        "message_anchor": "msg-user-failover",
                    },
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

            self.assertEqual(result["runner_action"], "wait_for_chatgpt")
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["chat_url"], "https://chatgpt.com/g/g-p-test/c/old-chat")
            bindings_payload = json.loads(bindings_path.read_text())
            self.assertEqual(bindings_payload["bindings"][0]["chat_url"], "https://chatgpt.com/g/g-p-test/c/old-chat")

    def test_run_once_blocks_after_repeated_thinking_recovery_rebriefs_with_changing_seconds(self):
        assistant_text = "Thought for 34s"
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-3",
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
            stale_time = time.time() - 120.0
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "bridge_control_failure_streak": 6,
                    "last_posted_return_packet_id": "packet-123",
                    "in_progress_assistant_anchor": "msg-assistant-1",
                    "in_progress_assistant_hash": sha1(assistant_text.encode("utf-8")).hexdigest(),
                    "in_progress_assistant_text": assistant_text,
                    "in_progress_assistant_started_at": stale_time,
                    "in_progress_assistant_last_progress_at": stale_time,
                },
            )
            recent_messages = [
                "[recovery-session-1-thinking-1]\nThen reply once with one fresh complete plain-language next prompt for Codex for this same session.",
                "[recovery-session-1-thinking-2]\nThen reply once with one fresh complete plain-language next prompt for Codex for this same session.",
                "[recovery-session-1-thinking-3]\nThen reply once with one fresh complete plain-language next prompt for Codex for this same session.",
            ]
            adapter = _FakeAdapter(
                assistant_text,
                user_messages=recent_messages,
                assistant_in_progress=True,
                cancel_result=False,
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
            self.assertEqual(executor.calls, [])
            self.assertEqual(adapter.posted_messages, [])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["human_attention_reason"], "")

    def test_run_once_escalates_no_response_follow_up_to_recovery_after_repeated_failures(self):
        assistant_text = "bridge"
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-3",
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
            assistant_hash = sha1(assistant_text.encode("utf-8")).hexdigest()
            base_hash = assistant_hash[:12]
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "last_seen_chat_message_anchor": "msg-assistant-1",
                    "latest_assistant_message_hash": assistant_hash,
                    "last_outbound_user_message_anchor": f"repair-session-1-{base_hash}-1",
                    "last_outbound_user_message_kind": "repair",
                    "last_outbound_user_message_sent_at": time.time() - 180.0,
                    "bridge_control_failure_streak": 3,
                    "last_posted_return_packet_id": "packet-123",
                },
            )
            prior_repair = f"[repair-session-1-{base_hash}-1]\nEarlier repair attempt."
            adapter = _FakeAdapter(assistant_text, user_messages=[prior_repair])
            executor = _FakeExecutor(report)
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1", require_new_message=True)

            self.assertEqual(result["runner_action"], "wait_for_chatgpt")
            self.assertEqual(adapter.posted_messages, [])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["bridge_control_failure_streak"], 3)
            self.assertEqual(session_payload["last_outbound_user_message_kind"], "")

    def test_run_once_treats_new_assistant_anchor_as_new_message_even_when_text_hash_matches(self):
        assistant_text = "bridge"
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-3",
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
            assistant_hash = sha1(assistant_text.encode("utf-8")).hexdigest()
            bindings_path, policy_path, sessions_dir = self._write_state(
                root,
                extra_session_fields={
                    "last_seen_chat_message_anchor": "msg-assistant-old",
                    "latest_assistant_message_hash": assistant_hash,
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

            result = runner.run_once("session-1", require_new_message=True)

            self.assertEqual(result["runner_action"], "wait_for_chatgpt")
            self.assertEqual(executor.calls, [])
            self.assertEqual(len(adapter.posted_messages), 0)

    def test_run_once_blocks_when_automatic_repair_delivery_fails(self):
        assistant_text = "bridge-control"
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-3",
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
            bindings_path, policy_path, sessions_dir = self._write_state(root)
            adapter = _FakeAdapter(
                assistant_text,
                post_results=[{"status": "failed", "error_signature": "Message delivery confirmation timed out."}],
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
            self.assertEqual(executor.calls, [])
            self.assertEqual(len(adapter.posted_messages), 0)
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["loop_state"], "waiting_for_chatgpt")
            self.assertEqual(session_payload["human_attention_reason"], "")

    def test_run_once_accepts_visible_repair_message_after_delivery_timeout(self):
        assistant_text = "Ich gleiche"
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-3",
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
            bindings_path, policy_path, sessions_dir = self._write_state(root)
            expected_anchor = f"repair-session-1-{sha1(assistant_text.encode('utf-8')).hexdigest()[:12]}-1"
            adapter = _FakeAdapter(
                assistant_text,
                post_results=[{"status": "failed", "error_signature": "Message delivery confirmation timed out."}],
                visible_message_ids={expected_anchor},
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
            self.assertEqual(len(executor.calls), 1)
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["loop_state"], "waiting_for_chatgpt_response")
            self.assertEqual(session_payload["human_attention_reason"], "")


    def test_run_once_uses_unique_repair_anchor_when_prior_attempt_exists(self):
        assistant_text = "bridge-control"
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-3",
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
            bindings_path, policy_path, sessions_dir = self._write_state(root)
            base_hash = sha1(assistant_text.encode("utf-8")).hexdigest()[:12]
            prior_repair = f"[repair-session-1-{base_hash}-1]\nEarlier repair attempt."
            adapter = _FakeAdapter(
                assistant_text,
                user_messages=[prior_repair],
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

            self.assertEqual(result["runner_action"], "wait_for_chatgpt")
            self.assertEqual(adapter.posted_messages, [])

    def test_run_once_uses_fenced_json_repair_template_after_multiple_failed_attempts(self):
        assistant_text = "bridge"
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-3",
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
            bindings_path, policy_path, sessions_dir = self._write_state(root)
            base_hash = sha1(assistant_text.encode("utf-8")).hexdigest()[:12]
            prior_repairs = [
                f"[repair-session-1-{base_hash}-1]\nEarlier repair attempt.",
                f"[repair-session-1-{base_hash}-2]\nSecond repair attempt.",
            ]
            adapter = _FakeAdapter(
                assistant_text,
                user_messages=prior_repairs,
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

            self.assertEqual(result["runner_action"], "wait_for_chatgpt")
            self.assertEqual(adapter.posted_messages, [])

    def test_run_once_does_not_treat_repair_prompt_as_manual_override(self):
        assistant_text = "bridge-control"
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-3",
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
            bindings_path, policy_path, sessions_dir = self._write_state(root)
            repair_prompt = "\n".join(
                [
                "[repair-session-1-deadbeef-1]",
                    "Your last reply was incomplete or malformed for this session.",
                    "",
                    "Session id: session-1",
                    "",
                    "- write the full detailed next prompt for Codex here.",
                ]
            )
            adapter = _FakeAdapter(
                assistant_text,
                user_messages=[repair_prompt],
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

            self.assertEqual(result["runner_action"], "wait_for_chatgpt")
            self.assertEqual(executor.calls, [])
            self.assertEqual(adapter.posted_messages, [])

    def test_run_once_uses_latest_user_bridge_control_override_when_assistant_turn_is_malformed(self):
        assistant_text = "Ich habe"
        user_text = "\n".join(
            [
                "Fixed.",
                "",
                "bridge-control",
                'protocol_version: \"1.0\"',
                'session_id: \"session-1\"',
                'decision: \"run_codex\"',
                'codex_thread_action: \"new_thread\"',
                'task_label: \"manual_recovery\"',
                "prompt: |",
                "  Stay in /tmp/repo only.",
                "  Start a fresh Codex thread and inspect the auth/runtime seam.",
            ]
        )
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-3",
                "summary": "Recovered from malformed assistant turn.",
                "files_touched": ["mastermind_bridge/orchestrator/loop.py"],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "Wait for the next ChatGPT turn.",
                "observed_codex_thread_id": "exec-999",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(root)
            adapter = _FakeAdapter(assistant_text, user_text=user_text)
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
            self.assertIn("Start a fresh Codex thread", executor.calls[0]["prompt"])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["latest_user_control_command"], "bridge_control_override")
            self.assertEqual(session_payload["last_seen_user_control_anchor"], "msg-user-1")
            self.assertEqual(session_payload["bridge_control_failure_streak"], 0)

    def test_run_once_scans_recent_user_messages_for_newest_valid_bridge_control_override(self):
        assistant_text = "Ich setze"
        valid_override = "\n".join(
            [
                "Fixed.",
                "",
                "bridge-control",
                'protocol_version: "1.0"',
                'session_id: "session-1"',
                'decision: "run_codex"',
                'codex_thread_action: "same_thread"',
                'task_label: "manual_recovery"',
                "prompt: |",
                "  Stay in /tmp/repo only.",
                "  Continue in the same Codex thread and inspect the auth/runtime seam.",
            ]
        )
        latest_non_control = "Session id: session-1\n\nHere is what Codex wrote:\n\nNo bridge-control block yet."
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-3",
                "summary": "Recovered from earlier user override.",
                "files_touched": ["mastermind_bridge/orchestrator/loop.py"],
                "checks": [],
                "blockers": [],
                "risks": [],
                "next_step": "Wait for the next ChatGPT turn.",
                "observed_codex_thread_id": "exec-888",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(root)
            adapter = _FakeAdapter(
                assistant_text,
                user_messages=[valid_override, latest_non_control],
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
            self.assertEqual(executor.calls[0]["thread_action"], "same_thread")
            self.assertIn("Continue in the same Codex thread", executor.calls[0]["prompt"])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["latest_user_control_command"], "bridge_control_override")
            self.assertEqual(session_payload["last_seen_user_control_anchor"], "msg-user-1")

    def test_run_once_ignores_stale_override_before_latest_bridge_packet_when_packet_id_is_missing(self):
        assistant_text = "Ich gleiche"
        stale_override = "\n".join(
            [
                "Fixed.",
                "",
                "bridge-control",
                'protocol_version: "1.0"',
                'session_id: "session-1"',
                'decision: "run_codex"',
                'codex_thread_action: "same_thread"',
                'task_label: "stale_override"',
                "prompt: |",
                "  Continue the old Codex thread.",
            ]
        )
        latest_bridge_packet = "\n".join(
            [
                "Session id: session-1",
                "",
                "- refresh your understanding of the current project sources and plan",
                "- write your whole actionable reply as the next plain-language prompt for Codex",
                "",
                "Here is what Codex wrote:",
                "",
                "Completed the previous cycle.",
            ]
        )
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-3",
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
            bindings_path, policy_path, sessions_dir = self._write_state(root)
            session_file = sessions_dir / "session-1.json"
            session_payload = json.loads(session_file.read_text())
            session_payload["session"]["latest_user_control_message_hash"] = sha1(
                stale_override.encode("utf-8")
            ).hexdigest()
            session_payload["session"]["last_seen_user_control_anchor"] = "msg-user-1"
            session_file.write_text(json.dumps(session_payload))
            adapter = _FakeAdapter(
                assistant_text,
                user_messages=[stale_override, latest_bridge_packet],
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
            self.assertEqual(len(executor.calls), 1)
            self.assertEqual(len(adapter.posted_messages), 1)

    def test_run_once_restores_assistant_checkpoint_when_executor_start_fails(self):
        assistant_text = "\n".join(
            [
                "Continue with the same Codex session.",
                "```bridge-control",
                '{"protocol_version":"1","session_id":"session-1","decision":"run_codex","codex_thread_action":"new_thread","prompt":"Continue the loop runner implementation.","task_label":"loop-runner"}',
                "```",
            ]
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(root, current_codex_run_id="")
            adapter = _FakeAdapter(assistant_text)
            executor = _RaisingExecutor(FileNotFoundError(2, "No such file or directory", "codex"))
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
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["last_seen_chat_message_anchor"], "")
            self.assertEqual(session_payload["latest_assistant_message_hash"], "")
            self.assertIn("No such file or directory", session_payload["human_attention_reason"])

    def test_run_once_pauses_session_on_pause_decision(self):
        assistant_text = "\n".join(
            [
                "Pause requested.",
                "```bridge-control",
                '{"protocol_version":"1","session_id":"session-1","decision":"pause","codex_thread_action":"same_thread","prompt":"Wait for the next human input.","task_label":"pause"}',
                "```",
            ]
        )
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
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

            self.assertEqual(result["policy_outcome"], "paused")
            self.assertEqual(result["loop_state"], "paused")
            self.assertEqual(executor.calls, [])
            session_payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
            self.assertEqual(session_payload["status"], "paused")

    def test_run_once_waits_for_new_assistant_message_when_latest_turn_is_already_processed(self):
        assistant_text = "\n".join(
            [
                "No new assistant turn yet.",
                "```bridge-control",
                '{"protocol_version":"1","session_id":"session-1","decision":"run_codex","codex_thread_action":"same_thread","prompt":"Continue.","task_label":"loop-runner"}',
                "```",
            ]
        )
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
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
            bindings_path, policy_path, sessions_dir = self._write_state(root)
            session_file = sessions_dir / "session-1.json"
            session_payload = json.loads(session_file.read_text())
            session_payload["session"]["last_seen_chat_message_anchor"] = "msg-assistant-1"
            session_payload["session"]["latest_assistant_message_hash"] = sha1(
                assistant_text.encode("utf-8")
            ).hexdigest()
            session_file.write_text(json.dumps(session_payload))

            adapter = _FakeAdapter(assistant_text)
            executor = _FakeExecutor(report)
            runner = LoopRunner(
                adapter=adapter,
                executor=executor,
                bindings_path=bindings_path,
                policy_path=policy_path,
                sessions_dir=sessions_dir,
            )

            result = runner.run_once("session-1", require_new_message=True)

            self.assertEqual(result["runner_action"], "wait_for_chatgpt")
            self.assertEqual(result["loop_state"], "waiting_for_chatgpt")
            self.assertEqual(executor.calls, [])

    def test_run_once_records_pause_command_while_waiting_for_new_turn(self):
        assistant_text = "\n".join(
            [
                "No new assistant turn yet.",
                "```bridge-control",
                '{"protocol_version":"1","session_id":"session-1","decision":"run_codex","codex_thread_action":"same_thread","prompt":"Continue.","task_label":"loop-runner"}',
                "```",
            ]
        )
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
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
            bindings_path, policy_path, sessions_dir = self._write_state(root)
            session_file = sessions_dir / "session-1.json"
            session_payload = json.loads(session_file.read_text())
            session_payload["session"]["last_seen_chat_message_anchor"] = "msg-assistant-1"
            session_payload["session"]["latest_assistant_message_hash"] = sha1(
                assistant_text.encode("utf-8")
            ).hexdigest()
            session_file.write_text(json.dumps(session_payload))

            adapter = _FakeAdapter(
                assistant_text,
                stop_commands=[
                    {
                        "command": "pause",
                        "text": "pause",
                        "message_anchor": "cmd-pause-1",
                    }
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

            result = runner.run_once("session-1", require_new_message=True)

            self.assertEqual(result["policy_outcome"], "paused")
            self.assertEqual(result["loop_state"], "paused")
            session_payload = json.loads(session_file.read_text())["session"]
            self.assertEqual(session_payload["latest_user_control_command"], "pause")
            self.assertEqual(session_payload["last_seen_user_control_anchor"], "cmd-pause-1")
            self.assertEqual(session_payload["status"], "paused")

    def test_run_once_ignores_already_processed_pause_command_anchor_while_waiting(self):
        assistant_text = "\n".join(
            [
                "No new assistant turn yet.",
                "```bridge-control",
                '{"protocol_version":"1","session_id":"session-1","decision":"run_codex","codex_thread_action":"same_thread","prompt":"Continue.","task_label":"loop-runner"}',
                "```",
            ]
        )
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
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
            bindings_path, policy_path, sessions_dir = self._write_state(root)
            session_file = sessions_dir / "session-1.json"
            session_payload = json.loads(session_file.read_text())
            session_payload["session"]["last_seen_chat_message_anchor"] = "msg-assistant-1"
            session_payload["session"]["latest_assistant_message_hash"] = sha1(
                assistant_text.encode("utf-8")
            ).hexdigest()
            session_payload["session"]["last_seen_user_control_anchor"] = "cmd-pause-1"
            session_file.write_text(json.dumps(session_payload))

            adapter = _FakeAdapter(
                assistant_text,
                stop_commands=[
                    {
                        "command": "pause",
                        "text": "pause",
                        "message_anchor": "cmd-pause-1",
                    }
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

            result = runner.run_once("session-1", require_new_message=True)

            self.assertEqual(result["runner_action"], "wait_for_chatgpt")
            self.assertEqual(result["loop_state"], "waiting_for_chatgpt")
            session_payload = json.loads(session_file.read_text())["session"]
            self.assertEqual(session_payload["status"], "active")

    def test_run_once_completes_after_cycle_when_stop_after_cycle_is_requested(self):
        assistant_text = "\n".join(
            [
                "Continue with the same Codex session.",
                "```bridge-control",
                '{"protocol_version":"1","session_id":"session-1","decision":"run_codex","codex_thread_action":"same_thread","prompt":"Continue the loop runner implementation.","task_label":"loop-runner"}',
                "```",
            ]
        )
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-2",
                "summary": "Loop runner executed successfully.",
                "files_touched": ["mastermind_bridge/orchestrator/loop.py"],
                "checks": ["python3 -m unittest discover -s tests"],
                "blockers": [],
                "risks": [],
                "next_step": "Wait for the next ChatGPT turn.",
                "observed_codex_thread_id": "exec-123",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bindings_path, policy_path, sessions_dir = self._write_state(root)
            session_file = sessions_dir / "session-1.json"
            session_payload = json.loads(session_file.read_text())
            session_payload["session"]["stop_after_cycle_requested"] = True
            session_file.write_text(json.dumps(session_payload))

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
            self.assertEqual(result["loop_state"], "completed")
            session_payload = json.loads(session_file.read_text())["session"]
            self.assertEqual(session_payload["status"], "completed")
            self.assertFalse(session_payload["stop_after_cycle_requested"])

    def test_run_once_pauses_without_posting_when_codex_run_is_interrupted(self):
        assistant_text = "\n".join(
            [
                "Continue with the same Codex session.",
                "```bridge-control",
                '{"protocol_version":"1","session_id":"session-1","decision":"run_codex","codex_thread_action":"same_thread","prompt":"Continue the loop runner implementation.","task_label":"loop-runner"}',
                "```",
            ]
        )
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T14:00:00+02:00",
                "thread_id": "thread-2",
                "summary": "Codex run was paused.",
                "files_touched": [],
                "checks": [],
                "blockers": ["Codex run was stopped by control request."],
                "risks": ["Partial changes may need review."],
                "next_step": "Inspect artifacts before resuming.",
                "interruption_reason": "pause_requested",
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

            self.assertEqual(result["policy_outcome"], "paused")
            self.assertEqual(result["loop_state"], "paused")
            self.assertEqual(adapter.posted_messages, [])


if __name__ == "__main__":
    unittest.main()
