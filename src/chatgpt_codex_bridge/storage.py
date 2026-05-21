from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .launching import LaunchPlan
from .models import DecisionContext, DecisionResult, RunReport


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(path)


def update_registry_with_decision(
    registry: dict[str, Any],
    context: DecisionContext,
    decision: DecisionResult,
) -> dict[str, Any]:
    threads = list(registry.get("threads", []))
    thread_id = decision.selected_thread_id
    thread_entry = next((item for item in threads if item.get("thread_id") == thread_id), None)

    if thread_entry is None:
        thread_entry = {"thread_id": thread_id}
        threads.append(thread_entry)

    parent_thread_id = ""
    if decision.thread_action == "fork_thread" and context.current_thread_id:
        parent_thread_id = context.current_thread_id
    elif decision.thread_action == "same_thread":
        parent_thread_id = str(thread_entry.get("parent_thread_id", ""))

    lineage = _resolve_thread_lineage(threads, thread_id, parent_thread_id or None)

    thread_entry.update(
        {
            "status": "active",
            "goal_family": context.goal_family,
            "work_unit": context.work_unit,
            "repo_path": context.repo_path,
            "worktree_path": decision.suggested_worktree or context.current_worktree,
            "branch": decision.suggested_branch or context.current_branch,
            "last_task_label": context.task_label,
            "last_decision_at": decision.timestamp,
            "last_context_continuity_percent": decision.context_continuity_percent,
            "last_continuity_band": decision.continuity_band,
            "recommended_prompt_mode": decision.recommended_prompt_mode,
            "parent_thread_id": lineage["parent_thread_id"],
            "lineage_root_thread_id": lineage["lineage_root_thread_id"],
            "lineage_depth": lineage["lineage_depth"],
            "lineage_path": lineage["lineage_path"],
        }
    )

    decision_log = list(registry.get("decision_log", []))
    decision_log.append(
        {
            "timestamp": decision.timestamp,
            "task_label": context.task_label,
            "thread_action": decision.thread_action,
            "worktree_action": decision.worktree_action,
            "branch_action": decision.branch_action,
            "context_continuity_percent": decision.context_continuity_percent,
            "continuity_band": decision.continuity_band,
            "recommended_prompt_mode": decision.recommended_prompt_mode,
            "selected_thread_id": decision.selected_thread_id,
            "parent_thread_id": lineage["parent_thread_id"],
            "lineage_root_thread_id": lineage["lineage_root_thread_id"],
            "lineage_depth": lineage["lineage_depth"],
            "lineage_path": lineage["lineage_path"],
            "reasons": decision.reasons,
        }
    )

    registry["threads"] = threads
    registry["decision_log"] = decision_log
    return registry


def update_registry_with_launch_plan(registry: dict[str, Any], plan: LaunchPlan) -> dict[str, Any]:
    threads = list(registry.get("threads", []))
    thread_entry = next((item for item in threads if item.get("thread_id") == plan.selected_thread_id), None)
    if thread_entry is None:
        thread_entry = {"thread_id": plan.selected_thread_id}
        threads.append(thread_entry)

    thread_entry.update(
        {
            "last_workspace_apply_status": plan.workspace_apply_status,
            "last_workspace_apply_commands": list(plan.workspace_apply_commands),
            "last_workspace_apply_warnings": list(plan.workspace_apply_warnings),
        }
    )

    decision_log = list(registry.get("decision_log", []))
    if decision_log:
        last_decision = decision_log[-1]
        if last_decision.get("selected_thread_id") == plan.selected_thread_id:
            last_decision.update(
                {
                    "workspace_apply_status": plan.workspace_apply_status,
                    "workspace_apply_commands": list(plan.workspace_apply_commands),
                    "workspace_apply_warnings": list(plan.workspace_apply_warnings),
                }
            )

    registry["threads"] = threads
    registry["decision_log"] = decision_log
    return registry


