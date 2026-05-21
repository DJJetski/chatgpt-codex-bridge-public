from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import threading
import time
from difflib import SequenceMatcher
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse
from uuid import uuid4

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None

_MAX_CONTROL_PANEL_POST_BYTES = 1_048_576

from ..app_paths import bridge_state_dir, codex_home
from ..executor import session_live_log_path
from ..models import now_iso, repo_root
from ..control_panel_runtime import control_panel_runtime_fingerprint
from .browser import RoutedChatAdapter, describe_browser_transport, detect_preferred_browser_channel
from .browser_support import _DEFAULT_POST_ACK_TIMEOUT_MS, assistant_message_looks_like_retryable_error
from .control_panel_view import _normalize_execution_setting, _session_is_running_state, render_dashboard_html
from .contracts import build_bootstrap_prompt as _build_bootstrap_prompt
from .loop_support import (
    _ASSISTANT_STALL_SECONDS,
    _OUTBOUND_USER_MESSAGE_TIMEOUT_SECONDS,
    _clear_delivery_retry_state,
)
from .models import ChatBinding, LoopPolicyDecision, OrchestratorSession, normalize_shell_wrapped_value
from .state import (
    load_chat_bindings,
    load_session,
    list_sessions,
    read_orchestrator_policy,
    save_chat_bindings,
    save_session,
    session_path,
    upsert_chat_binding,
    validate_state_id,
)
from .supervisor import terminate_locked_session_supervisor


_SUPERVISOR_HEARTBEAT_STALE_SECONDS = max(
    60.0,
    _ASSISTANT_STALL_SECONDS,
    _OUTBOUND_USER_MESSAGE_TIMEOUT_SECONDS,
    _DEFAULT_POST_ACK_TIMEOUT_MS / 1000.0,
) + 30.0


