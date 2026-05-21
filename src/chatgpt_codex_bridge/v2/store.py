from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from hashlib import sha1
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from ..defaults import (
    DEFAULT_CHATGPT_MODEL,
    DEFAULT_CHATGPT_REASONING_EFFORT,
    DEFAULT_CODEX_MODEL,
    DEFAULT_CODEX_REASONING_EFFORT,
)
from ..models import now_iso
from .types import EventRecord, SessionRecord, TurnRecord, WorkerLease

_NONTERMINAL_TURN_STATUSES = ("queued", "running")
_TERMINAL_TURN_STATUSES = ("completed", "failed", "failed_validation", "aborted", "committed")
_SESSION_TERMINAL_STATUSES = ("blocked_human", "stopped", "completed")
_DEFAULT_CHATGPT_MODEL = DEFAULT_CHATGPT_MODEL
_DEFAULT_CHATGPT_REASONING_EFFORT = DEFAULT_CHATGPT_REASONING_EFFORT
_DEFAULT_CODEX_MODEL = DEFAULT_CODEX_MODEL
_DEFAULT_CODEX_REASONING_EFFORT = DEFAULT_CODEX_REASONING_EFFORT
_DEFAULT_CODEX_EXECUTION_MODE = "cli_only"
_CODEX_EXECUTION_MODES = ("cli_only", "allow_app")


def _json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True)


def _json_loads(payload: str | None) -> dict[str, Any]:
    if not payload:
        return {}
    return json.loads(payload)


def _json_loads_list(payload: str | None) -> list[str]:
    if not payload:
        return []
    parsed = json.loads(payload)
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed]


def _generated_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


