from __future__ import annotations

import copy
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import (
    ChatBinding,
    ChatDeliveryAttempt,
    InstructionScopeUpdate,
    OrchestratorSession,
    now_iso,
    refresh_session_budget,
)

DEFAULT_CHAT_BINDINGS_PAYLOAD: dict[str, Any] = {
    "version": 1,
    "bindings": [],
}

_ORCHESTRATOR_POLICY_VERSION = 2
_PUBLIC_SAFE_POLICY_KEYS = (
    "allow_branch_worktree_creation",
    "allow_commit_push_pr",
    "allow_deployments",
    "allow_existing_local_secrets",
    "allow_operator_provided_secrets",
    "allow_keychain_access",
    "prefer_full_local_codex_environment",
    "allow_browser_and_screen_tools",
    "prefer_installed_mcp_tools",
    "prefer_installed_apps_plugins_and_clis",
)

DEFAULT_ORCHESTRATOR_POLICY: dict[str, Any] = {
    "version": _ORCHESTRATOR_POLICY_VERSION,
    "autonomy_mode": "balanced_aggressive",
    "require_explicit_budget": True,
    "allow_branch_worktree_creation": False,
    "allow_commit_push_pr": False,
    "allow_deployments": False,
    "allow_existing_local_secrets": False,
    "allow_operator_provided_secrets": False,
    "allow_keychain_access": False,
    "prefer_full_local_codex_environment": False,
    "allow_browser_and_screen_tools": False,
    "prefer_installed_mcp_tools": False,
    "prefer_installed_apps_plugins_and_clis": False,
    "human_gate_categories": [
        "paid_spend",
        "creative_product_ui_direction",
        "mission_reframe",
    ],
    "stop_phrases": [
        "stop",
        "pause",
        "stop after this cycle",
    ],
    "delivery_retry": {
        "enabled": True,
        "transport_direction": "codex_to_chatgpt_only",
        "max_attempts": 2,
        "known_error_signatures": [
            "Reasoning failed",
            "Message delivery confirmation timed out.",
        ],
    },
    "project_instruction_updates": [],
}

_SESSION_LOAD_RETRIES = 5
_SESSION_LOAD_RETRY_DELAY_SECONDS = 0.02
_SAFE_STATE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def load_chat_bindings(path: Path) -> list[ChatBinding]:
    payload = _load_or_initialize(path, DEFAULT_CHAT_BINDINGS_PAYLOAD)
    bindings = payload.get("bindings", [])
    return [ChatBinding.from_dict(item) for item in bindings if isinstance(item, dict)]


def save_chat_bindings(path: Path, bindings: list[ChatBinding | dict[str, Any]]) -> None:
    serialized = []
    for binding in bindings:
        if isinstance(binding, ChatBinding):
            serialized.append(binding.as_dict())
        else:
            serialized.append(ChatBinding.from_dict(dict(binding)).as_dict())
    _save_json(path, {"version": 1, "bindings": serialized})


def upsert_chat_binding(path: Path, binding: ChatBinding) -> list[ChatBinding]:
    bindings = load_chat_bindings(path)
    replaced = False
    updated_bindings: list[ChatBinding] = []
    for existing in bindings:
        if existing.binding_id == binding.binding_id:
            updated_bindings.append(binding)
            replaced = True
        else:
            updated_bindings.append(existing)
    if not replaced:
        updated_bindings.append(binding)
    save_chat_bindings(path, updated_bindings)
    return updated_bindings


def load_orchestrator_policy(path: Path) -> dict[str, Any]:
    return read_orchestrator_policy(path, persist_defaults=True)


def read_orchestrator_policy(path: Path, *, persist_defaults: bool = False) -> dict[str, Any]:
    payload = _load_or_initialize(path, DEFAULT_ORCHESTRATOR_POLICY, persist_default=persist_defaults)
    payload = _migrate_orchestrator_policy_payload(payload)
    merged = copy.deepcopy(DEFAULT_ORCHESTRATOR_POLICY)
    _deep_update(merged, payload)
    if persist_defaults:
        _save_json(path, merged)
    return merged


def save_orchestrator_policy(path: Path, payload: dict[str, Any]) -> None:
    merged = copy.deepcopy(DEFAULT_ORCHESTRATOR_POLICY)
    _deep_update(merged, _migrate_orchestrator_policy_payload(payload))
    _save_json(path, merged)


def validate_state_id(value: str, *, label: str = "id") -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"Invalid {label}: value must not be empty.")
    if os.path.isabs(normalized) or os.path.basename(normalized) != normalized or "\\" in normalized:
        raise ValueError(f"Invalid {label}: path separators are not allowed.")
    if not _SAFE_STATE_ID_RE.fullmatch(normalized):
        raise ValueError(f"Invalid {label}: use only letters, numbers, '.', '_', '-', or ':'.")
    return normalized