def append_execution_log(log_path: Path, report: RunReport) -> None:
    lines = [
        "",
        f"## {report.timestamp} | {report.thread_id}",
        "",
        f"Summary: {report.summary}",
        "",
        "Context signals:",
        *_bullet_lines(_context_signal_lines(report)),
        "",
        "Lineage:",
        *_bullet_lines(_lineage_lines(report)),
        "",
        "Files touched:",
        *_bullet_lines(report.files_touched),
        "",
        "Checks:",
        *_bullet_lines(report.checks),
        "",
        "Commands observed:",
        *_bullet_lines(_command_lines(report.commands_observed)),
        "",
        "Workspace apply:",
        *_bullet_lines(_workspace_apply_lines(report)),
        "",
        "Blockers:",
        *_bullet_lines(report.blockers),
        "",
        "Risks:",
        *_bullet_lines(report.risks),
        "",
        f"Next step: {report.next_step}",
        "",
    ]
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines))


def update_registry_with_report(registry: dict[str, Any], report: RunReport) -> dict[str, Any]:
    threads = list(registry.get("threads", []))
    thread_entry = next((item for item in threads if item.get("thread_id") == report.thread_id), None)
    if thread_entry is None:
        thread_entry = {"thread_id": report.thread_id}
        threads.append(thread_entry)
    lineage = _resolve_thread_lineage(
        threads,
        report.thread_id,
        str(thread_entry.get("parent_thread_id", "")) or None,
    )
    thread_entry.update(
        {
            "status": "active",
            "last_summary": report.summary,
            "last_result_at": report.timestamp,
            "last_next_step": report.next_step,
            "last_exec_thread_id": report.observed_codex_thread_id or thread_entry.get("last_exec_thread_id", ""),
            "last_context_window_tokens": report.context_window_tokens,
            "last_context_used_tokens": report.context_used_tokens,
            "last_estimated_context_remaining_percent": report.estimated_context_remaining_percent,
            "last_context_signal_source": report.context_signal_source,
            "last_context_continuity_percent": report.context_continuity_percent,
            "last_continuity_band": report.continuity_band,
            "parent_thread_id": lineage["parent_thread_id"] or str(thread_entry.get("parent_thread_id", "")),
            "lineage_root_thread_id": lineage["lineage_root_thread_id"],
            "lineage_depth": lineage["lineage_depth"],
            "lineage_path": lineage["lineage_path"],
        }
    )
    registry["threads"] = threads
    return registry


def enrich_report_with_registry_context(registry: dict[str, Any], report: RunReport) -> RunReport:
    threads = list(registry.get("threads", []))
    thread_entry = next((item for item in threads if item.get("thread_id") == report.thread_id), None)
    lineage = _resolve_thread_lineage(
        threads,
        report.thread_id,
        report.parent_thread_id or None,
    )
    report.parent_thread_id = lineage["parent_thread_id"]
    report.lineage_root_thread_id = lineage["lineage_root_thread_id"]
    report.lineage_depth = lineage["lineage_depth"]
    report.lineage_path = lineage["lineage_path"]
    if thread_entry:
        if not report.workspace_apply_status:
            report.workspace_apply_status = str(thread_entry.get("last_workspace_apply_status", ""))
        if not report.workspace_apply_commands:
            report.workspace_apply_commands = [
                str(item) for item in thread_entry.get("last_workspace_apply_commands", [])
            ]
        if not report.workspace_apply_warnings:
            report.workspace_apply_warnings = [
                str(item) for item in thread_entry.get("last_workspace_apply_warnings", [])
            ]
        if report.context_continuity_percent < 0:
            report.context_continuity_percent = int(thread_entry.get("last_context_continuity_percent", -1) or -1)
        if not report.continuity_band:
            report.continuity_band = str(thread_entry.get("last_continuity_band", ""))
        if report.context_window_tokens <= 0:
            report.context_window_tokens = int(thread_entry.get("last_context_window_tokens", 0) or 0)
        if report.context_used_tokens <= 0:
            report.context_used_tokens = int(thread_entry.get("last_context_used_tokens", 0) or 0)
        if report.estimated_context_remaining_percent < 0:
            report.estimated_context_remaining_percent = int(
                thread_entry.get("last_estimated_context_remaining_percent", -1) or -1
            )
        if not report.context_signal_source:
            report.context_signal_source = str(thread_entry.get("last_context_signal_source", ""))
    return report