def _future_iso(seconds: float) -> str:
    return (datetime.now().astimezone() + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _lease_name(session_id: str, kind: str) -> str:
    return f"session:{session_id}:{kind}"


def _is_past_due(timestamp: str) -> bool:
    if not timestamp:
        return True
    return datetime.now().astimezone() >= datetime.fromisoformat(timestamp)


class V2Store:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    repo_path TEXT NOT NULL,
                    workspace_path TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    active_worker TEXT NOT NULL DEFAULT '',
                    current_codex_thread_id TEXT NOT NULL DEFAULT '',
                    stop_requested INTEGER NOT NULL DEFAULT 0,
                    pause_requested INTEGER NOT NULL DEFAULT 0,
                    operator_goal TEXT NOT NULL DEFAULT '',
                    operator_notes TEXT NOT NULL DEFAULT '',
                    chatgpt_model TEXT NOT NULL DEFAULT 'gpt-5.5',
                    chatgpt_reasoning_effort TEXT NOT NULL DEFAULT 'xhigh',
                    codex_model TEXT NOT NULL DEFAULT 'gpt-5.5',
                    codex_reasoning_effort TEXT NOT NULL DEFAULT 'xhigh',
                    codex_execution_mode TEXT NOT NULL DEFAULT 'cli_only',
                    context_files_json TEXT NOT NULL DEFAULT '[]',
                    session_summary TEXT NOT NULL DEFAULT '',
                    active_turn_id TEXT NOT NULL DEFAULT '',
                    resume_target_status TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS turns (
                    turn_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    worker TEXT NOT NULL,
                    status TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    started_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT '',
                    artifact_path TEXT NOT NULL DEFAULT '',
                    idempotency_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error_text TEXT NOT NULL DEFAULT '',
                    committed_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                );

                CREATE UNIQUE INDEX IF NOT EXISTS turns_session_sequence_idx
                ON turns(session_id, sequence);

                CREATE UNIQUE INDEX IF NOT EXISTS turns_session_idempotency_idx
                ON turns(session_id, idempotency_key);

                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL DEFAULT '',
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS worker_leases (
                    lease_name TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    worker TEXT NOT NULL,
                    owner_pid INTEGER NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    artifact_path TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS codex_threads (
                    session_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    thread_mode TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_used_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                );
                """
            )
            self._ensure_session_columns(connection)
            connection.commit()

    def _ensure_session_columns(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute("PRAGMA table_info(sessions)").fetchall()
        columns = {str(row["name"]) for row in rows}
        desired_columns = {
            "chatgpt_model": f"ALTER TABLE sessions ADD COLUMN chatgpt_model TEXT NOT NULL DEFAULT '{_DEFAULT_CHATGPT_MODEL}'",
            "chatgpt_reasoning_effort": f"ALTER TABLE sessions ADD COLUMN chatgpt_reasoning_effort TEXT NOT NULL DEFAULT '{_DEFAULT_CHATGPT_REASONING_EFFORT}'",
            "codex_model": f"ALTER TABLE sessions ADD COLUMN codex_model TEXT NOT NULL DEFAULT '{_DEFAULT_CODEX_MODEL}'",
            "codex_reasoning_effort": f"ALTER TABLE sessions ADD COLUMN codex_reasoning_effort TEXT NOT NULL DEFAULT '{_DEFAULT_CODEX_REASONING_EFFORT}'",
            "codex_execution_mode": f"ALTER TABLE sessions ADD COLUMN codex_execution_mode TEXT NOT NULL DEFAULT '{_DEFAULT_CODEX_EXECUTION_MODE}'",
            "context_files_json": "ALTER TABLE sessions ADD COLUMN context_files_json TEXT NOT NULL DEFAULT '[]'",
        }
        for column_name, statement in desired_columns.items():
            if column_name in columns:
                continue
            connection.execute(statement)

    def create_session(
        self,
        *,
        repo_path: Path,
        workspace_path: Path,
        operator_goal: str,
        operator_notes: str = "",
        chatgpt_model: str = _DEFAULT_CHATGPT_MODEL,
        chatgpt_reasoning_effort: str = _DEFAULT_CHATGPT_REASONING_EFFORT,
        codex_model: str = _DEFAULT_CODEX_MODEL,
        codex_reasoning_effort: str = _DEFAULT_CODEX_REASONING_EFFORT,
        codex_execution_mode: str = _DEFAULT_CODEX_EXECUTION_MODE,
        context_files: list[str] | None = None,
        mode: str = "terminal_first",
        session_id: str | None = None,
    ) -> SessionRecord:
        now = now_iso()
        session_id = session_id or _generated_id("session")
        record = SessionRecord(
            session_id=session_id,
            repo_path=str(repo_path),
            workspace_path=str(workspace_path),
            mode=mode,
            status="manual_bootstrap",
            active_worker="",
            current_codex_thread_id="",
            stop_requested=False,
            pause_requested=False,
            operator_goal=operator_goal,
            operator_notes=operator_notes,
            chatgpt_model=str(chatgpt_model or _DEFAULT_CHATGPT_MODEL).strip() or _DEFAULT_CHATGPT_MODEL,
            chatgpt_reasoning_effort=str(chatgpt_reasoning_effort or _DEFAULT_CHATGPT_REASONING_EFFORT).strip()
            or _DEFAULT_CHATGPT_REASONING_EFFORT,
            codex_model=str(codex_model or _DEFAULT_CODEX_MODEL).strip() or _DEFAULT_CODEX_MODEL,
            codex_reasoning_effort=str(codex_reasoning_effort or _DEFAULT_CODEX_REASONING_EFFORT).strip()
            or _DEFAULT_CODEX_REASONING_EFFORT,
            codex_execution_mode=_normalize_codex_execution_mode(codex_execution_mode),
            context_files=[str(item) for item in (context_files or [])],
            session_summary="",
            active_turn_id="",
            resume_target_status="manual_bootstrap",
            last_error="",
            created_at=now,
            updated_at=now,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions (
                    session_id, repo_path, workspace_path, mode, status, active_worker,
                    current_codex_thread_id, stop_requested, pause_requested, operator_goal,
                    operator_notes, chatgpt_model, chatgpt_reasoning_effort, codex_model,
                    codex_reasoning_effort, codex_execution_mode, context_files_json,
                    session_summary, active_turn_id, resume_target_status,
                    last_error, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.session_id,
                    record.repo_path,
                    record.workspace_path,
                    record.mode,
                    record.status,
                    record.active_worker,
                    record.current_codex_thread_id,
                    int(record.stop_requested),
                    int(record.pause_requested),
                    record.operator_goal,
                    record.operator_notes,
                    record.chatgpt_model,
                    record.chatgpt_reasoning_effort,
                    record.codex_model,
                    record.codex_reasoning_effort,
                    record.codex_execution_mode,
                    json.dumps(record.context_files),
                    record.session_summary,
                    record.active_turn_id,
                    record.resume_target_status,
                    record.last_error,
                    record.created_at,
                    record.updated_at,
                ),
            )
            connection.commit()
        self.append_event(record.session_id, "session.created", {"mode": record.mode})
        return record

    def get_session(self, session_id: str) -> SessionRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown session_id: {session_id}")
        return self._session_from_row(row)

    def list_sessions(self) -> list[SessionRecord]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM sessions ORDER BY created_at").fetchall()
        return [self._session_from_row(row) for row in rows]

    def update_session(self, session_id: str, **fields: Any) -> SessionRecord:
        if not fields:
            return self.get_session(session_id)
        normalized: dict[str, Any] = {}
        for key, value in fields.items():
            if key == "context_files":
                normalized["context_files_json"] = json.dumps([str(item) for item in (value or [])])
                continue
            if key == "codex_execution_mode":
                normalized[key] = _normalize_codex_execution_mode(value)
                continue
            normalized[key] = value
        normalized["updated_at"] = now_iso()
        assignments = ", ".join(f"{key} = ?" for key in normalized)
        values = [self._normalize_value(value) for value in normalized.values()]
        values.append(session_id)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE sessions SET {assignments} WHERE session_id = ?",
                values,
            )
            connection.commit()
        return self.get_session(session_id)

    def queue_turn(
        self,
        session_id: str,
        *,
        worker: str,
        payload: dict[str, Any],
        idempotency_key: str,
        input_hash: str | None = None,
        turn_id: str | None = None,
    ) -> TurnRecord:
        now = now_iso()
        payload_json = _json_dumps(payload)
        input_hash = input_hash or sha1(payload_json.encode("utf-8")).hexdigest()
        turn_id = turn_id or _generated_id("turn")
        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT * FROM turns
                WHERE session_id = ? AND idempotency_key = ?
                """,
                (session_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                return self._turn_from_row(existing)

            active_count = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM turns
                WHERE session_id = ? AND status IN ('queued', 'running')
                """,
                (session_id,),
            ).fetchone()["count"]
            if active_count:
                raise RuntimeError("session already has a queued or running turn")

            sequence = (
                connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence FROM turns WHERE session_id = ?",
                    (session_id,),
                ).fetchone()["next_sequence"]
            )
            connection.execute(
                """
                INSERT INTO turns (
                    turn_id, session_id, sequence, worker, status, input_hash, started_at,
                    finished_at, artifact_path, idempotency_key, payload_json, result_json,
                    error_text, committed_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, 'queued', ?, '', '', '', ?, ?, '{}', '', '', ?, ?)
                """,
                (
                    turn_id,
                    session_id,
                    sequence,
                    worker,
                    input_hash,
                    idempotency_key,
                    payload_json,
                    now,
                    now,
                ),
            )
            connection.commit()
        self.append_event(session_id, "turn.queued", {"worker": worker, "turn_id": turn_id}, turn_id=turn_id)
        return self.get_turn(turn_id)

    def claim_queued_turn(
        self,
        session_id: str,
        *,
        worker_pid: int,
        artifact_path: Path,
        lease_ttl_seconds: float = 30.0,
    ) -> tuple[TurnRecord, WorkerLease] | None:
        now = now_iso()
        expires_at = _future_iso(lease_ttl_seconds)
        with self._connect() as connection:
            connection.isolation_level = None
            connection.execute("BEGIN IMMEDIATE")
            session_row = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if session_row is None:
                connection.execute("ROLLBACK")
                raise KeyError(f"unknown session_id: {session_id}")
            if str(session_row["active_worker"] or "").strip():
                connection.execute("COMMIT")
                return None
            row = connection.execute(
                """
                SELECT * FROM turns
                WHERE session_id = ? AND status = 'queued'
                ORDER BY sequence
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if row is None:
                connection.execute("COMMIT")
                return None
            lease_name = _lease_name(session_id, "active_worker")
            connection.execute(
                """
                UPDATE turns
                SET status = 'running', started_at = ?, artifact_path = ?, updated_at = ?
                WHERE turn_id = ?
                """,
                (now, str(artifact_path), now, row["turn_id"]),
            )
            connection.execute(
                """
                UPDATE sessions
                SET active_worker = ?, active_turn_id = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (row["worker"], row["turn_id"], now, session_id),
            )
            connection.execute(
                """
                INSERT OR REPLACE INTO worker_leases (
                    lease_name, session_id, turn_id, worker, owner_pid, heartbeat_at, expires_at, artifact_path
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lease_name,
                    session_id,
                    row["turn_id"],
                    row["worker"],
                    worker_pid,
                    now,
                    expires_at,
                    str(artifact_path),
                ),
            )
            connection.execute("COMMIT")
        turn = self.get_turn(str(row["turn_id"]))
        lease = self.get_worker_lease(session_id)
        if lease is None:
            raise RuntimeError("failed to create worker lease")
        self.append_event(session_id, "turn.running", {"worker": turn.worker, "turn_id": turn.turn_id}, turn_id=turn.turn_id)
        return turn, lease

    def update_worker_lease(
        self,
        session_id: str,
        *,
        owner_pid: int | None = None,
        artifact_path: Path | None = None,
        lease_ttl_seconds: float = 30.0,
    ) -> None:
        lease = self.get_worker_lease(session_id)
        if lease is None:
            return
        updates: dict[str, Any] = {
            "heartbeat_at": now_iso(),
            "expires_at": _future_iso(lease_ttl_seconds),
        }
        if owner_pid is not None:
            updates["owner_pid"] = owner_pid
        if artifact_path is not None:
            updates["artifact_path"] = str(artifact_path)
        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = [self._normalize_value(value) for value in updates.values()]
        values.append(lease.lease_name)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE worker_leases SET {assignments} WHERE lease_name = ?",
                values,
            )
            connection.commit()

    def clear_worker_lease(self, session_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM worker_leases WHERE lease_name = ?",
                (_lease_name(session_id, "active_worker"),),
            )
            connection.commit()

    def get_worker_lease(self, session_id: str) -> WorkerLease | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM worker_leases WHERE lease_name = ?",
                (_lease_name(session_id, "active_worker"),),
            ).fetchone()
        if row is None:
            return None
        return self._lease_from_row(row)

    def get_kernel_lease(self, session_id: str) -> WorkerLease | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM worker_leases WHERE lease_name = ?",
                (_lease_name(session_id, "kernel"),),
            ).fetchone()
        if row is None:
            return None
        return self._lease_from_row(row)

    def acquire_kernel_lease(
        self,
        session_id: str,
        *,
        owner_pid: int,
        lease_ttl_seconds: float = 10.0,
    ) -> WorkerLease | None:
        now = now_iso()
        expires_at = _future_iso(lease_ttl_seconds)
        lease_name = _lease_name(session_id, "kernel")
        with self._connect() as connection:
            connection.isolation_level = None
            connection.execute("BEGIN IMMEDIATE")
            session_row = connection.execute(
                "SELECT session_id FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if session_row is None:
                connection.execute("ROLLBACK")
                raise KeyError(f"unknown session_id: {session_id}")
            existing = connection.execute(
                "SELECT * FROM worker_leases WHERE lease_name = ?",
                (lease_name,),
            ).fetchone()
            if existing is not None and not _is_past_due(str(existing["expires_at"])):
                connection.execute("COMMIT")
                return None
            connection.execute(
                """
                INSERT OR REPLACE INTO worker_leases (
                    lease_name, session_id, turn_id, worker, owner_pid, heartbeat_at, expires_at, artifact_path
                )
                VALUES (?, ?, '', 'kernel', ?, ?, ?, '')
                """,
                (lease_name, session_id, owner_pid, now, expires_at),
            )
            connection.execute("COMMIT")
        return self.get_kernel_lease(session_id)

    def refresh_kernel_lease(self, session_id: str, *, owner_pid: int, lease_ttl_seconds: float = 10.0) -> None:
        lease_name = _lease_name(session_id, "kernel")
        now = now_iso()
        expires_at = _future_iso(lease_ttl_seconds)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE worker_leases
                SET heartbeat_at = ?, expires_at = ?, owner_pid = ?
                WHERE lease_name = ? AND owner_pid = ?
                """,
                (now, expires_at, owner_pid, lease_name, owner_pid),
            )
            connection.commit()

    def release_kernel_lease(self, session_id: str, *, owner_pid: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM worker_leases WHERE lease_name = ? AND owner_pid = ?",
                (_lease_name(session_id, "kernel"), owner_pid),
            )
            connection.commit()

    def get_turn(self, turn_id: str) -> TurnRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM turns WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown turn_id: {turn_id}")
        return self._turn_from_row(row)

    def list_turns(self, session_id: str) -> list[TurnRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM turns WHERE session_id = ? ORDER BY sequence",
                (session_id,),
            ).fetchall()
        return [self._turn_from_row(row) for row in rows]

    def get_active_turn(self, session_id: str) -> TurnRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM turns
                WHERE session_id = ? AND status = 'running'
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        return self._turn_from_row(row) if row is not None else None

    def get_pending_turn(self, session_id: str) -> TurnRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM turns
                WHERE session_id = ? AND status = 'queued'
                ORDER BY sequence
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        return self._turn_from_row(row) if row is not None else None

    def get_last_committed_turn(self, session_id: str, *, worker: str | None = None) -> TurnRecord | None:
        query = """
            SELECT * FROM turns
            WHERE session_id = ? AND status = 'committed'
        """
        params: list[Any] = [session_id]
        if worker:
            query += " AND worker = ?"
            params.append(worker)
        query += " ORDER BY sequence DESC LIMIT 1"
        with self._connect() as connection:
            row = connection.execute(query, params).fetchone()
        return self._turn_from_row(row) if row is not None else None

    def get_last_terminal_turn(self, session_id: str) -> TurnRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM turns
                WHERE session_id = ? AND status IN ('failed', 'failed_validation', 'aborted')
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        return self._turn_from_row(row) if row is not None else None

    def mark_turn_completed(self, turn_id: str, *, result: dict[str, Any], artifact_path: Path) -> TurnRecord:
        turn = self.get_turn(turn_id)
        if turn.status != "running":
            raise RuntimeError(f"turn must be running before completion, got {turn.status}")
        now = now_iso()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE turns
                SET status = 'completed', finished_at = ?, artifact_path = ?, result_json = ?, updated_at = ?
                WHERE turn_id = ?
                """,
                (now, str(artifact_path), _json_dumps(result), now, turn_id),
            )
            connection.commit()
        return self.get_turn(turn_id)

    def commit_turn(self, turn_id: str) -> TurnRecord:
        turn = self.get_turn(turn_id)
        if turn.status != "completed":
            raise RuntimeError(f"turn must be completed before commit, got {turn.status}")
        if turn.committed_at:
            raise RuntimeError("turn has already been committed")
        now = now_iso()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE turns
                SET status = 'committed', committed_at = ?, updated_at = ?
                WHERE turn_id = ?
                """,
                (now, now, turn_id),
            )
            connection.execute(
                """
                UPDATE sessions
                SET active_worker = '', active_turn_id = '', updated_at = ?
                WHERE session_id = ?
                """,
                (now, turn.session_id),
            )
            connection.execute(
                "DELETE FROM worker_leases WHERE lease_name = ?",
                (_lease_name(turn.session_id, "active_worker"),),
            )
            connection.commit()
        self.append_event(turn.session_id, "turn.committed", {"worker": turn.worker, "turn_id": turn_id}, turn_id=turn_id)
        return self.get_turn(turn_id)

    def mark_turn_terminal(
        self,
        turn_id: str,
        *,
        status: str,
        error_text: str = "",
        result: dict[str, Any] | None = None,
        ) -> TurnRecord:
        if status not in {"failed", "failed_validation", "aborted"}:
            raise ValueError(f"unsupported terminal status: {status}")
        turn = self.get_turn(turn_id)
        if turn.status not in {"queued", "running", "completed"}:
            raise RuntimeError(f"cannot mark turn terminal from {turn.status}")
        now = now_iso()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE turns
                SET status = ?, finished_at = ?, result_json = ?, error_text = ?, updated_at = ?
                WHERE turn_id = ?
                """,
                (status, now, _json_dumps(result or {}), error_text, now, turn_id),
            )
            connection.execute(
                """
                UPDATE sessions
                SET active_worker = '', active_turn_id = '', last_error = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (error_text, now, turn.session_id),
            )
            connection.execute(
                "DELETE FROM worker_leases WHERE lease_name = ?",
                (_lease_name(turn.session_id, "active_worker"),),
            )
            connection.commit()
        self.append_event(turn.session_id, f"turn.{status}", {"turn_id": turn_id, "error": error_text}, turn_id=turn_id)
        return self.get_turn(turn_id)

    def append_event(self, session_id: str, event_type: str, payload: dict[str, Any], *, turn_id: str = "") -> EventRecord:
        created_at = now_iso()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO events (session_id, turn_id, event_type, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, turn_id, event_type, _json_dumps(payload), created_at),
            )
            connection.commit()
            event_id = int(cursor.lastrowid)
        return EventRecord(
            event_id=event_id,
            session_id=session_id,
            turn_id=turn_id,
            event_type=event_type,
            payload=payload,
            created_at=created_at,
        )

    def list_recent_events(self, session_id: str, *, limit: int = 20) -> list[EventRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM events
                WHERE session_id = ?
                ORDER BY event_id DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def record_artifact(self, session_id: str, turn_id: str, *, kind: str, path: Path) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO artifacts (session_id, turn_id, kind, path, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, turn_id, kind, str(path), now_iso()),
            )
            connection.commit()

    def list_recent_artifacts(self, session_id: str, *, limit: int = 10) -> list[dict[str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT kind, path
                FROM artifacts
                WHERE session_id = ?
                ORDER BY artifact_id DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [{"kind": str(row["kind"]), "path": str(row["path"])} for row in rows]

    def register_codex_thread(self, session_id: str, *, thread_id: str, thread_mode: str) -> None:
        now = now_iso()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO codex_threads (session_id, thread_id, thread_mode, created_at, last_used_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    thread_id = excluded.thread_id,
                    thread_mode = excluded.thread_mode,
                    last_used_at = excluded.last_used_at
                """,
                (session_id, thread_id, thread_mode, now, now),
            )
            connection.execute(
                """
                UPDATE sessions
                SET current_codex_thread_id = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (thread_id, now, session_id),
            )
            connection.commit()

    def counts(self, session_id: str) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM turns
                WHERE session_id = ?
                GROUP BY status
                """,
                (session_id,),
            ).fetchall()
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        return {
            "queued_count": counts.get("queued", 0),
            "running_count": counts.get("running", 0),
            "committed_count": counts.get("committed", 0),
            "failed_count": counts.get("failed", 0) + counts.get("failed_validation", 0) + counts.get("aborted", 0),
        }

    def _normalize_value(self, value: Any) -> Any:
        if isinstance(value, bool):
            return int(value)
        return value

    def _session_from_row(self, row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(
            session_id=str(row["session_id"]),
            repo_path=str(row["repo_path"]),
            workspace_path=str(row["workspace_path"]),
            mode=str(row["mode"]),
            status=str(row["status"]),
            active_worker=str(row["active_worker"]),
            current_codex_thread_id=str(row["current_codex_thread_id"]),
            stop_requested=bool(row["stop_requested"]),
            pause_requested=bool(row["pause_requested"]),
            operator_goal=str(row["operator_goal"]),
            operator_notes=str(row["operator_notes"]),
            chatgpt_model=str(row["chatgpt_model"]),
            chatgpt_reasoning_effort=str(row["chatgpt_reasoning_effort"]),
            codex_model=str(row["codex_model"]),
            codex_reasoning_effort=str(row["codex_reasoning_effort"]),
            codex_execution_mode=_normalize_codex_execution_mode(row["codex_execution_mode"]),
            context_files=_json_loads_list(row["context_files_json"]),
            session_summary=str(row["session_summary"]),
            active_turn_id=str(row["active_turn_id"]),
            resume_target_status=str(row["resume_target_status"]),
            last_error=str(row["last_error"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def _turn_from_row(self, row: sqlite3.Row) -> TurnRecord:
        return TurnRecord(
            turn_id=str(row["turn_id"]),
            session_id=str(row["session_id"]),
            sequence=int(row["sequence"]),
            worker=str(row["worker"]),
            status=str(row["status"]),
            input_hash=str(row["input_hash"]),
            started_at=str(row["started_at"]),
            finished_at=str(row["finished_at"]),
            artifact_path=str(row["artifact_path"]),
            idempotency_key=str(row["idempotency_key"]),
            payload=_json_loads(row["payload_json"]),
            result=_json_loads(row["result_json"]),
            error_text=str(row["error_text"]),
            committed_at=str(row["committed_at"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def _event_from_row(self, row: sqlite3.Row) -> EventRecord:
        return EventRecord(
            event_id=int(row["event_id"]),
            session_id=str(row["session_id"]),
            turn_id=str(row["turn_id"]),
            event_type=str(row["event_type"]),
            payload=_json_loads(row["payload_json"]),
            created_at=str(row["created_at"]),
        )

    def _lease_from_row(self, row: sqlite3.Row) -> WorkerLease:
        return WorkerLease(
            lease_name=str(row["lease_name"]),
            session_id=str(row["session_id"]),
            turn_id=str(row["turn_id"]),
            worker=str(row["worker"]),
            owner_pid=int(row["owner_pid"]),
            heartbeat_at=str(row["heartbeat_at"]),
            expires_at=str(row["expires_at"]),
            artifact_path=str(row["artifact_path"]),
        )


def _normalize_codex_execution_mode(value: Any) -> str:
    normalized = str(value or _DEFAULT_CODEX_EXECUTION_MODE).strip() or _DEFAULT_CODEX_EXECUTION_MODE
    if normalized not in _CODEX_EXECUTION_MODES:
        raise ValueError(f"invalid codex_execution_mode: {normalized}")
    return normalized
