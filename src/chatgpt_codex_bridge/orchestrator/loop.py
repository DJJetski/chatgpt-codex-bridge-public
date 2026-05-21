from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ..models import derive_return_packet_id
from .browser import enrich_browser_blocker_reason, normalize_stop_command_event, stop_command_already_processed
from .browser_support import (
    _looks_like_host_browser_transport_failure_message,
    assistant_message_looks_like_retryable_error,
    canonical_delivery_error_signature,
)
from .loop_support import (
    _DELIVERY_CONFIRMATION_GRACE_INTERVAL_SECONDS,
    _DELIVERY_CONFIRMATION_GRACE_POLLS,
    _RETRYABLE_DELIVERY_ERROR_SIGNATURES,
    _block_for_human,
    _assistant_looks_like_thinking_disclosure,
    _assistant_message_already_processed,
    _assistant_response_in_progress,
    _awaiting_assistant_after_return_packet,
    _clear_delivery_retry_state,
    _clear_in_progress_assistant,
    _clear_outbound_user_message,
    _honor_non_active_session_state,
    _load_latest_run_report,
    _outbound_user_message_stalled,
    _persist_run_report,
    _project_root_from_sessions_dir,
    _record_chat_activity,
    _record_codex_activity,
    _record_outbound_user_message,
    _refresh_outbound_user_message_timer,
    _retry_without_blocking,
    _result_payload,
    _set_loop_state,
    _should_wait_for_missing_assistant_message,
    _synthesize_latest_completed_run_report,
    _track_in_progress_assistant,
)
from .models import ChatDeliveryAttempt, LoopPolicyDecision, refresh_session_budget
from .packets import build_return_packet, render_return_packet
from .policy import resolve_instruction_texts
from .state import (
    load_chat_bindings,
    load_orchestrator_policy,
    load_session,
    save_session,
    session_path,
)
_PENDING_RETURN_PACKET_RETRY_COOLDOWN_SECONDS = 30.0
_PENDING_RETURN_PACKET_HOST_TRANSPORT_RETRY_COOLDOWN_SECONDS = 300.0
_RETRY_REQUIRED_DELIVERY_STATUS = "retry_required"
_MAX_SILENT_ASSISTANT_RECOVERY_ATTEMPTS = 2
_RETRYABLE_CODEX_RUNTIME_FAILURE_MARKERS = (
    "failed to parse function arguments: eof while parsing an object",
    "stream disconnected - retrying sampling request",
    "failed to record rollout items: thread",
)
_STALE_PENDING_RETURN_PACKET_ERROR_MARKERS = (
    "different assistant turn than the one that started this codex run",
)
_RUN_DIR_ID_RE = re.compile(r"\b\d{8}T\d{6}-session-[A-Za-z0-9-]+\b")
_INDEXED_MESSAGE_ANCHOR_RE = re.compile(r"^(?P<role>assistant|user)-(?P<index>\d+)-[0-9a-f]{12}$")
_STALE_RECOVERY_PROMPT_MARKERS = ("truncated", "recover")
_LIVE_CHAT_SURFACE_RECOVERY_MARKERS = (
    "different chat url",
    "dom contract missing `composer` selector match",
    "dom contract missing `assistant_message` selector match",
)
_LIVE_CHAT_SURFACE_RECOVERY_WAIT_SECONDS = 2.0
_PLANNER_IDLE_RECOVERY_ATTEMPT_LIMIT = 2
_PLANNER_IDLE_TASK_ABSENCE_MARKERS = (
    "no codex prompt",
    "kein codex-prompt",
    "kein weiterer codex-prompt",
    "keinen weiteren codex-prompt",
    "keinen weiteren codex-lauf",
    "do not send another codex run",
    "do not start another codex",
    "no repo-local next step",
    "kein repo-lokaler naechster schritt",
    "kein repo-lokaler nächster schritt",
    "no actionable state change",
    "no repo work",
    "keine repo-arbeit",
)
_PLANNER_IDLE_PAUSE_MARKERS = (
    "state remains paused",
    "codex remains paused",
    "codex bleibt pausiert",
    "codex pausiert lassen",
    "pause codex",
    "pausiere codex",
    "automatic retry remains disabled",
)
_PLANNER_IDLE_CHURN_MARKERS = (
    "would only repeat",
    "only repeat the same no-op",
    "only no-op",
    "no-op-loop",
    "no-op-loops",
    "more repo work right now would be churn",
    "repo work right now would be churn",
    "further runs would only",
    "weitere codex-runs",
    "weitere runs",
    "nur no-op",
    "nur churn",
)
_BLOCKED_LANE_MARKERS = (
    "backoff",
    "cooldown",
    "rate limit",
    "ratelimit",
    "retry-after",
    "retry after",
    "blocked lane",
    "blocked connector",
    "external gate",
    "external blocker",
    "auth blocker",
    "auth/session blocker",
    "permission blocker",
    "missing confirmation",
    "requires confirmation",
    "human-only",
    "unavailable data",
    "unavailable source",
    "not cleared",
    "has not cleared",
    "nicht gecleared",
    "nicht freigegeben",
)
_BLOCKED_LANE_CHURN_MARKERS = (
    "first job is always",
    "always check",
    "check whether",
    "check if",
    "probe",
    "poll",
    "retry",
    "retest",
    "rerun",
    "catch-up",
    "catch up",
    "readiness",
    "harden",
    "hardening",
    "same blocker",
    "same lane",
    "fallback work",
    "if it has not cleared",
    "if it still reports",
    "if still blocked",
    "wenn es nicht",
)
_BINDING_APPROVAL_REQUEST_MARKERS = (
    "explizite freigabe",
    "explizite erlaubnis",
    "deine freigabe",
    "deine erlaubnis",
    "explicit approval",
    "explicit permission",
    "your approval",
    "your permission",
)
_BINDING_CHANGE_MARKERS = (
    "binding wechseln",
    "binding-wechsel",
    "binding aender",
    "binding change",
    "change the binding",
    "switch binding",
    "send codex into another repo",
    "codex in ein anderes repo",
    "codex einmal in das bridge-repo",
    "codex einmal in das bridge repo",
    "in ein anderes repo wechseln",
    "anderes repo",
)
_NEW_CODEX_SESSION_MARKERS = (
    "new codex session",
    "new session id:",
    "not a resume",
    "do not resume",
    "start a new codex session",
    "starte eine neue codex-session",
    "starte eine neue codex session",
    "neue codex-session",
    "neue codex session",
    "frische codex-session",
    "frische codex session",
    "frischen prozess",
    "neuen prozess",
)
_STRONG_NEW_CODEX_SESSION_MARKERS = (
    "new codex session",
    "new session id:",
    "not a resume",
    "do not resume",
    "start a new codex session",
    "starte eine neue codex-session",
    "starte eine neue codex session",
    "neue codex-session",
    "neue codex session",
    "frische codex-session",
    "frische codex session",
)
_SAME_CODEX_SESSION_MARKERS = (
    "same bound session",
    "same codex thread",
    "same session",
    "current codex thread",
    "gleiche session",
    "gleichen session",
    "gleicher thread",
    "gleichen codex thread",
    "bestehende session",
)
_NEW_CODEX_SESSION_CONTEXT_MARKERS = (
    "codex",
    "session_id",
    "session id",
    "session-",
    "thread",
    "resume",
    "prozess",
)


def _canonical_chat_url(chat_url: str) -> str:
    raw = str(chat_url or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw)
    path_segments = [segment for segment in parsed.path.split("/") if segment]
    path = f"/{'/'.join(path_segments)}" if path_segments else ""
    return urlunsplit(
        (
            str(parsed.scheme or "https").casefold(),
            str(parsed.netloc or "").casefold(),
            path,
            "",
            "",
        )
    )

def _pending_return_packet_retry_wait_seconds(session) -> float:
    pending_anchor = str(getattr(session, "last_outbound_user_message_anchor", "") or "").strip()
    pending_kind = str(getattr(session, "last_outbound_user_message_kind", "") or "").strip()
    last_attempted_at = float(getattr(session, "last_outbound_user_message_sent_at", 0.0) or 0.0)
    if not pending_anchor or pending_kind != "return_packet_retry_pending" or last_attempted_at <= 0.0:
        return 0.0

    failure_text = " ".join(
        part.strip()
        for part in (
            str(getattr(session, "last_error", "") or ""),
            str(getattr(session, "degraded_reason", "") or ""),
        )
        if part.strip()
    )
    cooldown_seconds = _PENDING_RETURN_PACKET_RETRY_COOLDOWN_SECONDS
    if failure_text and _looks_like_host_browser_transport_failure_message(failure_text):
        cooldown_seconds = _PENDING_RETURN_PACKET_HOST_TRANSPORT_RETRY_COOLDOWN_SECONDS

    return max(0.0, cooldown_seconds - max(time.time() - last_attempted_at, 0.0))


def _delivery_error_requires_cooldown(error_signature: str) -> bool:
    normalized = str(error_signature or "").strip().casefold()
    if not normalized:
        return False
    if _looks_like_host_browser_transport_failure_message(error_signature):
        return True
    return any(
        marker in normalized
        for marker in (
            "syntax error",
            "connection invalid",
            "kann nicht gelesen werden",
        )
    )


def _delivery_attempts_need_foreground_browser_reopen(attempts: list[dict[str, Any]]) -> bool:
    signatures = [
        str(item.get("error_signature", "") or "").strip().casefold()
        for item in attempts
        if isinstance(item, dict)
    ]
    return any(
        marker in signature
        for signature in signatures
        for marker in _LIVE_CHAT_SURFACE_RECOVERY_MARKERS
    )


def _pending_return_packet_became_stale(session, error_signatures: list[str]) -> bool:
    if str(getattr(session, "last_outbound_user_message_kind", "") or "").strip() != "return_packet_retry_pending":
        return False
    return _return_packet_delivery_became_stale(error_signatures)


def _same_indexed_message_turn(left_anchor: str, right_anchor: str) -> bool:
    left = _INDEXED_MESSAGE_ANCHOR_RE.match(str(left_anchor or "").strip())
    right = _INDEXED_MESSAGE_ANCHOR_RE.match(str(right_anchor or "").strip())
    if not left or not right:
        return False
    return left.group("role") == right.group("role") and left.group("index") == right.group("index")


def _return_packet_delivery_became_stale(error_signatures: list[str]) -> bool:
    normalized_signatures = [str(signature or "").strip().casefold() for signature in error_signatures]
    return any(
        marker in signature
        for signature in normalized_signatures
        for marker in _STALE_PENDING_RETURN_PACKET_ERROR_MARKERS
    )


def _allow_policy_decision(session, reasons: list[str]) -> LoopPolicyDecision:
    return LoopPolicyDecision(
        policy_outcome="allow",
        reasons=list(reasons),
        time_budget_minutes=session.time_budget_minutes,
        time_budget_remaining_minutes=session.budget_remaining_minutes,
    )


def _normalize_planner_text(text: str) -> str:
    return (
        str(text or "")
        .casefold()
        .replace("ä", "ae")
        .replace("ö", "oe")
        .replace("ü", "ue")
        .replace("ß", "ss")
    )


