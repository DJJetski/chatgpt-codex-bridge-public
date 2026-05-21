from __future__ import annotations

import shlex
import subprocess
from dataclasses import asdict, dataclass, field
from importlib import resources
from pathlib import Path
from string import Template

from .models import DecisionContext, DecisionResult


@dataclass(slots=True)
class LaunchPlan:
    project_name: str
    thread_action: str
    selected_thread_id: str
    parent_thread_id: str
    lineage_path: list[str]
    prompt_mode: str
    context_continuity_percent: int
    continuity_band: str
    repo_path: str
    worktree_path: str
    branch: str
    worktree_action: str
    branch_action: str
    reasons: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    workspace_apply_status: str = "not_requested"
    workspace_apply_commands: list[str] = field(default_factory=list)
    workspace_apply_warnings: list[str] = field(default_factory=list)
    next_prompt_path: str = ""

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def build_launch_plan(
    context: DecisionContext,
    decision: DecisionResult,
    registry: dict[str, object],
    prompt_output: Path,
) -> LaunchPlan:
    thread_entry = next(
        (
            item
            for item in registry.get("threads", [])
            if isinstance(item, dict) and item.get("thread_id") == decision.selected_thread_id
        ),
        {},
    )
    parent_thread_id = str(thread_entry.get("parent_thread_id", ""))
    lineage_path = [str(item) for item in thread_entry.get("lineage_path", [])]
    worktree_path = decision.suggested_worktree or context.current_worktree
    branch = decision.suggested_branch or context.current_branch
    git_repo = _is_git_repo(Path(context.repo_path))

    commands = _workspace_commands(context, decision, worktree_path, branch, git_repo, prompt_output)
    warnings = _workspace_warnings(context, decision, git_repo)

    return LaunchPlan(
        project_name=context.project_name,
        thread_action=decision.thread_action,
        selected_thread_id=decision.selected_thread_id,
        parent_thread_id=parent_thread_id,
        lineage_path=lineage_path,
        prompt_mode=decision.recommended_prompt_mode,
        context_continuity_percent=decision.context_continuity_percent,
        continuity_band=decision.continuity_band,
        repo_path=context.repo_path,
        worktree_path=worktree_path,
        branch=branch,
        worktree_action=decision.worktree_action,
        branch_action=decision.branch_action,
        reasons=list(decision.reasons),
        commands=commands,
        warnings=warnings,
        next_prompt_path=str(prompt_output),
    )


def render_launch_plan(plan: LaunchPlan) -> str:
    template_text = (
        resources.files("chatgpt_codex_bridge.prompts")
        .joinpath("start_cycle.md")
        .read_text(encoding="utf-8")
    )
    template = Template(template_text)
    mapping = {
        "project_name": plan.project_name,
        "thread_action": plan.thread_action,
        "selected_thread_id": plan.selected_thread_id,
        "parent_thread_id": plan.parent_thread_id or "none",
        "lineage_path": _format_list(plan.lineage_path),
        "prompt_mode": plan.prompt_mode,
        "context_continuity_percent": str(plan.context_continuity_percent),
        "continuity_band": plan.continuity_band,
        "repo_path": plan.repo_path,
        "worktree_path": plan.worktree_path,
        "branch": plan.branch,
        "worktree_action": plan.worktree_action,
        "branch_action": plan.branch_action,
        "workspace_apply_status": plan.workspace_apply_status,
        "workspace_apply_commands": _format_list(plan.workspace_apply_commands),
        "workspace_apply_warnings": _format_list(plan.workspace_apply_warnings),
        "reasons": _format_list(plan.reasons),
        "commands": _format_list(plan.commands),
        "warnings": _format_list(plan.warnings),
        "next_prompt_path": plan.next_prompt_path,
    }
    return template.safe_substitute(mapping).strip() + "\n"


