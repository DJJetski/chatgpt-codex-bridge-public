from __future__ import annotations

from typing import Any

from .models import (
    InstructionScopeUpdate,
    LoopPolicyDecision,
    OrchestratorSession,
    current_budget_remaining_seconds,
    refresh_session_budget,
)


def apply_instruction_updates(
    session_like: dict[str, Any] | OrchestratorSession,
    policy_state: dict[str, Any],
    updates: list[dict[str, Any] | InstructionScopeUpdate],
) -> None:
    session_updates = _get_instruction_updates(session_like)
    project_updates = [str(item) for item in policy_state.get("project_instruction_updates", []) if str(item).strip()]

    for update in updates:
        normalized = _normalize_update(update)
        if not normalized.text.strip():
            continue
        if normalized.scope == "project":
            if normalized.mode == "replace":
                project_updates = [normalized.text]
            elif normalized.text not in project_updates:
                project_updates.append(normalized.text)
            continue

        serialized = normalized.as_dict()
        if normalized.mode == "replace":
            session_updates = [item for item in session_updates if item.get("scope") != normalized.scope]
        session_updates.append(serialized)

    policy_state["project_instruction_updates"] = project_updates
    _set_instruction_updates(session_like, session_updates)


def resolve_instruction_texts(
    session_like: dict[str, Any] | OrchestratorSession,
    policy_state: dict[str, Any],
) -> list[str]:
    resolved: list[str] = []
    for item in policy_state.get("project_instruction_updates", []):
        text = str(item).strip()
        if text and text not in resolved:
            resolved.append(text)

    session_items = _get_instruction_updates(session_like)
    for scope in ("session", "next_run"):
        for item in session_items:
            if item.get("scope") != scope:
                continue
            text = str(item.get("text", "")).strip()
            if text and text not in resolved:
                resolved.append(text)
    return resolved


def consume_next_run_instructions(session_like: dict[str, Any] | OrchestratorSession) -> None:
    remaining = [item for item in _get_instruction_updates(session_like) if item.get("scope") != "next_run"]
    _set_instruction_updates(session_like, remaining)


def evaluate_loop_policy(
    session: OrchestratorSession,
    decision: str,
    *,
    human_gate_required: bool,
    human_gate_reason: str,
    human_gate_category: str,
) -> LoopPolicyDecision:
    refresh_session_budget(session)
    if decision == "pause":
        return LoopPolicyDecision(policy_outcome="paused", reasons=["Pause requested by orchestrator."])
    if decision == "stop":
        return LoopPolicyDecision(policy_outcome="stopped", reasons=["Stop requested by orchestrator."])
    if decision in {"wait_for_human", "draft_only"}:
        return LoopPolicyDecision(policy_outcome="require_human", reasons=["Human review requested by orchestrator."])
    if human_gate_required:
        return LoopPolicyDecision(
            policy_outcome="require_human",
            reasons=[human_gate_reason or "Human gate required."],
            human_gate_required=True,
            human_gate_reason=human_gate_reason,
            human_gate_category=human_gate_category,
        )
    if current_budget_remaining_seconds(session) <= 0:
        return LoopPolicyDecision(
            policy_outcome="budget_exhausted",
            reasons=["Session budget is exhausted."],
            time_budget_minutes=session.time_budget_minutes,
            time_budget_remaining_minutes=session.budget_remaining_minutes,
        )
    return LoopPolicyDecision(
        policy_outcome="allow",
        reasons=["Loop policy allows this Codex run."],
        time_budget_minutes=session.time_budget_minutes,
        time_budget_remaining_minutes=session.budget_remaining_minutes,
    )


def _get_instruction_updates(session_like: dict[str, Any] | OrchestratorSession) -> list[dict[str, Any]]:
    if isinstance(session_like, dict):
        updates = session_like.get("instruction_updates", [])
        return [dict(item) for item in updates if isinstance(item, dict)]
    return [item.as_dict() for item in session_like.instruction_updates]


def _set_instruction_updates(
    session_like: dict[str, Any] | OrchestratorSession,
    updates: list[dict[str, Any]],
) -> None:
    if isinstance(session_like, dict):
        session_like["instruction_updates"] = updates
        return
    session_like.instruction_updates = [InstructionScopeUpdate.from_dict(item) for item in updates]


def _normalize_update(update: dict[str, Any] | InstructionScopeUpdate) -> InstructionScopeUpdate:
    if isinstance(update, InstructionScopeUpdate):
        return update
    return InstructionScopeUpdate.from_dict(dict(update))
