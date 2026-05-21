from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from hashlib import sha1
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


@dataclass(slots=True)
class DecisionContext:
    project_name: str
    task_label: str
    goal_family: str
    work_unit: str
    repo_path: str
    current_worktree: str
    target_worktree: str
    current_branch: str
    target_branch: str
    same_goal_family: bool
    same_work_unit: bool
    same_repo: bool
    same_worktree: bool
    same_branch: bool
    assumptions_stable: bool
    rebrief_cost: str
    topic_shift: str
    context_overloaded: bool
    parallel_isolation_needed: bool
    risky_changes: bool
    unrelated_uncommitted_changes: bool
    needs_clean_replay: bool
    current_thread_id: str | None = None
    candidate_thread_id: str | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DecisionContext":
        return cls(
            project_name=str(payload["project_name"]),
            task_label=str(payload["task_label"]),
            current_thread_id=payload.get("current_thread_id"),
            candidate_thread_id=payload.get("candidate_thread_id"),
            goal_family=str(payload["goal_family"]),
            work_unit=str(payload["work_unit"]),
            repo_path=str(payload["repo_path"]),
            current_worktree=str(payload["current_worktree"]),
            target_worktree=str(payload.get("target_worktree", payload["current_worktree"])),
            current_branch=str(payload["current_branch"]),
            target_branch=str(payload.get("target_branch", payload["current_branch"])),
            same_goal_family=bool(payload["same_goal_family"]),
            same_work_unit=bool(payload["same_work_unit"]),
            same_repo=bool(payload["same_repo"]),
            same_worktree=bool(payload["same_worktree"]),
            same_branch=bool(payload["same_branch"]),
            assumptions_stable=bool(payload["assumptions_stable"]),
            rebrief_cost=str(payload["rebrief_cost"]),
            topic_shift=str(payload["topic_shift"]),
            context_overloaded=bool(payload["context_overloaded"]),
            parallel_isolation_needed=bool(payload["parallel_isolation_needed"]),
            risky_changes=bool(payload["risky_changes"]),
            unrelated_uncommitted_changes=bool(payload["unrelated_uncommitted_changes"]),
            needs_clean_replay=bool(payload["needs_clean_replay"]),
        )


@dataclass(slots=True)
class DecisionResult:
    thread_action: str
    worktree_action: str
    branch_action: str
    selected_thread_id: str
    context_continuity_percent: int
    continuity_band: str
    recommended_prompt_mode: str
    reasons: list[str]
    suggested_branch: str | None = None
    suggested_worktree: str | None = None
    timestamp: str = field(default_factory=now_iso)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class PromptRequest:
    mode: str
    project_name: str
    thread_id: str | None
    goal_family: str
    work_unit: str
    repo_path: str
    worktree_path: str
    branch: str
    thread_action: str
    objective: str = ""
    task: str = ""
    decision_summary: str = ""
    delta_summary: str = ""
    read_order: list[str] = field(default_factory=list)
    durable_state: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    recent_results: list[str] = field(default_factory=list)
    required_output: list[str] = field(default_factory=list)
    reflection_inputs: list[str] = field(default_factory=list)
    result_context: list[str] = field(default_factory=list)
    rebrief_reason: str = ""
    parent_thread_id: str = ""
    carry_forward: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PromptRequest":
        return cls(
            mode=str(payload["mode"]),
            project_name=str(payload["project_name"]),
            thread_id=payload.get("thread_id"),
            goal_family=str(payload.get("goal_family", "")),
            work_unit=str(payload.get("work_unit", "")),
            repo_path=str(payload.get("repo_path", "")),
            worktree_path=str(payload.get("worktree_path", "")),
            branch=str(payload.get("branch", "")),
            thread_action=str(payload.get("thread_action", "")),
            objective=str(payload.get("objective", "")),
            task=str(payload.get("task", "")),
            decision_summary=str(payload.get("decision_summary", "")),
            delta_summary=str(payload.get("delta_summary", "")),
            read_order=_as_list(payload.get("read_order")),
            durable_state=_as_list(payload.get("durable_state")),
            constraints=_as_list(payload.get("constraints")),
            acceptance_criteria=_as_list(payload.get("acceptance_criteria")),
            recent_results=_as_list(payload.get("recent_results")),
            required_output=_as_list(payload.get("required_output")),
            reflection_inputs=_as_list(payload.get("reflection_inputs")),
            result_context=_as_list(payload.get("result_context")),
            rebrief_reason=str(payload.get("rebrief_reason", "")),
            parent_thread_id=str(payload.get("parent_thread_id", "")),
            carry_forward=_as_list(payload.get("carry_forward")),
        )