class ControlPanelService:
    def __init__(
        self,
        *,
        bindings_path: Path,
        policy_path: Path,
        sessions_dir: Path,
        artifacts_root: Path | None = None,
        supervisor_manager: Any,
        default_repo_path: str | None = None,
        default_workspace_path: str | None = None,
        default_browser_profile_path: str | None = None,
        default_browser_channel: str | None = None,
        default_codex_model: str | None = None,
        default_codex_reasoning_effort: str | None = None,
        preview_adapter_factory: Any | None = None,
    ) -> None:
        self.bindings_path = bindings_path
        self.policy_path = policy_path
        self.sessions_dir = sessions_dir
        self.artifacts_root = Path(artifacts_root) if artifacts_root is not None else sessions_dir.parent / "artifacts" / "runs"
        self.supervisor_manager = supervisor_manager
        self.default_repo_path = str(default_repo_path or repo_root())
        self.default_workspace_path = str(default_workspace_path or self.default_repo_path)
        self.default_browser_profile_path = str(
            default_browser_profile_path or (bridge_state_dir() / "playwright-profile")
        )
        self.default_browser_channel = str(default_browser_channel or detect_preferred_browser_channel())
        self.default_codex_model = _normalize_execution_setting(default_codex_model)
        self.default_codex_reasoning_effort = _normalize_execution_setting(default_codex_reasoning_effort)
        self.preview_adapter_factory = preview_adapter_factory or (lambda: RoutedChatAdapter(headless=False))
        self.server_fingerprint = control_panel_runtime_fingerprint()
        self._preview_adapters: dict[str, Any] = {}
        self._terminal_watchers: dict[str, tuple[threading.Event, threading.Thread]] = {}

    def snapshot(self) -> dict[str, Any]:
        sessions = list_sessions(self.sessions_dir)
        bindings = load_chat_bindings(self.bindings_path)
        bindings_by_id = {binding.binding_id: binding for binding in bindings}
        supervisors = self.supervisor_manager.snapshot()
        sessions.sort(key=lambda session: str(session.updated_at or session.started_at), reverse=True)
        session_payloads = []
        for session in sessions:
            item = session.as_dict()
            binding = bindings_by_id.get(session.binding_id)
            if binding is not None:
                item["browser_transport_mode"] = describe_browser_transport(binding)
            latest_run = _latest_run_metadata(self.artifacts_root, session.session_id)
            if latest_run:
                item["latest_run"] = latest_run
            supervisor = supervisors.get(session.session_id, {})
            session_lock = supervisor.get("lock") if isinstance(supervisor, dict) else None
            if session_lock is not None:
                item["session_lock"] = session_lock
            item["health"] = _session_health_summary(session, session_lock=session_lock, latest_run=latest_run)
            session_payloads.append(item)
        return {
            "bindings": [binding.as_dict() for binding in bindings],
            "sessions": session_payloads,
            "policy": read_orchestrator_policy(self.policy_path),
            "supervisors": supervisors,
            "server_fingerprint": self.server_fingerprint,
        }

    def create_binding(self, payload: dict[str, Any]) -> ChatBinding:
        binding_payload = dict(payload)
        bindings = load_chat_bindings(self.bindings_path)
        canonical_chat_url = _canonical_chat_url(str(binding_payload.get("chat_url", "")))
        binding_payload["chat_url"] = canonical_chat_url
        exact_binding = _binding_for_chat_url(bindings, canonical_chat_url)
        family_binding = _binding_for_chat_family(bindings, canonical_chat_url) if exact_binding is None else None

        if exact_binding is not None:
            binding_payload["binding_id"] = exact_binding.binding_id
        else:
            binding_payload.setdefault("binding_id", _generated_id("binding"))

        resolved_repo_path = normalize_shell_wrapped_value(binding_payload.get("repo_path", ""))
        resolved_workspace_path = normalize_shell_wrapped_value(binding_payload.get("workspace_path", ""))
        resolved_project_name = normalize_shell_wrapped_value(binding_payload.get("project_name", ""))

        if not resolved_repo_path and exact_binding is not None:
            resolved_repo_path = exact_binding.repo_path
        if not resolved_workspace_path and exact_binding is not None:
            resolved_workspace_path = exact_binding.workspace_path
        if not resolved_project_name and exact_binding is not None:
            resolved_project_name = exact_binding.project_name

        if not resolved_repo_path:
            inferred_repo = _infer_repo_path_for_chat_url(
                canonical_chat_url,
                default_repo_path=Path(self.default_repo_path),
            )
            if inferred_repo is not None:
                resolved_repo_path = str(inferred_repo)
                resolved_workspace_path = resolved_workspace_path or str(inferred_repo)
                resolved_project_name = resolved_project_name or inferred_repo.name

        if not resolved_repo_path and family_binding is not None:
            resolved_repo_path = family_binding.repo_path
        if not resolved_workspace_path and family_binding is not None:
            resolved_workspace_path = family_binding.workspace_path
        if not resolved_project_name and family_binding is not None:
            resolved_project_name = family_binding.project_name

        binding_payload["repo_path"] = normalize_shell_wrapped_value(resolved_repo_path or self.default_repo_path)
        binding_payload["workspace_path"] = normalize_shell_wrapped_value(
            resolved_workspace_path or binding_payload["repo_path"] or self.default_workspace_path
        )
        binding_payload["project_name"] = resolved_project_name or Path(binding_payload["repo_path"]).name
        if not str(binding_payload.get("browser_profile_path", "")).strip():
            binding_payload["browser_profile_path"] = self.default_browser_profile_path
        if not str(binding_payload.get("browser_channel", "")).strip() and self.default_browser_channel:
            binding_payload["browser_channel"] = self.default_browser_channel
        if not str(binding_payload.get("browser_session_handle", "")).strip():
            inherited_handle = ""
            if exact_binding is not None:
                inherited_handle = exact_binding.browser_session_handle
            elif family_binding is not None:
                inherited_handle = family_binding.browser_session_handle
            binding_payload["browser_session_handle"] = inherited_handle or "default"
        binding = ChatBinding.from_dict(binding_payload)
        upsert_chat_binding(self.bindings_path, binding)
        return binding

    def create_session(self, payload: dict[str, Any]) -> OrchestratorSession:
        binding_id = str(payload.get("binding_id", ""))
        bindings = load_chat_bindings(self.bindings_path)
        binding = next((item for item in bindings if item.binding_id == binding_id), None)
        if binding is None:
            raise ValueError(f"Unknown binding_id: {binding_id}")

        time_budget_minutes = int(payload.get("time_budget_minutes", 0) or 0)
        if time_budget_minutes <= 0:
            raise ValueError("An explicit time budget is required.")

        seed_codex_thread_id = _seed_codex_thread_id(
            payload=payload,
            binding=binding,
            sessions_dir=self.sessions_dir,
        )

        session = OrchestratorSession(
            session_id=str(payload.get("session_id") or payload.get("binding_id") or ""),
            binding_id=binding.binding_id,
            repo_path=binding.repo_path,
            workspace_path=binding.workspace_path,
            chat_url=binding.chat_url,
            time_budget_minutes=time_budget_minutes,
            budget_remaining_minutes=time_budget_minutes,
            codex_model=_normalize_execution_setting(payload.get("codex_model", self.default_codex_model)),
            codex_reasoning_effort=_normalize_execution_setting(
                payload.get("codex_reasoning_effort", self.default_codex_reasoning_effort)
            ),
            current_codex_thread_id=seed_codex_thread_id,
            current_codex_run_id=seed_codex_thread_id,
            phase_started_at=now_iso(),
            policy_decision=LoopPolicyDecision(
                policy_outcome="allow",
                reasons=["Explicit time budget provided."],
                time_budget_minutes=time_budget_minutes,
                time_budget_remaining_minutes=time_budget_minutes,
            ),
        )
        save_session(session_path(self.sessions_dir, session.session_id), session)
        binding.last_session_id = session.session_id
        upsert_chat_binding(self.bindings_path, binding)
        return session

    def quickstart_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        chat_url = str(payload.get("chat_url", "")).strip()
        if not chat_url:
            raise ValueError("A ChatGPT chat URL is required.")

        time_budget_minutes = int(payload.get("time_budget_minutes", 0) or 0)
        if time_budget_minutes <= 0:
            raise ValueError("An explicit time budget is required.")

        binding_payload = {
            "binding_id": str(payload.get("binding_id") or _generated_id("binding")),
            "chat_url": chat_url,
            "browser_profile_path": self.default_browser_profile_path,
            "browser_channel": self.default_browser_channel,
            "browser_session_handle": str(payload.get("browser_session_handle") or "default"),
        }
        for key in ("project_name", "repo_path", "workspace_path", "seed_codex_thread_id", "seed_codex_thread_title"):
            value = str(payload.get(key, "") or "").strip()
            if value:
                binding_payload[key] = value

        binding = self.create_binding(binding_payload)
        session = self.create_session(
            {
                "session_id": str(payload.get("session_id") or _generated_id("session")),
                "binding_id": binding.binding_id,
                "time_budget_minutes": time_budget_minutes,
                "seed_codex_thread_id": str(payload.get("seed_codex_thread_id", "") or "").strip(),
                "seed_codex_thread_title": str(payload.get("seed_codex_thread_title", "") or "").strip(),
                "codex_model": _normalize_execution_setting(payload.get("codex_model", self.default_codex_model)),
                "codex_reasoning_effort": _normalize_execution_setting(
                    payload.get("codex_reasoning_effort", self.default_codex_reasoning_effort)
                ),
            }
        )
        return {
            "binding": binding.as_dict(),
            "session": session.as_dict(),
            "bootstrap_prompt": build_bootstrap_prompt(session),
        }

    def update_session_execution_settings(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        path = session_path(self.sessions_dir, session_id)
        session = load_session(path)
        session.codex_model = _normalize_execution_setting(payload.get("codex_model", session.codex_model))
        session.codex_reasoning_effort = _normalize_execution_setting(
            payload.get("codex_reasoning_effort", session.codex_reasoning_effort)
        )
        save_session(path, session)
        return {
            "session_id": session.session_id,
            "status": "updated",
            "codex_model": session.codex_model,
            "codex_reasoning_effort": session.codex_reasoning_effort,
        }

    def start_session(
        self,
        session_id: str,
        *,
        single_cycle: bool = False,
        stop_before_return_packet: bool = False,
    ) -> dict[str, str]:
        self._close_preview_adapter(session_id)
        self._cancel_terminal_watcher(session_id)
        session = load_session(session_path(self.sessions_dir, session_id))
        session.status = "active"
        session.auto_run_enabled = True
        session.supervisor_status = "running"
        session.stop_after_cycle_requested = bool(single_cycle)
        session.stop_before_return_packet_requested = bool(stop_before_return_packet)
        session.human_attention_reason = ""
        session.last_error = ""
        if _should_rearm_latest_assistant_message(session):
            session.last_seen_chat_message_anchor = ""
            session.latest_assistant_message_id = ""
            session.latest_assistant_message_hash = ""
        save_session(session_path(self.sessions_dir, session.session_id), session)
        try:
            result = self.supervisor_manager.ensure_session(session_id)
        except Exception as exc:
            self._mark_session_blocked(session_id, str(exc))
            raise ValueError(str(exc)) from exc
        self._schedule_terminal_auto_open(session.session_id)
        if _codex_app_auto_open_enabled():
            self._open_existing_codex_app_thread(session)
        return result

    def pause_session(self, session_id: str) -> OrchestratorSession:
        self._close_preview_adapter(session_id)
        self._cancel_terminal_watcher(session_id)
        session = load_session(session_path(self.sessions_dir, session_id))
        if session.loop_state in {"starting_codex", "posting_return_packet", "waiting_for_chatgpt_response"}:
            session.latest_user_control_command = "pause"
            session.auto_run_enabled = True
            session.supervisor_status = "running"
            reasons = ["Pause requested from the control panel; the active turn will drain before pausing."]
        else:
            session.status = "paused"
            session.loop_state = "paused"
            session.auto_run_enabled = False
            session.supervisor_status = "paused"
            _clear_delivery_retry_state(session)
            reasons = ["Pause requested from the control panel."]
        session.policy_decision = LoopPolicyDecision(
            policy_outcome="paused",
            reasons=reasons,
            time_budget_minutes=session.time_budget_minutes,
            time_budget_remaining_minutes=session.budget_remaining_minutes,
        )
        save_session(session_path(self.sessions_dir, session.session_id), session)
        if session.status == "paused":
            self.supervisor_manager.stop_session(session_id)
        return session

    def resume_session(
        self,
        session_id: str,
        *,
        single_cycle: bool = False,
        stop_before_return_packet: bool = False,
    ) -> dict[str, str]:
        self._cancel_terminal_watcher(session_id)
        session = load_session(session_path(self.sessions_dir, session_id))
        session.status = "active"
        session.loop_state = "idle"
        session.auto_run_enabled = True
        session.supervisor_status = "running"
        session.latest_user_control_command = ""
        session.stop_after_cycle_requested = bool(single_cycle)
        session.stop_before_return_packet_requested = bool(stop_before_return_packet)
        session.human_attention_reason = ""
        session.last_error = ""
        session.policy_decision = LoopPolicyDecision(
            policy_outcome="allow",
            reasons=[
                (
                    "Single-send resume requested from the control panel; the supervisor will stop before posting back to ChatGPT."
                    if stop_before_return_packet
                    else (
                        "Single-turn resume requested from the control panel; the supervisor will stop after one completed pong."
                        if single_cycle
                        else "Resume requested from the control panel; supervisor restart in progress."
                    )
                )
            ],
            time_budget_minutes=session.time_budget_minutes,
            time_budget_remaining_minutes=session.budget_remaining_minutes,
        )
        if _should_rearm_latest_assistant_message(session):
            session.last_seen_chat_message_anchor = ""
            session.latest_assistant_message_id = ""
            session.latest_assistant_message_hash = ""
        save_session(session_path(self.sessions_dir, session.session_id), session)
        try:
            result = self.supervisor_manager.ensure_session(session_id)
        except Exception as exc:
            self._mark_session_blocked(session_id, str(exc))
            raise ValueError(str(exc)) from exc
        self._schedule_terminal_auto_open(session.session_id)
        if _codex_app_auto_open_enabled():
            self._open_existing_codex_app_thread(session)
        return result

    def stop_session(self, session_id: str, *, after_cycle: bool) -> OrchestratorSession:
        if not after_cycle:
            self._close_preview_adapter(session_id)
            self._cancel_terminal_watcher(session_id)
        session = load_session(session_path(self.sessions_dir, session_id))
        if after_cycle or session.loop_state in {"starting_codex", "posting_return_packet", "waiting_for_chatgpt_response"}:
            session.latest_user_control_command = "stop"
            session.stop_after_cycle_requested = True
            session.auto_run_enabled = True
            session.supervisor_status = "running"
        else:
            session.stop_after_cycle_requested = False
            session.status = "completed"
            session.auto_run_enabled = False
            session.supervisor_status = "stopped"
        save_session(session_path(self.sessions_dir, session.session_id), session)
        if not after_cycle and session.status == "completed":
            self.supervisor_manager.stop_session(session_id)
            terminate_locked_session_supervisor(self.sessions_dir.parent / "session_locks", session_id)
        return session

    def delete_session(self, session_id: str) -> dict[str, str]:
        safe_session_id = validate_state_id(session_id, label="session_id")
        self._close_preview_adapter(safe_session_id)
        self._cancel_terminal_watcher(safe_session_id)
        path = session_path(self.sessions_dir, safe_session_id)
        session = load_session(path)
        if _session_is_running_state(
            status=session.status,
            supervisor_status=session.supervisor_status or session.loop_state,
            auto_run_enabled=session.auto_run_enabled,
        ):
            raise ValueError("Stop the session before deleting it.")
        self.supervisor_manager.stop_session(safe_session_id)
        # codeql[py/path-injection]
        if path.exists():
            # codeql[py/path-injection]
            path.unlink()
        self._delete_session_sidecars(safe_session_id)
        bindings = load_chat_bindings(self.bindings_path)
        updated = False
        for binding in bindings:
            if binding.last_session_id == safe_session_id:
                binding.last_session_id = ""
                updated = True
        if updated:
            save_chat_bindings(self.bindings_path, bindings)
        return {"session_id": safe_session_id, "status": "deleted"}

    def open_chat_preview(self, session_id: str) -> dict[str, str]:
        self._close_preview_adapter(session_id)
        session = load_session(session_path(self.sessions_dir, session_id))
        bindings = load_chat_bindings(self.bindings_path)
        binding = next((item for item in bindings if item.binding_id == session.binding_id), None)
        if binding is None:
            raise ValueError(f"Unknown binding_id: {session.binding_id}")
        adapter = self.preview_adapter_factory()
        adapter.open_chat(binding)
        self._preview_adapters[session_id] = adapter
        return {"session_id": session_id, "status": "opened", "chat_url": binding.chat_url}

    def open_latest_run(self, session_id: str) -> dict[str, str]:
        latest_run = _latest_run_metadata(self.artifacts_root, session_id)
        if not latest_run:
            raise ValueError(f"No run artifacts found for session {session_id}.")
        artifacts_dir = str(latest_run["artifacts_dir"])
        subprocess.run(["open", artifacts_dir], check=True)
        return {
            "session_id": session_id,
            "status": "opened",
            "artifacts_dir": artifacts_dir,
        }

    def open_latest_codex_thread(self, session_id: str) -> dict[str, str]:
        session = load_session(session_path(self.sessions_dir, session_id))
        repo_path = str(session.workspace_path or session.repo_path or repo_root())
        payload = _open_codex_live_monitor(
            session_id=session_id,
            repo_path=repo_path,
            artifacts_root=self.artifacts_root,
        )
        return {"session_id": session_id, "status": "opened", **payload}

    def open_latest_codex_app_thread(self, session_id: str) -> dict[str, str]:
        session = load_session(session_path(self.sessions_dir, session_id))
        latest_run = _latest_run_metadata(self.artifacts_root, session_id)
        thread_id = _latest_codex_thread_id(session, latest_run)
        if not thread_id:
            raise ValueError(f"No Codex thread is recorded for session {session_id}.")
        _open_codex_app_thread(thread_id)
        return {
            "session_id": session_id,
            "status": "opened",
            "thread_id": thread_id,
            "deeplink": _codex_thread_deeplink(thread_id),
        }

    def _open_existing_codex_app_thread(self, session: OrchestratorSession) -> None:
        thread_id = str(session.current_codex_thread_id or session.current_codex_run_id or "").strip()
        if not thread_id:
            return
        try:
            _open_codex_app_thread(thread_id)
        except (OSError, subprocess.SubprocessError):
            return

    def _close_preview_adapter(self, session_id: str) -> None:
        adapter = self._preview_adapters.pop(session_id, None)
        if adapter is None:
            return
        close = getattr(adapter, "close", None)
        if callable(close):
            close()

    def _cancel_terminal_watcher(self, session_id: str) -> None:
        watcher = self._terminal_watchers.pop(session_id, None)
        if watcher is None:
            return
        stop_event, thread = watcher
        stop_event.set()
        thread.join(timeout=1.0)

    def _schedule_terminal_auto_open(self, session_id: str) -> None:
        session = load_session(session_path(self.sessions_dir, session_id))
        try:
            _open_codex_live_monitor(
                session_id=session_id,
                repo_path=str(session.workspace_path or session.repo_path or repo_root()),
                artifacts_root=self.artifacts_root,
            )
        except (OSError, subprocess.SubprocessError):
            return

    def _mark_session_blocked(self, session_id: str, message: str) -> None:
        self._cancel_terminal_watcher(session_id)
        session = load_session(session_path(self.sessions_dir, session_id))
        session.status = "blocked"
        session.auto_run_enabled = False
        session.supervisor_status = "blocked"
        session.loop_state = "requires_human"
        session.human_attention_reason = message
        session.last_error = message
        session.policy_decision = LoopPolicyDecision(
            policy_outcome="require_human",
            reasons=[message],
            human_gate_required=True,
            human_gate_reason=message,
            human_gate_category="runtime_start_failure",
            time_budget_minutes=session.time_budget_minutes,
            time_budget_remaining_minutes=session.budget_remaining_minutes,
        )
        save_session(session_path(self.sessions_dir, session.session_id), session)

    def _delete_session_sidecars(self, session_id: str) -> None:
        safe_session_id = validate_state_id(session_id, label="session_id")
        runtime_prompt_dir = self.sessions_dir.parent / "runtime_prompts" / safe_session_id
        # codeql[py/path-injection]
        if runtime_prompt_dir.exists():
            # codeql[py/path-injection]
            shutil.rmtree(runtime_prompt_dir, ignore_errors=True)
        session_lock_path = session_path(self.sessions_dir.parent / "session_locks", safe_session_id)
        if session_lock_path.exists():
            try:
                session_lock_path.unlink()
            except OSError:
                pass

    def render_dashboard(self) -> str:
        state = self.snapshot()
        return render_dashboard_html(
            sessions=state["sessions"],
            default_browser_channel=self.default_browser_channel,
            default_codex_model=self.default_codex_model,
            default_codex_reasoning_effort=self.default_codex_reasoning_effort,
        )


class ControlPanelServer(ThreadingHTTPServer):
    def __init__(self, *, service: ControlPanelService, host: str, port: int) -> None:
        self.service = service
        super().__init__((host, port), _make_handler(service, self))


def _make_handler(service: ControlPanelService, server: ControlPanelServer):
    class ControlPanelRequestHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._write_response(200, service.render_dashboard(), "text/html; charset=utf-8")
                return
            if parsed.path == "/api/state":
                self._write_json(200, service.snapshot())
                return
            self._write_json(404, {"error": "Not found"})

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            try:
                payload = self._read_json()
                if parsed.path == "/api/control/shutdown":
                    self._write_json(200, {"status": "shutting_down"})
                    threading.Thread(target=server.shutdown, daemon=True).start()
                    return
                if parsed.path == "/api/bindings":
                    self._write_json(200, service.create_binding(payload).as_dict())
                    return
                if parsed.path == "/api/sessions":
                    self._write_json(200, service.create_session(payload).as_dict())
                    return
                if parsed.path == "/api/quickstart":
                    self._write_json(200, service.quickstart_session(payload))
                    return
                if parsed.path.startswith("/api/sessions/"):
                    self._write_json(200, _handle_session_action(service, parsed.path, payload))
                    return
            except ValueError as exc:
                self._write_json(400, {"error": str(exc)})
                return
            self._write_json(404, {"error": "Not found"})

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return

        def _read_json(self) -> dict[str, Any]:
            try:
                content_length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError as exc:
                raise ValueError("Invalid Content-Length header") from exc
            if content_length <= 0:
                return {}
            if content_length > _MAX_CONTROL_PANEL_POST_BYTES:
                raise ValueError("Request body is too large")
            body = self.rfile.read(content_length).decode("utf-8")
            if not body.strip():
                return {}
            return json.loads(body)

        def _write_json(self, status_code: int, payload: dict[str, Any]) -> None:
            self._write_response(status_code, json.dumps(payload), "application/json; charset=utf-8")

        def _write_response(self, status_code: int, body: str, content_type: str) -> None:
            data = body.encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.end_headers()
            self.wfile.write(data)

    return ControlPanelRequestHandler


def _handle_session_action(
    service: ControlPanelService,
    path: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    parts = [part for part in path.split("/") if part]
    if len(parts) != 4:
        raise ValueError(f"Unsupported session action path: {path}")
    session_id = parts[2]
    action = parts[3]
    if action == "start":
        return service.start_session(
            session_id,
            single_cycle=bool(payload.get("single_cycle", False)),
            stop_before_return_packet=bool(payload.get("stop_before_return_packet", False)),
        )
    if action == "pause":
        return service.pause_session(session_id).as_dict()
    if action == "resume":
        return service.resume_session(
            session_id,
            single_cycle=bool(payload.get("single_cycle", False)),
            stop_before_return_packet=bool(payload.get("stop_before_return_packet", False)),
        )
    if action == "open-chat":
        return service.open_chat_preview(session_id)
    if action == "open-run":
        return service.open_latest_run(session_id)
    if action == "open-codex-thread":
        return service.open_latest_codex_thread(session_id)
    if action == "open-codex-app-thread":
        return service.open_latest_codex_app_thread(session_id)
    if action == "execution-config":
        return service.update_session_execution_settings(session_id, payload)
    if action == "stop":
        return service.stop_session(session_id, after_cycle=bool(payload.get("after_cycle", False))).as_dict()
    if action == "delete":
        return service.delete_session(session_id)
    raise ValueError(f"Unsupported session action: {action}")


def _generated_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:8]}"


