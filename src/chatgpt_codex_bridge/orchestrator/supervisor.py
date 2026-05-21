from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..models import now_iso
from .browser_support import _looks_like_host_browser_transport_failure_message
from .state import load_session, save_session, session_path


class SessionSupervisor(threading.Thread):
    def __init__(
        self,
        *,
        session_id: str,
        sessions_dir: Path,
        runner: Any,
        poll_interval_seconds: float = 2.0,
        lock_dir: Path | None = None,
        lock_token: str = "",
    ) -> None:
        super().__init__(daemon=True, name=f"session-supervisor-{session_id}")
        self.session_id = session_id
        self.sessions_dir = sessions_dir
        self.runner = runner
        self.poll_interval_seconds = poll_interval_seconds
        self.lock_dir = lock_dir or (self.sessions_dir.parent / "session_locks")
        self.lock_token = lock_token
        self._stop_event = threading.Event()

    def request_stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        try:
            self._update_session(
                supervisor_status="running",
                auto_run_enabled=True,
                human_attention_reason="",
                last_error="",
                supervisor_heartbeat_at=now_iso(),
            )
            while not self._stop_event.is_set():
                session = load_session(session_path(self.sessions_dir, self.session_id))
                if session.status != "active" or not session.auto_run_enabled:
                    target_status = (
                        "paused"
                        if session.status == "paused"
                        else "stopped"
                        if session.status == "completed"
                        else "idle"
                    )
                    self._update_session(supervisor_status=target_status)
                    return
                self._update_session(supervisor_heartbeat_at=now_iso(), touch_updated_at=False)

                try:
                    cycles_before = session.cycles_completed
                    result = self.runner.run_once(self.session_id, require_new_message=True)
                    if not isinstance(result, dict):
                        raise RuntimeError(
                            f"Session runner returned invalid payload: expected dict, got {type(result).__name__}."
                        )
                    policy_outcome = str(result.get("policy_outcome", ""))
                    runner_action = str(result.get("runner_action", ""))
                    refreshed_session = load_session(session_path(self.sessions_dir, self.session_id))
                except Exception as exc:  # pragma: no cover - exercised by higher-level integration flows
                    message = str(exc)
                    if _looks_like_host_browser_transport_failure_message(message):
                        self._update_session(
                            supervisor_status="running",
                            auto_run_enabled=True,
                            supervisor_heartbeat_at=now_iso(),
                            human_attention_reason="",
                            last_error=message,
                            degraded_mode="host_browser_transport_retry",
                            degraded_reason=message,
                        )
                        self._wait_until_next_poll()
                        continue
                    self._update_session(
                        supervisor_status="failed",
                        auto_run_enabled=False,
                        human_attention_reason=message,
                        last_error=message,
                    )
                    return
                if runner_action == "cycle_completed" and refreshed_session.cycles_completed == cycles_before:
                    self._update_session(cycles_completed=cycles_before + 1)

                if runner_action == "wait_for_chatgpt":
                    waiting_session = load_session(session_path(self.sessions_dir, self.session_id))
                    if waiting_session.status != "active" or not waiting_session.auto_run_enabled:
                        target_status = (
                            "paused"
                            if waiting_session.status == "paused"
                            else "stopped"
                            if waiting_session.status == "completed"
                            else "idle"
                        )
                        self._update_session(supervisor_status=target_status)
                        return
                    self._update_session(supervisor_heartbeat_at=now_iso(), touch_updated_at=False)
                    self._wait_until_next_poll()
                    continue
                if policy_outcome == "allow":
                    continue
                if policy_outcome == "paused":
                    self._update_session(supervisor_status="paused", auto_run_enabled=False)
                    return
                if policy_outcome == "require_human":
                    self._update_session(supervisor_status="blocked", auto_run_enabled=False)
                    return
                if policy_outcome in {"stopped", "budget_exhausted"}:
                    self._update_session(supervisor_status="stopped", auto_run_enabled=False)
                    return

                self._update_session(supervisor_status="idle")
                return

            self._update_session(supervisor_status="stopped", auto_run_enabled=False)
        finally:
            close = getattr(self.runner, "close", None)
            if callable(close):
                close()
            if self.lock_token:
                _release_session_lock(self.lock_dir, self.session_id, self.lock_token)

    def _wait_until_next_poll(self) -> None:
        remaining_seconds = max(float(self.poll_interval_seconds or 0.0), 0.0)
        while remaining_seconds > 0 and not self._stop_event.is_set():
            session = load_session(session_path(self.sessions_dir, self.session_id))
            if session.status != "active" or not session.auto_run_enabled:
                return
            sleep_seconds = min(remaining_seconds, 0.1)
            started_wait = time.monotonic()
            self._stop_event.wait(sleep_seconds)
            remaining_seconds -= max(0.0, time.monotonic() - started_wait)

    def _update_session(self, **changes: Any) -> None:
        touch_updated_at = bool(changes.pop("touch_updated_at", True))
        path = session_path(self.sessions_dir, self.session_id)
        baseline_session = load_session(path)
        session = baseline_session
        for field_name, value in changes.items():
            setattr(session, field_name, value)
        latest_session = load_session(path)
        if (
            latest_session.status != baseline_session.status
            or latest_session.auto_run_enabled != baseline_session.auto_run_enabled
            or latest_session.loop_state != baseline_session.loop_state
            or latest_session.supervisor_status != baseline_session.supervisor_status
            or latest_session.updated_at != baseline_session.updated_at
        ):
            session = latest_session
            for field_name, value in changes.items():
                setattr(session, field_name, value)
        save_session(path, session, touch_updated_at=touch_updated_at)


