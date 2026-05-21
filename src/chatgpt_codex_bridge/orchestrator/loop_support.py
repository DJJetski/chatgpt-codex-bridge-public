from __future__ import annotations

from datetime import datetime
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ..models import RunReport, derive_return_packet_id, now_iso
from ..storage import save_json
from .control import BridgeControlParseError, extract_bridge_control_envelope
from .models import LoopPolicyDecision
from .state import load_session, save_session, session_path

_ASSISTANT_STALL_SECONDS = 180.0
_SHORT_FRAGMENT_STALL_SECONDS = 60.0
_DEGRADED_THINKING_STALL_SECONDS = 30.0
_OUTBOUND_USER_MESSAGE_TIMEOUT_SECONDS = 180.0
_DELIVERY_CONFIRMATION_GRACE_POLLS = 4
_DELIVERY_CONFIRMATION_GRACE_INTERVAL_SECONDS = 1.0
_RETRYABLE_DELIVERY_ERROR_SIGNATURES = {
    "Reasoning failed",
    "Message delivery confirmation timed out.",
}
_REPAIR_RECOVERY_THRESHOLD = 4
_NO_FOLLOWUP_REPAIR_RECOVERY_THRESHOLD = 2
_MAX_RECOVERY_ATTEMPTS_BEFORE_FRESH_CHAT_REQUIRED = 1
_PATHOLOGICAL_ASSISTANT_FRAGMENTS = {"ich", "bridge", "bridge-control", "chatgpt", "chatgpt:"}
_THINKING_DISCLOSURE_PATTERNS = (
    re.compile(r"^thought for (?:\d+\s*[hms])(?:\s+\d+\s*[hms])*$", re.IGNORECASE),
    re.compile(r"^nachgedacht für (?:\d+\s*[hms])(?:\s+\d+\s*[hms])*$", re.IGNORECASE),
    re.compile(r"^thinking(?:…|\.{3})?$", re.IGNORECASE),
    re.compile(r"^denke nach(?:…|\.{3})?$", re.IGNORECASE),
)


def _persist_run_report(report: RunReport) -> None:
    if not report.artifacts_dir:
        return
    _normalize_report_delivery_state(report)
    save_json(Path(report.artifacts_dir) / "run_report.json", report.as_dict())


def _normalize_report_delivery_state(report: RunReport) -> bool:
    delivered_attempt = next(
        (
            item
            for item in report.delivery_attempts
            if str(item.get("status", "") or "").strip().casefold() == "delivered"
        ),
        None,
    )
    if not delivered_attempt:
        return False
    changed = False
    packet_id = str(delivered_attempt.get("return_packet_id", "") or "").strip()
    if not str(report.return_packet_id or "").strip():
        report.return_packet_id = packet_id or derive_return_packet_id(report)
        changed = True
    if report.delivery_status != "delivered":
        report.delivery_status = "delivered"
        changed = True
    if report.delivery_attempt_count != len(report.delivery_attempts):
        report.delivery_attempt_count = len(report.delivery_attempts)
        changed = True
    if not str(report.policy_outcome or "").strip():
        report.policy_outcome = "allow"
        changed = True
    return changed


def _result_payload(
    session,
    policy_decision,
    return_packet_id: str,
    delivery: dict[str, Any] | None = None,
    runner_action: str = "",
) -> dict[str, Any]:
    payload = {
        "session_id": session.session_id,
        "loop_state": session.loop_state,
        "policy_outcome": policy_decision.policy_outcome,
        "return_packet_id": return_packet_id,
        "runner_action": runner_action,
    }
    if delivery is not None:
        payload["delivery_status"] = delivery["status"]
        payload["delivery_attempt_count"] = delivery["attempt_count"]
    return payload


def _clear_delivery_retry_state(session) -> None:
    _clear_outbound_user_message(session)
    session.last_posted_return_packet_id = ""
    session.degraded_mode = ""
    session.degraded_reason = ""


def _honor_non_active_session_state(
    sessions_dir: Path,
    session,
    *,
    runner_action: str = "paused",
) -> dict[str, Any] | None:
    latest_session = load_session(session_path(sessions_dir, session.session_id))
    if latest_session.status == "active" and latest_session.auto_run_enabled:
        return None
    if latest_session.status == "paused":
        _clear_delivery_retry_state(latest_session)
        latest_session.policy_decision = LoopPolicyDecision(
            policy_outcome="paused",
            reasons=["Pause requested; automatic retry remains disabled until an explicit resume."],
            time_budget_minutes=latest_session.time_budget_minutes,
            time_budget_remaining_minutes=latest_session.budget_remaining_minutes,
        )
        save_session(session_path(sessions_dir, latest_session.session_id), latest_session)
        return _result_payload(latest_session, latest_session.policy_decision, "", runner_action=runner_action)
    if latest_session.status == "completed":
        latest_session.policy_decision = LoopPolicyDecision(
            policy_outcome="stopped",
            reasons=["Stop requested; automatic retry remains disabled."],
            time_budget_minutes=latest_session.time_budget_minutes,
            time_budget_remaining_minutes=latest_session.budget_remaining_minutes,
        )
        save_session(session_path(sessions_dir, latest_session.session_id), latest_session)
        return _result_payload(latest_session, latest_session.policy_decision, "", runner_action="stopped")
    return None


def _block_for_human(
    sessions_dir: Path,
    session,
    *,
    reason: str,
    last_error: str,
    category: str = "bridge_control_or_browser_error",
) -> dict[str, Any]:
    session.status = "blocked"
    _set_loop_state(session, "requires_human")
    session.auto_run_enabled = False
    session.supervisor_status = "blocked"
    session.human_attention_reason = reason
    session.last_error = last_error
    session.policy_decision = LoopPolicyDecision(
        policy_outcome="require_human",
        reasons=[reason],
        human_gate_required=True,
        human_gate_reason=reason,
        human_gate_category=category,
        time_budget_minutes=session.time_budget_minutes,
        time_budget_remaining_minutes=session.budget_remaining_minutes,
    )
    save_session(session_path(sessions_dir, session.session_id), session)
    return _result_payload(session, session.policy_decision, "", runner_action="blocked")