def _assistant_text_looks_like_planner_idle(assistant_text: str) -> bool:
    normalized = _normalize_planner_text(assistant_text)
    if not normalized.strip():
        return False
    absence = any(marker in normalized for marker in _PLANNER_IDLE_TASK_ABSENCE_MARKERS)
    pause = any(marker in normalized for marker in _PLANNER_IDLE_PAUSE_MARKERS)
    churn = any(marker in normalized for marker in _PLANNER_IDLE_CHURN_MARKERS)
    external_gate = any(
        marker in normalized
        for marker in (
            "external decision",
            "externen entscheidungs-gate",
            "explizite freigabe",
            "explicit authorization",
            "explicit approval",
            "backoff",
            "waiting for either",
            "wartet auf entweder",
        )
    )
    return absence and (pause or churn or external_gate)


def _assistant_text_looks_like_blocked_lane_churn(assistant_text: str) -> bool:
    normalized = _normalize_planner_text(assistant_text)
    if not normalized.strip():
        return False
    blocked_lane = any(marker in normalized for marker in _BLOCKED_LANE_MARKERS)
    repeat_or_harden = any(marker in normalized for marker in _BLOCKED_LANE_CHURN_MARKERS)
    return blocked_lane and repeat_or_harden


def _render_planner_idle_recovery_prompt(
    *,
    session_id: str,
    repo_path: str,
    recovery_anchor: str,
    assistant_text: str,
) -> str:
    repo_line = repo_path or "the currently bound repo"
    excerpt = str(assistant_text or "").strip()[:900]
    lines = [
        f"Session id: {session_id}",
        f"planner_idle_recovery: {recovery_anchor}",
        "",
        "Your latest assistant reply did not contain a runnable Codex prompt.",
        "Do not answer with `No Codex prompt`, `No-op`, `pause Codex`, or an equivalent global stop.",
        "",
        "General bridge policy:",
        "- Normal bridge behavior is to keep finding substantial repo-local work from the current plan; this recovery path is a last-resort guardrail, not a normal stop path.",
        "- A blocked external gate, missing confirmation, cooldown, backoff, auth flow, or unavailable data source blocks only that specific lane.",
        "- It does not block the entire bound repo or project unless every safe repo-local and already-authorized work lane is genuinely exhausted.",
        "- If one lane is blocked, choose another safe substantial task from the current project plan, repo docs, source tree, tests, data backfills, indexes, inventory, memory/brain graph linking, media/OCR/transcription derivation, cleanup, validation, or implementation backlog.",
        "- In data, memory, search, assistant, import, or knowledge-graph repos, actual information completion is first-class product work: fill canonical stores, inventories, indexes, memories, and relationship graphs from already-authorized messages, reminders, notes, tasks, files, media metadata, transcripts, and adjacent records.",
        "- If the obvious plan items look complete, continue with data population, source inventory, memory/search/brain indexing, relationship linking, hardening, tests, validation, media OCR/transcription, structural cleanup, duplication removal, reliability, performance, or durable-truth docs that follow real code changes.",
        "- Do not spend repeated cycles only improving runners, prompts, policy gates, dashboards, or readiness checks when a safe bounded or resumable backfill can actually fill missing information coverage.",
        "- Long-running repo-native local jobs are valid progress when they are safe, already authorized, observable, resumable or bounded, and plan-aligned; do not reject OCR, audio/video transcription, attachment text extraction, media derivation, indexing, or inventory backfills merely because they may take hours.",
        "- Stay in the same bound repo and do not switch repositories.",
        f'- Bound repo path: "{repo_line}"',
        "- Respect any exact confirmation boundary from the user, but scope that boundary narrowly to the blocked action.",
        "- Only say no runnable Codex prompt exists if the whole project, its plan, and its adjacent hardening frontier are truly exhausted or blocked; in that rare case, name the exact human decision or missing artifact and do not start an automatic no-op loop.",
        "",
        "Now reply once with a fresh complete plain-language prompt for Codex.",
        "The prompt must ask Codex to take the largest safe forward step available in the bound repo.",
        "Do not use bridge-control, JSON, YAML, markdown fences, or tool calls.",
    ]
    if excerpt:
        lines.extend(["", "Idle reply to replace:", excerpt])
    return "\n".join(lines)


def _render_blocked_lane_churn_codex_prompt(
    *,
    session_id: str,
    repo_path: str,
    assistant_text: str,
) -> str:
    repo_line = repo_path or "the currently bound repo"
    lines = [
        f"Session id: {session_id}",
        "",
        "Blocked-lane anti-churn override.",
        "",
        "The ChatGPT prompt below appears to center a lane that is blocked by backoff, cooldown, auth, permission, missing confirmation, or unavailable external state.",
        "Keep the original prompt as context, but do not let that single blocked lane consume the whole run.",
        "",
        "Bound repo:",
        f"- {repo_line}",
        "",
        "Instructions:",
        "- stay in the bound repo only",
        "- if the original prompt includes a cheap repo-native status check for the blocked lane, run at most that bounded check",
        "- if the lane is still blocked, do not spend the run hardening, retesting, polishing, documenting, or repeatedly probing the same blocked lane",
        "- treat the blocked lane as only one blocked lane, not as proof that the project is out of work",
        "- reread the repo-local guidance plus the narrowest current plan/backlog/inventory/test/source files needed to choose a different safe frontier",
        "- choose a substantial alternate lane that can make real repo-local progress now: implementation, data backfill, inventory, indexing, import/sync coverage, media/OCR/transcription derivation, memory/search/brain transfer, relationship linking, tests, validation, cleanup, structure, deduplication, reliability, or performance",
        "- in data, search, assistant-memory, import, or knowledge-graph projects, prefer expanding already-authorized local inventories, source coverage, media-derived text, OCR/transcription outputs, transfer/indexing paths, memory/brain stores, and relationship graphs over repeated probing of a blocked external connector",
        "- actual data population is product work; prioritize moving already-readable messages, reminders, notes, tasks, files, media metadata, transcripts, and adjacent records into canonical stores, inventories, indexes, memory, and brain/search surfaces",
        "- do not spend repeated cycles only improving runners, prompts, policy gates, dashboards, or readiness checks when a safe bounded or resumable backfill can actually fill missing information coverage",
        "- when several source lanes exist, choose the lane with the highest real information gap and safe availability, then run or repair the smallest repo-native batch that moves real records from source to inventory/index/memory/brain surfaces",
        "- long-running repo-native local jobs are allowed when safe, already authorized, observable, resumable or bounded, and aligned with the plan; do not avoid OCR, audio/video transcription, attachment text extraction, media derivation, indexing, or inventory backfills merely because they may take hours",
        "- if starting a long job, make it resumable or bounded when the repo supports that, state the progress artifacts/logs to watch, and keep supervising it instead of treating duration as a blocker",
        "- respect exact confirmation boundaries and do not perform destructive, account, permission, secret, financial, legal, medical, CAPTCHA, or human-physical-presence actions without explicit approval",
        "- verify the touched surface with the most relevant practical checks",
        "",
        "Original ChatGPT prompt to treat as context, not as permission to churn on a blocked lane:",
        assistant_text.rstrip(),
    ]
    return "\n".join(lines).strip() + "\n"


def _render_planner_idle_fallback_codex_prompt(
    *,
    session_id: str,
    repo_path: str,
    assistant_text: str,
) -> str:
    repo_line = repo_path or "the currently bound repo"
    excerpt = str(assistant_text or "").strip()[:900]
    lines = [
        f"Session id: {session_id}",
        "",
        "Planner-idle emergency fallback.",
        "",
        "ChatGPT repeatedly replied without a runnable Codex prompt. Do not treat that as project completion.",
        "Your job is to recover productive forward motion in the bound repo without using the blocked lane that caused the idle reply.",
        "",
        "Bound repo:",
        f"- {repo_line}",
        "",
        "Instructions:",
        "- stay in the bound repo only",
        "- start by reading the repo-local guidance and the narrowest canonical plan/backlog sources for this repo, without assuming fixed filenames from another project",
        "- look for active plans, roadmaps, TODOs, issue inventories, indexes, tests, architecture notes, source hotspots, failing or missing verification, cleanup frontiers, and durable decisions",
        "- if one external lane is blocked by cooldown, auth, missing confirmation, unavailable data, a permission boundary, or an explicit human-only action, scope that blocker narrowly and choose another safe repo-local lane",
        "- choose the largest safe forward step currently available from the plan or adjacent hardening frontier",
        "- in data, memory, search, assistant, import, or knowledge-graph repos, prefer actual information completion: read already-authorized messages, reminders, notes, tasks, files, media metadata, transcripts, and adjacent records into canonical stores, inventories, indexes, memory, and brain/search relationship surfaces",
        "- if the obvious plan items appear complete, continue with data population, source inventory, memory/search/brain indexing, relationship linking, hardening, validation, media OCR/transcription, attachment text derivation, folder or code structure improvement, duplication removal, reliability, performance, tests, or durable-truth docs that follow real code changes",
        "- do not spend repeated cycles only improving runners, prompts, policy gates, dashboards, or readiness checks when a safe bounded or resumable backfill can actually fill missing information coverage",
        "- long-running repo-native local jobs are allowed when safe, already authorized, observable, resumable or bounded, and aligned with the plan; do not avoid OCR, audio/video transcription, media derivation, indexing, or inventory backfills merely because they may take hours",
        "- respect exact confirmation boundaries and do not perform destructive, account, permission, secret, financial, legal, medical, CAPTCHA, or human-physical-presence actions without explicit approval",
        "- implement or repair real project behavior when safe; do not spend the run only confirming that no work exists",
        "- verify the touched surface with the most relevant practical checks",
        "- stop only if you can prove that every safe repo-local plan, backlog, source, test, inventory, cleanup, validation, and hardening lane is complete or blocked by an exact human-only decision",
        "",
        "Return a detailed final answer that includes what you inspected, what you changed, checks run, remaining blockers, and the next best frontier.",
    ]
    if excerpt:
        lines.extend(["", "Latest idle ChatGPT reply that this fallback is replacing:", excerpt])
    return "\n".join(lines)