class SupervisorManager:
    def __init__(
        self,
        *,
        sessions_dir: Path,
        runner_factory: Any,
        poll_interval_seconds: float = 2.0,
        lock_dir: Path | None = None,
    ) -> None:
        self.sessions_dir = sessions_dir
        self.runner_factory = runner_factory
        self.poll_interval_seconds = poll_interval_seconds
        self.lock_dir = lock_dir or (self.sessions_dir.parent / "session_locks")
        self._supervisors: dict[str, SessionSupervisor] = {}

    def ensure_session(self, session_id: str) -> dict[str, str]:
        supervisor = self._supervisors.get(session_id)
        if supervisor is not None and supervisor.is_alive():
            return {"session_id": session_id, "status": "running"}
        session = load_session(session_path(self.sessions_dir, session_id))
        if session.status != "active" or not session.auto_run_enabled:
            return {"session_id": session_id, "status": session.status or "idle"}
        lock_info = _acquire_session_lock(self.lock_dir, session_id)
        try:
            supervisor = SessionSupervisor(
                session_id=session_id,
                sessions_dir=self.sessions_dir,
                runner=self.runner_factory(),
                poll_interval_seconds=self.poll_interval_seconds,
                lock_dir=self.lock_dir,
                lock_token=str(lock_info["token"]),
            )
            self._supervisors[session_id] = supervisor
            supervisor.start()
        except Exception:
            _release_session_lock(self.lock_dir, session_id, str(lock_info["token"]))
            raise
        return {"session_id": session_id, "status": "running"}

    def stop_session(self, session_id: str) -> None:
        supervisor = self._supervisors.get(session_id)
        if supervisor is not None:
            supervisor.request_stop()
            supervisor.join(timeout=max(self.poll_interval_seconds, 0.1) + 0.5)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        snapshot: dict[str, dict[str, Any]] = {}
        for session_id, supervisor in self._supervisors.items():
            snapshot[session_id] = {
                "alive": supervisor.is_alive(),
                "lock": describe_session_lock(self.lock_dir, session_id),
            }
        for path in sorted(self.lock_dir.glob("*.json")) if self.lock_dir.exists() else []:
            session_id = path.stem
            snapshot.setdefault(
                session_id,
                {
                    "alive": False,
                    "lock": describe_session_lock(self.lock_dir, session_id),
                },
            )
        return snapshot


def terminate_locked_session_supervisor(
    lock_dir: Path,
    session_id: str,
    *,
    timeout_seconds: float = 2.0,
) -> dict[str, Any]:
    lock_path = _session_lock_path(lock_dir, session_id)
    lock = describe_session_lock(lock_dir, session_id)
    if lock is None:
        return {"status": "no_lock", "session_id": session_id}
    if str(lock.get("status", "")) == "corrupt":
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
        return {"status": "corrupt_lock_removed", "session_id": session_id, "lock_removed": True}
    pid = int(lock.get("pid", 0) or 0)
    if not lock.get("pid_alive", False):
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
        return {"status": "stale_lock_removed", "session_id": session_id, "pid": pid, "lock_removed": True}

    hostname = str(lock.get("hostname", "") or "")
    if hostname and hostname != socket.gethostname():
        return {"status": "foreign_host_lock", "session_id": session_id, "pid": pid, "hostname": hostname}

    command = _process_command(pid)
    if not _looks_like_locked_session_supervisor_process(command, session_id):
        return {
            "status": "refused_unrecognized_process",
            "session_id": session_id,
            "pid": pid,
        }

    process_ids = _descendant_process_ids(pid) + [pid]
    _signal_processes(process_ids, signal.SIGTERM)
    remaining = _wait_for_process_exit(process_ids, timeout_seconds)
    forced = False
    if remaining:
        forced = True
        _signal_processes(remaining, signal.SIGKILL)
        remaining = _wait_for_process_exit(remaining, 1.0)

    if not _pid_is_alive(pid):
        _release_session_lock(lock_dir, session_id, str(lock.get("token", "")))
        if lock_path.exists():
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass

    return {
        "status": "terminated" if not remaining else "termination_incomplete",
        "session_id": session_id,
        "pid": pid,
        "descendant_pids": [item for item in process_ids if item != pid],
        "forced": forced,
        "remaining_pids": remaining,
        "lock_removed": not lock_path.exists(),
    }