def _retry_without_blocking(
    sessions_dir: Path,
    session,
    *,
    reason: str,
    last_error: str = "",
    loop_state: str = "waiting_for_chatgpt",
    runner_action: str = "wait_for_chatgpt",
    degraded_mode: str = "",
    degraded_reason: str = "",
) -> dict[str, Any]:
    honored_state = _honor_non_active_session_state(sessions_dir, session, runner_action=runner_action)
    if honored_state is not None:
        return honored_state
    session.status = "active"
    _set_loop_state(session, loop_state)
    session.auto_run_enabled = True
    session.supervisor_status = "running"
    session.human_attention_reason = ""
    session.last_error = last_error or reason
    if degraded_mode:
        session.degraded_mode = degraded_mode
    if degraded_reason or reason:
        session.degraded_reason = degraded_reason or reason
    session.policy_decision = LoopPolicyDecision(
        policy_outcome="allow",
        reasons=[
            reason,
            "The loop stayed active and will retry automatically instead of requiring human attention.",
        ],
        time_budget_minutes=session.time_budget_minutes,
        time_budget_remaining_minutes=session.budget_remaining_minutes,
    )
    save_session(session_path(sessions_dir, session.session_id), session)
    return _result_payload(session, session.policy_decision, "", runner_action=runner_action)


def _assistant_message_already_processed(session, assistant_anchor: str, assistant_hash: str) -> bool:
    recorded_anchor = str(getattr(session, "last_seen_chat_message_anchor", "") or "").strip()
    recorded_hash = str(getattr(session, "latest_assistant_message_hash", "") or "").strip()
    if assistant_anchor and recorded_anchor:
        if assistant_anchor != recorded_anchor:
            return False
        if assistant_hash and recorded_hash:
            return assistant_hash == recorded_hash
        return True
    return bool(assistant_hash and assistant_hash == recorded_hash)


def _awaiting_assistant_after_return_packet(session) -> bool:
    outbound_kind = str(getattr(session, "last_outbound_user_message_kind", "") or "").strip()
    if outbound_kind == "return_packet":
        return True
    if outbound_kind:
        return False
    packet_id = str(getattr(session, "last_posted_return_packet_id", "") or "").strip()
    if not packet_id:
        return False
    last_delivery_seconds = _parse_iso_timestamp(str(getattr(session, "last_delivery_at", "") or ""))
    if last_delivery_seconds <= 0:
        return False
    last_chat_seconds = _parse_iso_timestamp(str(getattr(session, "last_chat_activity_at", "") or ""))
    return last_chat_seconds <= 0 or last_chat_seconds <= last_delivery_seconds


def _should_wait_for_missing_assistant_message(session, error_signature: str) -> bool:
    normalized = str(error_signature or "").strip().casefold()
    if "assistant_message" not in normalized:
        return False
    return bool(getattr(session, "last_outbound_user_message_anchor", "")) or _awaiting_assistant_after_return_packet(
        session
    )


def _user_override_already_processed(session, user_message: dict[str, str], user_hash: str) -> bool:
    message_anchor = str(user_message.get("message_anchor", ""))
    if message_anchor:
        return bool(message_anchor == session.last_seen_user_control_anchor)
    return bool(user_hash and user_hash == session.latest_user_control_message_hash)


def _assistant_response_in_progress(adapter, session) -> bool:
    checker = getattr(adapter, "assistant_response_in_progress", None)
    if not callable(checker):
        return False
    try:
        return bool(checker(session))
    except RuntimeError:
        return False


def _track_in_progress_assistant(session, *, assistant_anchor: str, assistant_hash: str, assistant_text: str) -> bool:
    current_time = time.time()
    normalized_text = str(assistant_text or "").strip()[:4000]
    inferred_elapsed_seconds = _parse_thinking_disclosure_elapsed_seconds(normalized_text)
    tracking_text, tracking_hash = _assistant_tracking_signature(normalized_text, assistant_hash)
    previous_tracking_text, previous_tracking_hash = _assistant_tracking_signature(
        str(session.in_progress_assistant_text or "").strip()[:4000],
        str(session.in_progress_assistant_hash or ""),
    )
    same_anchor = session.in_progress_assistant_anchor == assistant_anchor
    same_hash = previous_tracking_hash == tracking_hash
    same_text = previous_tracking_text == tracking_text
    if same_anchor and same_hash and same_text:
        seeded_time = current_time - inferred_elapsed_seconds if inferred_elapsed_seconds is not None else current_time
        started_at = session.in_progress_assistant_started_at or seeded_time
        last_progress_at = session.in_progress_assistant_last_progress_at or started_at
    else:
        if inferred_elapsed_seconds is not None:
            seeded_time = current_time - inferred_elapsed_seconds
            started_at = seeded_time
            last_progress_at = seeded_time
        else:
            started_at = (
                session.in_progress_assistant_started_at
                if same_anchor and session.in_progress_assistant_started_at
                else current_time
            )
            last_progress_at = current_time
        _record_chat_activity(session)
    session.in_progress_assistant_anchor = assistant_anchor
    session.in_progress_assistant_hash = tracking_hash
    session.in_progress_assistant_text = tracking_text
    session.in_progress_assistant_started_at = started_at
    session.in_progress_assistant_last_progress_at = last_progress_at
    stall_seconds = _assistant_stall_seconds(session, normalized_text)
    observed_elapsed_exceeds_threshold = (
        inferred_elapsed_seconds is not None and (current_time - last_progress_at) >= stall_seconds
    )
    return bool(
        (same_anchor and same_hash and same_text and (current_time - last_progress_at) >= stall_seconds)
        or observed_elapsed_exceeds_threshold
    )