def _should_rearm_latest_assistant_message(session: OrchestratorSession) -> bool:
    if (
        session.cycles_completed == 0
        and not (session.current_codex_thread_id or session.current_codex_run_id)
        and not session.last_posted_return_packet_id
    ):
        return True
    has_latest_assistant = any(
        str(value or "").strip()
        for value in (
            session.last_seen_chat_message_anchor,
            session.latest_assistant_message_id,
            session.latest_assistant_message_hash,
        )
    )
    if not has_latest_assistant:
        return False
    if (
        str(session.last_productive_task_label or "").strip() == "accepted_assistant_text"
        and assistant_message_looks_like_retryable_error(str(session.last_productive_prompt or ""))
    ):
        return True
    if str(session.last_outbound_user_message_anchor or "").strip() or str(session.last_outbound_user_message_kind or "").strip():
        return False
    last_chat_seconds = _parse_timestamp(session.last_chat_activity_at)
    if last_chat_seconds <= 0:
        return False
    last_delivery_seconds = _parse_timestamp(session.last_delivery_at)
    return last_delivery_seconds <= 0 or last_chat_seconds > last_delivery_seconds

def build_bootstrap_prompt(session: OrchestratorSession) -> str:
    return _build_bootstrap_prompt(session)


def _canonical_chat_url(chat_url: str) -> str:
    raw = str(chat_url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    path = re.sub(r"/+", "/", parsed.path or "").rstrip("/")
    return urlunparse(
        (
            str(parsed.scheme or "https").casefold(),
            str(parsed.netloc or "").casefold(),
            path,
            "",
            "",
            "",
        )
    )


def _chat_family_key(chat_url: str) -> str:
    parsed = urlparse(_canonical_chat_url(chat_url))
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) >= 2 and segments[0] == "g":
        return f"g:{segments[1]}"
    return ""


