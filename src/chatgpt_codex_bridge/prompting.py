from __future__ import annotations

from importlib import resources
from string import Template

from .codex_capabilities import codex_exec_capability_guidance_text
from .models import DecisionContext, DecisionResult, PromptRequest, RunReport


TEMPLATE_FILES = {
    "codex_new_thread": "codex_new_thread.md",
    "codex_continue_thread": "codex_continue_thread.md",
    "mastermind_reflection": "mastermind_reflection.md",
    "mastermind_return": "mastermind_return.md",
    "codex_rebrief": "codex_rebrief.md",
}

DEFAULT_REFLECTION_STATE_FILES = [
    "README.md",
    "docs/ARCHITECTURE.md",
    "docs/private/DECISIONS.md",
    "docs/private/ARCHITECTURE_DECISION.md",
    "docs/THREAD_POLICY.md",
    "BRIDGE_HOME/state/THREAD_REGISTRY.json",
]


def _format_list(items: list[str]) -> str:
    if not items:
        return "- none"
    return "\n".join(f"- {item}" for item in items)


def _format_block(lines: list[str]) -> str:
    if not lines:
        return "none"
    return "\n".join(lines)


def _indented_lines(items: list[str]) -> list[str]:
    if not items:
        return ["  - none"]
    return [f"  - {item}" for item in items]


def _inline_list(items: list[str]) -> str:
    if not items:
        return "none"
    return "; ".join(items)


def _local_context_thread_hint(estimated_context_remaining_percent: int) -> str:
    if estimated_context_remaining_percent < 0:
        return "none"
    if estimated_context_remaining_percent < 40:
        return "prefer_new_thread_due_to_context"
    return "same_thread_context_ok"


def _merge_reflection_state_files(state_files: list[str]) -> list[str]:
    merged: list[str] = []
    for item in [*state_files, *DEFAULT_REFLECTION_STATE_FILES]:
        if item and item not in merged:
            merged.append(item)
    return merged


def _summarize_command_runs(commands: list[dict[str, object]]) -> list[str]:
    if not commands:
        return ["none"]
    lines: list[str] = []
    for command in commands:
        command_text = str(command.get("command", "") or "unknown command")
        status_text = str(command.get("status", "") or "unknown")
        exit_code = command.get("exit_code")
        exit_text = "none" if exit_code is None else str(exit_code)
        output = " ".join(str(command.get("aggregated_output", "")).split())
        if len(output) > 120:
            output = output[:117] + "..."
        output_text = output or "none"
        lines.append(
            f"{command_text} | status: {status_text} | exit: {exit_text} | output: {output_text}"
        )
    return lines


def available_prompt_templates() -> list[str]:
    prompt_root = resources.files("chatgpt_codex_bridge.prompts")
    return sorted(child.name for child in prompt_root.iterdir() if child.name.endswith(".md"))


def _template_text(mode: str) -> str:
    try:
        filename = TEMPLATE_FILES[mode]
    except KeyError as exc:
        raise ValueError(f"Unsupported prompt mode: {mode}") from exc
    return (
        resources.files("chatgpt_codex_bridge.prompts")
        .joinpath(filename)
        .read_text(encoding="utf-8")
    )


def render_prompt(request: PromptRequest) -> str:
    template = Template(_template_text(request.mode))
    mapping = {
        "project_name": request.project_name,
        "thread_id": request.thread_id or "",
        "goal_family": request.goal_family,
        "work_unit": request.work_unit,
        "repo_path": request.repo_path,
        "worktree_path": request.worktree_path,
        "branch": request.branch,
        "thread_action": request.thread_action,
        "objective": request.objective,
        "task": request.task,
        "decision_summary": request.decision_summary,
        "delta_summary": request.delta_summary,
        "read_order": _format_list(request.read_order),
        "durable_state": _format_list(request.durable_state),
        "constraints": _format_list(request.constraints),
        "acceptance_criteria": _format_list(request.acceptance_criteria),
        "recent_results": _format_list(request.recent_results),
        "required_output": _format_list(request.required_output),
        "reflection_inputs": _format_list(request.reflection_inputs),
        "result_context": _format_block(request.result_context),
        "rebrief_reason": request.rebrief_reason,
        "parent_thread_id": request.parent_thread_id,
        "carry_forward": _format_list(request.carry_forward),
        "exec_capability_notes": codex_exec_capability_guidance_text(),
    }
    return template.safe_substitute(mapping).strip() + "\n"