def apply_launch_plan(
    context: DecisionContext,
    decision: DecisionResult,
    plan: LaunchPlan,
) -> LaunchPlan:
    git_repo = _is_git_repo(Path(context.repo_path))
    warnings = _workspace_warnings(context, decision, git_repo)

    if not git_repo and (
        decision.branch_action == "new_branch" or decision.worktree_action == "new_worktree"
    ):
        plan.workspace_apply_status = "skipped"
        plan.workspace_apply_commands = []
        plan.workspace_apply_warnings = warnings
        return plan

    if decision.worktree_action == "new_worktree":
        if decision.branch_action != "new_branch":
            plan.workspace_apply_status = "skipped"
            plan.workspace_apply_commands = []
            plan.workspace_apply_warnings = warnings or [
                "Workspace apply only supports auto-creating a new worktree when a dedicated branch is created at the same time."
            ]
            return plan
        command = [
            "git",
            "-C",
            context.repo_path,
            "worktree",
            "add",
            "-b",
            plan.branch,
            plan.worktree_path,
            context.current_branch,
        ]
        return _run_workspace_command(plan, command)

    if decision.branch_action == "new_branch":
        command = ["git", "-C", plan.worktree_path, "checkout", "-b", plan.branch]
        return _run_workspace_command(plan, command)

    if decision.branch_action == "reuse_branch" and context.current_branch != plan.branch:
        command = ["git", "-C", plan.worktree_path, "checkout", plan.branch]
        return _run_workspace_command(plan, command)

    plan.workspace_apply_status = "not_needed"
    plan.workspace_apply_commands = []
    plan.workspace_apply_warnings = []
    return plan


def _workspace_commands(
    context: DecisionContext,
    decision: DecisionResult,
    worktree_path: str,
    branch: str,
    git_repo: bool,
    prompt_output: Path,
) -> list[str]:
    repo_q = shlex.quote(context.repo_path)
    worktree_q = shlex.quote(worktree_path)
    current_branch_q = shlex.quote(context.current_branch)
    branch_q = shlex.quote(branch)

    if decision.worktree_action == "new_worktree" and git_repo and decision.branch_action == "new_branch":
        return [
            f"git -C {repo_q} worktree add -b {branch_q} {worktree_q} {current_branch_q}",
            f"cd {worktree_q}",
            f"# then paste {shlex.quote(str(prompt_output))} into the selected Codex thread",
        ]

    commands = [f"cd {worktree_q}"]

    if git_repo and decision.branch_action == "new_branch":
        commands.append(f"git -C {worktree_q} checkout -b {branch_q}")
    elif git_repo and decision.branch_action == "reuse_branch" and context.current_branch != branch:
        commands.append(f"git -C {worktree_q} checkout {branch_q}")

    commands.append(f"# then paste {shlex.quote(str(prompt_output))} into the selected Codex thread")
    return commands


def _workspace_warnings(
    context: DecisionContext,
    decision: DecisionResult,
    git_repo: bool,
) -> list[str]:
    warnings: list[str] = []
    if not git_repo and (
        decision.branch_action == "new_branch" or decision.worktree_action == "new_worktree"
    ):
        warnings.append(f"`{context.repo_path}` is not a Git work tree, so branch/worktree commands are advisory only.")
    if git_repo and decision.worktree_action == "new_worktree" and decision.branch_action != "new_branch":
        warnings.append(
            "A new worktree without a new branch is not auto-generated because Git usually needs a dedicated branch or detached HEAD."
        )
    return warnings


def _is_git_repo(path: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def _run_workspace_command(plan: LaunchPlan, command: list[str]) -> LaunchPlan:
    plan.workspace_apply_commands = [shlex.join(command)]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        stdout = result.stdout.strip()
        detail = stderr or stdout or "unknown error"
        plan.workspace_apply_status = "failed"
        plan.workspace_apply_warnings = [f"Workspace apply failed: {detail}"]
        return plan

    plan.workspace_apply_status = "applied"
    plan.workspace_apply_warnings = []
    return plan


def _format_list(items: list[str]) -> str:
    if not items:
        return "- none"
    return "\n".join(f"- {item}" for item in items)