def _binding_for_chat_url(bindings: list[ChatBinding], chat_url: str) -> ChatBinding | None:
    canonical = _canonical_chat_url(chat_url)
    for binding in bindings:
        if _canonical_chat_url(binding.chat_url) == canonical:
            return binding
    return None


def _binding_for_chat_family(bindings: list[ChatBinding], chat_url: str) -> ChatBinding | None:
    family_key = _chat_family_key(chat_url)
    if not family_key:
        return None
    candidates = [
        binding
        for binding in bindings
        if _chat_family_key(binding.chat_url) == family_key
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda item: str(item.updated_at or item.created_at), reverse=True)
    return candidates[0]


def _normalized_slug(text: str) -> str:
    tokens = re.findall(r"[a-z0-9]+", str(text or "").casefold())
    return "-".join(tokens)


def _normalized_tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", str(text or "").casefold())


def _chat_slug_candidates(chat_url: str) -> list[str]:
    parsed = urlparse(_canonical_chat_url(chat_url))
    segments = [segment for segment in parsed.path.split("/") if segment]
    candidates: list[str] = []
    if len(segments) >= 2 and segments[0] == "g":
        gpt_segment = segments[1]
        candidates.append(gpt_segment)
        match = re.match(r"^g-p-[0-9a-f]+-(.+)$", gpt_segment)
        if match is not None:
            candidates.append(match.group(1))
    for candidate in list(candidates):
        normalized = _normalized_slug(candidate)
        if normalized and normalized not in candidates:
            candidates.append(normalized)
    return [candidate for candidate in candidates if candidate]