def _assistant_stall_seconds(session, assistant_text: str) -> float:
    if _assistant_text_is_pathological_fragment(assistant_text):
        return _SHORT_FRAGMENT_STALL_SECONDS
    if _assistant_looks_like_thinking_disclosure(assistant_text):
        if getattr(session, "bridge_control_failure_streak", 0) or getattr(
            session,
            "last_outbound_user_message_kind",
            "",
        ) in {"repair", "recovery"}:
            return _DEGRADED_THINKING_STALL_SECONDS
        return _ASSISTANT_STALL_SECONDS
    normalized = str(assistant_text or "").strip().casefold()
    if normalized and "\n" not in normalized and len(normalized) <= 16:
        return _SHORT_FRAGMENT_STALL_SECONDS
    return _ASSISTANT_STALL_SECONDS


def _assistant_text_is_pathological_fragment(assistant_text: str) -> bool:
    normalized = str(assistant_text or "").strip().casefold()
    return normalized in _PATHOLOGICAL_ASSISTANT_FRAGMENTS


def _parse_iso_timestamp(value: str) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


def _assistant_looks_like_thinking_disclosure(assistant_text: str) -> bool:
    raw_text = str(assistant_text or "").strip()
    return _assistant_terminal_thinking_disclosure(raw_text) is not None


def _assistant_tracking_signature(assistant_text: str, assistant_hash: str) -> tuple[str, str]:
    normalized_text = str(assistant_text or "").strip()[:4000]
    normalized_hash = str(assistant_hash or "").strip()
    if _assistant_looks_like_thinking_disclosure(normalized_text) or normalized_hash == "thinking":
        return "thinking", "thinking"
    return normalized_text, normalized_hash


def _parse_thinking_disclosure_elapsed_seconds(assistant_text: str) -> float | None:
    raw_text = str(assistant_text or "").strip()
    disclosure_text = _assistant_terminal_thinking_disclosure(raw_text)
    if disclosure_text is None:
        return None
    normalized = disclosure_text.casefold()
    if normalized.startswith("thought for "):
        suffix = normalized[len("thought for ") :]
    elif normalized.startswith("nachgedacht für "):
        suffix = normalized[len("nachgedacht für ") :]
    else:
        return None
    matches = re.findall(r"(\d+)\s*([hms])", suffix)
    if not matches:
        return None
    total_seconds = 0
    multipliers = {"h": 3600, "m": 60, "s": 1}
    for value, unit in matches:
        total_seconds += int(value) * multipliers[unit]
    return float(total_seconds)


def _assistant_terminal_thinking_disclosure(assistant_text: str) -> str | None:
    raw_text = str(assistant_text or "").strip()
    if not raw_text:
        return None
    if any(pattern.fullmatch(raw_text) for pattern in _THINKING_DISCLOSURE_PATTERNS):
        return raw_text
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    if not lines:
        return None
    terminal_line = lines[-1]
    if any(pattern.fullmatch(terminal_line) for pattern in _THINKING_DISCLOSURE_PATTERNS):
        return terminal_line
    return None


def _assistant_failure_anchor_key(assistant_text: str, assistant_hash: str) -> str:
    if _assistant_looks_like_thinking_disclosure(assistant_text):
        return "thinking"
    return (assistant_hash or "assistant")[:12]


def _clear_in_progress_assistant(session) -> None:
    session.in_progress_assistant_anchor = ""
    session.in_progress_assistant_hash = ""
    session.in_progress_assistant_text = ""
    session.in_progress_assistant_started_at = 0.0
    session.in_progress_assistant_last_progress_at = 0.0


def _set_loop_state(session, value: str) -> None:
    normalized = str(value or "").strip()
    if session.loop_state != normalized or not str(getattr(session, "phase_started_at", "") or "").strip():
        session.phase_started_at = now_iso()
    session.loop_state = normalized


def _record_chat_activity(session) -> None:
    session.last_chat_activity_at = now_iso()


def _record_codex_activity(session) -> None:
    session.last_codex_activity_at = now_iso()


def _record_outbound_user_message(session, *, message_anchor: str, kind: str) -> None:
    session.last_outbound_user_message_anchor = str(message_anchor or "")
    session.last_outbound_user_message_kind = str(kind or "")
    session.last_outbound_user_message_sent_at = time.time() if message_anchor else 0.0
    if message_anchor:
        session.last_delivery_at = now_iso()


def _clear_outbound_user_message(session) -> None:
    session.last_outbound_user_message_anchor = ""
    session.last_outbound_user_message_kind = ""
    session.last_outbound_user_message_sent_at = 0.0


def _refresh_outbound_user_message_timer(session) -> None:
    if getattr(session, "last_outbound_user_message_anchor", "") or _awaiting_assistant_after_return_packet(session):
        session.last_outbound_user_message_sent_at = time.time()


def _outbound_user_message_stalled(session) -> bool:
    sent_at = float(getattr(session, "last_outbound_user_message_sent_at", 0.0) or 0.0)
    if sent_at > 0.0 and (
        getattr(session, "last_outbound_user_message_anchor", "") or _awaiting_assistant_after_return_packet(session)
    ):
        return (time.time() - sent_at) >= _OUTBOUND_USER_MESSAGE_TIMEOUT_SECONDS
    if not _awaiting_assistant_after_return_packet(session):
        return False
    delivered_at = _parse_iso_timestamp(str(getattr(session, "last_delivery_at", "") or ""))
    if delivered_at <= 0.0:
        return False
    return (time.time() - delivered_at) >= _OUTBOUND_USER_MESSAGE_TIMEOUT_SECONDS


def _cancel_stalled_assistant_response(adapter, session) -> bool:
    canceller = getattr(adapter, "cancel_assistant_response", None)
    if not callable(canceller):
        return False
    try:
        return bool(canceller(session))
    except RuntimeError:
        return False


