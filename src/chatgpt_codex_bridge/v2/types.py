from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class SessionRecord:
    session_id: str
    repo_path: str
    workspace_path: str
    mode: str
    status: str
    active_worker: str
    current_codex_thread_id: str
    stop_requested: bool
    pause_requested: bool
    operator_goal: str
    operator_notes: str
    chatgpt_model: str
    chatgpt_reasoning_effort: str
    codex_model: str
    codex_reasoning_effort: str
    codex_execution_mode: str
    context_files: list[str]
    session_summary: str
    active_turn_id: str
    resume_target_status: str
    last_error: str
    created_at: str
    updated_at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class TurnRecord:
    turn_id: str
    session_id: str
    sequence: int
    worker: str
    status: str
    input_hash: str
    started_at: str
    finished_at: str
    artifact_path: str
    idempotency_key: str
    payload: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    error_text: str = ""
    committed_at: str = ""
    created_at: str = ""
    updated_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class EventRecord:
    event_id: int
    session_id: str
    turn_id: str
    event_type: str
    payload: dict[str, Any]
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class WorkerLease:
    lease_name: str
    session_id: str
    turn_id: str
    worker: str
    owner_pid: int
    heartbeat_at: str
    expires_at: str
    artifact_path: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ChatGPTTurnResult:
    decision: str
    codex_thread_mode: str
    codex_prompt: str
    summary: str
    reasoning: str
    needs_human_reason: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CodexTurnResult:
    status: str
    summary: str
    final_output: str
    observed_thread_id: str
    exit_code: int
    files_touched: list[str]
    checks: list[str]
    blockers: list[str]
    estimated_context_remaining_percent: int
    artifacts_dir: str
    codex_compaction: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