def _bullet_lines(items: list[str]) -> list[str]:
    if not items:
        return ["- none"]
    return [f"- {item}" for item in items]


def _command_lines(commands: list[dict[str, Any]]) -> list[str]:
    if not commands:
        return ["none"]
    lines: list[str] = []
    for command in commands:
        command_text = str(command.get("command", "") or "unknown command")
        status_text = str(command.get("status", "") or "unknown")
        exit_code = command.get("exit_code")
        exit_text = "none" if exit_code is None else str(exit_code)
        lines.append(f"{command_text} | status: {status_text} | exit: {exit_text}")
    return lines


def _lineage_lines(report: RunReport) -> list[str]:
    path_text = " -> ".join(report.lineage_path) if report.lineage_path else report.thread_id
    return [
        f"parent: {report.parent_thread_id or 'none'}",
        f"root: {report.lineage_root_thread_id or report.thread_id}",
        f"depth: {report.lineage_depth}",
        f"path: {path_text}",
    ]


def _workspace_apply_lines(report: RunReport) -> list[str]:
    status = report.workspace_apply_status or "none"
    lines = [f"status: {status}"]
    if report.workspace_apply_commands:
        lines.extend(f"command: {command}" for command in report.workspace_apply_commands)
    if report.workspace_apply_warnings:
        lines.extend(f"warning: {warning}" for warning in report.workspace_apply_warnings)
    return lines


def _context_signal_lines(report: RunReport) -> list[str]:
    lines: list[str] = []
    if report.estimated_context_remaining_percent >= 0:
        lines.append(
            "estimated remaining context: "
            f"{report.estimated_context_remaining_percent}% "
            f"(used: {report.context_used_tokens}, window: {report.context_window_tokens}, source: {report.context_signal_source or 'unknown'})"
        )
    if report.context_continuity_percent >= 0:
        lines.append(
            "continuity heuristic: "
            f"{report.context_continuity_percent}% ({report.continuity_band or 'unknown'})"
        )
    if report.usage:
        usage_text = ", ".join(f"{key}={value}" for key, value in sorted(report.usage.items()))
        lines.append(f"usage: {usage_text}")
    return lines or ["none"]


def _resolve_thread_lineage(
    threads: list[dict[str, Any]],
    thread_id: str,
    fallback_parent_thread_id: str | None = None,
) -> dict[str, Any]:
    if not thread_id:
        return {
            "parent_thread_id": "",
            "lineage_root_thread_id": "",
            "lineage_depth": 0,
            "lineage_path": [],
        }

    path_reversed: list[str] = []
    seen: set[str] = set()
    current_thread_id = thread_id
    next_parent_thread_id: str | None = fallback_parent_thread_id

    while current_thread_id and current_thread_id not in seen:
        path_reversed.append(current_thread_id)
        seen.add(current_thread_id)
        thread_entry = next((item for item in threads if item.get("thread_id") == current_thread_id), None)
        if next_parent_thread_id is None:
            current_thread_id = str(thread_entry.get("parent_thread_id", "")) if thread_entry else ""
        else:
            current_thread_id = next_parent_thread_id
            next_parent_thread_id = None

    lineage_path = list(reversed(path_reversed))
    parent_thread_id = lineage_path[-2] if len(lineage_path) > 1 else ""
    return {
        "parent_thread_id": parent_thread_id,
        "lineage_root_thread_id": lineage_path[0],
        "lineage_depth": max(0, len(lineage_path) - 1),
        "lineage_path": lineage_path,
    }