def describe_session_lock(lock_dir: Path, session_id: str) -> dict[str, Any] | None:
    lock_path = _session_lock_path(lock_dir, session_id)
    if not lock_path.exists():
        return None
    try:
        payload = json.loads(lock_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"status": "corrupt", "path": str(lock_path)}
    owner_pid = int(payload.get("pid", 0) or 0)
    payload["session_id"] = session_id
    payload["path"] = str(lock_path)
    payload["pid_alive"] = _pid_is_alive(owner_pid)
    return payload


def _session_lock_path(lock_dir: Path, session_id: str) -> Path:
    return lock_dir / f"{session_id}.json"


def _acquire_session_lock(lock_dir: Path, session_id: str) -> dict[str, Any]:
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = _session_lock_path(lock_dir, session_id)
    owner = {
        "session_id": session_id,
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "token": uuid4().hex,
        "thread_name": threading.current_thread().name,
        "acquired_at": time.time(),
    }
    while True:
        try:
            fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            existing = describe_session_lock(lock_dir, session_id)
            if existing and str(existing.get("status", "")) == "corrupt":
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
                continue
            if existing and not existing.get("pid_alive", True):
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
                continue
            owner_pid = existing.get("pid") if isinstance(existing, dict) else "unknown"
            raise RuntimeError(
                f"Session {session_id} is already supervised by pid {owner_pid}; refusing duplicate runner."
            )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(owner, handle, indent=2)
        return owner


def _release_session_lock(lock_dir: Path, session_id: str, lock_token: str) -> None:
    lock_path = _session_lock_path(lock_dir, session_id)
    if not lock_path.exists():
        return
    try:
        payload = json.loads(lock_path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    if str(payload.get("token", "")) != str(lock_token):
        return
    try:
        lock_path.unlink()
    except FileNotFoundError:
        return


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        # Sandboxed environments may block signaling unrelated live processes.
        # EPERM still means the pid exists, so do not treat it as a stale lock.
        return True
    except OSError:
        return False
    if _pid_is_zombie(pid):
        return False
    return True


def _pid_is_zombie(pid: int) -> bool:
    try:
        result = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True,
            check=False,
            text=True,
            timeout=1.0,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    status = str(result.stdout or "").strip().upper()
    return status.startswith("Z")


def _process_command(pid: int) -> str:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            check=False,
            text=True,
            timeout=1.0,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return str(result.stdout or "").strip()


def _looks_like_locked_session_supervisor_process(command: str, session_id: str) -> bool:
    text = str(command or "")
    if "supervise-session" not in text or str(session_id) not in text:
        return False
    return "mastermind_bridge.cli" in text or "bridgectl" in text


def _descendant_process_ids(pid: int) -> list[int]:
    descendants: list[int] = []
    seen: set[int] = set()
    stack = [pid]
    while stack:
        parent_pid = stack.pop()
        for child_pid in _child_process_ids(parent_pid):
            if child_pid in seen:
                continue
            seen.add(child_pid)
            descendants.append(child_pid)
            stack.append(child_pid)
    descendants.reverse()
    return descendants


def _child_process_ids(pid: int) -> list[int]:
    try:
        result = subprocess.run(
            ["pgrep", "-P", str(pid)],
            capture_output=True,
            check=False,
            text=True,
            timeout=1.0,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    child_pids: list[int] = []
    for line in str(result.stdout or "").splitlines():
        try:
            child_pids.append(int(line.strip()))
        except ValueError:
            continue
    return child_pids


def _signal_processes(process_ids: list[int], sig: signal.Signals) -> None:
    for pid in process_ids:
        try:
            os.kill(pid, sig)
        except OSError:
            continue


def _wait_for_process_exit(process_ids: list[int], timeout_seconds: float) -> list[int]:
    deadline = time.monotonic() + max(float(timeout_seconds or 0.0), 0.0)
    remaining = [pid for pid in process_ids if _pid_is_alive(pid)]
    while remaining and time.monotonic() < deadline:
        time.sleep(0.05)
        remaining = [pid for pid in process_ids if _pid_is_alive(pid)]
    return remaining
