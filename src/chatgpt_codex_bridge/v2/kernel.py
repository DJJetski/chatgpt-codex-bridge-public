from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ..models import now_iso
from .store import (
    V2Store,
    _DEFAULT_CHATGPT_MODEL,
    _DEFAULT_CHATGPT_REASONING_EFFORT,
    _DEFAULT_CODEX_EXECUTION_MODE,
    _DEFAULT_CODEX_MODEL,
    _DEFAULT_CODEX_REASONING_EFFORT,
)
from .types import SessionRecord, TurnRecord
from .workers import validate_chatgpt_result, validate_codex_result


class V2Kernel:
    def __init__(
        self,
        *,
        db_path: Path,
        artifacts_root: Path,
        codex_bin: str = "codex",
        poll_interval_seconds: float = 0.5,
        chatgpt_timeout_seconds: float = 120.0,
        codex_timeout_seconds: float = 1800.0,
        worker_lease_ttl_seconds: float = 30.0,
        kernel_lease_ttl_seconds: float = 10.0,
    ):
        self.db_path = Path(db_path)
        self.artifacts_root = Path(artifacts_root)
        self.artifacts_root.mkdir(parents=True, exist_ok=True)
        self.codex_bin = codex_bin
        self.poll_interval_seconds = poll_interval_seconds
        self.chatgpt_timeout_seconds = chatgpt_timeout_seconds
        self.codex_timeout_seconds = codex_timeout_seconds
        self.worker_lease_ttl_seconds = worker_lease_ttl_seconds
        self.kernel_lease_ttl_seconds = kernel_lease_ttl_seconds
        self.store = V2Store(self.db_path)

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
        session_id: str | None = None,
    ) -> SessionRecord:
        return self.store.create_session(
            repo_path=repo_path,
            workspace_path=workspace_path,
            operator_goal=operator_goal,
            operator_notes=operator_notes,
            chatgpt_model=chatgpt_model,
            chatgpt_reasoning_effort=chatgpt_reasoning_effort,
            codex_model=codex_model,
            codex_reasoning_effort=codex_reasoning_effort,
            codex_execution_mode=codex_execution_mode,
            context_files=context_files,
            session_id=session_id,
        )

    def configure_session(self, session_id: str, **fields: Any) -> SessionRecord:
        if not fields:
            return self.store.get_session(session_id)
        return self.store.update_session(session_id, **fields)

    def bootstrap_turn(
        self,
        session_id: str,
        *,
        worker: str,
        prompt: str = "",
        thread_mode: str = "resume_current",
    ) -> TurnRecord:
        session = self.store.get_session(session_id)
        if session.status not in {"manual_bootstrap", "paused", "blocked_human"}:
            raise RuntimeError(
                f"bootstrap is only allowed in manual_bootstrap/paused/blocked_human, got {session.status}"
            )
        if session.status == "blocked_human":
            self.store.update_session(
                session_id,
                status="manual_bootstrap",
                pause_requested=False,
                stop_requested=False,
                resume_target_status="manual_bootstrap",
                last_error="",
            )
        if worker == "chatgpt":
            return self._queue_chatgpt_turn(session_id, source="manual_bootstrap")
        if worker == "codex":
            return self._queue_codex_turn(session_id, prompt=prompt, thread_mode=thread_mode)
        raise ValueError(f"unsupported bootstrap worker: {worker}")

    def arm_session(self, session_id: str) -> SessionRecord:
        session = self.store.get_session(session_id)
        if session.status not in {"manual_bootstrap", "paused"}:
            raise RuntimeError(f"cannot arm session from {session.status}")
        return self.store.update_session(
            session_id,
            status="running",
            pause_requested=False,
            stop_requested=False,
            resume_target_status="running",
        )

    def pause_session(self, session_id: str) -> SessionRecord:
        session = self.store.get_session(session_id)
        target_status = session.status if session.status != "paused" else session.resume_target_status or "running"
        updated = self.store.update_session(
            session_id,
            pause_requested=True,
            resume_target_status=target_status,
            status="paused" if not session.active_worker else session.status,
        )
        self.store.append_event(session_id, "session.pause_requested", {"active_worker": bool(session.active_worker)})
        return updated

    def resume_session(self, session_id: str) -> SessionRecord:
        session = self.store.get_session(session_id)
        if session.stop_requested:
            raise RuntimeError("cannot resume a stop-requested session")
        target_status = session.resume_target_status or "running"
        updated = self.store.update_session(
            session_id,
            pause_requested=False,
            status=target_status if session.status in {"paused", "blocked_human"} else session.status,
        )
        self.store.append_event(session_id, "session.resumed", {"status": updated.status})
        return updated

    def stop_session(self, session_id: str) -> SessionRecord:
        session = self.store.get_session(session_id)
        status = "stopped" if not session.active_worker else session.status
        updated = self.store.update_session(session_id, stop_requested=True, status=status)
        self.store.append_event(session_id, "session.stop_requested", {"active_worker": bool(session.active_worker)})
        return updated

    def abort_turn(self, session_id: str) -> dict[str, Any]:
        lease = self.store.get_worker_lease(session_id)
        if lease is None:
            return {"session_id": session_id, "aborted": False, "reason": "no active worker"}
        self._terminate_process_group(lease.owner_pid)
        turn = self.store.mark_turn_terminal(
            lease.turn_id,
            status="aborted",
            error_text="Turn aborted by operator request.",
        )
        self.store.update_session(session_id, status="blocked_human")
        return {"session_id": session_id, "aborted": True, "turn_id": turn.turn_id}

    def status_snapshot(self, session_id: str) -> dict[str, Any]:
        session = self.store.get_session(session_id)
        active_turn = self.store.get_active_turn(session_id)
        pending_turn = self.store.get_pending_turn(session_id)
        last_committed_turn = self.store.get_last_committed_turn(session_id)
        last_terminal_turn = self.store.get_last_terminal_turn(session_id)
        return {
            "session": session.as_dict(),
            "turns": self.store.counts(session_id),
            "active_turn": self._turn_payload(active_turn),
            "pending_turn": self._turn_payload(pending_turn),
            "last_committed_turn": self._turn_payload(last_committed_turn),
            "last_terminal_turn": self._turn_payload(last_terminal_turn),
            "recent_events": [event.as_dict() for event in self.store.list_recent_events(session_id)],
            "monitor": self._monitor_snapshot(
                session,
                active_turn=active_turn,
                pending_turn=pending_turn,
                last_committed_turn=last_committed_turn,
                last_terminal_turn=last_terminal_turn,
            ),
            "next_action": self._next_action(session),
        }

    def render_summary(self, session_id: str) -> str:
        snapshot = self.status_snapshot(session_id)
        session = snapshot["session"]
        monitor = snapshot["monitor"]
        active_turn = snapshot["active_turn"]
        pending_turn = snapshot["pending_turn"]
        last_committed_turn = snapshot["last_committed_turn"]
        lines = [
            f"Session: {session['session_id']} [{session['status']}]",
            f"Goal: {session['operator_goal']}",
            f"ChatGPT model: {session['chatgpt_model']} (reasoning={session['chatgpt_reasoning_effort']})",
            (
                "Codex model: "
                f"{session['codex_model']} "
                f"(reasoning={session['codex_reasoning_effort']}, execution={session['codex_execution_mode']})"
            ),
            (
                "Context files: "
                + ", ".join(session["context_files"])
                if session["context_files"]
                else "Context files: none"
            ),
            f"Next action: {snapshot['next_action']}",
        ]
        if active_turn is not None:
            lines.append(
                f"Active turn: {active_turn['worker']}#{active_turn['sequence']} [{active_turn['status']}]"
            )
        elif pending_turn is not None:
            lines.append(
                f"Pending turn: {pending_turn['worker']}#{pending_turn['sequence']} [{pending_turn['status']}]"
            )
        else:
            lines.append("Active turn: none")
        if monitor["active_prompt_preview"]:
            lines.append(f"Prompt preview: {monitor['active_prompt_preview']}")
        if last_committed_turn is not None:
            lines.append(
                f"Last turn: {last_committed_turn['worker']} -> "
                f"{monitor['last_summary'] or '<no summary>'}"
            )
        if monitor["trace_tail"]:
            lines.append(f"Trace tail: {monitor['trace_tail']}")
        return "\n".join(lines)

    def watch(self, session_id: str, *, poll_interval_seconds: float = 1.0, output_format: str = "json") -> None:
        previous = ""
        try:
            while True:
                if output_format == "summary":
                    snapshot = self.render_summary(session_id)
                else:
                    snapshot = json.dumps(self.status_snapshot(session_id), indent=2, sort_keys=True)
                if snapshot != previous:
                    print(snapshot)
                    previous = snapshot
                time.sleep(poll_interval_seconds)
        except KeyboardInterrupt:
            return

    def start(self, session_id: str, *, max_turns: int | None = None, poll_interval_seconds: float | None = None) -> dict[str, Any]:
        owner_pid = os.getpid()
        acquired = self.store.acquire_kernel_lease(
            session_id,
            owner_pid=owner_pid,
            lease_ttl_seconds=self.kernel_lease_ttl_seconds,
        )
        if acquired is None:
            self.store.append_event(session_id, "kernel.start_skipped", {"reason": "kernel_lease_active"})
            return self.status_snapshot(session_id)
        turns_executed = 0
        interval = poll_interval_seconds or self.poll_interval_seconds
        try:
            while True:
                self.store.refresh_kernel_lease(
                    session_id,
                    owner_pid=owner_pid,
                    lease_ttl_seconds=self.kernel_lease_ttl_seconds,
                )
                reconciled = self.reconcile_session(session_id)
                session = self.store.get_session(session_id)
                if session.stop_requested and not session.active_worker:
                    self.store.update_session(session_id, status="stopped")
                    break
                if session.pause_requested and not session.active_worker:
                    self.store.update_session(session_id, status="paused")
                    break
                if session.status in {"blocked_human", "stopped", "completed"} and not session.active_worker:
                    break
                if max_turns is not None and turns_executed >= max_turns:
                    break

                lease = self.store.get_worker_lease(session_id)
                if lease is not None:
                    if not self._worker_lease_expired(lease) and self._pid_exists(lease.owner_pid):
                        time.sleep(interval)
                        continue
                    if not reconciled:
                        reconciled = self.reconcile_session(session_id)
                    session = self.store.get_session(session_id)
                    if session.status in {"blocked_human", "stopped", "completed"} and not session.active_worker:
                        break

                pending = self.store.get_pending_turn(session_id)
                if pending is None:
                    if session.status == "running":
                        try:
                            queued = self._maybe_queue_follow_up_turn(session_id)
                        except RuntimeError as exc:
                            self.store.append_event(
                                session_id,
                                "kernel.queue_follow_up_failed",
                                {"error": str(exc)},
                            )
                            self.store.update_session(
                                session_id,
                                status="blocked_human",
                                last_error=str(exc),
                            )
                            break
                        if queued is None:
                            break
                        pending = queued
                    else:
                        break

                if pending is None:
                    break
                self._run_turn(pending)
                turns_executed += 1
            return self.status_snapshot(session_id)
        finally:
            self.store.release_kernel_lease(session_id, owner_pid=owner_pid)

    def reconcile_session(self, session_id: str) -> bool:
        lease = self.store.get_worker_lease(session_id)
        if lease is None:
            return False
        if not self._worker_lease_expired(lease) and self._pid_exists(lease.owner_pid):
            return False
        turn = self.store.get_turn(lease.turn_id)
        if turn.status != "running":
            self.store.clear_worker_lease(session_id)
            return True
        artifact_path = Path(turn.artifact_path or lease.artifact_path)
        if artifact_path.exists():
            self._finalize_turn(turn, artifact_path)
            return True
        self.store.mark_turn_terminal(
            turn.turn_id,
            status="aborted",
            error_text="Worker stopped without a committed result artifact.",
        )
        self.store.update_session(session_id, status="blocked_human")
        return True

    def _queue_chatgpt_turn(self, session_id: str, *, source: str) -> TurnRecord:
        session = self.store.get_session(session_id)
        last_committed_turn = self.store.get_last_committed_turn(session_id)
        last_codex_turn = self.store.get_last_committed_turn(session_id, worker="codex")
        payload = {
            "session_id": session.session_id,
            "repo_path": session.repo_path,
            "workspace_path": session.workspace_path,
            "operator_goal": session.operator_goal,
            "session_summary": session.session_summary,
            "last_committed_codex_turn": last_codex_turn.result if last_codex_turn else {},
            "relevant_artifacts_manifest": self.store.list_recent_artifacts(session_id),
            "operator_notes": session.operator_notes,
            "chatgpt_model": session.chatgpt_model,
            "chatgpt_reasoning_effort": session.chatgpt_reasoning_effort,
            "context_files": self._load_context_files(session),
            "source": source,
        }
        predecessor_turn_id = last_codex_turn.turn_id if last_codex_turn else (last_committed_turn.turn_id if last_committed_turn else "initial")
        idempotency_key = f"chatgpt:{source}:{predecessor_turn_id}"
        return self.store.queue_turn(
            session_id,
            worker="chatgpt",
            payload=payload,
            idempotency_key=idempotency_key,
        )

    def _queue_codex_turn(self, session_id: str, *, prompt: str = "", thread_mode: str = "resume_current") -> TurnRecord:
        session = self.store.get_session(session_id)
        last_chatgpt_turn = self.store.get_last_committed_turn(session_id, worker="chatgpt")
        if not prompt:
            if last_chatgpt_turn is None:
                raise RuntimeError("no committed ChatGPT turn exists to source the Codex prompt")
            last_result = last_chatgpt_turn.result
            if last_result.get("decision") != "run_codex":
                raise RuntimeError("last committed ChatGPT turn did not request a Codex run")
            prompt = str(last_result.get("codex_prompt", "")).strip()
            thread_mode = str(last_result.get("codex_thread_mode", thread_mode)).strip() or thread_mode
        if thread_mode == "resume_current" and not session.current_codex_thread_id.strip():
            raise RuntimeError("resume_current requires an existing current_codex_thread_id")
        payload = {
            "session_id": session.session_id,
            "workspace_path": session.workspace_path,
            "thread_mode": thread_mode,
            "current_codex_thread_id": session.current_codex_thread_id,
            "codex_prompt": prompt,
            "codex_model": session.codex_model,
            "codex_reasoning_effort": session.codex_reasoning_effort,
            "codex_execution_mode": session.codex_execution_mode,
        }
        source_turn_id = last_chatgpt_turn.turn_id if last_chatgpt_turn else "manual"
        idempotency_key = f"codex:{source_turn_id}:{sha1_text(prompt)}:{thread_mode}"
        return self.store.queue_turn(
            session_id,
            worker="codex",
            payload=payload,
            idempotency_key=idempotency_key,
        )

    def _maybe_queue_follow_up_turn(self, session_id: str) -> TurnRecord | None:
        session = self.store.get_session(session_id)
        if session.status != "running" or session.pause_requested or session.stop_requested:
            return None
        last_turn = self.store.get_last_committed_turn(session_id)
        if last_turn is None:
            return self._queue_chatgpt_turn(session_id, source="auto_initial")
        if last_turn.worker == "chatgpt":
            decision = str(last_turn.result.get("decision", "")).strip()
            if decision == "run_codex":
                return self._queue_codex_turn(session_id)
            if decision == "pause":
                self.store.update_session(session_id, status="paused")
                return None
            if decision == "stop":
                self.store.update_session(session_id, status="stopped", stop_requested=True)
                return None
            if decision == "require_human":
                self.store.update_session(session_id, status="blocked_human")
                return None
            self.store.update_session(session_id, status="blocked_human", last_error="unknown ChatGPT decision")
            return None
        if last_turn.worker == "codex":
            return self._queue_chatgpt_turn(session_id, source=f"after_codex:{last_turn.turn_id}")
        return None

    def _run_turn(self, turn: TurnRecord) -> None:
        artifact_dir = self.artifacts_root / turn.session_id / f"{turn.sequence:04d}-{turn.worker}"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        worker_input_path = artifact_dir / "worker_input.json"
        worker_output_path = artifact_dir / "worker_result.json"
        worker_input_path.write_text(json.dumps(turn.payload, indent=2), encoding="utf-8")

        claimed = self.store.claim_queued_turn(
            turn.session_id,
            worker_pid=0,
            artifact_path=worker_output_path,
            lease_ttl_seconds=self.worker_lease_ttl_seconds,
        )
        if claimed is None:
            return
        running_turn, _lease = claimed
        self.store.record_artifact(turn.session_id, running_turn.turn_id, kind="worker_input", path=worker_input_path)
        command = [
            sys.executable,
            "-m",
            "mastermind_bridge.cli",
            "v2",
            "internal",
            f"run-{running_turn.worker}-turn",
            "--input",
            str(worker_input_path),
            "--output",
            str(worker_output_path),
        ]
        env = os.environ.copy()
        env.setdefault("BRIDGE_V2_CODEX_BIN", self.codex_bin)
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env=env,
        )
        timeout_seconds = self._worker_timeout_seconds(running_turn.worker)
        deadline = time.monotonic() + timeout_seconds if timeout_seconds > 0 else None
        self.store.update_worker_lease(
            turn.session_id,
            owner_pid=process.pid,
            artifact_path=worker_output_path,
            lease_ttl_seconds=self.worker_lease_ttl_seconds,
        )
        self.store.append_event(turn.session_id, "worker.spawned", {"pid": process.pid, "worker": running_turn.worker}, turn_id=running_turn.turn_id)

        timed_out = False
        while process.poll() is None:
            self.store.update_worker_lease(
                turn.session_id,
                owner_pid=process.pid,
                artifact_path=worker_output_path,
                lease_ttl_seconds=self.worker_lease_ttl_seconds,
            )
            self.store.refresh_kernel_lease(
                turn.session_id,
                owner_pid=os.getpid(),
                lease_ttl_seconds=self.kernel_lease_ttl_seconds,
            )
            if deadline is not None and time.monotonic() >= deadline:
                timed_out = True
                self._terminate_process_group(process.pid)
                break
            time.sleep(self.poll_interval_seconds)
        stdout, stderr = process.communicate()
        current_turn = self.store.get_turn(running_turn.turn_id)
        if current_turn.status == "aborted":
            self.store.record_artifact(turn.session_id, current_turn.turn_id, kind="worker_stdout", path=self._write_text(artifact_dir / "worker.stdout.log", stdout))
            self.store.record_artifact(turn.session_id, current_turn.turn_id, kind="worker_stderr", path=self._write_text(artifact_dir / "worker.stderr.log", stderr))
            return
        if stdout:
            self.store.record_artifact(turn.session_id, current_turn.turn_id, kind="worker_stdout", path=self._write_text(artifact_dir / "worker.stdout.log", stdout))
        if stderr:
            self.store.record_artifact(turn.session_id, current_turn.turn_id, kind="worker_stderr", path=self._write_text(artifact_dir / "worker.stderr.log", stderr))
        if timed_out:
            self.store.mark_turn_terminal(
                current_turn.turn_id,
                status="failed",
                error_text=f"{current_turn.worker} worker timed out after {timeout_seconds} seconds",
            )
            self.store.update_session(turn.session_id, status="blocked_human")
            return
        if process.returncode != 0 and not worker_output_path.exists():
            current_turn = self._wait_for_external_terminal_state(current_turn.turn_id)
            if current_turn.status == "aborted":
                return
            self.store.mark_turn_terminal(
                current_turn.turn_id,
                status="failed",
                error_text=stderr.strip() or f"{current_turn.worker} worker exited with code {process.returncode}",
            )
            self.store.update_session(turn.session_id, status="blocked_human")
            return
        self._finalize_turn(current_turn, worker_output_path)

    def _finalize_turn(self, turn: TurnRecord, artifact_path: Path) -> None:
        try:
            payload = json.loads(artifact_path.read_text(encoding="utf-8"))
            if turn.worker == "chatgpt":
                validated = validate_chatgpt_result(payload).as_dict()
            elif turn.worker == "codex":
                validated = validate_codex_result(
                    payload,
                    expected_thread_mode=str(turn.payload.get("thread_mode", "")),
                    current_thread_id=str(turn.payload.get("current_codex_thread_id", "")),
                ).as_dict()
            else:
                raise RuntimeError(f"unknown worker type: {turn.worker}")
        except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
            self.store.mark_turn_terminal(
                turn.turn_id,
                status="failed_validation",
                error_text=str(exc),
            )
            self.store.update_session(turn.session_id, status="blocked_human")
            return
        self.store.mark_turn_completed(turn.turn_id, result=validated, artifact_path=artifact_path)
        self.store.record_artifact(turn.session_id, turn.turn_id, kind="worker_result", path=artifact_path)
        committed = self.store.commit_turn(turn.turn_id)
        if committed.worker == "chatgpt":
            self._apply_chatgpt_side_effects(committed)
        if committed.worker == "codex":
            self._apply_codex_side_effects(committed)

    def _apply_chatgpt_side_effects(self, turn: TurnRecord) -> None:
        summary = str(turn.result.get("summary", "")).strip()
        session = self.store.get_session(turn.session_id)
        updates: dict[str, Any] = {"session_summary": summary, "last_error": ""}
        decision = str(turn.result.get("decision", "")).strip()
        if decision == "pause":
            updates["status"] = "paused"
        elif decision == "stop":
            updates["status"] = "completed"
        elif decision == "require_human":
            updates["status"] = "blocked_human"
            updates["last_error"] = str(turn.result.get("needs_human_reason", "")).strip()
        elif session.pause_requested:
            updates["status"] = "paused"
        self.store.update_session(turn.session_id, **updates)

    def _apply_codex_side_effects(self, turn: TurnRecord) -> None:
        summary = str(turn.result.get("summary", "")).strip()
        observed_thread_id = str(turn.result.get("observed_thread_id", "")).strip()
        updates: dict[str, Any] = {"session_summary": summary, "last_error": ""}
        if observed_thread_id:
            self.store.register_codex_thread(
                turn.session_id,
                thread_id=observed_thread_id,
                thread_mode=str(turn.payload.get("thread_mode", "")),
            )
        if self.store.get_session(turn.session_id).pause_requested:
            updates["status"] = "paused"
        self.store.update_session(turn.session_id, **updates)

    def _turn_payload(self, turn: TurnRecord | None) -> dict[str, Any] | None:
        return turn.as_dict() if turn is not None else None

    def _load_context_files(self, session: SessionRecord) -> list[dict[str, str]]:
        context_payloads: list[dict[str, str]] = []
        for raw_path in session.context_files:
            path = Path(raw_path)
            if not path.exists():
                raise RuntimeError(f"context file is missing: {path}")
            context_payloads.append(
                {
                    "path": str(path),
                    "content": path.read_text(encoding="utf-8"),
                }
            )
        return context_payloads

    def _monitor_snapshot(
        self,
        session: SessionRecord,
        *,
        active_turn: TurnRecord | None,
        pending_turn: TurnRecord | None,
        last_committed_turn: TurnRecord | None,
        last_terminal_turn: TurnRecord | None,
    ) -> dict[str, Any]:
        tracked_turn = active_turn or pending_turn or last_committed_turn or last_terminal_turn
        return {
            "active_prompt_preview": self._active_prompt_preview(tracked_turn),
            "last_summary": self._last_turn_summary(last_committed_turn),
            "last_reasoning": self._last_turn_reasoning(last_committed_turn),
            "last_output_excerpt": self._last_turn_output_excerpt(last_committed_turn),
            "trace_tail": self._trace_tail(active_turn),
        }

    def _active_prompt_preview(self, turn: TurnRecord | None) -> str:
        if turn is None:
            return ""
        payload = turn.payload
        if turn.worker == "codex":
            return _preview_text(str(payload.get("codex_prompt", "")))
        if turn.worker == "chatgpt":
            return _preview_text(str(payload.get("operator_goal", "")) or str(payload.get("session_summary", "")))
        return ""

    def _last_turn_summary(self, turn: TurnRecord | None) -> str:
        if turn is None:
            return ""
        return _preview_text(str(turn.result.get("summary", "")), max_length=160)

    def _last_turn_reasoning(self, turn: TurnRecord | None) -> str:
        if turn is None or turn.worker != "chatgpt":
            return ""
        return _preview_text(str(turn.result.get("reasoning", "")), max_length=240)

    def _last_turn_output_excerpt(self, turn: TurnRecord | None) -> str:
        if turn is None or turn.worker != "codex":
            return ""
        return _preview_text(str(turn.result.get("final_output", "")), max_length=240)

    def _trace_tail(self, turn: TurnRecord | None) -> str:
        if turn is None or not turn.artifact_path:
            return ""
        artifact_dir = Path(turn.artifact_path).parent
        candidates = list(artifact_dir.rglob("last_message.md")) + list(artifact_dir.rglob("stdout.jsonl"))
        for candidate in candidates:
            if not candidate.exists():
                continue
            try:
                text = candidate.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if text:
                return _preview_text(text.splitlines()[-1], max_length=240)
        return ""

    def _terminate_process_group(self, pid: int) -> None:
        if pid <= 0:
            return
        try:
            os.killpg(pid, signal.SIGTERM)
        except (PermissionError, ProcessLookupError):
            return
        deadline = time.time() + 2
        while time.time() < deadline:
            if not self._pid_exists(pid):
                return
            time.sleep(0.05)
        try:
            os.killpg(pid, signal.SIGKILL)
        except (PermissionError, ProcessLookupError):
            return

    def _pid_exists(self, pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def _write_text(self, path: Path, content: str) -> Path:
        path.write_text(content, encoding="utf-8")
        return path

    def _next_action(self, session: SessionRecord) -> str:
        if session.active_worker:
            return f"wait_for_{session.active_worker}"
        if session.stop_requested:
            return "session_stopped"
        if session.pause_requested or session.status == "paused":
            return "await_resume"
        if session.status == "blocked_human":
            return "await_human"
        if session.status == "manual_bootstrap":
            return "manual_bootstrap"
        if session.status == "completed":
            return "session_completed"
        if session.status == "running":
            pending = self.store.get_pending_turn(session.session_id)
            if pending is not None:
                return f"run_{pending.worker}"
            return "queue_follow_up"
        return session.status

    def _worker_lease_expired(self, lease) -> bool:
        if not lease.expires_at:
            return True
        return datetime.now().astimezone() >= datetime.fromisoformat(lease.expires_at)

    def _worker_timeout_seconds(self, worker: str) -> float:
        if worker == "chatgpt":
            return self.chatgpt_timeout_seconds
        if worker == "codex":
            return self.codex_timeout_seconds
        return 0.0

    def _wait_for_external_terminal_state(self, turn_id: str, *, timeout_seconds: float = 0.5) -> TurnRecord:
        deadline = time.time() + timeout_seconds
        current_turn = self.store.get_turn(turn_id)
        while time.time() < deadline and current_turn.status == "running":
            time.sleep(0.05)
            current_turn = self.store.get_turn(turn_id)
        return current_turn


def sha1_text(value: str) -> str:
    import hashlib

    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def _preview_text(value: str, *, max_length: int = 120) -> str:
    normalized = " ".join(str(value or "").split())
    if len(normalized) <= max_length:
        return normalized
    return normalized[: max_length - 3].rstrip() + "..."