def _candidate_repo_paths(default_repo_path: Path) -> list[Path]:
    candidates: dict[str, Path] = {}
    parent = default_repo_path.parent
    if parent.exists():
        for path in parent.iterdir():
            if path.is_dir() and not path.name.startswith("."):
                candidates[_normalized_slug(path.name) or str(path)] = path
    config_path = codex_home() / "config.toml"
    if tomllib is not None and config_path.exists():
        try:
            payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):  # pragma: no cover - defensive
            payload = {}
        for path_text in (payload.get("projects") or {}).keys():
            path = Path(str(path_text)).expanduser()
            if path.exists() and path.is_dir():
                candidates.setdefault(_normalized_slug(path.name) or str(path), path)
    return sorted(candidates.values(), key=lambda path: str(path))


def _repo_match_score(chat_slug: str, repo_name: str) -> float:
    normalized_slug = _normalized_slug(chat_slug)
    normalized_repo = _normalized_slug(repo_name)
    if not normalized_slug or not normalized_repo:
        return 0.0
    if normalized_slug == normalized_repo:
        return 1.0
    full_ratio = SequenceMatcher(a=normalized_slug, b=normalized_repo).ratio()
    slug_tokens = _normalized_tokens(chat_slug)
    repo_tokens = _normalized_tokens(repo_name)
    if not slug_tokens or not repo_tokens:
        return full_ratio
    matched_scores: list[float] = []
    for slug_token in slug_tokens:
        best_token_score = 0.0
        for repo_token in repo_tokens:
            if slug_token == repo_token:
                best_token_score = 1.0
                break
            token_ratio = SequenceMatcher(a=slug_token, b=repo_token).ratio()
            if slug_token in repo_token or repo_token in slug_token:
                token_ratio = max(token_ratio, 0.88)
            best_token_score = max(best_token_score, token_ratio)
        matched_scores.append(best_token_score)
    token_ratio = sum(matched_scores) / len(matched_scores)
    return max(full_ratio, token_ratio)


def _infer_repo_path_for_chat_url(chat_url: str, *, default_repo_path: Path) -> Path | None:
    best_match: tuple[float, Path] | None = None
    second_best_score = 0.0
    for slug in _chat_slug_candidates(chat_url):
        for candidate in _candidate_repo_paths(default_repo_path):
            score = _repo_match_score(slug, candidate.name)
            if best_match is None or score > best_match[0]:
                if best_match is not None:
                    second_best_score = max(second_best_score, best_match[0])
                best_match = (score, candidate)
            else:
                second_best_score = max(second_best_score, score)
    if best_match is None:
        return None
    if best_match[0] < 0.72:
        return None
    if second_best_score and (best_match[0] - second_best_score) < 0.08:
        return None
    return best_match[1]