def _latest_user_bridge_control_override(adapter, session):
    messages: list[dict[str, str]] = []
    read_recent_user_messages = getattr(adapter, "read_recent_user_messages", None)
    if callable(read_recent_user_messages):
        try:
            recent_messages = read_recent_user_messages(session, limit=8)
        except RuntimeError:
            recent_messages = []
        messages.extend(item for item in recent_messages if isinstance(item, dict))

    if not messages:
        read_user_message = getattr(adapter, "read_latest_user_message", None)
        if not callable(read_user_message):
            return None
        try:
            user_message = read_user_message(session)
        except RuntimeError:
            return None
        if isinstance(user_message, dict):
            messages.append(user_message)

    boundary_index = _latest_bridge_packet_message_index(messages, getattr(session, "last_posted_return_packet_id", ""))
    candidate_messages = messages[boundary_index + 1 :] if boundary_index >= 0 else messages

    for user_message in reversed(candidate_messages):
        user_text = str(user_message.get("text", ""))
        if not user_text:
            continue
        if _is_repair_prompt_message(user_text):
            continue
        user_hash = hashlib.sha1(user_text.encode("utf-8")).hexdigest()
        user_anchor = str(user_message.get("message_anchor", ""))
        if user_anchor and user_anchor == session.last_seen_user_control_anchor:
            continue
        if not user_anchor and user_hash and user_hash == session.latest_user_control_message_hash:
            continue
        try:
            envelope = extract_bridge_control_envelope(user_text)
        except BridgeControlParseError:
            continue
        return envelope, user_message, user_hash
    return None


def _is_repair_prompt_message(text: str) -> bool:
    first_line = str(text or "").strip().splitlines()[0].strip()
    return first_line.startswith("[repair-") or first_line.startswith("[recovery-")


def _latest_bridge_packet_message_index(messages: list[dict[str, str]], return_packet_id: str) -> int:
    packet_id = str(return_packet_id or "").strip()
    latest_index = -1
    for index, message in enumerate(messages):
        text = str(message.get("text", ""))
        if packet_id and packet_id in text:
            latest_index = index
            continue
        if _looks_like_bridge_return_packet_message(text):
            latest_index = index
    return latest_index


def _looks_like_bridge_return_packet_message(text: str) -> bool:
    normalized = str(text or "")
    if "Session id:" not in normalized or "Here is what Codex wrote:" not in normalized:
        return False
    if "Technical loop context:" in normalized and "return_packet_id:" in normalized:
        return True
    return (
        "- refresh your understanding of the current project sources and plan" in normalized
        and "- write your whole actionable reply as the next plain-language prompt for Codex"
        in normalized
    )


def _should_attempt_fresh_chat_failover(error_signature: str) -> bool:
    normalized = str(error_signature or "").strip().casefold()
    return "fresh chatgpt conversation" in normalized


def _project_root_from_sessions_dir(sessions_dir: Path) -> Path:
    parent = sessions_dir.parent
    if parent.name == "state":
        return parent.parent
    return parent


def _load_latest_run_report(sessions_dir: Path, session_id: str) -> RunReport | None:
    runs_root = _project_root_from_sessions_dir(sessions_dir) / "artifacts" / "runs"
    if not runs_root.exists():
        return None
    for run_dir in sorted(runs_root.glob(f"*-{session_id}"), reverse=True):
        report_path = run_dir / "run_report.json"
        if not report_path.exists():
            continue
        try:
            report = RunReport.from_dict(json.loads(report_path.read_text()))
        except (json.JSONDecodeError, ValueError, KeyError, TypeError):
            continue
        if _normalize_report_delivery_state(report):
            save_json(report_path, report.as_dict())
        return report
    return None


def _synthesize_latest_completed_run_report(sessions_dir: Path, session) -> RunReport | None:
    session_id = str(getattr(session, "session_id", "") or "").strip()
    if not session_id:
        return None
    runs_root = _project_root_from_sessions_dir(sessions_dir) / "artifacts" / "runs"
    if not runs_root.exists():
        return None
    latest_run_dir = next(iter(sorted(runs_root.glob(f"*-{session_id}"), reverse=True)), None)
    if latest_run_dir is None:
        return None
    report_path = latest_run_dir / "run_report.json"
    if report_path.exists():
        return None
    if not _completed_run_dir_has_final_message(latest_run_dir):
        return None
    if not _run_dir_has_completed_turn_event(latest_run_dir):
        return None
    report = _build_recovered_run_report_from_artifacts(latest_run_dir, session)
    save_json(report_path, report.as_dict())
    return report


def _completed_run_dir_has_final_message(run_dir: Path) -> bool:
    last_message = run_dir / "last_message.md"
    try:
        return last_message.exists() and last_message.stat().st_size > 0
    except OSError:
        return False


def _run_dir_has_completed_turn_event(run_dir: Path) -> bool:
    markers = (
        '"type":"turn.completed"',
        '"type": "turn.completed"',
        '"method":"turn/completed"',
        '"method": "turn/completed"',
        "turn.completed",
        "turn/completed",
    )
    for name in ("stdout.jsonl", "live_output.log"):
        path = run_dir / name
        if not path.exists():
            continue
        if _file_contains_any_marker(path, markers):
            return True
    return False


def _file_contains_any_marker(path: Path, markers: tuple[str, ...]) -> bool:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if any(marker in line for marker in markers):
                    return True
    except OSError:
        return False
    return False


