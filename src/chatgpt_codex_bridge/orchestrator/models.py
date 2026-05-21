from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..models import now_iso

BUDGET_SEMANTICS_ELAPSED_ACTIVE_MINUTES = "elapsed_active_wall_clock_minutes"


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def normalize_shell_wrapped_value(value: Any) -> str:
    normalized = str(value or "").strip()
    while len(normalized) >= 2:
        first = normalized[0]
        last = normalized[-1]
        if first != last or first not in {"'", '"'}:
            break
        normalized = normalized[1:-1].strip()
    return normalized


def _normalize_project_name(value: Any, *, repo_path: str = "", workspace_path: str = "") -> str:
    normalized = normalize_shell_wrapped_value(value).strip().strip("'\"").strip()
    if normalized:
        return normalized
    fallback = normalize_shell_wrapped_value(repo_path) or normalize_shell_wrapped_value(workspace_path)
    return Path(fallback).name if fallback else ""


@dataclass(slots=True)
class ChatBinding:
    binding_id: str
    project_name: str
    repo_path: str
    workspace_path: str
    chat_url: str
    browser_profile_path: str = ""
    browser_channel: str = ""
    browser_session_handle: str = ""
    status: str = "active"
    last_session_id: str = ""
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ChatBinding":
        timestamp = str(payload.get("updated_at") or payload.get("created_at") or now_iso())
        repo_path = normalize_shell_wrapped_value(payload.get("repo_path", ""))
        workspace_path = normalize_shell_wrapped_value(payload.get("workspace_path", repo_path))
        return cls(
            binding_id=str(payload["binding_id"]),
            project_name=_normalize_project_name(
                payload.get("project_name", ""),
                repo_path=repo_path,
                workspace_path=workspace_path,
            ),
            repo_path=repo_path,
            workspace_path=workspace_path,
            chat_url=str(payload.get("chat_url", "")),
            browser_profile_path=str(payload.get("browser_profile_path", "")),
            browser_channel=str(payload.get("browser_channel", "")),
            browser_session_handle=str(payload.get("browser_session_handle", "")),
            status=str(payload.get("status", "active")),
            last_session_id=str(payload.get("last_session_id", "")),
            created_at=str(payload.get("created_at", timestamp)),
            updated_at=timestamp,
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class InstructionScopeUpdate:
    scope: str
    mode: str
    text: str
    created_at: str = field(default_factory=now_iso)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "InstructionScopeUpdate":
        return cls(
            scope=str(payload["scope"]),
            mode=str(payload["mode"]),
            text=str(payload["text"]),
            created_at=str(payload.get("created_at", now_iso())),
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class LoopPolicyDecision:
    policy_outcome: str
    reasons: list[str]
    human_gate_required: bool = False
    human_gate_reason: str = ""
    human_gate_category: str = ""
    time_budget_minutes: int = 0
    time_budget_remaining_minutes: int = 0
    timestamp: str = field(default_factory=now_iso)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "LoopPolicyDecision":
        return cls(
            policy_outcome=str(payload.get("policy_outcome", "")),
            reasons=_as_list(payload.get("reasons")),
            human_gate_required=bool(payload.get("human_gate_required", False)),
            human_gate_reason=str(payload.get("human_gate_reason", "")),
            human_gate_category=str(payload.get("human_gate_category", "")),
            time_budget_minutes=int(payload.get("time_budget_minutes", 0) or 0),
            time_budget_remaining_minutes=int(payload.get("time_budget_remaining_minutes", 0) or 0),
            timestamp=str(payload.get("timestamp", now_iso())),
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ChatDeliveryAttempt:
    attempt_number: int
    status: str
    transport: str = "chatgpt_browser"
    return_packet_id: str = ""
    error_signature: str = ""
    created_at: str = field(default_factory=now_iso)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ChatDeliveryAttempt":
        return cls(
            attempt_number=int(payload.get("attempt_number", 0) or 0),
            status=str(payload.get("status", "")),
            transport=str(payload.get("transport", "chatgpt_browser")),
            return_packet_id=str(payload.get("return_packet_id", "")),
            error_signature=str(payload.get("error_signature", "")),
            created_at=str(payload.get("created_at", now_iso())),
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class BridgeControlEnvelope:
    protocol_version: str
    session_id: str
    decision: str
    codex_thread_action: str
    prompt: str
    task_label: str
    human_gate_required: bool = False
    human_gate_reason: str = ""
    human_gate_category: str = ""
    instruction_updates: list[InstructionScopeUpdate] = field(default_factory=list)
    time_budget_remaining_hint: str = ""
    notes_for_audit: list[str] = field(default_factory=list)
    delivery_attempts: list[ChatDeliveryAttempt] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "BridgeControlEnvelope":
        human_gate = payload.get("human_gate", {})
        if not isinstance(human_gate, dict):
            human_gate = {}
        return cls(
            protocol_version=str(payload.get("protocol_version", "")),
            session_id=str(payload.get("session_id", "")),
            decision=str(payload.get("decision", "")),
            codex_thread_action=_normalize_codex_thread_action(str(payload.get("codex_thread_action", ""))),
            prompt=str(payload.get("prompt", "")),
            task_label=str(payload.get("task_label", "")),
            human_gate_required=bool(human_gate.get("required", False)),
            human_gate_reason=str(human_gate.get("reason", "")),
            human_gate_category=str(human_gate.get("category", "")),
            instruction_updates=[
                InstructionScopeUpdate.from_dict(item)
                for item in payload.get("instruction_updates", [])
                if isinstance(item, dict)
            ],
            time_budget_remaining_hint=str(payload.get("time_budget_remaining_hint", "")),
            notes_for_audit=_as_list(payload.get("notes_for_audit")),
            delivery_attempts=[
                ChatDeliveryAttempt.from_dict(item)
                for item in payload.get("delivery_attempts", [])
                if isinstance(item, dict)
            ],
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "session_id": self.session_id,
            "decision": self.decision,
            "codex_thread_action": self.codex_thread_action,
            "prompt": self.prompt,
            "task_label": self.task_label,
            "human_gate": {
                "required": self.human_gate_required,
                "reason": self.human_gate_reason,
                "category": self.human_gate_category,
            },
            "instruction_updates": [item.as_dict() for item in self.instruction_updates],
            "time_budget_remaining_hint": self.time_budget_remaining_hint,
            "notes_for_audit": list(self.notes_for_audit),
            "delivery_attempts": [item.as_dict() for item in self.delivery_attempts],
        }


def _normalize_codex_thread_action(value: str) -> str:
    normalized = str(value or "").strip().casefold().replace("-", "_")
    if normalized in {"same_thread", "continue_same_thread", "continue_thread"}:
        return "same_thread"
    if normalized in {"new_thread", "start_new_thread", "fresh_thread"}:
        return "new_thread"
    if normalized in {"fork_thread", "fork_new_thread"}:
        return "fork_thread"
    return str(value or "").strip()


@dataclass(slots=True)
class OrchestratorSession:
    session_id: str
    binding_id: str
    repo_path: str
    workspace_path: str
    chat_url: str
    status: str = "active"
    loop_state: str = "idle"
    auto_run_enabled: bool = False
    supervisor_status: str = "idle"
    time_budget_minutes: int = 0
    budget_remaining_minutes: int = 0
    budget_semantics: str = BUDGET_SEMANTICS_ELAPSED_ACTIVE_MINUTES
    budget_consumed_seconds: float = 0.0
    budget_clock_started_at: float = 0.0
    cycles_completed: int = 0
    codex_model: str = ""
    codex_reasoning_effort: str = ""
    current_codex_thread_id: str = ""
    current_codex_run_id: str = ""
    last_thread_action: str = ""
    supervisor_heartbeat_at: str = ""
    phase_started_at: str = ""
    last_chat_activity_at: str = ""
    last_codex_activity_at: str = ""
    last_delivery_at: str = ""
    last_seen_chat_message_anchor: str = ""
    last_posted_return_packet_id: str = ""
    latest_assistant_message_id: str = ""
    latest_assistant_message_hash: str = ""
    in_progress_assistant_anchor: str = ""
    in_progress_assistant_hash: str = ""
    in_progress_assistant_text: str = ""
    in_progress_assistant_started_at: float = 0.0
    in_progress_assistant_last_progress_at: float = 0.0
    last_outbound_user_message_anchor: str = ""
    last_outbound_user_message_kind: str = ""
    last_outbound_user_message_sent_at: float = 0.0
    bridge_control_failure_streak: int = 0
    last_seen_user_control_anchor: str = ""
    latest_user_control_message_hash: str = ""
    latest_user_control_command: str = ""
    last_productive_prompt: str = ""
    last_productive_task_label: str = ""
    last_productive_thread_action: str = ""
    productive_rewind_attempts: int = 0
    stop_after_cycle_requested: bool = False
    stop_before_return_packet_requested: bool = False
    human_attention_reason: str = ""
    last_error: str = ""
    degraded_mode: str = ""
    degraded_reason: str = ""
    policy_decision: LoopPolicyDecision = field(
        default_factory=lambda: LoopPolicyDecision(policy_outcome="", reasons=[])
    )
    instruction_updates: list[InstructionScopeUpdate] = field(default_factory=list)
    delivery_attempts: list[ChatDeliveryAttempt] = field(default_factory=list)
    started_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "OrchestratorSession":
        timestamp = str(payload.get("updated_at") or payload.get("started_at") or now_iso())
        repo_path = normalize_shell_wrapped_value(payload.get("repo_path", ""))
        workspace_path = normalize_shell_wrapped_value(payload.get("workspace_path", repo_path))
        return cls(
            session_id=str(payload["session_id"]),
            binding_id=str(payload["binding_id"]),
            repo_path=repo_path,
            workspace_path=workspace_path,
            chat_url=str(payload.get("chat_url", "")),
            status=str(payload.get("status", "active")),
            loop_state=str(payload.get("loop_state", "idle")),
            auto_run_enabled=bool(payload.get("auto_run_enabled", False)),
            supervisor_status=str(payload.get("supervisor_status", "idle")),
            time_budget_minutes=int(payload.get("time_budget_minutes", 0) or 0),
            budget_remaining_minutes=int(
                payload.get("budget_remaining_minutes", payload.get("time_budget_minutes", 0)) or 0
            ),
            budget_semantics=str(
                payload.get("budget_semantics", BUDGET_SEMANTICS_ELAPSED_ACTIVE_MINUTES)
                or BUDGET_SEMANTICS_ELAPSED_ACTIVE_MINUTES
            ),
            budget_consumed_seconds=float(
                payload.get(
                    "budget_consumed_seconds",
                    max(
                        0,
                        int(payload.get("time_budget_minutes", 0) or 0)
                        - int(
                            payload.get(
                                "budget_remaining_minutes",
                                payload.get("time_budget_minutes", 0),
                            )
                            or 0
                        ),
                    )
                    * 60,
                )
                or 0.0
            ),
            budget_clock_started_at=float(payload.get("budget_clock_started_at", 0.0) or 0.0),
            cycles_completed=int(payload.get("cycles_completed", 0) or 0),
            codex_model=str(payload.get("codex_model", "")),
            codex_reasoning_effort=str(payload.get("codex_reasoning_effort", "")),
            current_codex_thread_id=str(
                payload.get("current_codex_thread_id", payload.get("current_codex_run_id", ""))
            ),
            current_codex_run_id=str(
                payload.get("current_codex_run_id", payload.get("current_codex_thread_id", ""))
            ),
            last_thread_action=str(payload.get("last_thread_action", "")),
            supervisor_heartbeat_at=str(payload.get("supervisor_heartbeat_at", "")),
            phase_started_at=str(payload.get("phase_started_at", "")),
            last_chat_activity_at=str(payload.get("last_chat_activity_at", "")),
            last_codex_activity_at=str(payload.get("last_codex_activity_at", "")),
            last_delivery_at=str(payload.get("last_delivery_at", "")),
            last_seen_chat_message_anchor=str(payload.get("last_seen_chat_message_anchor", "")),
            last_posted_return_packet_id=str(payload.get("last_posted_return_packet_id", "")),
            latest_assistant_message_id=str(payload.get("latest_assistant_message_id", "")),
            latest_assistant_message_hash=str(payload.get("latest_assistant_message_hash", "")),
            in_progress_assistant_anchor=str(payload.get("in_progress_assistant_anchor", "")),
            in_progress_assistant_hash=str(payload.get("in_progress_assistant_hash", "")),
            in_progress_assistant_text=str(payload.get("in_progress_assistant_text", "")),
            in_progress_assistant_started_at=float(payload.get("in_progress_assistant_started_at", 0.0) or 0.0),
            in_progress_assistant_last_progress_at=float(
                payload.get("in_progress_assistant_last_progress_at", 0.0) or 0.0
            ),
            last_outbound_user_message_anchor=str(payload.get("last_outbound_user_message_anchor", "")),
            last_outbound_user_message_kind=str(payload.get("last_outbound_user_message_kind", "")),
            last_outbound_user_message_sent_at=float(payload.get("last_outbound_user_message_sent_at", 0.0) or 0.0),
            bridge_control_failure_streak=int(payload.get("bridge_control_failure_streak", 0) or 0),
            last_seen_user_control_anchor=str(payload.get("last_seen_user_control_anchor", "")),
            latest_user_control_message_hash=str(payload.get("latest_user_control_message_hash", "")),
            latest_user_control_command=str(payload.get("latest_user_control_command", "")),
            last_productive_prompt=str(payload.get("last_productive_prompt", "")),
            last_productive_task_label=str(payload.get("last_productive_task_label", "")),
            last_productive_thread_action=str(payload.get("last_productive_thread_action", "")),
            productive_rewind_attempts=int(payload.get("productive_rewind_attempts", 0) or 0),
            stop_after_cycle_requested=bool(payload.get("stop_after_cycle_requested", False)),
            stop_before_return_packet_requested=bool(payload.get("stop_before_return_packet_requested", False)),
            human_attention_reason=str(payload.get("human_attention_reason", "")),
            last_error=str(payload.get("last_error", "")),
            degraded_mode=str(payload.get("degraded_mode", "")),
            degraded_reason=str(payload.get("degraded_reason", "")),
            policy_decision=LoopPolicyDecision.from_dict(dict(payload.get("policy_decision", {}))),
            instruction_updates=[
                InstructionScopeUpdate.from_dict(item)
                for item in payload.get("instruction_updates", [])
                if isinstance(item, dict)
            ],
            delivery_attempts=[
                ChatDeliveryAttempt.from_dict(item)
                for item in payload.get("delivery_attempts", [])
                if isinstance(item, dict)
            ],
            started_at=str(payload.get("started_at", timestamp)),
            updated_at=timestamp,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "binding_id": self.binding_id,
            "repo_path": self.repo_path,
            "workspace_path": self.workspace_path,
            "chat_url": self.chat_url,
            "status": self.status,
            "loop_state": self.loop_state,
            "auto_run_enabled": self.auto_run_enabled,
            "supervisor_status": self.supervisor_status,
            "time_budget_minutes": self.time_budget_minutes,
            "budget_remaining_minutes": self.budget_remaining_minutes,
            "budget_semantics": self.budget_semantics,
            "budget_consumed_seconds": self.budget_consumed_seconds,
            "budget_clock_started_at": self.budget_clock_started_at,
            "cycles_completed": self.cycles_completed,
            "codex_model": self.codex_model,
            "codex_reasoning_effort": self.codex_reasoning_effort,
            "current_codex_thread_id": self.current_codex_thread_id or self.current_codex_run_id,
            "current_codex_run_id": self.current_codex_thread_id or self.current_codex_run_id,
            "last_thread_action": self.last_thread_action,
            "supervisor_heartbeat_at": self.supervisor_heartbeat_at,
            "phase_started_at": self.phase_started_at,
            "last_chat_activity_at": self.last_chat_activity_at,
            "last_codex_activity_at": self.last_codex_activity_at,
            "last_delivery_at": self.last_delivery_at,
            "last_seen_chat_message_anchor": self.last_seen_chat_message_anchor,
            "last_posted_return_packet_id": self.last_posted_return_packet_id,
            "latest_assistant_message_id": self.latest_assistant_message_id,
            "latest_assistant_message_hash": self.latest_assistant_message_hash,
            "in_progress_assistant_anchor": self.in_progress_assistant_anchor,
            "in_progress_assistant_hash": self.in_progress_assistant_hash,
            "in_progress_assistant_text": self.in_progress_assistant_text,
            "in_progress_assistant_started_at": self.in_progress_assistant_started_at,
            "in_progress_assistant_last_progress_at": self.in_progress_assistant_last_progress_at,
            "last_outbound_user_message_anchor": self.last_outbound_user_message_anchor,
            "last_outbound_user_message_kind": self.last_outbound_user_message_kind,
            "last_outbound_user_message_sent_at": self.last_outbound_user_message_sent_at,
            "bridge_control_failure_streak": self.bridge_control_failure_streak,
            "last_seen_user_control_anchor": self.last_seen_user_control_anchor,
            "latest_user_control_message_hash": self.latest_user_control_message_hash,
            "latest_user_control_command": self.latest_user_control_command,
            "last_productive_prompt": self.last_productive_prompt,
            "last_productive_task_label": self.last_productive_task_label,
            "last_productive_thread_action": self.last_productive_thread_action,
            "productive_rewind_attempts": self.productive_rewind_attempts,
            "stop_after_cycle_requested": self.stop_after_cycle_requested,
            "stop_before_return_packet_requested": self.stop_before_return_packet_requested,
            "human_attention_reason": self.human_attention_reason,
            "last_error": self.last_error,
            "degraded_mode": self.degraded_mode,
            "degraded_reason": self.degraded_reason,
            "policy_decision": self.policy_decision.as_dict(),
            "instruction_updates": [item.as_dict() for item in self.instruction_updates],
            "delivery_attempts": [item.as_dict() for item in self.delivery_attempts],
            "started_at": self.started_at,
            "updated_at": self.updated_at,
        }


def current_budget_remaining_seconds(
    session: OrchestratorSession,
    *,
    now: float | None = None,
) -> float:
    current_time = time.time() if now is None else float(now)
    consumed_seconds = max(float(session.budget_consumed_seconds or 0.0), 0.0)
    if session.auto_run_enabled and float(session.budget_clock_started_at or 0.0) > 0.0:
        consumed_seconds += max(0.0, current_time - float(session.budget_clock_started_at))
    total_seconds = max(int(session.time_budget_minutes or 0), 0) * 60.0
    return max(total_seconds - consumed_seconds, 0.0)


def refresh_session_budget(
    session: OrchestratorSession,
    *,
    now: float | None = None,
) -> None:
    remaining_seconds = current_budget_remaining_seconds(session, now=now)
    if remaining_seconds <= 0.0:
        session.budget_remaining_minutes = 0
        return
    session.budget_remaining_minutes = max(1, int(math.ceil(remaining_seconds / 60.0)))