@dataclass(slots=True)
class RunReport:
    timestamp: str
    thread_id: str
    summary: str
    files_touched: list[str]
    checks: list[str]
    blockers: list[str]
    risks: list[str]
    next_step: str
    workspace_path: str = ""
    thread_action: str = ""
    parent_thread_id: str = ""
    lineage_root_thread_id: str = ""
    lineage_depth: int = 0
    lineage_path: list[str] = field(default_factory=list)
    observed_codex_thread_id: str = ""
    final_agent_message: str = ""
    visible_assistant_trace: list[str] = field(default_factory=list)
    event_types: list[str] = field(default_factory=list)
    commands_observed: list[dict[str, Any]] = field(default_factory=list)
    workspace_apply_status: str = ""
    workspace_apply_commands: list[str] = field(default_factory=list)
    workspace_apply_warnings: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    context_window_tokens: int = 0
    context_used_tokens: int = 0
    estimated_context_remaining_percent: int = -1
    context_signal_source: str = ""
    context_continuity_percent: int = -1
    continuity_band: str = ""
    artifacts_dir: str = ""
    prompt_path: str = ""
    raw_output_path: str = ""
    last_message_path: str = ""
    stderr_path: str = ""
    session_live_log_path: str = ""
    exit_code: int = 0
    command: list[str] = field(default_factory=list)
    interruption_reason: str = ""
    return_packet_id: str = ""
    delivery_status: str = ""
    delivery_attempt_count: int = 0
    delivery_attempts: list[dict[str, Any]] = field(default_factory=list)
    policy_outcome: str = ""
    budget_snapshot: dict[str, int] = field(default_factory=dict)
    session_id: str = ""
    bridge_session_id: str = ""
    binding_id: str = ""
    run_id: str = ""
    requested_codex_thread_id: str = ""
    codex_thread_id: str = ""
    codex_compaction: dict[str, Any] = field(default_factory=dict)
    thread_operation: str = ""
    degraded_mode: str = ""
    degraded_reasons: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RunReport":
        return cls(
            timestamp=str(payload.get("timestamp", now_iso())),
            thread_id=str(payload["thread_id"]),
            summary=str(payload["summary"]),
            parent_thread_id=str(payload.get("parent_thread_id", "")),
            lineage_root_thread_id=str(payload.get("lineage_root_thread_id", "")),
            lineage_depth=int(payload.get("lineage_depth", 0)),
            lineage_path=_as_list(payload.get("lineage_path")),
            observed_codex_thread_id=str(payload.get("observed_codex_thread_id", "")),
            final_agent_message=str(payload.get("final_agent_message", "")),
            visible_assistant_trace=_as_list(payload.get("visible_assistant_trace")),
            event_types=_as_list(payload.get("event_types")),
            commands_observed=[
                dict(item) for item in payload.get("commands_observed", []) if isinstance(item, dict)
            ],
            workspace_apply_status=str(payload.get("workspace_apply_status", "")),
            workspace_apply_commands=_as_list(payload.get("workspace_apply_commands")),
            workspace_apply_warnings=_as_list(payload.get("workspace_apply_warnings")),
            usage=dict(payload.get("usage", {})),
            context_window_tokens=int(payload.get("context_window_tokens", 0) or 0),
            context_used_tokens=int(payload.get("context_used_tokens", 0) or 0),
            estimated_context_remaining_percent=int(payload.get("estimated_context_remaining_percent", -1)),
            context_signal_source=str(payload.get("context_signal_source", "")),
            context_continuity_percent=int(payload.get("context_continuity_percent", -1)),
            continuity_band=str(payload.get("continuity_band", "")),
            files_touched=_as_list(payload.get("files_touched")),
            checks=_as_list(payload.get("checks")),
            blockers=_as_list(payload.get("blockers")),
            risks=_as_list(payload.get("risks")),
            next_step=str(payload.get("next_step", "")),
            workspace_path=str(payload.get("workspace_path", "")),
            thread_action=str(payload.get("thread_action", "")),
            artifacts_dir=str(payload.get("artifacts_dir", "")),
            prompt_path=str(payload.get("prompt_path", "")),
            raw_output_path=str(payload.get("raw_output_path", "")),
            last_message_path=str(payload.get("last_message_path", "")),
            stderr_path=str(payload.get("stderr_path", "")),
            session_live_log_path=str(payload.get("session_live_log_path", "")),
            exit_code=int(payload.get("exit_code", 0)),
            command=_as_list(payload.get("command")),
            interruption_reason=str(payload.get("interruption_reason", "")),
            return_packet_id=str(payload.get("return_packet_id", "")),
            delivery_status=str(payload.get("delivery_status", "")),
            delivery_attempt_count=int(payload.get("delivery_attempt_count", 0)),
            delivery_attempts=[
                dict(item) for item in payload.get("delivery_attempts", []) if isinstance(item, dict)
            ],
            policy_outcome=str(payload.get("policy_outcome", "")),
            budget_snapshot={
                str(key): int(value)
                for key, value in dict(payload.get("budget_snapshot", {})).items()
                if isinstance(value, int)
            },
            session_id=str(payload.get("session_id", "")),
            bridge_session_id=str(payload.get("bridge_session_id", payload.get("session_id", ""))),
            binding_id=str(payload.get("binding_id", "")),
            run_id=str(payload.get("run_id", "")),
            requested_codex_thread_id=str(payload.get("requested_codex_thread_id", "")),
            codex_thread_id=str(
                payload.get(
                    "codex_thread_id",
                    payload.get("observed_codex_thread_id", payload.get("requested_codex_thread_id", "")),
                )
            ),
            codex_compaction=dict(payload.get("codex_compaction", {}))
            if isinstance(payload.get("codex_compaction", {}), dict)
            else {},
            thread_operation=str(payload.get("thread_operation", "")),
            degraded_mode=str(payload.get("degraded_mode", "")),
            degraded_reasons=_as_list(payload.get("degraded_reasons")),
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ReturnPacket:
    return_packet_id: str
    thread_id: str
    session_id: str
    binding_id: str
    run_id: str
    summary: str
    final_output: str
    visible_trace: list[str]
    commands_observed: list[str]
    files_touched: list[str]
    checks: list[str]
    blockers: list[str]
    risks: list[str]
    next_step: str
    artifacts: list[str]
    session_live_log_path: str = ""
    workspace_path: str = ""
    observed_codex_thread_id: str = ""
    thread_action: str = ""
    parent_thread_id: str = ""
    lineage_root_thread_id: str = ""
    lineage_path: list[str] = field(default_factory=list)
    workspace_apply_status: str = ""
    workspace_apply_commands: list[str] = field(default_factory=list)
    workspace_apply_warnings: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    context_window_tokens: int = 0
    context_used_tokens: int = 0
    estimated_context_remaining_percent: int = -1
    context_signal_source: str = ""
    context_continuity_percent: int = -1
    continuity_band: str = ""
    delivery_status: str = ""
    delivery_attempt_count: int = 0
    budget_snapshot: dict[str, int] = field(default_factory=dict)
    policy_outcome: str = ""
    requested_codex_thread_id: str = ""
    codex_thread_id: str = ""
    thread_operation: str = ""
    degraded_mode: str = ""
    degraded_reasons: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ReturnPacket":
        return cls(
            return_packet_id=str(payload["return_packet_id"]),
            thread_id=str(payload.get("thread_id", "")),
            session_id=str(payload.get("session_id", "")),
            binding_id=str(payload.get("binding_id", "")),
            run_id=str(payload.get("run_id", "")),
            summary=str(payload.get("summary", "")),
            final_output=str(payload.get("final_output", "")),
            visible_trace=_as_list(payload.get("visible_trace")),
            commands_observed=_as_list(payload.get("commands_observed")),
            files_touched=_as_list(payload.get("files_touched")),
            checks=_as_list(payload.get("checks")),
            blockers=_as_list(payload.get("blockers")),
            risks=_as_list(payload.get("risks")),
            next_step=str(payload.get("next_step", "")),
            artifacts=_as_list(payload.get("artifacts")),
            session_live_log_path=str(payload.get("session_live_log_path", "")),
            workspace_path=str(payload.get("workspace_path", "")),
            observed_codex_thread_id=str(payload.get("observed_codex_thread_id", "")),
            thread_action=str(payload.get("thread_action", "")),
            parent_thread_id=str(payload.get("parent_thread_id", "")),
            lineage_root_thread_id=str(payload.get("lineage_root_thread_id", "")),
            lineage_path=_as_list(payload.get("lineage_path")),
            workspace_apply_status=str(payload.get("workspace_apply_status", "")),
            workspace_apply_commands=_as_list(payload.get("workspace_apply_commands")),
            workspace_apply_warnings=_as_list(payload.get("workspace_apply_warnings")),
            usage={
                str(key): int(value)
                for key, value in dict(payload.get("usage", {})).items()
                if isinstance(value, int)
            },
            context_window_tokens=int(payload.get("context_window_tokens", 0) or 0),
            context_used_tokens=int(payload.get("context_used_tokens", 0) or 0),
            estimated_context_remaining_percent=int(payload.get("estimated_context_remaining_percent", -1)),
            context_signal_source=str(payload.get("context_signal_source", "")),
            context_continuity_percent=int(payload.get("context_continuity_percent", -1)),
            continuity_band=str(payload.get("continuity_band", "")),
            delivery_status=str(payload.get("delivery_status", "")),
            delivery_attempt_count=int(payload.get("delivery_attempt_count", 0)),
            budget_snapshot={
                str(key): int(value)
                for key, value in dict(payload.get("budget_snapshot", {})).items()
                if isinstance(value, int)
            },
            policy_outcome=str(payload.get("policy_outcome", "")),
            requested_codex_thread_id=str(payload.get("requested_codex_thread_id", "")),
            codex_thread_id=str(
                payload.get(
                    "codex_thread_id",
                    payload.get("observed_codex_thread_id", payload.get("requested_codex_thread_id", "")),
                )
            ),
            thread_operation=str(payload.get("thread_operation", "")),
            degraded_mode=str(payload.get("degraded_mode", "")),
            degraded_reasons=_as_list(payload.get("degraded_reasons")),
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def derive_return_packet_id(report: RunReport) -> str:
    if report.return_packet_id:
        return report.return_packet_id
    digest = sha1(
        "|".join(
            [
                report.timestamp,
                report.thread_id,
                report.summary,
                report.observed_codex_thread_id,
                report.run_id,
            ]
        ).encode("utf-8")
    ).hexdigest()[:12]
    return f"packet-{digest}"


def repo_root() -> Path:
    override = os.environ.get("BRIDGE_REPO_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    package_file = Path(__file__).resolve()
    for candidate in package_file.parents:
        if (candidate / "pyproject.toml").is_file() and (candidate / "src" / "chatgpt_codex_bridge").is_dir():
            return candidate
    return package_file.parent.parent