def _build_recovered_run_report_from_artifacts(run_dir: Path, session) -> RunReport:
    last_message_path = run_dir / "last_message.md"
    stdout_path = run_dir / "stdout.jsonl"
    live_output_path = run_dir / "live_output.log"
    stderr_path = run_dir / "stderr.txt"
    prompt_path = run_dir / "prompt.md"
    try:
        final_agent_message = last_message_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        final_agent_message = ""
    thread_id = (
        str(getattr(session, "current_codex_thread_id", "") or "").strip()
        or str(getattr(session, "current_codex_run_id", "") or "").strip()
        or run_dir.name
    )
    usage = _extract_completed_turn_usage(stdout_path)
    return RunReport(
        timestamp=now_iso(),
        thread_id=thread_id,
        summary=_derive_recovered_report_summary(final_agent_message),
        files_touched=[],
        checks=[],
        blockers=[],
        risks=[],
        next_step=_derive_recovered_report_next_step(final_agent_message),
        workspace_path=str(getattr(session, "workspace_path", "") or ""),
        observed_codex_thread_id=thread_id,
        final_agent_message=final_agent_message,
        event_types=["turn.completed"],
        usage=usage,
        artifacts_dir=str(run_dir),
        prompt_path=str(prompt_path) if prompt_path.exists() else "",
        raw_output_path=str(stdout_path) if stdout_path.exists() else str(live_output_path),
        last_message_path=str(last_message_path),
        stderr_path=str(stderr_path) if stderr_path.exists() else "",
        session_live_log_path=str(live_output_path) if live_output_path.exists() else "",
        exit_code=0,
        interruption_reason="none",
        session_id=str(getattr(session, "session_id", "") or ""),
        bridge_session_id=str(getattr(session, "session_id", "") or ""),
        binding_id=str(getattr(session, "binding_id", "") or ""),
        run_id=run_dir.name,
        requested_codex_thread_id=str(getattr(session, "current_codex_run_id", "") or ""),
        codex_thread_id=thread_id,
    )


def _extract_completed_turn_usage(stdout_path: Path) -> dict[str, int]:
    if not stdout_path.exists():
        return {}
    usage: dict[str, int] = {}
    try:
        lines = stdout_path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return {}
    with lines:
        for line in lines:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            event_type = str(payload.get("type", "") or "")
            method = str(payload.get("method", "") or "")
            if event_type == "turn.completed":
                raw_usage = payload.get("usage", {})
            elif method == "turn/completed":
                params = payload.get("params", {})
                raw_usage = params.get("usage", {}) if isinstance(params, dict) else {}
            else:
                continue
            if isinstance(raw_usage, dict):
                usage = {str(key): int(value) for key, value in raw_usage.items() if isinstance(value, int)}
    return usage


def _derive_recovered_report_summary(final_agent_message: str) -> str:
    for line in str(final_agent_message or "").splitlines():
        normalized = line.strip().lstrip("#").strip()
        if normalized:
            return normalized[:240]
    return "Recovered completed Codex run from local artifacts."


def _derive_recovered_report_next_step(final_agent_message: str) -> str:
    lines = str(final_agent_message or "").splitlines()
    for index, line in enumerate(lines):
        normalized = line.strip().casefold()
        if normalized.startswith("suggested next step") or normalized.startswith("next step"):
            remainder = line.split(":", 1)[1].strip() if ":" in line else ""
            if remainder:
                return remainder[:500]
            for candidate in lines[index + 1 : index + 5]:
                candidate = candidate.strip().lstrip("-*").strip()
                if candidate:
                    return candidate[:500]
    return ""


def _fresh_chat_candidate_urls(chat_url: str) -> list[str]:
    raw_url = str(chat_url or "").strip()
    if not raw_url:
        return []
    parsed = urlsplit(raw_url)
    if not parsed.scheme or not parsed.netloc:
        return []
    segments = [segment for segment in parsed.path.split("/") if segment]
    candidates: list[str] = []
    if "c" in segments:
        segments = segments[: segments.index("c")]
    if segments:
        project_new = urlunsplit((parsed.scheme, parsed.netloc, f"/{'/'.join(segments)}/new", "", ""))
        candidates.append(project_new)
    generic_new = urlunsplit((parsed.scheme, parsed.netloc, "/new", "", ""))
    if generic_new not in candidates:
        candidates.append(generic_new)
    return candidates


def _resolve_rebound_chat_url(adapter, session, candidate_url: str) -> str:
    rebound_chat_url = ""
    for _ in range(5):
        try:
            observed = str(adapter.current_chat_url(session) or "").strip()
        except RuntimeError:
            observed = ""
        if observed:
            rebound_chat_url = observed
            if not _looks_like_fresh_chat_launcher_url(observed):
                return observed
        time.sleep(0.25)
    return rebound_chat_url or str(candidate_url or "").strip()


def _looks_like_fresh_chat_launcher_url(chat_url: str) -> bool:
    raw_url = str(chat_url or "").strip()
    if not raw_url:
        return False
    parsed = urlsplit(raw_url)
    path = parsed.path.rstrip("/")
    return path == "/new" or path.endswith("/new")