def apply_decision_to_request(
    request: PromptRequest,
    context: DecisionContext,
    decision: DecisionResult,
) -> PromptRequest:
    request.mode = decision.recommended_prompt_mode
    request.thread_id = decision.selected_thread_id
    request.thread_action = decision.thread_action
    request.worktree_path = decision.suggested_worktree or context.current_worktree
    request.branch = decision.suggested_branch or context.current_branch
    request.decision_summary = (
        f"Thread action: {decision.thread_action}. "
        f"Context continuity: {decision.context_continuity_percent} percent ({decision.continuity_band}). "
        f"Reasons: {' '.join(decision.reasons)}"
    )
    if decision.thread_action == "same_thread":
        guidance = (
            "Reuse the existing thread context; keep pushing the active implementation strand forward and do not restate "
            "the whole project or reread all docs unless they are stale or newly relevant."
        )
    else:
        guidance = (
            "Use a fresh-thread handoff with enough inline baseline project context, recent decisions, active-strand "
            "position, and the latest verified findings before asking Codex to act. Do not ask Codex to manufacture "
            "continuation files just to recreate context."
        )
    if request.decision_summary:
        request.decision_summary = f"{request.decision_summary} {guidance}"
    else:
        request.decision_summary = guidance
    if decision.thread_action != "same_thread" and context.current_thread_id:
        request.parent_thread_id = context.current_thread_id
    if not request.rebrief_reason and decision.thread_action != "same_thread":
        request.rebrief_reason = " ".join(decision.reasons)
    if not request.carry_forward:
        request.carry_forward = list(request.recent_results) or list(request.durable_state)
    return request


def build_return_prompt_request(report: RunReport) -> PromptRequest:
    context_lines = [
        f"Timestamp: {report.timestamp}",
        f"Thread: {report.thread_id}",
        f"Parent thread: {report.parent_thread_id or 'none'}",
        f"Lineage root thread: {report.lineage_root_thread_id or report.thread_id}",
        f"Lineage depth: {report.lineage_depth}",
        f"Observed Codex exec thread: {report.observed_codex_thread_id or 'none'}",
        f"Summary: {report.summary}",
        f"Final agent message: {report.final_agent_message or 'none'}",
        f"Exit code: {report.exit_code}",
        f"Estimated remaining context: {report.estimated_context_remaining_percent if report.estimated_context_remaining_percent >= 0 else 'none'}",
        f"Local context thread hint: {_local_context_thread_hint(report.estimated_context_remaining_percent)}",
        f"Context continuity heuristic: {report.context_continuity_percent if report.context_continuity_percent >= 0 else 'none'} ({report.continuity_band or 'none'})",
        f"Artifacts directory: {report.artifacts_dir or 'none'}",
        f"Prompt copy: {report.prompt_path or 'none'}",
        f"Raw event stream: {report.raw_output_path or 'none'}",
        f"Last message file: {report.last_message_path or 'none'}",
        f"Stderr file: {report.stderr_path or 'none'}",
        "Lineage path:",
        *_indented_lines(report.lineage_path),
        "Event types:",
        *_indented_lines(report.event_types),
        "Commands observed:",
        *_indented_lines(_summarize_command_runs(report.commands_observed)),
        f"Workspace apply status: {report.workspace_apply_status or 'none'}",
        "Workspace apply commands:",
        *_indented_lines(report.workspace_apply_commands),
        "Workspace apply warnings:",
        *_indented_lines(report.workspace_apply_warnings),
        "Usage:",
        *_indented_lines([f"{key}: {value}" for key, value in report.usage.items()]),
        "Files touched:",
        *_indented_lines(report.files_touched),
        "Checks:",
        *_indented_lines(report.checks),
        "Blockers:",
        *_indented_lines(report.blockers),
        "Risks:",
        *_indented_lines(report.risks),
        f"Recommended next step: {report.next_step}",
        "Paste the full raw Codex reply below this packet in ChatGPT when deeper analysis is needed.",
    ]
    return PromptRequest(
        mode="mastermind_return",
        project_name="",
        thread_id=report.thread_id,
        goal_family="",
        work_unit="",
        repo_path="",
        worktree_path="",
        branch="",
        thread_action="",
        result_context=context_lines,
    )


def build_reflection_prompt_request(
    report: RunReport,
    *,
    state_files: list[str] | None = None,
    report_path: str = "",
) -> PromptRequest:
    lineage_path = " -> ".join(report.lineage_path) if report.lineage_path else report.thread_id
    reflection_inputs = [
        f"Durable state file: {path}"
        for path in _merge_reflection_state_files(state_files or [])
    ]
    if report_path:
        reflection_inputs.append(f"Latest run report: {report_path}")
    reflection_inputs.extend(
        [
            f"Thread: {report.thread_id}",
            f"Parent thread: {report.parent_thread_id or 'none'}",
            f"Thread lineage: {lineage_path}",
            f"Latest run summary: {report.summary}",
            f"Latest files touched: {_inline_list(report.files_touched)}",
            f"Latest checks: {_inline_list(report.checks)}",
            f"Latest blockers: {_inline_list(report.blockers)}",
            f"Latest risks: {_inline_list(report.risks)}",
            f"Recommended next step: {report.next_step}",
        ]
    )
    if report.raw_output_path:
        reflection_inputs.append(f"Latest raw event stream: {report.raw_output_path}")
    if report.last_message_path:
        reflection_inputs.append(f"Latest last-message file: {report.last_message_path}")
    if report.workspace_apply_status:
        reflection_inputs.append(f"Workspace apply status: {report.workspace_apply_status}")

    return PromptRequest(
        mode="mastermind_reflection",
        project_name="",
        thread_id=report.thread_id,
        goal_family="",
        work_unit="",
        repo_path="",
        worktree_path="",
        branch="",
        thread_action="",
        reflection_inputs=reflection_inputs,
    )


__all__ = [
    "PromptRequest",
    "apply_decision_to_request",
    "build_reflection_prompt_request",
    "build_return_prompt_request",
    "render_prompt",
]