def session_path(sessions_dir: Path, session_id: str) -> Path:
    safe_session_id = validate_state_id(session_id, label="session_id")
    return sessions_dir / os.path.basename(f"{safe_session_id}.json")


def _validated_json_state_path(path: Path) -> Path:
    candidate = Path(path)
    if os.path.basename(candidate.name) != candidate.name or candidate.suffix != ".json":
        raise ValueError(f"Invalid state file path: {path}")
    return candidate


def _migrate_orchestrator_policy_payload(payload: dict[str, Any]) -> dict[str, Any]:
    migrated = copy.deepcopy(payload)
    version = int(migrated.get("version", 0) or 0)
    if version < _ORCHESTRATOR_POLICY_VERSION:
        for key in _PUBLIC_SAFE_POLICY_KEYS:
            migrated[key] = False
        migrated["version"] = _ORCHESTRATOR_POLICY_VERSION
    return migrated


def load_session(path: Path) -> OrchestratorSession:
    path = _validated_json_state_path(path)
    last_error: json.JSONDecodeError | None = None
    for attempt in range(_SESSION_LOAD_RETRIES):
        try:
            payload = json.loads(path.read_text())
            session_payload = payload.get("session", payload)
            if not isinstance(session_payload, dict):
                raise ValueError(f"Invalid session payload in {path}")
            session = OrchestratorSession.from_dict(session_payload)
            refresh_session_budget(session)
            return session
        except json.JSONDecodeError as exc:
            last_error = exc
            if attempt == _SESSION_LOAD_RETRIES - 1:
                raise
            time.sleep(_SESSION_LOAD_RETRY_DELAY_SECONDS)
    if last_error is not None:  # pragma: no cover - defensive fallback
        raise last_error
    raise ValueError(f"Unable to load session payload from {path}")


def save_session(path: Path, session: OrchestratorSession, *, touch_updated_at: bool = True) -> None:
    current_time = time.time()
    existing_session: OrchestratorSession | None = None
    if path.exists():
        try:
            existing_session = load_session(path)
        except (OSError, ValueError, json.JSONDecodeError):
            existing_session = None
    if existing_session is not None and existing_session.session_id == session.session_id:
        _merge_external_session_updates(session, existing_session)
    if session.auto_run_enabled:
        if session.budget_clock_started_at <= 0 and session.time_budget_minutes > 0:
            session.budget_clock_started_at = current_time
    elif session.budget_clock_started_at > 0:
        session.budget_consumed_seconds += max(0.0, current_time - session.budget_clock_started_at)
        session.budget_clock_started_at = 0.0
    refresh_session_budget(session, now=current_time)
    if touch_updated_at:
        session.updated_at = now_iso()
    _save_json(path, {"version": 1, "session": session.as_dict()})


def _merge_external_session_updates(session: OrchestratorSession, existing_session: OrchestratorSession) -> None:
    session.instruction_updates = _merge_unique_instruction_updates(
        session.instruction_updates,
        existing_session.instruction_updates,
    )
    session.delivery_attempts = _merge_unique_delivery_attempts(
        session.delivery_attempts,
        existing_session.delivery_attempts,
    )
    _merge_newer_outbound_tracking(session, existing_session)
    _merge_newer_execution_settings(session, existing_session)
    for field_name in (
        "last_seen_user_control_anchor",
        "latest_user_control_message_hash",
        "latest_user_control_command",
    ):
        if not str(getattr(session, field_name, "") or "").strip():
            setattr(session, field_name, str(getattr(existing_session, field_name, "") or ""))


def _merge_newer_execution_settings(session: OrchestratorSession, existing_session: OrchestratorSession) -> None:
    existing_updated_at = str(existing_session.updated_at or "").strip()
    session_updated_at = str(session.updated_at or "").strip()
    if not existing_updated_at or (session_updated_at and existing_updated_at <= session_updated_at):
        return
    session.codex_model = str(existing_session.codex_model or "")
    session.codex_reasoning_effort = str(existing_session.codex_reasoning_effort or "")