def _request_bridge_control_repair(
    adapter,
    session,
    *,
    assistant_message: dict[str, str],
    assistant_hash: str,
    parse_error: str,
):
    failure_streak = _increment_bridge_control_failure_streak(session)
    observed_failure_streak = _observed_recent_bridge_repair_streak(adapter, session)
    if observed_failure_streak > 0:
        failure_streak = max(failure_streak, observed_failure_streak + 1)
        session.bridge_control_failure_streak = failure_streak
    message_kind = "repair"
    assistant_text = str(assistant_message.get("text", ""))
    pathological_fragment = _assistant_text_is_pathological_fragment(assistant_text)
    thinking_disclosure = _assistant_looks_like_thinking_disclosure(assistant_text)
    assistant_failure_key = _assistant_failure_anchor_key(assistant_text, assistant_hash)
    repair_anchor = _next_repair_anchor(adapter, session, assistant_failure_key)
    repair_attempt_number = _repair_attempt_number(repair_anchor)
    if pathological_fragment:
        repair_text = _render_bridge_control_minimal_json_repair_prompt(
            session_id=session.session_id,
            repair_anchor=repair_anchor,
        )
    else:
        repair_text = _render_bridge_control_repair_prompt(
            session_id=session.session_id,
            assistant_message=assistant_message,
            parse_error=parse_error,
            repair_anchor=repair_anchor,
            repair_attempt_number=repair_attempt_number,
        )
    parse_error_text = str(parse_error or "").casefold()
    recovery_threshold = _REPAIR_RECOVERY_THRESHOLD
    if "stalled in-progress" in parse_error_text and (pathological_fragment or thinking_disclosure):
        recovery_threshold = min(recovery_threshold, 2)
    if "no new assistant response arrived after the latest repair request" in parse_error_text:
        recovery_threshold = min(recovery_threshold, _NO_FOLLOWUP_REPAIR_RECOVERY_THRESHOLD)
    if failure_streak >= recovery_threshold:
        message_kind = "recovery"
        if _observed_recent_recovery_prompt_count(
            adapter,
            session,
            required_signature="fresh complete plain-language next prompt for codex",
            required_anchor_prefix=f"[recovery-{session.session_id}-{assistant_failure_key}-",
        ) >= _MAX_RECOVERY_ATTEMPTS_BEFORE_FRESH_CHAT_REQUIRED:
            return {
                "status": "failed",
                "error_signature": (
                    "ChatGPT conversation remained malformed after repeated recovery rebriefs; "
                    "manual intervention or a fresh ChatGPT conversation is required."
                ),
            }
        repair_anchor = _next_recovery_anchor(adapter, session, assistant_failure_key)
        repair_text = _render_bridge_control_recovery_prompt(
            session_id=session.session_id,
            repo_path=str(session.repo_path or session.workspace_path or "").strip(),
            last_posted_return_packet_id=str(session.last_posted_return_packet_id or "").strip(),
            assistant_message=assistant_message,
            parse_error=parse_error,
            repair_anchor=repair_anchor,
        )

    response = dict(adapter.post_user_message(session, repair_text, repair_anchor))
    if str(response.get("status", "")) == "delivered":
        return {"status": "requested", "message_anchor": repair_anchor, "message_kind": message_kind}
    if _confirm_message_visible(adapter, session, repair_anchor):
        return {"status": "requested", "message_anchor": repair_anchor, "message_kind": message_kind}
    return {
        "status": "failed",
        "error_signature": str(response.get("error_signature", "")).strip() or "Repair message delivery failed.",
    }


def _next_repair_anchor(adapter, session, assistant_key: str) -> str:
    return _next_repair_like_anchor(adapter, session, prefix="repair", assistant_key=assistant_key)


def _next_recovery_anchor(adapter, session, assistant_key: str) -> str:
    return _next_repair_like_anchor(adapter, session, prefix="recovery", assistant_key=assistant_key)


def _next_repair_like_anchor(adapter, session, *, prefix: str, assistant_key: str) -> str:
    base_anchor = f"{prefix}-{session.session_id}-{(assistant_key or 'assistant')[:12]}"
    read_recent_user_messages = getattr(adapter, "read_recent_user_messages", None)
    suffix = 1
    if callable(read_recent_user_messages):
        try:
            recent_messages = read_recent_user_messages(session, limit=20)
        except RuntimeError:
            recent_messages = []
        seen = {
            str(item.get("text", "")).splitlines()[0].strip().strip("[]")
            for item in recent_messages
            if isinstance(item, dict) and str(item.get("text", "")).strip()
        }
        while f"{base_anchor}-{suffix}" in seen:
            suffix += 1
    return f"{base_anchor}-{suffix}"


def _repair_attempt_number(repair_anchor: str) -> int:
    suffix = str(repair_anchor).rsplit("-", 1)[-1].strip()
    if suffix.isdigit():
        return max(int(suffix), 1)
    return 1