def _keep_waiting(
    sessions_dir: Path,
    session,
    *,
    reasons: list[str],
    loop_state: str,
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
    session.last_error = ""
    session.degraded_mode = degraded_mode
    session.degraded_reason = degraded_reason
    session.policy_decision = _allow_policy_decision(session, reasons)
    save_session(session_path(sessions_dir, session.session_id), session)
    return _result_payload(session, session.policy_decision, "", runner_action=runner_action)


def _assistant_reply_is_fragmentary(assistant_text: str) -> bool:
    normalized = str(assistant_text or "").strip().casefold()
    if not normalized:
        return True
    if normalized in {"bridge-control", "bridge", "chatgpt", "chatgpt:"}:
        return True
    if _assistant_looks_like_thinking_disclosure(assistant_text):
        return True
    if "\n" not in normalized and len(normalized) <= 16:
        return True
    return False


def _accepted_assistant_prompt_looks_retryable_error(session) -> bool:
    if str(getattr(session, "last_productive_task_label", "") or "").strip() != "accepted_assistant_text":
        return False
    return assistant_message_looks_like_retryable_error(str(getattr(session, "last_productive_prompt", "") or ""))


def _assistant_turn_can_retry(session) -> bool:
    if _awaiting_assistant_after_return_packet(session):
        return True
    return _accepted_assistant_prompt_looks_retryable_error(session)


def _assistant_requests_binding_change_approval(assistant_text: str) -> bool:
    normalized = (
        str(assistant_text or "")
        .casefold()
        .replace("ä", "ae")
        .replace("ö", "oe")
        .replace("ü", "ue")
        .replace("ß", "ss")
    )
    if not normalized.strip():
        return False
    return any(marker in normalized for marker in _BINDING_APPROVAL_REQUEST_MARKERS) and any(
        marker in normalized for marker in _BINDING_CHANGE_MARKERS
    )


def _assistant_requests_new_codex_session(assistant_text: str) -> bool:
    normalized = (
        str(assistant_text or "")
        .casefold()
        .replace("ä", "ae")
        .replace("ö", "oe")
        .replace("ü", "ue")
        .replace("ß", "ss")
    )
    if not normalized.strip():
        return False
    if normalized.lstrip().startswith("session id:") and "return_packet_id:" in normalized:
        return "codex_thread_action: new_thread" in normalized or "codex_thread_action: start_fresh" in normalized
    has_context = any(marker in normalized for marker in _NEW_CODEX_SESSION_CONTEXT_MARKERS)
    if not has_context:
        return False
    if any(marker in normalized for marker in _STRONG_NEW_CODEX_SESSION_MARKERS):
        return True
    if any(marker in normalized for marker in _SAME_CODEX_SESSION_MARKERS):
        return False
    return any(marker in normalized for marker in _NEW_CODEX_SESSION_MARKERS)


def _render_stalled_assistant_recovery_prompt(
    *,
    session_id: str,
    return_packet_id: str,
    recovery_anchor: str,
) -> str:
    lines = [
        f"Session id: {session_id}",
        f"stalled_assistant_recovery: {recovery_anchor}",
        "",
        (
            f"The latest visible return packet `{return_packet_id}` in this same chat did not produce "
            "a new assistant reply."
        ),
        "Use that latest visible return packet as the authoritative input.",
        "Stay in this same chat.",
        "Write the next plain-language prompt for Codex now.",
        "Do not ask for clarification.",
        "Do not emit bridge-control, JSON, or YAML.",
    ]
    return "\n".join(lines)


class LoopRunner:
    def __init__(
        self,
        *,
        adapter: Any,
        executor: Any,
        bindings_path: Path,
        policy_path: Path,
        sessions_dir: Path,
    ) -> None:
        self.adapter = adapter
        self.executor = executor
        self.bindings_path = bindings_path
        self.policy_path = policy_path
        self.sessions_dir = sessions_dir

    def close(self) -> None:
        close = getattr(self.adapter, "close", None)
        if callable(close):
            close()

    def run_once(self, session_id: str, require_new_message: bool = False) -> dict[str, Any]:
        session = load_session(session_path(self.sessions_dir, session_id))
        bindings = load_chat_bindings(self.bindings_path)
        binding = next((item for item in bindings if item.binding_id == session.binding_id), None)
        if binding is None:
            raise ValueError(f"Unknown binding_id for session {session_id}: {session.binding_id}")

        honored_state = _honor_non_active_session_state(self.sessions_dir, session, runner_action="paused")
        if honored_state is not None:
            return honored_state

        policy_state = load_orchestrator_policy(self.policy_path)
        stop_phrases = [str(item) for item in policy_state.get("stop_phrases", [])]
        pending_outbound_kind = str(getattr(session, "last_outbound_user_message_kind", "") or "").strip()
        pending_outbound_anchor = str(getattr(session, "last_outbound_user_message_anchor", "") or "").strip()
        awaiting_assistant_after_packet = _awaiting_assistant_after_return_packet(session)
        try:
            self.adapter.open_chat(binding)
        except Exception as exc:
            reason = enrich_browser_blocker_reason(str(exc))
            return _retry_without_blocking(
                self.sessions_dir,
                session,
                reason=reason,
                last_error=reason,
                loop_state="waiting_for_chatgpt",
                degraded_mode=(
                    "retrying_return_packet" if pending_outbound_kind.startswith("return_packet") else "retrying_chatgpt_reply"
                ),
            )
        completed_codex_result = self._recover_completed_codex_run_without_delivery(session, binding, policy_state)
        if completed_codex_result is not None:
            return completed_codex_result
        delivered_report_recovery = self._recover_latest_delivered_report_state_if_needed(session)
        if delivered_report_recovery is not None:
            return delivered_report_recovery
        if pending_outbound_anchor and pending_outbound_kind in {"return_packet_retry_pending", "return_packet_ready"}:
            if pending_outbound_kind == "return_packet_retry_pending":
                retry_wait_seconds = _pending_return_packet_retry_wait_seconds(session)
                if retry_wait_seconds > 0:
                    return _keep_waiting(
                        self.sessions_dir,
                        session,
                        reasons=[
                            (
                                "Pending return-packet delivery is cooling down after repeated browser delivery failures "
                                f"for another {max(int(retry_wait_seconds), 1)} seconds."
                            ),
                            "The session stays active and will retry automatically at a safer cadence.",
                        ],
                        loop_state="posting_return_packet",
                        degraded_mode="retrying_return_packet",
                        degraded_reason="Return packet delivery retry is cooling down before the next automatic attempt.",
                    )
            retry_result = self._resume_pending_return_packet_delivery(session, binding, policy_state)
            if retry_result is not None:
                return retry_result
        if awaiting_assistant_after_packet:
            retry_result = self._recover_missing_visible_return_packet(session, binding, policy_state)
            if retry_result is not None:
                return retry_result
        try:
            assistant_message = self.adapter.read_latest_assistant_message(session)
        except RuntimeError as exc:
            if _should_wait_for_missing_assistant_message(session, str(exc)):
                visible_error_retry_result = self._recover_visible_assistant_error(
                    session,
                    reason_prefix="ChatGPT shows a visible retryable error instead of a completed assistant reply",
                )
                if visible_error_retry_result is not None:
                    return visible_error_retry_result
                same_chat_retry_result = self._retry_stalled_assistant_response(
                    session,
                    reason="No assistant message is visible yet after the latest Codex packet.",
                )
                if same_chat_retry_result is not None:
                    return same_chat_retry_result
                silent_recovery_result = self._recover_silent_assistant_stall(
                    session,
                    binding,
                    reason="No assistant message is visible yet after the latest Codex packet.",
                )
                if silent_recovery_result is not None:
                    return silent_recovery_result
                waiting_reason = "No assistant message is visible yet in the current chat; waiting for ChatGPT to answer."
                if awaiting_assistant_after_packet:
                    waiting_reason = (
                        "No assistant message is visible yet after the latest Codex packet; "
                        "the bridge kept waiting in the same chat."
                    )
                return _keep_waiting(
                    self.sessions_dir,
                    session,
                    reasons=[waiting_reason],
                    loop_state=("waiting_for_chatgpt_response" if awaiting_assistant_after_packet else "waiting_for_chatgpt"),
                )
            reason = enrich_browser_blocker_reason(str(exc))
            return _retry_without_blocking(
                self.sessions_dir,
                session,
                reason=reason,
                last_error=reason,
                loop_state="waiting_for_chatgpt",
                degraded_mode="retrying_chatgpt_reply",
            )
        assistant_text = str(assistant_message.get("text", ""))
        visible_error_retry_result = self._recover_visible_assistant_error(
            session,
            reason_prefix="ChatGPT shows a visible retryable error for the current assistant turn",
            fallback_error_text=assistant_text,
        )
        if visible_error_retry_result is not None:
            return visible_error_retry_result
        if assistant_message_looks_like_retryable_error(assistant_text):
            error_signature = canonical_delivery_error_signature(assistant_text)
            first_line = assistant_text.splitlines()[0].strip() if assistant_text.splitlines() else error_signature
            after_codex_packet = awaiting_assistant_after_packet
            reason_prefix = (
                "ChatGPT surfaced a retryable error instead of a usable assistant reply after the latest Codex packet"
                if after_codex_packet
                else "ChatGPT surfaced a retryable error instead of a usable assistant reply for the current assistant turn"
            )
            reason = f"{reason_prefix}: {first_line} ({error_signature})."
            retry_result = self._retry_stalled_assistant_response(
                session,
                reason=reason,
                require_stall=False,
            )
            if retry_result is not None:
                return retry_result
            return _retry_without_blocking(
                self.sessions_dir,
                session,
                reason=reason,
                last_error=error_signature,
                loop_state="waiting_for_chatgpt_response" if after_codex_packet else "waiting_for_chatgpt",
                degraded_mode="retrying_chatgpt_reply",
                degraded_reason=(
                    "A retryable ChatGPT error surface is visible in place of the assistant reply; "
                    "the bridge stayed active instead of forwarding that error text to Codex."
                ),
            )
        assistant_hash = hashlib.sha1(assistant_text.encode("utf-8")).hexdigest()
        assistant_anchor = str(assistant_message.get("message_anchor", ""))
        assistant_already_processed = _assistant_message_already_processed(session, assistant_anchor, assistant_hash)
        if not assistant_already_processed:
            _record_chat_activity(session)
            _clear_outbound_user_message(session)

        if not assistant_already_processed and _assistant_requests_binding_change_approval(assistant_text):
            session.last_seen_chat_message_anchor = assistant_anchor
            session.latest_assistant_message_id = str(assistant_message.get("message_id", ""))
            session.latest_assistant_message_hash = assistant_hash
            session.last_productive_prompt = ""
            session.last_productive_task_label = ""
            session.last_productive_thread_action = ""
            return _block_for_human(
                self.sessions_dir,
                session,
                reason=(
                    "ChatGPT requested explicit user approval for a binding or repo change; "
                    "the bridge did not forward that approval request to Codex as executable work."
                ),
                last_error="Human approval required before changing the active repo binding.",
                category="binding_change_required",
            )

        if not assistant_already_processed and _assistant_requests_new_codex_session(assistant_text):
            session.last_seen_chat_message_anchor = assistant_anchor
            session.latest_assistant_message_id = str(assistant_message.get("message_id", ""))
            session.latest_assistant_message_hash = assistant_hash
            session.last_productive_prompt = ""
            session.last_productive_task_label = ""
            session.last_productive_thread_action = ""
            return _block_for_human(
                self.sessions_dir,
                session,
                reason=(
                    "ChatGPT requested a new or fresh Codex session from inside an existing bound session; "
                    "the bridge did not forward that topology change to the current Codex thread."
                ),
                last_error="Human approval required before changing the active Codex session/thread topology.",
                category="new_codex_session_requested",
            )

        assistant_is_in_progress = (
            _assistant_response_in_progress(self.adapter, session)
            or _assistant_looks_like_thinking_disclosure(assistant_text)
            or _assistant_reply_is_fragmentary(assistant_text)
        )

        if assistant_is_in_progress:
            stalled_in_progress_assistant = _track_in_progress_assistant(
                session,
                assistant_anchor=assistant_anchor,
                assistant_hash=assistant_hash,
                assistant_text=assistant_text,
            )
            if awaiting_assistant_after_packet and stalled_in_progress_assistant:
                same_chat_retry_result = self._retry_stalled_assistant_response(
                    session,
                    reason="The latest ChatGPT reply is stalled or incomplete after the latest Codex packet.",
                )
                if same_chat_retry_result is not None:
                    return same_chat_retry_result
                silent_recovery_result = self._recover_silent_assistant_stall(
                    session,
                    binding,
                    reason="The latest ChatGPT reply is stalled or incomplete after the latest Codex packet.",
                )
                if silent_recovery_result is not None:
                    return silent_recovery_result
            return _keep_waiting(
                self.sessions_dir,
                session,
                reasons=[
                    "Assistant response is still incomplete or in progress.",
                    "The bridge will wait for a complete assistant reply before starting the next Codex run.",
                ],
                loop_state=("waiting_for_chatgpt_response" if awaiting_assistant_after_packet else "waiting_for_chatgpt"),
            )

        _clear_in_progress_assistant(session)

        stop_event = normalize_stop_command_event(self.adapter.poll_stop_command(session, stop_phrases), stop_phrases)
        if stop_event and not stop_command_already_processed(session, stop_event):
            stop_command = stop_event["command"]
            session.latest_user_control_command = stop_command
            session.last_seen_user_control_anchor = stop_event["message_anchor"]
            session.latest_user_control_message_hash = stop_event["message_hash"]
            if stop_command == "pause":
                session.status = "paused"
                session.degraded_mode = "paused_manual"
                session.degraded_reason = "Pause requested from the control surface."
                _set_loop_state(session, "paused")
                session.auto_run_enabled = False
                session.supervisor_status = "paused"
                session.policy_decision = LoopPolicyDecision(
                    policy_outcome="paused",
                    reasons=["Pause requested from the control surface."],
                )
                save_session(session_path(self.sessions_dir, session.session_id), session)
                return _result_payload(session, session.policy_decision, "", runner_action="paused")
            if stop_command == "stop":
                session.status = "completed"
                _set_loop_state(session, "completed")
                session.auto_run_enabled = False
                session.supervisor_status = "stopped"
                session.policy_decision = LoopPolicyDecision(
                    policy_outcome="stopped",
                    reasons=["Stop requested from the control surface."],
                )
                save_session(session_path(self.sessions_dir, session.session_id), session)
                return _result_payload(session, session.policy_decision, "", runner_action="stopped")
            if stop_command == "stop after this cycle":
                session.stop_after_cycle_requested = True
                if session.loop_state in {"idle", "waiting_for_chatgpt", "waiting_for_chatgpt_response"}:
                    session.status = "completed"
                    _set_loop_state(session, "completed")
                    session.auto_run_enabled = False
                    session.supervisor_status = "stopped"
                    session.stop_after_cycle_requested = False
                    session.policy_decision = LoopPolicyDecision(
                        policy_outcome="stopped",
                        reasons=["Stop after cycle was requested while no Codex cycle was active."],
                    )
                    save_session(session_path(self.sessions_dir, session.session_id), session)
                    return _result_payload(session, session.policy_decision, "", runner_action="stopped")

        if assistant_already_processed:
            same_chat_retry_result = self._retry_stalled_assistant_response(
                session,
                reason="The latest ChatGPT assistant turn has not advanced since the last Codex packet.",
            )
            if same_chat_retry_result is not None:
                return same_chat_retry_result
            silent_recovery_result = self._recover_silent_assistant_stall(
                session,
                binding,
                reason="The latest ChatGPT assistant turn has not advanced since the last Codex packet.",
            )
            if silent_recovery_result is not None:
                return silent_recovery_result
            return _keep_waiting(
                self.sessions_dir,
                session,
                reasons=[
                    "No fresh ChatGPT assistant reply is available yet.",
                    "The bridge will wait for a new assistant turn instead of rerunning the same prompt.",
                ],
                loop_state=("waiting_for_chatgpt_response" if awaiting_assistant_after_packet else "waiting_for_chatgpt"),
            )
        honored_state = _honor_non_active_session_state(self.sessions_dir, session, runner_action="paused")
        if honored_state is not None:
            return honored_state
        previous_assistant_anchor = session.last_seen_chat_message_anchor
        previous_assistant_message_id = session.latest_assistant_message_id
        previous_assistant_hash = session.latest_assistant_message_hash
        previous_codex_thread_id = session.current_codex_thread_id
        previous_codex_run_id = session.current_codex_run_id
        next_thread_action = (
            "same_thread"
            if str(session.current_codex_thread_id or session.current_codex_run_id or "").strip()
            else "new_thread"
        )
        accepted_prompt = assistant_text
        accepted_task_label = "accepted_assistant_text"
        accepted_policy_reasons = ["Accepted the latest complete assistant message as the next Codex task."]
        if _assistant_text_looks_like_planner_idle(assistant_text):
            attempts = max(int(getattr(session, "productive_rewind_attempts", 0) or 0), 0)
            session.last_seen_chat_message_anchor = assistant_anchor
            session.latest_assistant_message_id = str(assistant_message.get("message_id", ""))
            session.latest_assistant_message_hash = assistant_hash
            session.last_productive_prompt = assistant_text
            session.last_productive_task_label = "planner_idle_reply"
            session.degraded_mode = "planner_idle_recovery"
            session.degraded_reason = (
                "ChatGPT replied without a runnable Codex prompt even though the bridge can continue "
                "with another safe lane in the bound repo."
            )
            if attempts >= _PLANNER_IDLE_RECOVERY_ATTEMPT_LIMIT:
                accepted_prompt = _render_planner_idle_fallback_codex_prompt(
                    session_id=session.session_id,
                    repo_path=str(session.repo_path or session.workspace_path or "").strip(),
                    assistant_text=assistant_text,
                )
                accepted_task_label = "planner_idle_fallback_codex_prompt"
                accepted_policy_reasons = [
                    "ChatGPT repeatedly replied without a runnable Codex prompt.",
                    (
                        "The bridge launched a generic plan-continuation Codex prompt instead of "
                        "stopping or starting another No-op loop."
                    ),
                ]
                session.degraded_mode = "planner_idle_fallback_codex"
                session.degraded_reason = (
                    "ChatGPT repeatedly replied without a runnable Codex prompt; the bridge synthesized "
                    "a generic plan-continuation prompt for the bound repo."
                )
            else:
                recovery_anchor = f"planner-idle-recovery-{session.session_id}-{int(time.time())}"
                recovery_text = _render_planner_idle_recovery_prompt(
                    session_id=session.session_id,
                    repo_path=str(session.repo_path or session.workspace_path or "").strip(),
                    recovery_anchor=recovery_anchor,
                    assistant_text=assistant_text,
                )
                try:
                    delivery = self.adapter.post_user_message(session, recovery_text, recovery_anchor)
                except RuntimeError as exc:
                    delivery = {
                        "status": "failed",
                        "error_signature": str(exc).strip() or "Planner-idle recovery prompt delivery failed.",
                    }
                if str(delivery.get("status", "")).strip() != "delivered":
                    error_signature = (
                        str(delivery.get("error_signature", "")).strip()
                        or "Planner-idle recovery prompt delivery failed."
                    )
                    return _retry_without_blocking(
                        self.sessions_dir,
                        session,
                        reason=(
                            "ChatGPT replied without a runnable Codex prompt, and the bridge could not deliver "
                            f"the generic planner-idle recovery prompt: {error_signature}"
                        ),
                        last_error=error_signature,
                        loop_state="waiting_for_chatgpt_response",
                        runner_action="wait_for_chatgpt",
                        degraded_mode="planner_idle_recovery",
                        degraded_reason="Planner-idle recovery prompt delivery failed.",
                    )
                session.productive_rewind_attempts = attempts + 1
                _record_outbound_user_message(session, message_anchor=recovery_anchor, kind="recovery")
                _record_chat_activity(session)
                _set_loop_state(session, "waiting_for_chatgpt_response")
                session.status = "active"
                session.auto_run_enabled = True
                session.supervisor_status = "running"
                session.human_attention_reason = ""
                session.last_error = ""
                session.policy_decision = _allow_policy_decision(
                    session,
                    [
                        "ChatGPT replied without a runnable Codex prompt.",
                        "The bridge posted a generic planner-idle recovery prompt instead of launching another No-op Codex run.",
                    ],
                )
                save_session(session_path(self.sessions_dir, session.session_id), session)
                return _result_payload(session, session.policy_decision, "", runner_action="wait_for_chatgpt")

        if (
            accepted_task_label == "accepted_assistant_text"
            and _assistant_text_looks_like_blocked_lane_churn(assistant_text)
        ):
            accepted_prompt = _render_blocked_lane_churn_codex_prompt(
                session_id=session.session_id,
                repo_path=str(session.repo_path or session.workspace_path or "").strip(),
                assistant_text=assistant_text,
            )
            accepted_task_label = "blocked_lane_churn_redirect"
            accepted_policy_reasons = [
                "ChatGPT produced a prompt centered on a blocked/backoff/cooldown/auth lane.",
                (
                    "The bridge wrapped it with an anti-churn override so Codex performs at most "
                    "a cheap status check before pivoting to another safe repo-local plan lane."
                ),
            ]

        session.productive_rewind_attempts = 0
        session.last_seen_chat_message_anchor = assistant_anchor
        session.latest_assistant_message_id = str(assistant_message.get("message_id", ""))
        session.latest_assistant_message_hash = assistant_hash

        _set_loop_state(session, "starting_codex")
        session.status = "active"
        session.auto_run_enabled = True
        session.supervisor_status = "running"
        session.human_attention_reason = ""
        session.last_error = ""
        session.degraded_mode = ""
        session.degraded_reason = ""
        session.last_thread_action = next_thread_action
        if accepted_task_label == "planner_idle_fallback_codex_prompt":
            session.degraded_mode = "planner_idle_fallback_codex"
            session.degraded_reason = (
                "ChatGPT repeatedly replied without a runnable Codex prompt; the bridge synthesized "
                "a generic plan-continuation prompt for the bound repo."
            )
        session.last_productive_prompt = accepted_prompt
        session.last_productive_task_label = accepted_task_label
        session.last_productive_thread_action = next_thread_action
        session.policy_decision = _allow_policy_decision(
            session,
            accepted_policy_reasons,
        )
        instructions = [
            *resolve_instruction_texts(session, policy_state),
            *_completed_run_recovery_overrides(
                accepted_prompt,
                sessions_dir=self.sessions_dir,
                session_id=session.session_id,
            ),
        ]
        save_session(session_path(self.sessions_dir, session.session_id), session)
        try:
            report: RunReport = self.executor(
                prompt=accepted_prompt,
                thread_action=next_thread_action,
                session=session,
                binding=binding,
                instructions=instructions,
            )
        except Exception as exc:
            session.last_seen_chat_message_anchor = previous_assistant_anchor
            session.latest_assistant_message_id = previous_assistant_message_id
            session.latest_assistant_message_hash = previous_assistant_hash
            session.current_codex_thread_id = previous_codex_thread_id
            session.current_codex_run_id = previous_codex_run_id
            return _retry_without_blocking(
                self.sessions_dir,
                session,
                reason=str(exc),
                last_error=str(exc),
                loop_state="starting_codex",
                runner_action="retry_codex_run",
                degraded_mode="retrying_codex_run",
            )

        latest_session = load_session(session_path(self.sessions_dir, session.session_id))
        drain_control_command = str(latest_session.latest_user_control_command or "").strip()
        if latest_session.status == "paused":
            drain_control_command = "pause"
        elif latest_session.status == "completed":
            drain_control_command = "stop"
        if drain_control_command in {"pause", "stop"}:
            session.latest_user_control_command = drain_control_command
        if latest_session.stop_after_cycle_requested or drain_control_command == "stop":
            session.stop_after_cycle_requested = True
        if drain_control_command in {"pause", "stop"}:
            session.status = "active"
            session.auto_run_enabled = True
            session.supervisor_status = "running"

        if report.interruption_reason == "pause_requested":
            session.status = "paused"
            _record_codex_activity(session)
            _set_loop_state(session, "paused")
            session.auto_run_enabled = False
            session.supervisor_status = "paused"
            session.policy_decision = LoopPolicyDecision(
                policy_outcome="paused",
                reasons=["Pause requested while Codex was running."],
                time_budget_minutes=session.time_budget_minutes,
                time_budget_remaining_minutes=session.budget_remaining_minutes,
            )
            save_session(session_path(self.sessions_dir, session.session_id), session)
            return _result_payload(session, session.policy_decision, "", runner_action="paused")
        if report.interruption_reason == "stop_requested":
            session.status = "completed"
            _record_codex_activity(session)
            _set_loop_state(session, "completed")
            session.auto_run_enabled = False
            session.supervisor_status = "stopped"
            session.policy_decision = LoopPolicyDecision(
                policy_outcome="stopped",
                reasons=["Stop requested while Codex was running."],
                time_budget_minutes=session.time_budget_minutes,
                time_budget_remaining_minutes=session.budget_remaining_minutes,
            )
            save_session(session_path(self.sessions_dir, session.session_id), session)
            return _result_payload(session, session.policy_decision, "", runner_action="stopped")
        if report.interruption_reason == "progress_stall":
            session.last_seen_chat_message_anchor = previous_assistant_anchor
            session.latest_assistant_message_id = previous_assistant_message_id
            session.latest_assistant_message_hash = previous_assistant_hash
            session.current_codex_thread_id = previous_codex_thread_id
            session.current_codex_run_id = previous_codex_run_id
            failure_reason = (
                next((item for item in report.blockers if str(item).strip()), "")
                or str(report.summary or "").strip()
                or "Codex stalled without new output and the bridge is retrying the same assistant turn."
            )
            return _retry_without_blocking(
                self.sessions_dir,
                session,
                reason=failure_reason,
                last_error=failure_reason,
                loop_state="starting_codex",
                runner_action="retry_codex_run",
                degraded_mode="retrying_codex_run",
            )

        if report.exit_code != 0:
            session.last_seen_chat_message_anchor = previous_assistant_anchor
            session.latest_assistant_message_id = previous_assistant_message_id
            session.latest_assistant_message_hash = previous_assistant_hash
            session.current_codex_thread_id = previous_codex_thread_id
            session.current_codex_run_id = previous_codex_run_id
            detailed_blockers = [
                item
                for item in report.blockers
                if str(item).strip() and str(item).strip() != f"codex exec exited with code {report.exit_code}"
            ]
            failure_reason = (
                detailed_blockers[0]
                if detailed_blockers
                else str(report.summary or "").strip()
                or f"Codex exited with code {report.exit_code} before the loop could continue safely."
            )
            if _report_looks_like_retryable_codex_runtime_failure(report):
                return _retry_without_blocking(
                    self.sessions_dir,
                    session,
                    reason=failure_reason,
                    last_error=failure_reason,
                    loop_state="starting_codex",
                    runner_action="retry_codex_run",
                    degraded_mode="retrying_codex_runtime_failure",
                )
            return _block_for_human(
                self.sessions_dir,
                session,
                reason=failure_reason,
                last_error=failure_reason,
            )

        _record_codex_activity(session)
        _set_loop_state(session, "posting_return_packet")
        session.supervisor_status = "running" if session.auto_run_enabled else session.supervisor_status
        refresh_session_budget(session)
        report.session_id = session.session_id
        report.bridge_session_id = report.bridge_session_id or session.session_id
        report.binding_id = binding.binding_id
        report.policy_outcome = "allow"
        report.budget_snapshot = {
            "time_budget_minutes": session.time_budget_minutes,
            "budget_remaining_minutes": session.budget_remaining_minutes,
        }
        report.return_packet_id = derive_return_packet_id(report)
        report.run_id = report.run_id or (report.artifacts_dir.rsplit("/", 1)[-1] if report.artifacts_dir else "")

        session.current_codex_thread_id = (
            report.codex_thread_id
            or report.observed_codex_thread_id
            or session.current_codex_thread_id
            or session.current_codex_run_id
        )
        session.current_codex_run_id = session.current_codex_thread_id
        packet = build_return_packet(report)
        if session.stop_before_return_packet_requested:
            report.delivery_status = "ready_to_post"
            report.delivery_attempts = []
            report.delivery_attempt_count = 0
            session.delivery_attempts = []
            _record_outbound_user_message(session, message_anchor=packet.return_packet_id, kind="return_packet_ready")
            session.last_posted_return_packet_id = ""
            session.status = "paused"
            _set_loop_state(session, "codex_completed_waiting_to_post")
            session.auto_run_enabled = False
            session.supervisor_status = "paused"
            session.stop_before_return_packet_requested = False
            session.human_attention_reason = (
                "Codex finished. The return to ChatGPT is queued but has not been posted yet."
            )
            session.last_error = ""
            session.policy_decision = LoopPolicyDecision(
                policy_outcome="paused",
                reasons=[
                    "Codex finished and the return packet is queued locally.",
                    "The supervisor stopped before posting back to ChatGPT so you can inspect the first handoff.",
                ],
                time_budget_minutes=session.time_budget_minutes,
                time_budget_remaining_minutes=session.budget_remaining_minutes,
            )
            report.policy_outcome = session.policy_decision.policy_outcome
            _persist_run_report(report)
            save_session(session_path(self.sessions_dir, session.session_id), session)
            return _result_payload(
                session,
                session.policy_decision,
                packet.return_packet_id,
                runner_action="paused_before_return_packet",
            )

        packet_text = render_return_packet(packet)
        delivery = self._deliver_packet_with_refocus(
            session,
            binding,
            policy_state,
            packet.return_packet_id,
            packet_text,
        )
        report.delivery_status = delivery["status"]
        report.delivery_attempts = list(delivery.get("attempts", []))
        report.delivery_attempt_count = len(report.delivery_attempts)
        session.delivery_attempts = [
            ChatDeliveryAttempt.from_dict(item)
            for item in delivery.get("attempts", [])
            if isinstance(item, dict)
        ]
        if delivery["status"] == "delivered":
            _record_outbound_user_message(session, message_anchor=packet.return_packet_id, kind="return_packet")
        session.last_posted_return_packet_id = packet.return_packet_id if delivery["status"] == "delivered" else ""
        stop_command = str(delivery.get("stop_command", ""))
        if stop_command:
            session.latest_user_control_command = stop_command
        if delivery["status"] == "delivered":
            session.cycles_completed += 1
        if delivery["status"] != "delivered":
            honored_state = _honor_non_active_session_state(self.sessions_dir, session, runner_action="paused")
            if honored_state is not None:
                report.delivery_status = "paused"
                report.policy_outcome = "paused"
                _persist_run_report(report)
                return honored_state
            last_attempts = [item for item in delivery.get("attempts", []) if isinstance(item, dict)]
            error_signatures = [
                str(item.get("error_signature", "")).strip()
                for item in last_attempts
                if str(item.get("error_signature", "")).strip()
            ]
            retry_reason = (
                "; ".join(error_signatures)
                or "Return packet delivery could not be confirmed."
            )
            if _return_packet_delivery_became_stale(error_signatures):
                reason = (
                    "ChatGPT shifted to a different assistant turn before the Codex return packet "
                    "could be delivered. The bridge refused to discard the undelivered packet and start a "
                    "new Codex turn automatically."
                )
                _record_outbound_user_message(
                    session,
                    message_anchor=packet.return_packet_id,
                    kind="return_packet_retry_pending",
                )
                session.last_posted_return_packet_id = ""
                session.last_error = reason
                session.degraded_mode = "stale_return_packet"
                session.degraded_reason = reason
                report.delivery_status = "stale_chat_state"
                report.policy_outcome = "require_human"
                _persist_run_report(report)
                return _block_for_human(
                    self.sessions_dir,
                    session,
                    reason=reason,
                    last_error=reason,
                    category="stale_return_packet_chat_state",
                )
            _record_outbound_user_message(
                session,
                message_anchor=packet.return_packet_id,
                kind="return_packet_retry_pending",
            )
            session.status = "active"
            _set_loop_state(session, "posting_return_packet")
            session.auto_run_enabled = True
            session.supervisor_status = "running"
            session.human_attention_reason = ""
            session.last_error = retry_reason
            session.degraded_mode = "retrying_return_packet"
            session.degraded_reason = retry_reason
            session.last_posted_return_packet_id = ""
            session.policy_decision = _allow_policy_decision(
                session,
                [
                    "Return packet delivery could not be confirmed.",
                    "The loop kept the session active and will retry delivery automatically.",
                ],
            )
            report.policy_outcome = session.policy_decision.policy_outcome
            _persist_run_report(report)
            save_session(session_path(self.sessions_dir, session.session_id), session)
            return _result_payload(
                session,
                session.policy_decision,
                packet.return_packet_id,
                delivery,
                runner_action="wait_for_chatgpt",
            )

        session.degraded_mode = ""
        session.degraded_reason = ""
        session.policy_decision = _allow_policy_decision(
            session,
            ["The return packet was delivered successfully."],
        )
        report.policy_outcome = session.policy_decision.policy_outcome
        if session.stop_after_cycle_requested:
            session.status = "completed"
            _set_loop_state(session, "completed")
            session.auto_run_enabled = False
            session.supervisor_status = "stopped"
            session.stop_after_cycle_requested = False
            session.latest_user_control_command = ""
            session.policy_decision = LoopPolicyDecision(
                policy_outcome="stopped",
                reasons=["Stop requested after the current cycle."],
                time_budget_minutes=session.time_budget_minutes,
                time_budget_remaining_minutes=session.budget_remaining_minutes,
            )
            report.policy_outcome = session.policy_decision.policy_outcome
            _persist_run_report(report)
            save_session(session_path(self.sessions_dir, session.session_id), session)
            return _result_payload(
                session,
                session.policy_decision,
                packet.return_packet_id,
                delivery,
                runner_action="stopped",
            )

        if str(session.latest_user_control_command or "").strip() == "pause":
            session.status = "paused"
            _set_loop_state(session, "paused")
            session.auto_run_enabled = False
            session.supervisor_status = "paused"
            session.latest_user_control_command = ""
            session.policy_decision = LoopPolicyDecision(
                policy_outcome="paused",
                reasons=["Pause requested after the current cycle."],
                time_budget_minutes=session.time_budget_minutes,
                time_budget_remaining_minutes=session.budget_remaining_minutes,
            )
            report.policy_outcome = session.policy_decision.policy_outcome
            _persist_run_report(report)
            save_session(session_path(self.sessions_dir, session.session_id), session)
            return _result_payload(
                session,
                session.policy_decision,
                packet.return_packet_id,
                delivery,
                runner_action="paused",
            )

        _set_loop_state(session, "waiting_for_chatgpt_response")
        _persist_run_report(report)
        save_session(session_path(self.sessions_dir, session.session_id), session)
        return _result_payload(
            session,
            session.policy_decision,
            packet.return_packet_id,
            delivery,
            runner_action="cycle_completed",
        )

    def _recover_completed_codex_run_without_delivery(
        self,
        session,
        binding,
        policy_state: dict[str, Any],
    ) -> dict[str, Any] | None:
        if str(getattr(session, "loop_state", "") or "") != "starting_codex":
            return None
        report = _synthesize_latest_completed_run_report(self.sessions_dir, session)
        if report is None:
            report = _load_latest_run_report(self.sessions_dir, session.session_id)
        if report is None:
            return None
        if str(getattr(report, "delivery_status", "") or "").strip().casefold() == "delivered":
            return self._recover_delivered_report_state(session, report)
        if str(getattr(report, "delivery_status", "") or "").strip():
            return None
        if int(getattr(report, "exit_code", 0) or 0) != 0:
            return None
        interruption = str(getattr(report, "interruption_reason", "") or "").strip()
        if interruption and interruption != "none":
            return None
        artifacts_dir = str(getattr(report, "artifacts_dir", "") or "").strip()
        if artifacts_dir and not _completed_run_dir_has_final_message(Path(artifacts_dir)):
            return None
        pending_packet_id = str(getattr(report, "return_packet_id", "") or "").strip() or derive_return_packet_id(report)
        return self._resume_pending_return_packet_delivery(
            session,
            binding,
            policy_state,
            pending_packet_id=pending_packet_id,
        )

    def _recover_latest_delivered_report_state_if_needed(self, session) -> dict[str, Any] | None:
        loop_state = str(getattr(session, "loop_state", "") or "").strip()
        outbound_kind = str(getattr(session, "last_outbound_user_message_kind", "") or "").strip()
        needs_recovery = (
            str(getattr(session, "status", "") or "").strip() == "blocked"
            or loop_state in {"posting_return_packet", "requires_human", "starting_codex"}
            or outbound_kind.startswith("return_packet")
            or bool(str(getattr(session, "last_error", "") or "").strip())
            or bool(str(getattr(session, "degraded_mode", "") or "").strip())
            or bool(str(getattr(session, "degraded_reason", "") or "").strip())
        )
        if not needs_recovery:
            return None
        report = _load_latest_run_report(self.sessions_dir, session.session_id)
        if report is None:
            return None
        if str(getattr(report, "delivery_status", "") or "").strip().casefold() != "delivered":
            return None
        return self._recover_delivered_report_state(session, report)

    def _recover_delivered_report_state(self, session, report: RunReport) -> dict[str, Any] | None:
        packet_id = str(getattr(report, "return_packet_id", "") or "").strip() or derive_return_packet_id(report)
        if not packet_id:
            return None
        already_recorded = (
            str(getattr(session, "last_outbound_user_message_anchor", "") or "").strip() == packet_id
            and str(getattr(session, "last_outbound_user_message_kind", "") or "").strip() == "return_packet"
            and str(getattr(session, "last_posted_return_packet_id", "") or "").strip() == packet_id
        )
        already_recovered = (
            already_recorded
            and str(getattr(session, "status", "") or "").strip() == "active"
            and bool(getattr(session, "auto_run_enabled", False))
            and str(getattr(session, "supervisor_status", "") or "").strip() == "running"
            and str(getattr(session, "loop_state", "") or "").strip() == "waiting_for_chatgpt_response"
            and not str(getattr(session, "human_attention_reason", "") or "").strip()
            and not str(getattr(session, "last_error", "") or "").strip()
            and not str(getattr(session, "degraded_mode", "") or "").strip()
            and not str(getattr(session, "degraded_reason", "") or "").strip()
        )
        if already_recovered:
            return None
        should_count_cycle = str(getattr(session, "last_posted_return_packet_id", "") or "").strip() != packet_id
        _record_outbound_user_message(session, message_anchor=packet_id, kind="return_packet")
        session.last_posted_return_packet_id = packet_id
        if should_count_cycle:
            session.cycles_completed += 1
        session.status = "active"
        _set_loop_state(session, "waiting_for_chatgpt_response")
        session.auto_run_enabled = True
        session.supervisor_status = "running"
        session.human_attention_reason = ""
        session.last_error = ""
        session.degraded_mode = ""
        session.degraded_reason = ""
        session.policy_decision = _allow_policy_decision(
            session,
            [
                "The latest run report already contains a confirmed delivered return packet.",
                "The loop restored session state from the delivery artifact instead of posting a duplicate packet.",
            ],
        )
        save_session(session_path(self.sessions_dir, session.session_id), session)
        delivery = {
            "status": "delivered",
            "attempt_count": int(getattr(report, "delivery_attempt_count", 0) or 0),
            "attempts": list(getattr(report, "delivery_attempts", []) or []),
        }
        return _result_payload(
            session,
            session.policy_decision,
            packet_id,
            delivery,
            runner_action="wait_for_chatgpt",
        )

    def _retry_stalled_assistant_response(
        self,
        session,
        *,
        reason: str,
        require_stall: bool = True,
    ) -> dict[str, Any] | None:
        if not _assistant_turn_can_retry(session):
            return None
        if require_stall and not _outbound_user_message_stalled(session):
            return None
        retrier = getattr(self.adapter, "retry_latest_assistant_response", None)
        if not callable(retrier):
            return None
        try:
            retried = bool(retrier(session))
        except RuntimeError:
            retried = False
        if not retried:
            return None
        _refresh_outbound_user_message_timer(session)
        _record_chat_activity(session)
        _set_loop_state(session, "waiting_for_chatgpt_response")
        session.status = "active"
        session.auto_run_enabled = True
        session.supervisor_status = "running"
        session.human_attention_reason = ""
        session.last_error = ""
        session.degraded_mode = "retrying_chatgpt_reply"
        session.degraded_reason = (
            "The bridge clicked ChatGPT's retry button in the same chat and kept waiting for the same assistant turn."
        )
        session.policy_decision = _allow_policy_decision(
            session,
            [
                reason,
                "The bridge clicked ChatGPT's retry button in the same chat instead of opening a new chat or sending another prompt.",
            ],
        )
        save_session(session_path(self.sessions_dir, session.session_id), session)
        return _result_payload(
            session,
            session.policy_decision,
            "",
            runner_action="wait_for_chatgpt",
        )

    def _recover_visible_assistant_error(
        self,
        session,
        *,
        reason_prefix: str,
        fallback_error_text: str = "",
    ) -> dict[str, Any] | None:
        if not _assistant_turn_can_retry(session):
            return None
        reader = getattr(self.adapter, "latest_assistant_response_error", None)
        error_text = ""
        if callable(reader):
            try:
                error_text = str(reader(session) or "").strip()
            except RuntimeError:
                error_text = ""
        if not error_text and assistant_message_looks_like_retryable_error(fallback_error_text):
            error_text = str(fallback_error_text or "").strip()
        if not error_text:
            return None
        error_signature = canonical_delivery_error_signature(error_text)
        after_codex_packet = str(getattr(session, "last_outbound_user_message_kind", "") or "").strip() == "return_packet"
        context_suffix = " after the latest Codex packet" if after_codex_packet else ""
        reason = f"{reason_prefix}{context_suffix}: {error_text.splitlines()[0].strip()} ({error_signature})."
        retry_result = self._retry_stalled_assistant_response(
            session,
            reason=reason,
            require_stall=False,
        )
        if retry_result is not None:
            return retry_result
        return _retry_without_blocking(
            self.sessions_dir,
            session,
            reason=reason,
            last_error=error_signature,
            loop_state="waiting_for_chatgpt_response" if after_codex_packet else "waiting_for_chatgpt",
            degraded_mode="retrying_chatgpt_reply",
            degraded_reason=(
                "A visible ChatGPT retry/error surface is still on screen for the current assistant turn; "
                "the bridge stayed active instead of classifying the session as honest waiting."
            ),
        )

    def _recover_silent_assistant_stall(
        self,
        session,
        binding,
        *,
        reason: str,
    ) -> dict[str, Any] | None:
        if not _assistant_turn_can_retry(session):
            return None
        if not _outbound_user_message_stalled(session):
            return None
        attempts = max(int(getattr(session, "productive_rewind_attempts", 0) or 0), 0)
        reloader = getattr(self.adapter, "reload_chat", None)
        if attempts <= 0 and callable(reloader):
            try:
                reloaded = bool(reloader(session))
            except RuntimeError:
                reloaded = False
            if reloaded:
                session.productive_rewind_attempts = 1
                _refresh_outbound_user_message_timer(session)
                _set_loop_state(session, "waiting_for_chatgpt_response")
                session.status = "active"
                session.auto_run_enabled = True
                session.supervisor_status = "running"
                session.human_attention_reason = ""
                session.last_error = ""
                session.degraded_mode = "retrying_chatgpt_reply"
                session.degraded_reason = (
                    "The bridge reloaded the bound chat after the latest return packet stayed visible "
                    "without a fresh assistant reply."
                )
                session.policy_decision = _allow_policy_decision(
                    session,
                    [
                        reason,
                        "The bridge reloaded the bound chat before escalating to a same-chat recovery prompt.",
                    ],
                )
                save_session(session_path(self.sessions_dir, session.session_id), session)
                return _result_payload(
                    session,
                    session.policy_decision,
                    str(getattr(session, "last_posted_return_packet_id", "") or ""),
                    runner_action="wait_for_chatgpt",
                )
        if attempts >= _MAX_SILENT_ASSISTANT_RECOVERY_ATTEMPTS:
            return None
        recovery_anchor = f"stalled-assistant-recovery-{int(time.time())}"
        recovery_text = _render_stalled_assistant_recovery_prompt(
            session_id=session.session_id,
            return_packet_id=str(getattr(session, "last_posted_return_packet_id", "") or ""),
            recovery_anchor=recovery_anchor,
        )
        try:
            delivery = self.adapter.post_user_message(session, recovery_text, recovery_anchor)
        except RuntimeError as exc:
            delivery = {
                "status": "failed",
                "error_signature": str(exc).strip() or "Browser recovery prompt delivery failed.",
            }
        if delivery.get("status") != "delivered":
            error_signature = str(delivery.get("error_signature", "")).strip() or (
                "Same-chat recovery prompt delivery failed."
            )
            return _retry_without_blocking(
                self.sessions_dir,
                session,
                reason=(
                    f"{reason} The bridge tried to send a narrow same-chat recovery prompt, "
                    f"but delivery failed: {error_signature}"
                ),
                last_error=error_signature,
                loop_state="waiting_for_chatgpt_response",
                degraded_mode="retrying_chatgpt_reply",
                degraded_reason=(
                    "A silent ChatGPT stall remained after reload, but the same-chat recovery prompt "
                    "could not be delivered."
                ),
            )
        session.productive_rewind_attempts = attempts + 1
        _record_outbound_user_message(session, message_anchor=recovery_anchor, kind="return_packet")
        _set_loop_state(session, "waiting_for_chatgpt_response")
        session.status = "active"
        session.auto_run_enabled = True
        session.supervisor_status = "running"
        session.human_attention_reason = ""
        session.last_error = ""
        session.degraded_mode = "retrying_chatgpt_reply"
        session.degraded_reason = (
            "The bridge posted a narrow same-chat recovery prompt after the latest return packet stayed "
            "visible without a fresh assistant reply."
        )
        session.policy_decision = _allow_policy_decision(
            session,
            [
                reason,
                "The bridge posted a narrow same-chat recovery prompt that tells ChatGPT to answer the latest visible return packet.",
            ],
        )
        save_session(session_path(self.sessions_dir, session.session_id), session)
        return _result_payload(
            session,
            session.policy_decision,
            str(getattr(session, "last_posted_return_packet_id", "") or ""),
            runner_action="wait_for_chatgpt",
        )

    def _deliver_packet_with_refocus(
        self,
        session,
        binding,
        policy_state: dict[str, Any],
        return_packet_id: str,
        packet_text: str,
    ) -> dict[str, Any]:
        delivery = self._deliver_packet(session, policy_state, return_packet_id, packet_text)
        if delivery["status"] != "preflight_failed":
            return delivery
        first_attempts = [item for item in delivery.get("attempts", []) if isinstance(item, dict)]
        try:
            self.adapter.open_chat(binding)
        except Exception:
            return delivery
        retried = self._deliver_packet(session, policy_state, return_packet_id, packet_text)
        retried_attempts = [item for item in retried.get("attempts", []) if isinstance(item, dict)]
        merged_attempts = list(first_attempts)
        for attempt in retried_attempts:
            merged_attempt = dict(attempt)
            merged_attempt["attempt_number"] = len(merged_attempts) + 1
            merged_attempts.append(merged_attempt)
        if retried["status"] == "preflight_failed" and _delivery_attempts_need_foreground_browser_reopen(merged_attempts):
            activator = getattr(self.adapter, "activate_chat", None)
            if callable(activator):
                try:
                    activator(binding)
                    time.sleep(_LIVE_CHAT_SURFACE_RECOVERY_WAIT_SECONDS)
                except Exception:
                    activator = None
            if callable(activator):
                activated = self._deliver_packet(session, policy_state, return_packet_id, packet_text)
                for attempt in [item for item in activated.get("attempts", []) if isinstance(item, dict)]:
                    merged_attempt = dict(attempt)
                    merged_attempt["attempt_number"] = len(merged_attempts) + 1
                    merged_attempts.append(merged_attempt)
                return {
                    "status": activated["status"],
                    "attempt_count": len(merged_attempts),
                    "attempts": merged_attempts,
                    "stop_command": activated.get("stop_command", ""),
                }
        return {
            "status": retried["status"],
            "attempt_count": len(merged_attempts),
            "attempts": merged_attempts,
            "stop_command": retried.get("stop_command", ""),
        }

    def _deliver_packet(
        self,
        session,
        policy_state: dict[str, Any],
        return_packet_id: str,
        packet_text: str,
        *,
        expected_chat_url: str = "",
        validate_chat_state: bool = True,
    ) -> dict[str, Any]:
        retry_policy = dict(policy_state.get("delivery_retry", {}))
        max_attempts = int(retry_policy.get("max_attempts", 1) or 1)
        known_error_signatures = {
            str(item)
            for item in retry_policy.get("known_error_signatures", [])
            if str(item).strip()
        }
        known_error_signatures.update(_RETRYABLE_DELIVERY_ERROR_SIGNATURES)
        attempt_count = 0
        attempts: list[dict[str, Any]] = []
        while attempt_count < max_attempts:
            preflight = self._prepare_return_packet_delivery(
                session,
                return_packet_id,
                expected_chat_url=expected_chat_url,
                validate_chat_state=validate_chat_state,
            )
            if preflight["status"] != "ready":
                if preflight["status"] == "already_visible":
                    return {
                        "status": "delivered",
                        "attempt_count": attempt_count,
                        "attempts": attempts,
                    }
                attempt_count += 1
                attempts.append(
                    {
                        "attempt_number": attempt_count,
                        "status": "failed",
                        "transport": "chatgpt_browser",
                        "return_packet_id": return_packet_id,
                        "error_signature": str(preflight.get("error_signature", "")).strip(),
                    }
                )
                return {
                    "status": "preflight_failed",
                    "attempt_count": attempt_count,
                    "attempts": attempts,
                }
            try:
                if self.adapter.return_packet_visible(session, return_packet_id):
                    return {"status": "delivered", "attempt_count": attempt_count, "attempts": attempts}
            except Exception:
                pass
            try:
                response = dict(self.adapter.post_user_message(session, packet_text, return_packet_id))
            except Exception as exc:
                response = {
                    "status": "failed",
                    "error_signature": enrich_browser_blocker_reason(str(exc)),
                    "return_packet_id": return_packet_id,
                }
            attempt_count += 1
            status = str(response.get("status", "failed"))
            error_signature = str(response.get("error_signature", ""))
            attempts.append(
                {
                    "attempt_number": attempt_count,
                    "status": status,
                    "transport": "chatgpt_browser",
                    "return_packet_id": return_packet_id,
                    "error_signature": error_signature,
                }
            )
            if status == "delivered":
                return {"status": "delivered", "attempt_count": attempt_count, "attempts": attempts}
            try:
                packet_visible = self.adapter.return_packet_visible(session, return_packet_id)
            except Exception as exc:
                packet_visible = False
                if not error_signature:
                    error_signature = enrich_browser_blocker_reason(str(exc))
                    attempts[-1]["error_signature"] = error_signature
            if packet_visible:
                attempts[-1]["status"] = "delivered"
                return {"status": "delivered", "attempt_count": attempt_count, "attempts": attempts}
            if _delivery_error_requires_cooldown(error_signature):
                return {"status": _RETRY_REQUIRED_DELIVERY_STATUS, "attempt_count": attempt_count, "attempts": attempts}
            if error_signature not in known_error_signatures:
                return {"status": _RETRY_REQUIRED_DELIVERY_STATUS, "attempt_count": attempt_count, "attempts": attempts}
        if attempts and self._confirm_packet_visibility_with_grace(
            session,
            return_packet_id,
            expected_chat_url=expected_chat_url,
            validate_chat_state=validate_chat_state,
        ):
            attempts[-1]["status"] = "delivered"
            return {"status": "delivered", "attempt_count": attempt_count, "attempts": attempts}
        return {"status": _RETRY_REQUIRED_DELIVERY_STATUS, "attempt_count": attempt_count, "attempts": attempts}

    def _prepare_return_packet_delivery(
        self,
        session,
        return_packet_id: str,
        *,
        expected_chat_url: str = "",
        validate_chat_state: bool = True,
    ) -> dict[str, str]:
        if validate_chat_state:
            normalized_expected_chat_url = _canonical_chat_url(
                str(expected_chat_url or getattr(session, "chat_url", "") or "").strip()
            )
            if normalized_expected_chat_url:
                try:
                    observed_chat_url = str(self.adapter.current_chat_url(session) or "").strip()
                except Exception as exc:
                    return {
                        "status": "failed",
                        "error_signature": enrich_browser_blocker_reason(str(exc)),
                    }
                normalized_observed_chat_url = _canonical_chat_url(observed_chat_url)
                if normalized_observed_chat_url != normalized_expected_chat_url:
                    return {
                        "status": "failed",
                        "error_signature": (
                            "ChatGPT is showing a different chat URL than the active session expects. "
                            "The bridge refused to post the return packet into a shifted chat surface."
                        ),
                    }
        try:
            if self.adapter.return_packet_visible(session, return_packet_id):
                return {"status": "already_visible"}
        except Exception:
            pass
        expected_anchor = str(getattr(session, "last_seen_chat_message_anchor", "") or "").strip() if validate_chat_state else ""
        expected_hash = str(getattr(session, "latest_assistant_message_hash", "") or "").strip() if validate_chat_state else ""
        if validate_chat_state and (expected_anchor or expected_hash):
            try:
                latest_assistant = self.adapter.read_latest_assistant_message(session)
            except Exception as exc:
                return {
                    "status": "failed",
                    "error_signature": enrich_browser_blocker_reason(str(exc)),
                }
            assistant_text = str(latest_assistant.get("text", "") or "")
            assistant_anchor = str(
                latest_assistant.get("message_anchor", "")
                or latest_assistant.get("message_id", "")
                or ""
            ).strip()
            assistant_hash = hashlib.sha1(assistant_text.encode("utf-8")).hexdigest() if assistant_text else ""
            same_indexed_turn = _same_indexed_message_turn(expected_anchor, assistant_anchor)
            anchor_mismatch = bool(
                expected_anchor and assistant_anchor and assistant_anchor != expected_anchor and not same_indexed_turn
            )
            hash_mismatch = bool(
                expected_hash and assistant_hash and assistant_hash != expected_hash and not same_indexed_turn
            )
            if anchor_mismatch or hash_mismatch:
                return {
                    "status": "failed",
                    "error_signature": (
                        "ChatGPT is showing a different assistant turn than the one that started this Codex run. "
                        "The bridge refused to post the return packet into a shifted chat state."
                    ),
                }
        try:
            prepared = dict(self.adapter.prepare_return_packet_delivery(session))
        except Exception as exc:
            prepared = {
                "status": "failed",
                "error_signature": enrich_browser_blocker_reason(str(exc)),
            }
        if str(prepared.get("status", "")).strip() == "ready":
            return {"status": "ready"}
        return {
            "status": "failed",
            "error_signature": str(prepared.get("error_signature", "")).strip()
            or f"Return packet `{return_packet_id}` delivery preflight failed.",
        }

    def _confirm_packet_visibility_with_grace(
        self,
        session,
        return_packet_id: str,
        *,
        expected_chat_url: str = "",
        validate_chat_state: bool = True,
    ) -> bool:
        normalized_expected_chat_url = (
            _canonical_chat_url(str(expected_chat_url or getattr(session, "chat_url", "") or "").strip())
            if validate_chat_state
            else ""
        )
        for attempt in range(_DELIVERY_CONFIRMATION_GRACE_POLLS):
            if normalized_expected_chat_url:
                try:
                    observed_chat_url = str(self.adapter.current_chat_url(session) or "").strip()
                except Exception:
                    return False
                if _canonical_chat_url(observed_chat_url) != normalized_expected_chat_url:
                    return False
            try:
                if self.adapter.return_packet_visible(session, return_packet_id):
                    return True
            except Exception:
                return False
            if attempt < _DELIVERY_CONFIRMATION_GRACE_POLLS - 1:
                time.sleep(_DELIVERY_CONFIRMATION_GRACE_INTERVAL_SECONDS)
        return False

    def _recover_missing_visible_return_packet(self, session, binding, policy_state: dict[str, Any]) -> dict[str, Any] | None:
        pending_packet_id = str(getattr(session, "last_outbound_user_message_anchor", "") or "").strip()
        if not pending_packet_id:
            pending_packet_id = str(getattr(session, "last_posted_return_packet_id", "") or "").strip()
        if not pending_packet_id:
            return None
        try:
            if self.adapter.return_packet_visible(session, pending_packet_id):
                return None
        except Exception:
            return None
        return self._resume_pending_return_packet_delivery(
            session,
            binding,
            policy_state,
            pending_packet_id=pending_packet_id,
        )

    def _resume_pending_return_packet_delivery(
        self,
        session,
        binding,
        policy_state: dict[str, Any],
        *,
        pending_packet_id: str = "",
    ) -> dict[str, Any] | None:
        honored_state = _honor_non_active_session_state(self.sessions_dir, session, runner_action="paused")
        if honored_state is not None:
            return honored_state
        pending_packet_id = pending_packet_id or str(getattr(session, "last_outbound_user_message_anchor", "") or "").strip()
        if not pending_packet_id:
            return None
        report = _load_latest_run_report(self.sessions_dir, session.session_id)
        if report is None:
            _clear_outbound_user_message(session)
            save_session(session_path(self.sessions_dir, session.session_id), session)
            return None
        if str(getattr(report, "delivery_status", "") or "").strip().casefold() == "delivered":
            return self._recover_delivered_report_state(session, report)
        report.session_id = report.session_id or session.session_id
        report.bridge_session_id = report.bridge_session_id or session.session_id
        report.binding_id = report.binding_id or session.binding_id
        report.return_packet_id = report.return_packet_id or pending_packet_id or derive_return_packet_id(report)
        packet = build_return_packet(report)
        packet_text = render_return_packet(packet)
        delivery = self._deliver_packet_with_refocus(
            session,
            binding,
            policy_state,
            packet.return_packet_id,
            packet_text,
        )
        latest_attempts = [item for item in delivery.get("attempts", []) if isinstance(item, dict)]
        previous_attempts = [
            item
            for item in getattr(report, "delivery_attempts", [])
            if isinstance(item, dict)
        ]
        aggregated_attempts = [*previous_attempts, *latest_attempts]
        report.delivery_status = delivery["status"]
        report.delivery_attempts = aggregated_attempts
        report.delivery_attempt_count = len(aggregated_attempts)
        session.delivery_attempts = [
            ChatDeliveryAttempt.from_dict(item)
            for item in aggregated_attempts
            if isinstance(item, dict)
        ]
        if delivery["status"] == "delivered":
            _record_outbound_user_message(session, message_anchor=packet.return_packet_id, kind="return_packet")
            session.last_posted_return_packet_id = packet.return_packet_id
            session.cycles_completed += 1
            if session.stop_after_cycle_requested:
                session.status = "completed"
                _set_loop_state(session, "completed")
                session.auto_run_enabled = False
                session.supervisor_status = "stopped"
                session.stop_after_cycle_requested = False
                session.human_attention_reason = ""
                session.last_error = ""
                session.policy_decision = LoopPolicyDecision(
                    policy_outcome="stopped",
                    reasons=["Stop requested after the current cycle."],
                    time_budget_minutes=session.time_budget_minutes,
                    time_budget_remaining_minutes=session.budget_remaining_minutes,
                )
                report.policy_outcome = session.policy_decision.policy_outcome
                _persist_run_report(report)
                save_session(session_path(self.sessions_dir, session.session_id), session)
                return _result_payload(
                    session,
                    session.policy_decision,
                    packet.return_packet_id,
                    delivery,
                    runner_action="stopped",
                )
            session.status = "active"
            _set_loop_state(session, "waiting_for_chatgpt_response")
            session.auto_run_enabled = True
            session.supervisor_status = "running"
            session.human_attention_reason = ""
            session.last_error = ""
            session.degraded_mode = ""
            session.degraded_reason = ""
            session.policy_decision = _allow_policy_decision(
                session,
                [
                    "The pending return packet was delivered successfully on an automatic retry.",
                ],
            )
            report.policy_outcome = session.policy_decision.policy_outcome
            _persist_run_report(report)
            save_session(session_path(self.sessions_dir, session.session_id), session)
            return _result_payload(
                session,
                session.policy_decision,
                packet.return_packet_id,
                delivery,
                runner_action="wait_for_chatgpt",
            )

        error_signatures = [
            str(item.get("error_signature", "")).strip()
            for item in latest_attempts
            if str(item.get("error_signature", "")).strip()
        ]
        if _pending_return_packet_became_stale(session, error_signatures):
            reason = (
                "ChatGPT shifted to a different assistant turn before the pending Codex return packet "
                "could be delivered. The bridge refused to discard the undelivered packet and start a "
                "new Codex turn automatically."
            )
            session.last_posted_return_packet_id = ""
            session.last_error = reason
            session.degraded_mode = "stale_return_packet"
            session.degraded_reason = reason
            report.delivery_status = "stale_chat_state"
            report.policy_outcome = "require_human"
            _persist_run_report(report)
            return _block_for_human(
                self.sessions_dir,
                session,
                reason=reason,
                last_error=reason,
                category="stale_return_packet_chat_state",
            )
        retry_reason = "; ".join(error_signatures) or "Return packet delivery could not be confirmed."
        honored_state = _honor_non_active_session_state(self.sessions_dir, session, runner_action="paused")
        if honored_state is not None:
            report.delivery_status = "paused"
            report.policy_outcome = "paused"
            _persist_run_report(report)
            return honored_state
        _record_outbound_user_message(
            session,
            message_anchor=packet.return_packet_id,
            kind="return_packet_retry_pending",
        )
        session.status = "active"
        _set_loop_state(session, "posting_return_packet")
        session.auto_run_enabled = True
        session.supervisor_status = "running"
        session.human_attention_reason = ""
        session.last_error = retry_reason
        session.degraded_mode = "retrying_return_packet"
        session.degraded_reason = retry_reason
        session.last_posted_return_packet_id = ""
        session.policy_decision = _allow_policy_decision(
            session,
            [
                "A pending return packet still could not be delivered.",
                "The loop kept the session active and will retry delivery automatically.",
            ],
        )
        report.policy_outcome = session.policy_decision.policy_outcome
        _persist_run_report(report)
        save_session(session_path(self.sessions_dir, session.session_id), session)
        return _result_payload(
            session,
            session.policy_decision,
            packet.return_packet_id,
            delivery,
            runner_action="wait_for_chatgpt",
        )


def _report_looks_like_retryable_codex_runtime_failure(report) -> bool:
    text = "\n".join(
        str(item)
        for item in [
            getattr(report, "summary", ""),
            getattr(report, "next_step", ""),
            *list(getattr(report, "blockers", []) or []),
            _read_report_failure_stderr_tail(str(getattr(report, "stderr_path", "") or "")),
        ]
        if str(item).strip()
    ).casefold()
    return any(marker in text for marker in _RETRYABLE_CODEX_RUNTIME_FAILURE_MARKERS)


def _completed_run_recovery_overrides(
    assistant_text: str,
    *,
    sessions_dir: Path,
    session_id: str,
) -> list[str]:
    normalized = str(assistant_text or "").casefold()
    if not all(marker in normalized for marker in _STALE_RECOVERY_PROMPT_MARKERS):
        return []
    project_root = _project_root_from_sessions_dir(sessions_dir)
    run_ids = list(dict.fromkeys(_RUN_DIR_ID_RE.findall(str(assistant_text or ""))))
    if not run_ids and "latest" in normalized:
        latest = _latest_completed_run_id(project_root, session_id)
        if latest:
            run_ids = [latest]
    completed_run_ids: list[str] = []
    for run_id in run_ids:
        if not run_id.endswith(session_id):
            continue
        run_dir = project_root / "artifacts" / "runs" / run_id
        if _run_dir_completed_successfully(run_dir):
            completed_run_ids.append(run_id)
    if not completed_run_ids:
        return []
    joined = ", ".join(completed_run_ids[:3])
    return [
        (
            "Bridge completion proof: ChatGPT described run(s) "
            f"{joined} as truncated/recovery work, but local bridge artifacts show the referenced run completed "
            "successfully with exit_code=0 and a final Codex message. Treat that wording as stale visibility "
            "context, do not recover or re-verify those completed run(s), and continue from their committed outcome "
            "to the next concrete project frontier."
        )
    ]


def _latest_completed_run_id(project_root: Path, session_id: str) -> str:
    runs_root = project_root / "artifacts" / "runs"
    if not runs_root.exists():
        return ""
    for run_dir in sorted(runs_root.glob(f"*-{session_id}"), reverse=True):
        if _run_dir_completed_successfully(run_dir):
            return run_dir.name
    return ""


def _run_dir_completed_successfully(run_dir: Path) -> bool:
    if not run_dir.exists():
        return False
    report_path = run_dir / "run_report.json"
    if report_path.exists():
        try:
            report_payload = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            report_payload = {}
        if isinstance(report_payload, dict):
            exit_code = int(report_payload.get("exit_code", 1) or 0)
            interruption = str(report_payload.get("interruption_reason", "") or "").strip()
            final_message = str(report_payload.get("final_agent_message", "") or "").strip()
            if exit_code == 0 and interruption in {"", "none"} and (
                final_message or _completed_run_dir_has_final_message(run_dir)
            ):
                return True
    live_output = run_dir / "live_output.log"
    if not live_output.exists() or not _completed_run_dir_has_final_message(run_dir):
        return False
    try:
        tail = live_output.read_text(encoding="utf-8", errors="replace")[-2000:]
    except OSError:
        return False
    return "=== run finished" in tail and "exit_code=0" in tail


def _completed_run_dir_has_final_message(run_dir: Path) -> bool:
    last_message = run_dir / "last_message.md"
    try:
        return last_message.exists() and last_message.stat().st_size > 0
    except OSError:
        return False


def _read_report_failure_stderr_tail(stderr_path: str) -> str:
    if not stderr_path:
        return ""
    path = Path(stderr_path)
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > 65536:
                handle.seek(-65536, 2)
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