def _seed_codex_thread_id(*, payload: dict[str, Any], binding: ChatBinding, sessions_dir: Path) -> str:
    explicit_thread_id = str(
        payload.get("current_codex_thread_id")
        or payload.get("seed_codex_thread_id")
        or ""
    ).strip()
    if explicit_thread_id:
        return explicit_thread_id
    explicit_title = str(
        payload.get("current_codex_thread_title")
        or payload.get("seed_codex_thread_title")
        or ""
    ).strip()
    if explicit_title:
        resolved = _find_codex_thread_id_by_title(binding.workspace_path or binding.repo_path, explicit_title)
        if resolved:
            return resolved
    if binding.last_session_id:
        prior_path = session_path(sessions_dir, binding.last_session_id)
        if prior_path.exists():
            prior_session = load_session(prior_path)
            prior_thread_id = str(prior_session.current_codex_thread_id or prior_session.current_codex_run_id or "").strip()
            if prior_thread_id:
                return prior_thread_id
    sessions = list_sessions(sessions_dir)
    sessions.sort(key=lambda session: str(session.updated_at or session.started_at), reverse=True)
    for candidate in sessions:
        if candidate.repo_path != binding.repo_path and candidate.workspace_path != binding.workspace_path:
            continue
        prior_thread_id = str(candidate.current_codex_thread_id or candidate.current_codex_run_id or "").strip()
        if prior_thread_id:
            return prior_thread_id
    return ""