def _render_bridge_control_repair_prompt(
    *,
    session_id: str,
    assistant_message: dict[str, str],
    parse_error: str,
    repair_anchor: str,
    repair_attempt_number: int,
) -> str:
    assistant_text = str(assistant_message.get("text", "")).strip()
    assistant_excerpt = assistant_text[:800]
    if repair_attempt_number <= 1:
        lines = [
            f"[{repair_anchor}]",
            "Your last reply was incomplete or malformed for this session.",
            f"Problem detected: {parse_error}",
            "",
            "Reply again now and write only one complete plain-language next prompt for Codex for the same intended action as your previous reply.",
            "Do not answer with JSON, bridge-control, YAML, markdown fences, or any other transport wrapper.",
            "Do not use GitHub connectors, browsing, or any tools for this reply.",
            "Use only the existing chat context and the latest valid Codex packet already pasted in this conversation.",
            "",
            "Requirements:",
            f'- start with the exact line `Session id: {session_id}`',
            "- preserve the same intended substantive direction as your previous reply",
            "- keep the prompt detailed, structured, and human-readable",
            "- do not redirect Codex to another repo unless the user explicitly changes the binding",
            "- if a missing dependency, SDK, toolchain, browser, app, helper, CLI, runtime, or host permission is blocking real progress and it is directly installable or enableable on this machine, tell Codex to install or enable it instead of working around the gap",
            "- if the user already made authenticated local state, open apps, open browser sessions, or granted local capabilities available on this machine, tell Codex to use them operationally when they help real progress",
            "- do not add explanations, analysis about the bridge, or any text before or after the prompt",
        ]
    elif repair_attempt_number == 2:
        lines = [
            f"[{repair_anchor}]",
            "Your last reply is still incomplete for this session.",
            f"Problem detected: {parse_error}",
            "",
            "Do not reply with JSON, bridge-control, YAML, markdown fences, or a partial fragment.",
            "Reply now with one complete plain-language next prompt for Codex.",
            "If the prior intended action is unclear, choose the safest concrete next step yourself so the loop can continue.",
            "Do not use GitHub connectors, browsing, or any tools for this reply.",
            "Use only the existing chat context and the latest valid Codex packet already pasted in this conversation.",
            "",
            "Hard requirements:",
            f'- start with the exact line `Session id: {session_id}`',
            "- the prompt must contain multiple detailed lines",
            "- keep moving the work forward instead of switching into meta-analysis",
            "- stay in the same bound repo unless the user explicitly changes the binding",
            "- if a missing dependency, SDK, toolchain, browser, app, helper, CLI, runtime, or host permission is blocking real progress and it is directly installable or enableable on this machine, tell Codex to install or enable it instead of working around the gap",
            "- if the user already made authenticated local state, open apps, open browser sessions, or granted local capabilities available on this machine, tell Codex to use them operationally when they help real progress",
            "- do not add any text before or after the prompt",
            "",
            "If you are unsure, use this safe default shape:",
            f"Session id: {session_id}",
            "",
            "- analyze the latest Codex output and visible trace deeply before choosing the next safe step",
            "- continue toward the broader project goal and prioritize the next real operational or product step",
            "- stay on the current main implementation strand and do not divert into handoff churn, repo cleanup, or meta side quests unless that is the clearest blocker to forward progress",
            "- stay in the same bound repo unless the user explicitly changes the binding",
            "- do not ask Codex to create or update standalone continuation or meta files such as PLAN.md, HANDOFF.md, PROJECT_STATE.md, NEXT_PROMPT.md, or CODEX.md unless the human explicitly asked or durable truth changed after real progress",
            "- if a missing dependency, SDK, toolchain, browser, app, helper, CLI, runtime, or host permission is blocking real progress and it is directly installable or enableable on this machine, install or enable it instead of working around the gap",
            "- use already-available authenticated local state, open apps, open browser sessions, and granted local capabilities whenever they materially help progress",
            "- if the user already supplied concrete credentials or authorized live secure sources, pass that through to Codex instead of defaulting to a repo-only credential search",
            "- answer in a detailed human-readable way",
            "",
            "Your response is invalid if it stops after the first line or falls back to transport syntax.",
        ]
    else:
        lines = [
            f"[{repair_anchor}]",
            "Your last reply is still incomplete.",
            f"Problem detected: {parse_error}",
            "",
            "Reply now with exactly one complete plain-language next prompt for Codex and nothing else.",
            "Do not stop after the first line or switch to transport syntax.",
            "Do not use GitHub connectors, browsing, or any tools for this reply.",
            "Use only the existing chat context and the latest valid Codex packet already pasted in this conversation.",
            "",
            f"Session id: {session_id}",
            "",
            "- stay in the same bound repo only",
            "- analyze the latest Codex output and visible trace deeply before choosing the next safe step",
            "- continue toward the broader project goal and prioritize the next real operational or product step",
            "- stay on the current main implementation strand and do not divert into handoff churn, repo cleanup, or meta side quests unless that is the clearest blocker to forward progress",
            "- only update durable docs at the end if repo truth changed",
            "- do not ask Codex to create or update standalone continuation or meta files such as PLAN.md, HANDOFF.md, PROJECT_STATE.md, NEXT_PROMPT.md, or CODEX.md unless the human explicitly asked or durable truth changed after real progress",
            "- if a missing dependency, SDK, toolchain, browser, app, helper, CLI, runtime, or host permission is blocking real progress and it is directly installable or enableable on this machine, install or enable it instead of working around the gap",
            "- use already-available authenticated local state, open apps, open browser sessions, and granted local capabilities whenever they materially help progress",
            "- if the user already supplied concrete credentials or authorized live secure sources, pass that through to Codex instead of defaulting to a repo-only credential search",
            "- answer in a detailed human-readable way",
        ]
    if assistant_excerpt:
        lines.extend(
            [
                "",
                "Your malformed previous reply began like this:",
                assistant_excerpt,
            ]
        )
    return "\n".join(lines)


def _render_bridge_control_minimal_json_repair_prompt(*, session_id: str, repair_anchor: str) -> str:
    return "\n".join(
        [
            f"[{repair_anchor}]",
            "Reply now with exactly one complete plain-language next prompt for Codex and nothing else.",
            "Do not reply with only bridge, bridge-control, JSON, YAML, or a partial fragment.",
            "Do not use GitHub connectors, browsing, or any tools for this reply.",
            "Use only the existing chat context and the latest valid Codex packet already pasted in this conversation.",
            "",
            f"Session id: {session_id}",
            "",
            "- stay in the bound repo only",
            "- analyze the latest Codex result deeply and choose the next safe step",
            "- prioritize the next real operational or product move",
            "- only update durable docs at the end if repo truth changed",
            "- if a missing dependency, SDK, toolchain, browser, app, helper, CLI, runtime, or host permission is blocking real progress and it is directly installable or enableable on this machine, install or enable it instead of working around the gap",
            "- use already-available authenticated local state, open apps, open browser sessions, and granted local capabilities whenever they materially help progress",
            "- if the user already supplied concrete credentials or authorized live secure sources, pass that through to Codex instead of defaulting to a repo-only credential search",
            "- answer in a detailed human-readable way",
        ]
    )


