from __future__ import annotations

from .models import DecisionContext, DecisionResult


def _continuity_percent(context: DecisionContext) -> int:
    score = 0

    if context.same_goal_family:
        score += 20
    if context.same_work_unit:
        score += 15
    if context.same_repo:
        score += 10
    if context.same_worktree:
        score += 10
    if context.same_branch:
        score += 10
    if context.assumptions_stable:
        score += 15

    score += {"low": 10, "medium": 5, "high": 0}.get(context.rebrief_cost, 0)
    score += {"none": 10, "adjacent": 5, "major": 0}.get(context.topic_shift, 0)

    if context.context_overloaded:
        score -= 15
    if context.parallel_isolation_needed:
        score -= 10
    if context.risky_changes:
        score -= 5
    if context.unrelated_uncommitted_changes:
        score -= 10

    if not context.same_goal_family:
        score -= 45
    if not context.assumptions_stable:
        score -= 35
    if context.needs_clean_replay:
        score -= 35

    return max(0, min(100, score))


def _continuity_band(percent: int) -> str:
    if percent < 40:
        return "low"
    if percent < 70:
        return "medium"
    return "high"


def _recommended_prompt_mode(thread_action: str) -> str:
    if thread_action == "same_thread":
        return "codex_continue_thread"
    if thread_action == "fork_thread":
        return "codex_rebrief"
    return "codex_new_thread"


def _candidate_thread_id(context: DecisionContext) -> str:
    if context.candidate_thread_id:
        return context.candidate_thread_id
    if context.current_thread_id:
        return f"{context.current_thread_id}-fork"
    return f"{context.project_name}-{context.task_label}"


def decide_actions(context: DecisionContext) -> DecisionResult:
    reasons: list[str] = []
    continuity_percent = _continuity_percent(context)
    continuity_band = _continuity_band(continuity_percent)

    if context.same_goal_family:
        reasons.append("Goal family is unchanged.")
    else:
        reasons.append("Goal family changed.")

    if not context.assumptions_stable:
        reasons.append("Core assumptions changed.")

    if context.needs_clean_replay:
        reasons.append("A clean replayable narrative is required.")

    if context.context_overloaded:
        reasons.append("Current thread is context-overloaded.")

    if continuity_percent < 40:
        reasons.append("Continuity fell below the 40 percent threshold.")
    elif continuity_percent < 70:
        reasons.append("Continuity remains above 40 percent but needs a cleaner handoff.")
    else:
        reasons.append("Continuity is strong enough to keep the thread if no other rule blocks it.")

    if context.parallel_isolation_needed and (
        context.risky_changes
        or context.unrelated_uncommitted_changes
        or not context.same_worktree
    ):
        worktree_action = "new_worktree"
        reasons.append("Parallel risky changes need isolated workspace.")
    elif not context.same_worktree:
        worktree_action = "new_worktree"
        reasons.append("Target worktree differs from the current worktree.")
    else:
        worktree_action = "reuse_worktree"

    if (
        not context.same_branch
        or context.unrelated_uncommitted_changes
        or (context.parallel_isolation_needed and context.risky_changes)
    ):
        branch_action = "new_branch"
        reasons.append("A separate branch keeps this change line reviewable.")
    else:
        branch_action = "reuse_branch"

    if continuity_percent < 40:
        thread_action = "new_thread"
        selected_thread_id = _candidate_thread_id(context)
    elif (
        context.same_work_unit
        and context.same_repo
        and context.same_worktree
        and context.same_branch
        and context.topic_shift == "none"
        and context.rebrief_cost != "high"
        and not context.context_overloaded
        and context.assumptions_stable
    ):
        thread_action = "same_thread"
        selected_thread_id = context.current_thread_id or _candidate_thread_id(context)
    else:
        thread_action = "fork_thread"
        selected_thread_id = _candidate_thread_id(context)

    suggested_branch = context.target_branch if branch_action == "new_branch" else context.current_branch
    suggested_worktree = (
        context.target_worktree if worktree_action == "new_worktree" else context.current_worktree
    )
    recommended_prompt_mode = _recommended_prompt_mode(thread_action)

    return DecisionResult(
        thread_action=thread_action,
        worktree_action=worktree_action,
        branch_action=branch_action,
        selected_thread_id=selected_thread_id,
        context_continuity_percent=continuity_percent,
        continuity_band=continuity_band,
        recommended_prompt_mode=recommended_prompt_mode,
        suggested_branch=suggested_branch,
        suggested_worktree=suggested_worktree,
        reasons=reasons,
    )


__all__ = ["DecisionContext", "DecisionResult", "decide_actions"]