def _find_codex_thread_id_by_title(repo_path: str, title_hint: str) -> str:
    state_db = codex_home() / "state_5.sqlite"
    if not state_db.exists():
        return ""
    normalized_hint = _normalized_slug(title_hint)
    if not normalized_hint:
        return ""
    try:
        connection = sqlite3.connect(state_db)
        try:
            rows = connection.execute(
                "select id, title from threads where cwd = ? and archived = 0 order by updated_at desc",
                (str(repo_path),),
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:  # pragma: no cover - defensive
        return ""
    best_match: tuple[float, str] | None = None
    second_best_score = 0.0
    for thread_id, title in rows:
        score = _repo_match_score(normalized_hint, str(title or ""))
        if best_match is None or score > best_match[0]:
            if best_match is not None:
                second_best_score = max(second_best_score, best_match[0])
            best_match = (score, str(thread_id))
        else:
            second_best_score = max(second_best_score, score)
    if best_match is None or best_match[0] < 0.72:
        return ""
    if second_best_score and (best_match[0] - second_best_score) < 0.08:
        return ""
    return best_match[1]


def _parse_timestamp(value: str) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return _iso_to_epoch_seconds(text)
    except ValueError:
        return 0.0


def _iso_to_epoch_seconds(value: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def _session_health_summary(
    session: OrchestratorSession,
    *,
    session_lock: dict[str, Any] | None = None,
    latest_run: dict[str, str] | None = None,
) -> dict[str, Any]:
    now_seconds = time.time()
    heartbeat_seconds = _parse_timestamp(session.supervisor_heartbeat_at)
    phase_seconds = _parse_timestamp(session.phase_started_at)
    last_chat_seconds = _parse_timestamp(session.last_chat_activity_at)
    last_codex_seconds = _parse_timestamp(session.last_codex_activity_at)
    last_delivery_seconds = _parse_timestamp(session.last_delivery_at)
    latest_progress_seconds = max(last_chat_seconds, last_codex_seconds, last_delivery_seconds)
    codex_progress_age = max(0.0, now_seconds - last_codex_seconds) if last_codex_seconds else -1.0
    codex_progress_in_current_phase = bool(phase_seconds > 0 and last_codex_seconds >= phase_seconds)
    heartbeat_age = max(0.0, now_seconds - heartbeat_seconds) if heartbeat_seconds else -1.0
    phase_age = max(0.0, now_seconds - phase_seconds) if phase_seconds else -1.0
    latest_progress_age = max(0.0, now_seconds - latest_progress_seconds) if latest_progress_seconds else -1.0
    status = "inactive"
    reason = ""
    if session.supervisor_status in {"blocked", "failed"} or session.loop_state == "requires_human":
        if latest_progress_seconds > 0 and latest_progress_age >= 1800.0:
            status = "stalled"
            reason = (
                "Session has been blocked without any new ChatGPT, Codex, or delivery progress for over 30 minutes. "
                "Active intervention is required; do not keep passively monitoring the same blocker."
            )
        else:
            status = "blocked"
            reason = session.human_attention_reason or session.last_error or "Manual attention is required."
    elif session.status == "active" and session.auto_run_enabled:
        lock_pid_alive = bool(session_lock.get("pid_alive", True)) if isinstance(session_lock, dict) else True
        latest_run_status = str(latest_run.get("status", "") if isinstance(latest_run, dict) else "").strip()
        compaction_started_seconds = _parse_timestamp(
            str(latest_run.get("compaction_started_at", "") if isinstance(latest_run, dict) else "")
        )
        compaction_age = (
            max(0.0, now_seconds - compaction_started_seconds) if compaction_started_seconds > 0 else -1.0
        )
        if isinstance(session_lock, dict) and not lock_pid_alive:
            status = "stalled"
            reason = (
                "Session is still marked active, but the supervisor lock belongs to a dead pid. "
                "Active intervention is required; do not keep passively monitoring the same blocker."
            )
        elif latest_run_status == "compacting":
            status = "post_run_pending"
            reason = (
                "Codex finished successfully and post-run compaction is still running. "
                "Verify the Codex app-server is alive and do not deliver the return packet until compaction completes."
            )
            if compaction_age >= 900.0:
                status = "suspected_hang"
                reason = (
                    "Post-run compaction has been running for over 15 minutes without completion. "
                    "Inspect the app-server, compaction method, and run_report before continuing delivery."
                )
        elif latest_run_status == "pending_delivery":
            status = "post_run_pending"
            reason = (
                "Codex finished and the latest run is waiting for return-packet delivery. "
                "Do not treat the session as fully healthy until the packet is posted to the bound ChatGPT chat."
            )
            if latest_progress_seconds > 0 and latest_progress_age >= 900.0:
                status = "suspected_hang"
                reason = (
                    "The latest Codex run is complete but return-packet delivery has not progressed for over 15 minutes. "
                    "Inspect delivery status and stale-chat preflight before starting another Codex turn."
                )
        elif heartbeat_seconds <= 0:
            status = "starting"
            reason = "Session is active but no supervisor heartbeat has been recorded yet."
        elif heartbeat_age >= _SUPERVISOR_HEARTBEAT_STALE_SECONDS:
            waiting_after_packet = (
                session.loop_state == "waiting_for_chatgpt_response"
                and str(session.last_outbound_user_message_kind or "") == "return_packet"
                and last_delivery_seconds > heartbeat_seconds
                and last_delivery_seconds >= phase_seconds
            )
            if waiting_after_packet and latest_progress_age < 3600.0:
                status = "waiting_for_chatgpt"
                reason = (
                    "Return packet was delivered and the session is waiting for ChatGPT. "
                    "The supervisor heartbeat is stale, so the next monitor pass should poll the browser before "
                    "treating this as a hung Codex run."
                )
            else:
                status = "suspected_hang"
                reason = "Supervisor heartbeat is stale while the session is still marked active."
        elif session.loop_state == "starting_codex" and phase_age >= 60.0 and not codex_progress_in_current_phase:
            status = "running_quiet"
            reason = (
                "Codex was launched for this phase, but no new Codex output has been recorded yet. "
                "The supervisor is alive; verify the live run log before treating this as real progress."
            )
            if phase_age >= 300.0:
                status = "suspected_hang"
                reason = (
                    "Codex was launched for this phase, but no new Codex output has been recorded for over 5 minutes. "
                    "The supervisor is alive, but the run may be stuck before producing usable output."
                )
        elif session.loop_state == "starting_codex" and codex_progress_age >= 300.0:
            status = "running_quiet"
            reason = (
                "Codex is still marked active, but no new Codex output has been recorded for over 5 minutes. "
                "The supervisor is alive; inspect the live run log before assuming the run is productive."
            )
            if codex_progress_age >= 900.0:
                status = "suspected_hang"
                reason = (
                    "Codex is still marked active, but no new Codex output has been recorded for over 15 minutes. "
                    "The run may be stuck even though the supervisor heartbeat is fresh."
                )
        elif latest_progress_seconds > 0 and latest_progress_age >= 1200.0:
            status = "stalled"
            reason = (
                "Session is still marked active, but no new ChatGPT, Codex, or delivery progress has been recorded "
                "for over 20 minutes. Active intervention is required."
            )
        else:
            status = "healthy"
            reason = "Supervisor heartbeat is fresh."
    return {
        "status": status,
        "reason": reason,
        "heartbeat_age_seconds": int(heartbeat_age) if heartbeat_age >= 0 else -1,
        "phase_age_seconds": int(phase_age) if phase_age >= 0 else -1,
        "codex_progress_age_seconds": int(codex_progress_age) if codex_progress_age >= 0 else -1,
        "latest_progress_age_seconds": int(latest_progress_age) if latest_progress_age >= 0 else -1,
    }

def _latest_run_metadata(artifacts_root: Path, session_id: str) -> dict[str, str] | None:
    safe_session_id = validate_state_id(session_id, label="session_id")
    runs_root = artifacts_root
    if not runs_root.exists():
        return None
    candidates = sorted(
        (path for path in runs_root.iterdir() if path.is_dir() and path.name.endswith(f"-{safe_session_id}")),
        reverse=True,
    )
    if not candidates:
        return None
    run_dir = candidates[0]
    report_path = run_dir / "run_report.json"
    metadata = {
        "artifacts_dir": str(run_dir),
        "status": "running",
        "summary": "",
        "next_step": "",
        "codex_thread_id": "",
        "observed_codex_thread_id": "",
        "thread_action": "",
        "thread_operation": "",
        "estimated_context_remaining_percent": "-1",
        "context_continuity_percent": "-1",
        "continuity_band": "",
        "delivery_status": "",
        "return_packet_id": "",
        "compaction_status": "",
        "compaction_started_at": "",
        "compaction_completed_at": "",
        "compaction_error": "",
        "degraded_mode": "",
        "final_output_preview": "",
    }
    if not report_path.exists():
        return metadata
    try:
        payload = json.loads(report_path.read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        return metadata
    metadata["status"] = "completed"
    metadata["summary"] = str(payload.get("summary", ""))
    metadata["next_step"] = str(payload.get("next_step", ""))
    metadata["codex_thread_id"] = str(payload.get("codex_thread_id", ""))
    metadata["observed_codex_thread_id"] = str(payload.get("observed_codex_thread_id", ""))
    metadata["thread_action"] = str(payload.get("thread_action", ""))
    metadata["thread_operation"] = str(payload.get("thread_operation", ""))
    metadata["estimated_context_remaining_percent"] = str(payload.get("estimated_context_remaining_percent", -1))
    metadata["context_continuity_percent"] = str(payload.get("context_continuity_percent", -1))
    metadata["continuity_band"] = str(payload.get("continuity_band", ""))
    metadata["delivery_status"] = str(payload.get("delivery_status", ""))
    metadata["return_packet_id"] = str(payload.get("return_packet_id", ""))
    compaction = payload.get("codex_compaction", {})
    if isinstance(compaction, dict):
        metadata["compaction_status"] = str(compaction.get("status", ""))
        metadata["compaction_started_at"] = str(compaction.get("started_at", ""))
        metadata["compaction_completed_at"] = str(compaction.get("completed_at", ""))
        metadata["compaction_error"] = str(compaction.get("error", ""))
    if metadata["compaction_status"] == "running":
        metadata["status"] = "compacting"
    elif metadata["delivery_status"] and metadata["delivery_status"] != "delivered":
        metadata["status"] = "delivery_attention"
    elif not metadata["delivery_status"] and str(payload.get("exit_code", "")) in {"", "0"}:
        metadata["status"] = "pending_delivery"
    metadata["degraded_mode"] = str(payload.get("degraded_mode", ""))
    metadata["final_output_preview"] = _preview_text(
        str(payload.get("final_agent_message", "") or payload.get("summary", ""))
    )
    return metadata


def _preview_text(value: str, *, limit: int = 900) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + "…"


def _latest_codex_thread_id(session: OrchestratorSession, latest_run: dict[str, str] | None) -> str:
    if isinstance(latest_run, dict):
        thread_id = str(latest_run.get("codex_thread_id", "")).strip() or str(
            latest_run.get("observed_codex_thread_id", "")
        ).strip()
        if thread_id:
            return thread_id
    return str(session.current_codex_thread_id or session.current_codex_run_id or "").strip()


_TERMINAL_AUTO_OPEN_LOOP_STATES = {
    "starting_codex",
    "posting_return_packet",
    "waiting_for_chatgpt_response",
    "codex_completed_waiting_to_post",
}


def _open_codex_live_monitor(*, session_id: str, repo_path: str, artifacts_root: Path) -> dict[str, str]:
    log_path = session_live_log_path(artifacts_root, session_id)
    command = _terminal_live_monitor_command(
        session_id=session_id,
        repo_path=repo_path,
        artifacts_root=artifacts_root,
    )
    _open_terminal_with_command(command)
    return {
        "session_id": session_id,
        "repo_path": repo_path,
        "log_path": str(log_path),
    }


def _open_codex_terminal_thread(*, thread_id: str, repo_path: str) -> dict[str, str]:
    codex_bin = _resolve_codex_bin_for_control_panel()
    command = _terminal_resume_command(
        codex_bin=codex_bin,
        repo_path=repo_path,
        thread_id=thread_id,
    )
    _open_terminal_with_command(command)
    return {
        "thread_id": thread_id,
        "repo_path": repo_path,
        "codex_bin": codex_bin,
    }


def _start_codex_terminal_open_watcher(
    *,
    sessions_dir: Path,
    artifacts_root: Path,
    session_id: str,
    repo_path: str,
    baseline_thread_id: str,
    baseline_artifacts_dir: str,
) -> tuple[threading.Event, threading.Thread] | None:
    stop_event = threading.Event()
    session_file = session_path(sessions_dir, session_id)
    baseline_thread = str(baseline_thread_id or "").strip()
    baseline_artifacts = str(baseline_artifacts_dir or "").strip()

    def _watch() -> None:
        deadline = time.monotonic() + 90.0
        while not stop_event.is_set() and time.monotonic() < deadline:
            if not session_file.exists():
                return
            try:
                session = load_session(session_file)
            except Exception:
                time.sleep(0.25)
                continue
            latest_run = _latest_run_metadata(artifacts_root, session_id)
            latest_thread_id = _latest_codex_thread_id(session, latest_run)
            latest_artifacts_dir = str(latest_run.get("artifacts_dir", "")) if isinstance(latest_run, dict) else ""
            loop_state = str(getattr(session, "loop_state", "") or "").strip().casefold()

            should_open = False
            if latest_thread_id and latest_artifacts_dir and latest_artifacts_dir != baseline_artifacts:
                should_open = True
            elif latest_thread_id and baseline_thread and latest_thread_id != baseline_thread:
                should_open = True
            elif latest_thread_id and loop_state in _TERMINAL_AUTO_OPEN_LOOP_STATES:
                should_open = True

            if should_open:
                try:
                    _open_codex_live_monitor(
                        session_id=session_id,
                        repo_path=repo_path,
                        artifacts_root=artifacts_root,
                    )
                except (OSError, subprocess.SubprocessError):
                    return
                stop_event.set()
                return

            normalized_status = str(getattr(session, "status", "") or "").strip().casefold()
            normalized_supervisor = str(getattr(session, "supervisor_status", "") or "").strip().casefold()
            if normalized_status in {"completed"} or normalized_supervisor in {"blocked", "failed", "stopped"}:
                return
            time.sleep(0.25)

    watcher = threading.Thread(
        target=_watch,
        daemon=True,
        name=f"codex-terminal-open-{session_id}",
    )
    watcher.start()
    return stop_event, watcher


def _resolve_codex_bin_for_control_panel() -> str:
    candidates = [
        _normalize_executable_path(os.environ.get("CODEX_BIN")),
        _normalize_executable_path(shutil.which("codex")),
        _resolve_codex_bin_with_login_shell(),
        _normalize_executable_path(str(Path.home() / ".dual-graph" / "codex")),
        _normalize_executable_path("/Applications/Codex.app/Contents/Resources/codex"),
    ]
    for candidate in candidates:
        if candidate:
            return candidate
    return "codex"


def _resolve_codex_bin_with_login_shell() -> str | None:
    try:
        completed = subprocess.run(
            ["/bin/zsh", "-lc", "command -v codex"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return _normalize_executable_path(completed.stdout)

def _normalize_executable_path(candidate: str | None) -> str | None:
    if not candidate:
        return None
    first_line = candidate.strip().splitlines()[0].strip()
    if not first_line:
        return None
    path = Path(first_line).expanduser()
    if not path.exists() or not path.is_file():
        return None
    return str(path.resolve())


def _terminal_resume_command(*, codex_bin: str, repo_path: str, thread_id: str) -> str:
    quoted_repo = shlex.quote(repo_path)
    quoted_codex = shlex.quote(codex_bin)
    quoted_thread = shlex.quote(thread_id)
    return (
        f"cd {quoted_repo} && "
        f"{quoted_codex} resume {quoted_thread} --include-non-interactive"
    )


def _terminal_live_monitor_command(*, session_id: str, repo_path: str, artifacts_root: Path) -> str:
    quoted_session_id = shlex.quote(session_id)
    quoted_workspace = shlex.quote(repo_path)
    quoted_artifacts_root = shlex.quote(str(artifacts_root))
    bridge_repo = repo_root()
    quoted_bridge_repo = shlex.quote(str(bridge_repo))
    source_path = bridge_repo / "src"
    python_path = source_path if (source_path / "chatgpt_codex_bridge").is_dir() else bridge_repo
    quoted_python_path = shlex.quote(str(python_path))
    return (
        f"cd {quoted_bridge_repo} && "
        f"PYTHONUNBUFFERED=1 PYTHONPATH={quoted_python_path} "
        f"python3 -m chatgpt_codex_bridge.control_panel_runtime "
        f"--session-id {quoted_session_id} "
        f"--workspace {quoted_workspace} "
        f"--artifacts-root {quoted_artifacts_root} "
        f"--tail-lines 80 "
        f"--no-initial-prompt"
    )


def _codex_thread_deeplink(thread_id: str) -> str:
    return f"codex://threads/{thread_id}"


def _codex_app_auto_open_enabled() -> bool:
    normalized = str(os.environ.get("BRIDGE_AUTO_OPEN_CODEX_APP_THREADS", "")).strip().casefold()
    return normalized in {"1", "true", "yes", "on"}


def _open_codex_app_thread(thread_id: str) -> None:
    deeplink = _codex_thread_deeplink(thread_id)
    bundle_id = "com.openai.codex"
    script = "\n".join(
        [
            f'tell application id "{bundle_id}"',
            "  activate",
            "end tell",
            "delay 0.2",
            f"open location {_apple_script_string(deeplink)}",
            "delay 0.2",
            f'tell application id "{bundle_id}"',
            "  activate",
            "end tell",
        ]
    )
    try:
        subprocess.run(["osascript", "-"], input=script, text=True, check=True)
        return
    except (OSError, subprocess.SubprocessError):
        subprocess.run(["open", "-b", bundle_id], check=True)
        subprocess.run(["open", deeplink], check=True)


def _open_terminal_with_command(command: str) -> None:
    script = "\n".join(
        [
            f"set shellCommand to {_apple_script_string(command)}",
            'tell application "Terminal"',
            "  activate",
            "  do script shellCommand",
            "end tell",
        ]
    )
    subprocess.run(["osascript", "-"], input=script, text=True, check=True)


def _apple_script_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