def _render_bridge_control_recovery_prompt(
    *,
    session_id: str,
    repo_path: str,
    last_posted_return_packet_id: str,
    assistant_message: dict[str, str],
    parse_error: str,
    repair_anchor: str,
) -> str:
    assistant_text = str(assistant_message.get("text", "")).strip()
    assistant_excerpt = assistant_text[:800]
    packet_id = last_posted_return_packet_id or "unknown"
    repo_line = repo_path or "the currently bound repo"
    lines = [
        f"[{repair_anchor}]",
        "The last several assistant replies in this chat were malformed partial outputs.",
        f"Problem detected: {parse_error}",
        "",
        "Ignore the malformed partial replies immediately above this message.",
        "Re-anchor on the latest valid bridge context earlier in this same chat before answering.",
        "Specifically reread the most recent message from me that starts with this exact line:",
        f'- "Session id: {session_id}"',
        "and contains all of these markers:",
        '- "Here is what Codex wrote:"',
        '- "- refresh your understanding of the current project sources and plan"',
        '- "- write your whole actionable reply as the next plain-language prompt for Codex"',
        f'- if the most recent valid packet is from the older format, it may also contain "return_packet_id: {packet_id}"',
        "",
        "Then reply once with one fresh complete plain-language next prompt for Codex for this same session.",
        "Do not mention orchestration, controllers, or ping-pong systems.",
        "Do not answer with bridge-control, JSON, YAML, markdown fences, or a partial fragment.",
        "Do not use GitHub connectors, browsing, or any tools for this reply.",
        "Use only the existing chat context and the latest valid Codex packet already pasted in this conversation.",
        "",
        "Requirements for the new prompt:",
        f'- start with the exact line `Session id: {session_id}`',
        f'- require Codex to stay in this bound repo path: "{repo_line}"',
        "- make the prompt detailed, structured, and human-readable",
        "- make the prompt reflect the latest valid Codex packet, the broader project plan, and the important decisions already made in this chat",
        "- if same-thread continuity matters, say that in plain prose inside the prompt instead of emitting transport syntax",
        "- if a fresh thread is obviously needed, provide compact startup context in plain prose without turning the run into docs-first work",
        "- if a missing dependency, SDK, toolchain, browser, app, helper, CLI, runtime, or host permission is blocking real progress and it is directly installable or enableable on this machine, tell Codex to install or enable it instead of working around the gap",
        "- if the user already made authenticated local state, open apps, open browser sessions, or granted local capabilities available on this machine, tell Codex to use them operationally when they help real progress",
        "- if the user already supplied concrete credentials or authorized live secure sources, tell Codex to use or inspect those first instead of defaulting to a repo-only credential hunt",
        "- tell Codex to keep durable doc updates secondary to the main work and do them only after real progress when durable project truth changed",
        "- tell Codex to stay on the current main implementation strand and not drift into handoff churn, repo cleanup, or meta side quests unless that is the clearest blocker to forward progress",
        "- tell Codex not to create or update standalone continuation or meta files such as PLAN.md, HANDOFF.md, PROJECT_STATE.md, NEXT_PROMPT.md, or CODEX.md unless the human explicitly asked or durable truth changed after the main work",
        "- output only the prompt and nothing before or after it",
        "",
        "Output exactly this shape with real values filled in:",
        f"Session id: {session_id}",
        "",
        "- stay in this bound repo only",
        "- analyze the latest Codex output deeply before choosing the next safe step",
        "- continue toward the broader project goal and prioritize the next real operational or product step",
        "- stay on the current main implementation strand and do not drift into handoff churn, repo cleanup, or meta side quests unless that is the clearest blocker to forward progress",
        "- if a missing dependency, SDK, toolchain, browser, app, helper, CLI, runtime, or host permission is blocking real progress and it is directly installable or enableable on this machine, install or enable it instead of working around the gap",
        "- use already-available authenticated local state, open apps, open browser sessions, and granted local capabilities whenever they materially help progress",
        "- keep durable doc work secondary to real progress",
        "- do not create or update standalone continuation or meta files such as PLAN.md, HANDOFF.md, PROJECT_STATE.md, NEXT_PROMPT.md, or CODEX.md unless the human explicitly asked or durable truth changed after the main work",
    ]
    if assistant_excerpt:
        lines.extend(
            [
                "",
                "Malformed reply to ignore:",
                assistant_excerpt,
            ]
        )
    return "\n".join(lines)


def _confirm_message_visible(
    adapter,
    session,
    message_anchor: str,
    *,
    timeout_seconds: float = 6.0,
    poll_interval_seconds: float = 0.5,
) -> bool:
    deadline = time.monotonic() + max(timeout_seconds, 0.0)
    while time.monotonic() <= deadline:
        if adapter.return_packet_visible(session, message_anchor):
            return True
        time.sleep(max(poll_interval_seconds, 0.1))
    return False


def _increment_bridge_control_failure_streak(session: Any) -> int:
    session.bridge_control_failure_streak = (
        max(int(getattr(session, "bridge_control_failure_streak", 0) or 0), 0) + 1
    )
    return session.bridge_control_failure_streak


def _reset_bridge_control_failure_streak(session: Any) -> None:
    session.bridge_control_failure_streak = 0


def _observed_recent_bridge_repair_streak(adapter: Any, session: Any) -> int:
    read_recent_user_messages = getattr(adapter, "read_recent_user_messages", None)
    if not callable(read_recent_user_messages):
        return 0
    try:
        recent_messages = read_recent_user_messages(session, limit=20)
    except RuntimeError:
        return 0
    streak = 0
    for message in reversed(recent_messages):
        text = str(message.get("text", "")).strip()
        if not text:
            continue
        if _is_repair_prompt_message(text):
            streak += 1
            continue
        break
    return streak


def _observed_recent_recovery_prompt_count(
    adapter: Any,
    session: Any,
    *,
    required_signature: str = "",
    required_anchor_prefix: str = "",
) -> int:
    read_recent_user_messages = getattr(adapter, "read_recent_user_messages", None)
    if not callable(read_recent_user_messages):
        return 0
    try:
        recent_messages = read_recent_user_messages(session, limit=20)
    except RuntimeError:
        return 0
    count = 0
    signature = str(required_signature or "").strip().casefold()
    anchor_prefix = str(required_anchor_prefix or "").strip()
    for message in reversed(recent_messages):
        text = str(message.get("text", "")).strip()
        first_line = text.splitlines()[0].strip()
        if first_line.startswith("[recovery-"):
            if anchor_prefix and not first_line.startswith(anchor_prefix):
                break
            if signature and signature not in text.casefold():
                break
            count += 1
            continue
        if first_line.startswith("[repair-"):
            continue
        break
    return count