def _merge_unique_instruction_updates(
    preferred: list[InstructionScopeUpdate],
    concurrent: list[InstructionScopeUpdate],
) -> list[InstructionScopeUpdate]:
    merged: list[InstructionScopeUpdate] = []
    indexed: list[tuple[int, InstructionScopeUpdate]] = []
    seen: set[str] = set()
    for index, update in enumerate([*preferred, *concurrent]):
        payload = update.as_dict()
        key = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        if key in seen:
            continue
        seen.add(key)
        normalized = InstructionScopeUpdate.from_dict(payload)
        indexed.append((index, normalized))
        merged.append(normalized)
    latest_replace_by_scope: dict[str, tuple[float, int]] = {}
    for index, update in indexed:
        if str(update.mode).strip().casefold() != "replace":
            continue
        scope = str(update.scope)
        timestamp = _instruction_update_timestamp(update, fallback=float(index))
        sort_key = (timestamp, index)
        if sort_key > latest_replace_by_scope.get(scope, (-1.0, -1)):
            latest_replace_by_scope[scope] = sort_key
    if not latest_replace_by_scope:
        return merged
    filtered: list[InstructionScopeUpdate] = []
    for index, update in indexed:
        scope = str(update.scope)
        latest_replace = latest_replace_by_scope.get(scope)
        if latest_replace is None:
            filtered.append(update)
            continue
        timestamp = _instruction_update_timestamp(update, fallback=float(index))
        sort_key = (timestamp, index)
        mode = str(update.mode).strip().casefold()
        if mode == "replace":
            if sort_key == latest_replace:
                filtered.append(update)
            continue
        if sort_key > latest_replace:
            filtered.append(update)
    return filtered


def _instruction_update_timestamp(update: InstructionScopeUpdate, *, fallback: float) -> float:
    value = str(update.created_at or "").strip()
    if not value:
        return fallback
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return fallback


def _merge_unique_delivery_attempts(
    preferred: list[ChatDeliveryAttempt],
    concurrent: list[ChatDeliveryAttempt],
) -> list[ChatDeliveryAttempt]:
    merged: list[ChatDeliveryAttempt] = []
    seen: set[str] = set()
    for attempt in [*preferred, *concurrent]:
        payload = attempt.as_dict()
        key = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        if key in seen:
            continue
        seen.add(key)
        merged.append(ChatDeliveryAttempt.from_dict(payload))
    return merged


def _merge_newer_outbound_tracking(session: OrchestratorSession, existing_session: OrchestratorSession) -> None:
    if session.loop_state not in {"idle", "waiting_for_chatgpt", "waiting_for_chatgpt_response"}:
        return
    if existing_session.last_seen_chat_message_anchor != session.last_seen_chat_message_anchor:
        return
    if existing_session.latest_assistant_message_hash != session.latest_assistant_message_hash:
        return
    if (
        not str(existing_session.last_outbound_user_message_anchor or "").strip()
        and not str(existing_session.last_outbound_user_message_kind or "").strip()
        and str(session.last_outbound_user_message_kind or "").strip() in {"repair", "recovery"}
    ):
        session.last_outbound_user_message_anchor = ""
        session.last_outbound_user_message_kind = ""
        session.last_outbound_user_message_sent_at = 0.0
        return
    if (
        not str(session.last_outbound_user_message_anchor or "").strip()
        and str(existing_session.last_outbound_user_message_kind or "").strip() in {"repair", "recovery"}
    ):
        return
    existing_sent_at = float(existing_session.last_outbound_user_message_sent_at or 0.0)
    session_sent_at = float(session.last_outbound_user_message_sent_at or 0.0)
    if existing_sent_at <= session_sent_at or not existing_session.last_outbound_user_message_anchor:
        return
    session.last_outbound_user_message_anchor = existing_session.last_outbound_user_message_anchor
    session.last_outbound_user_message_kind = existing_session.last_outbound_user_message_kind
    session.last_outbound_user_message_sent_at = existing_sent_at
    if existing_session.last_delivery_at:
        session.last_delivery_at = existing_session.last_delivery_at


def list_sessions(sessions_dir: Path) -> list[OrchestratorSession]:
    if not sessions_dir.exists():
        return []
    sessions: list[OrchestratorSession] = []
    for path in sorted(sessions_dir.glob("*.json")):
        sessions.append(load_session(path))
    return sessions


def _load_or_initialize(
    path: Path,
    default_payload: dict[str, Any],
    *,
    persist_default: bool = True,
) -> dict[str, Any]:
    path = _validated_json_state_path(path)
    if path.exists():
        return json.loads(path.read_text())
    payload = copy.deepcopy(default_payload)
    if persist_default:
        _save_json(path, payload)
    return payload


def _save_json(path: Path, payload: dict[str, Any]) -> None:
    path = _validated_json_state_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.parent / f".{path.name}.{time.time_ns()}.tmp"
    temp_path.write_text(json.dumps(payload, indent=2) + "\n")
    temp_path.replace(path)


def _deep_update(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
            continue
        target[key] = value
