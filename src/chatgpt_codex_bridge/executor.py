from __future__ import annotations

import json
import os
import queue
import re
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Callable

from .executor_events import (
    ParsedExecEvents,
    _build_native_turn_exec_stdout,
    _estimate_context_metrics,
    _extract_native_agent_message,
    _native_turn_until_complete,
    _normalize_thread_turn_text,
    _resumed_thread_turn_matches,
    _select_native_turn_payload,
    parse_exec_events,
)
from .executor_reporting import (
    _build_interruption_stderr,
    _build_progress_stall_stderr,
    _build_timeout_stderr,
    _coerce_timeout_output,
    _derive_blockers,
    _derive_next_step,
    _derive_risks,
    _derive_summary,
    _display_command,
    _extract_explicit_report_fields,
    _infer_checks_from_commands,
    _infer_files_touched_from_snapshots,
    _is_git_repo,
    _make_run_dir,
    _snapshot_workspace_files,
)
from .defaults import DEFAULT_CODEX_MODEL, DEFAULT_CODEX_REASONING_EFFORT
from .app_paths import codex_home
from .models import RunReport, now_iso
from .profiles import active_profile, dangerous_bypass_opted_in, profile_allows
from .storage import save_json

_ROLLOUT_SESSION_PATH_RE = re.compile(
    r"rollout-.*-([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\.jsonl$",
    flags=re.IGNORECASE,
)
_CODEX_APP_BUNDLE_ID = "com.openai.codex"
_CODEX_APP_SERVER_BIN = Path("/Applications/Codex.app/Contents/Resources/codex")
_OPENED_CODEX_APP_THREADS: set[str] = set()
_APP_SERVER_TIMEOUT_SECONDS = 20.0
_APP_SERVER_STREAM_CLOSED = object()
_CODEX_APP_INTEGRATION_ENV_VAR = "BRIDGE_ENABLE_CODEX_APP_INTEGRATION"
_CODEX_APP_AUTO_OPEN_ENV_VAR = "BRIDGE_AUTO_OPEN_CODEX_APP_THREADS"
_CODEX_EXEC_IGNORE_USER_CONFIG_ENV_VAR = "BRIDGE_CODEX_EXEC_IGNORE_USER_CONFIG"
_CODEX_EXEC_REQUIRED_PATHS = (
    str(Path.home() / ".local" / "bin"),
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
    str(_CODEX_APP_SERVER_BIN.parent),
)
_DEFAULT_CODEX_MODEL = DEFAULT_CODEX_MODEL
_DEFAULT_CODEX_REASONING_EFFORT = DEFAULT_CODEX_REASONING_EFFORT
_DEFAULT_CODEX_SANDBOX = "workspace-write"
_DEFAULT_CODEX_APPROVAL_POLICY = "on-request"
_DEFAULT_NATIVE_TURN_TIMEOUT_SECONDS = 1800.0
_DEFAULT_CODEX_PROGRESS_STALL_SECONDS = 300.0
_NATIVE_THREAD_ROLLOUT_WAIT_SECONDS = 2.0
_PROCESS_ACTIVITY_CPU_THRESHOLD = 0.1
_ACTIVE_PROCESS_TIMEOUT_EXTENSION_SECONDS = 900.0
_PROTECTED_LONG_RUNNING_CHILD_COMMAND_MARKERS = (
    "swift-test",
    "swift test",
    "swift build",
    "xcodebuild",
    "pab-sync refresh",
    "python3 -m unittest",
    "python -m unittest",
    "pytest",
)
_NATIVE_THREAD_ROLLOUT_POLL_SECONDS = 0.05
_LIVE_LOG_TAIL_LINES = 200
_WORKSPACE_SNAPSHOT_MAX_DURATION_SECONDS = 3.0
_OPENAI_API_HOST = "api.openai.com"
_OPENAI_API_PORT = 443
_OPENAI_API_RESPONSES_URL = "wss://api.openai.com/v1/responses"


def _turn_completed_before_timeout(parsed_events: ParsedExecEvents, completed: subprocess.CompletedProcess[str]) -> bool:
    if int(completed.returncode or 0) != 124:
        return False
    if "turn.completed" not in parsed_events.event_types:
        return False
    return bool(str(parsed_events.final_agent_message or "").strip())


def _strip_bridge_timeout_stderr(stderr: str, timeout_seconds: float | None) -> str:
    timeout_line = f"codex exec timed out after {timeout_seconds} seconds."
    kept_lines = [line for line in str(stderr or "").splitlines() if line.strip() != timeout_line]
    return "\n".join(kept_lines).strip()


def _command_looks_like_codex_exec(command: str) -> bool:
    normalized = f" {str(command or '').casefold()} "
    return " codex exec " in normalized or "/codex exec " in normalized


def _codex_exec_environment(env: dict[str, str] | None) -> dict[str, str]:
    child_env = dict(os.environ if env is None else env)
    existing_path = child_env.get("PATH") or os.environ.get("PATH", "")
    ordered_parts: list[str] = []
    seen: set[str] = set()
    for raw_part in (*_CODEX_EXEC_REQUIRED_PATHS, *existing_path.split(os.pathsep)):
        part = str(raw_part or "").strip()
        if not part or part in seen:
            continue
        ordered_parts.append(part)
        seen.add(part)
    child_env["PATH"] = os.pathsep.join(ordered_parts)
    child_env.setdefault("SHELL", "/bin/zsh")
    return child_env


def _preflight_openai_api_reachability() -> str:
    try:
        socket.getaddrinfo(_OPENAI_API_HOST, _OPENAI_API_PORT, type=socket.SOCK_STREAM)
    except OSError as exc:
        detail = str(exc).strip() or exc.__class__.__name__
        return (
            "preflight_openai_api: failed to lookup address information before starting nested codex exec: "
            f"{detail}, url: {_OPENAI_API_RESPONSES_URL}"
        )
    return ""


def _should_preflight_openai_reachability(*, codex_bin: str, enabled: bool) -> bool:
    if not enabled:
        return False
    normalized = str(codex_bin or "").strip()
    if not normalized:
        return False
    if Path(normalized).name.casefold() == "codex":
        return True
    normalized_path = normalized.replace("\\", "/")
    return normalized_path.endswith("/Resources/codex")


@lru_cache(maxsize=16)
def _codex_exec_help_text(codex_bin: str) -> str:
    normalized = str(codex_bin or "").strip()
    if not _should_preflight_openai_reachability(codex_bin=normalized, enabled=True):
        return ""
    try:
        completed = subprocess.run(
            [normalized, "exec", "--help"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    return "\n".join(part for part in (completed.stdout, completed.stderr) if part)


def _codex_exec_supports_dangerous_bypass_flag(codex_bin: str) -> bool:
    return "--dangerously-bypass-approvals-and-sandbox" in _codex_exec_help_text(codex_bin)


def _codex_exec_supports_approval_policy_flag(codex_bin: str) -> bool:
    help_text = _codex_exec_help_text(codex_bin)
    return "-a, --approval-policy" in help_text or "--approval-policy" in help_text


def _codex_exec_launch_flags(
    codex_bin: str,
    *,
    sandbox: str | None = None,
    approval_policy: str | None = None,
    bridge_profile: str | None = None,
) -> list[str]:
    profile = active_profile(bridge_profile)
    effective_sandbox = str(sandbox or profile.default_sandbox or _DEFAULT_CODEX_SANDBOX).strip()
    effective_approval = str(
        approval_policy or profile.default_approval_policy or _DEFAULT_CODEX_APPROVAL_POLICY
    ).strip()
    if not effective_sandbox:
        effective_sandbox = _DEFAULT_CODEX_SANDBOX
    if not effective_approval:
        effective_approval = _DEFAULT_CODEX_APPROVAL_POLICY
    if (
        profile_allows("dangerous-codex-bypass", profile.name)
        and dangerous_bypass_opted_in()
        and effective_sandbox == "danger-full-access"
        and _codex_exec_supports_dangerous_bypass_flag(codex_bin)
    ):
        return ["--dangerously-bypass-approvals-and-sandbox"]
    flags = ["-s", effective_sandbox]
    if _codex_exec_supports_approval_policy_flag(codex_bin):
        flags = ["-a", effective_approval, *flags]
    return flags


def codex_app_integration_enabled() -> bool:
    normalized = str(os.environ.get(_CODEX_APP_INTEGRATION_ENV_VAR, "")).strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    if sys.platform != "darwin":
        return False
    return _CODEX_APP_SERVER_BIN.exists()


def codex_app_auto_open_enabled() -> bool:
    normalized = str(os.environ.get(_CODEX_APP_AUTO_OPEN_ENV_VAR, "")).strip().casefold()
    return normalized in {"1", "true", "yes", "on"}


def codex_exec_ignore_user_config_enabled() -> bool:
    normalized = str(os.environ.get(_CODEX_EXEC_IGNORE_USER_CONFIG_ENV_VAR, "")).strip().casefold()
    return normalized in {"1", "true", "yes", "on"}


def prepare_native_codex_fork_thread(
    *,
    codex_bin: str,
    source_thread_id: str,
    workdir: Path,
    thread_name_hint: str = "",
) -> str | None:
    if sys.platform != "darwin":
        return None
    normalized_source = str(source_thread_id or "").strip()
    if not normalized_source:
        return None
    try:
        with _CodexAppServerSession(codex_bin=codex_bin) as session:
            response = session.request(
                "thread/fork",
                {
                    "threadId": normalized_source,
                    "cwd": str(workdir),
                    "persistExtendedHistory": True,
                },
            )
            thread_payload = response.get("thread") if isinstance(response, dict) else None
            native_thread_id = str((thread_payload or {}).get("id", "")).strip()
            if not native_thread_id:
                return None
            rollout_path = Path(str((thread_payload or {}).get("path", "")).strip()).expanduser()
            _sanitize_forked_rollout_session_file(rollout_path, thread_id=native_thread_id)
            thread_name = str(thread_name_hint or "").strip() or _default_native_codex_thread_name(workdir, native_thread_id)
            try:
                session.request(
                    "thread/name/set",
                    {
                        "threadId": native_thread_id,
                        "name": thread_name,
                    },
                )
            except (OSError, RuntimeError, subprocess.SubprocessError):
                pass
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return None
    if codex_app_auto_open_enabled():
        _open_codex_app_thread_once_best_effort(native_thread_id)
    return native_thread_id


def prepare_native_codex_start_thread(
    *,
    codex_bin: str,
    workdir: Path,
    thread_name_hint: str = "",
) -> str | None:
    if sys.platform != "darwin":
        return None
    try:
        with _CodexAppServerSession(codex_bin=codex_bin) as session:
            response = session.request(
                "thread/start",
                {
                    "cwd": str(workdir),
                },
            )
            thread_payload = response.get("thread") if isinstance(response, dict) else None
            native_thread_id = str((thread_payload or {}).get("id", "")).strip()
            if not native_thread_id:
                return None
            rollout_path_text = str((thread_payload or {}).get("path", "")).strip()
            if rollout_path_text:
                rollout_path = Path(rollout_path_text).expanduser()
                # `thread/start` can return before the rollout file is visible on disk.
                # The thread id is authoritative, so wait briefly but keep the thread.
                _wait_for_rollout_session_file(rollout_path)
            thread_name = str(thread_name_hint or "").strip() or _default_native_codex_thread_name(workdir, native_thread_id)
            try:
                session.request(
                    "thread/name/set",
                    {
                        "threadId": native_thread_id,
                        "name": thread_name,
                    },
                )
            except (OSError, RuntimeError, subprocess.SubprocessError):
                pass
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return None
    if codex_app_auto_open_enabled():
        _open_codex_app_thread_once_best_effort(native_thread_id)
    return native_thread_id


def register_codex_app_thread_best_effort(
    *,
    codex_bin: str,
    thread_id: str,
    workdir: Path,
    thread_name_hint: str = "",
) -> None:
    normalized_thread_id = str(thread_id or "").strip()
    if not normalized_thread_id:
        return
    thread_name = str(thread_name_hint or "").strip() or _default_native_codex_thread_name(workdir, normalized_thread_id)
    try:
        with _CodexAppServerSession(codex_bin=codex_bin) as session:
            session.request(
                "thread/name/set",
                {
                    "threadId": normalized_thread_id,
                    "name": thread_name,
                },
            )
    except (OSError, RuntimeError, subprocess.SubprocessError):
        pass
    if codex_app_auto_open_enabled():
        _open_codex_app_thread_once_best_effort(normalized_thread_id)


def _default_native_codex_thread_name(workdir: Path, thread_id: str) -> str:
    label = workdir.name.strip() or "codex-thread"
    return f"{label} {thread_id[:8]}"


def session_live_log_path(artifacts_root: Path, session_id: str) -> Path:
    return artifacts_root.parent / "session_logs" / f"{session_id}.log"


def _wait_for_rollout_session_file(
    path: Path,
    *,
    timeout_seconds: float = _NATIVE_THREAD_ROLLOUT_WAIT_SECONDS,
) -> bool:
    if path.exists():
        return True
    deadline = time.monotonic() + max(timeout_seconds, 0.0)
    while time.monotonic() < deadline:
        remaining = max(deadline - time.monotonic(), 0.0)
        time.sleep(min(_NATIVE_THREAD_ROLLOUT_POLL_SECONDS, remaining))
        if path.exists():
            return True
    return path.exists()


def _sanitize_forked_rollout_session_file(path: Path, *, thread_id: str) -> None:
    if not path.exists() or not path.is_file():
        return
    normalized_thread_id = str(thread_id or "").strip()
    if not normalized_thread_id:
        return
    original_lines = path.read_text(encoding="utf-8").splitlines()
    sanitized_lines: list[str] = []
    changed = False
    for raw_line in original_lines:
        stripped = raw_line.strip()
        if not stripped:
            sanitized_lines.append(raw_line)
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            sanitized_lines.append(raw_line)
            continue
        if (
            payload.get("type") == "session_meta"
            and str(payload.get("payload", {}).get("id", "")).strip() != normalized_thread_id
        ):
            changed = True
            continue
        sanitized_lines.append(raw_line)
    if changed:
        path.write_text("\n".join(sanitized_lines) + "\n", encoding="utf-8")


def _resolve_codex_app_server_bin(codex_bin: str) -> str:
    normalized = str(codex_bin or "").strip()
    bundle_bin = _CODEX_APP_SERVER_BIN
    if bundle_bin.exists():
        if not normalized or normalized == "codex":
            return str(bundle_bin)
        expanded = str(Path(normalized).expanduser())
        if expanded == str(bundle_bin):
            return expanded
        if "/.dual-graph/" in expanded or expanded.endswith("/.dual-graph/codex"):
            return str(bundle_bin)
    return normalized or "codex"


class _CodexAppServerSession:
    def __init__(
        self,
        *,
        codex_bin: str,
        timeout_seconds: float = _APP_SERVER_TIMEOUT_SECONDS,
    ) -> None:
        self.codex_bin = codex_bin
        self.timeout_seconds = timeout_seconds
        self.process: subprocess.Popen[str] | None = None
        self.stdout_queue: queue.Queue[object] = queue.Queue()
        self.stderr_lines: list[str] = []
        self.stdout_thread: threading.Thread | None = None
        self.stderr_thread: threading.Thread | None = None
        self._next_request_id = 2

    def __enter__(self) -> "_CodexAppServerSession":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def open(self) -> None:
        if self.process is not None:
            return
        resolved_codex_bin = _resolve_codex_app_server_bin(self.codex_bin)
        process = subprocess.Popen(
            [resolved_codex_bin, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self.process = process
        self.stdout_thread = threading.Thread(
            target=_drain_app_server_stream,
            args=(process.stdout, self.stdout_queue),
            daemon=True,
            name="codex-app-server-stdout",
        )
        self.stderr_thread = threading.Thread(
            target=_drain_app_server_stderr,
            args=(process.stderr, self.stderr_lines),
            daemon=True,
            name="codex-app-server-stderr",
        )
        self.stdout_thread.start()
        self.stderr_thread.start()
        _write_codex_app_server_message(
            process,
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "chatgpt-codex-bridge",
                        "title": "ChatGPT Codex Bridge",
                        "version": "1.0",
                    },
                    "capabilities": {"experimentalApi": True},
                },
            },
        )
        _read_codex_app_server_response(
            process,
            self.stdout_queue,
            request_id=1,
            method_name="initialize",
            timeout_seconds=self.timeout_seconds,
            stderr_lines=self.stderr_lines,
        )
        _write_codex_app_server_message(process, {"method": "initialized", "params": {}})

    def close(self) -> None:
        process = self.process
        if process is None:
            return
        self.process = None
        _shutdown_codex_app_server_process(process)
        if self.stdout_thread is not None:
            self.stdout_thread.join(timeout=0.2)
            self.stdout_thread = None
        if self.stderr_thread is not None:
            self.stderr_thread.join(timeout=0.2)
            self.stderr_thread = None

    def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
        response, _notifications = self.request_until(method, params)
        return response

    def request_until(
        self,
        method: str,
        params: dict[str, object],
        *,
        until: Callable[[dict[str, object], list[dict[str, object]]], bool] | None = None,
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        self.open()
        process = self.process
        if process is None:
            raise RuntimeError("codex app-server process is unavailable.")
        request_id = self._next_request_id
        self._next_request_id += 1
        _write_codex_app_server_message(
            process,
            {
                "id": request_id,
                "method": method,
                "params": params,
            },
        )
        response_payload, notifications = _read_codex_app_server_response_until(
            process,
            self.stdout_queue,
            request_id=request_id,
            method_name=method,
            timeout_seconds=self.timeout_seconds,
            stderr_lines=self.stderr_lines,
            until=until,
        )
        return _unwrap_codex_app_server_response(method, response_payload), notifications


def _run_codex_app_server_request(
    *,
    codex_bin: str,
    method: str,
    params: dict[str, object],
    timeout_seconds: float = _APP_SERVER_TIMEOUT_SECONDS,
) -> dict[str, object]:
    with _CodexAppServerSession(codex_bin=codex_bin, timeout_seconds=timeout_seconds) as session:
        return session.request(method, params)


def compact_codex_thread_after_turn(
    *,
    codex_bin: str,
    thread_id: str,
    workdir: Path | None = None,
    timeout_seconds: float = _APP_SERVER_TIMEOUT_SECONDS,
) -> dict[str, object]:
    normalized_thread_id = str(thread_id or "").strip()
    if not normalized_thread_id:
        raise RuntimeError("Codex thread compaction requires a thread id.")

    resume_params: dict[str, object] = {
        "threadId": normalized_thread_id,
        "excludeTurns": True,
    }
    if workdir is not None:
        resume_params["cwd"] = str(workdir)

    with _CodexAppServerSession(codex_bin=codex_bin, timeout_seconds=timeout_seconds) as session:
        session.request("thread/resume", resume_params)

        def is_complete(
            compact_response: dict[str, object],
            compact_notifications: list[dict[str, object]],
        ) -> bool:
            return _codex_thread_compaction_complete(
                normalized_thread_id,
                compact_response,
                compact_notifications,
            )

        response, notifications = session.request_until(
            "thread/compact/start",
            {"threadId": normalized_thread_id},
            until=is_complete,
        )

    completion = _codex_thread_compaction_completion(
        normalized_thread_id,
        response,
        notifications,
    )
    return {
        "status": "completed",
        "thread_id": normalized_thread_id,
        "method": "thread/compact/start",
        "completion": str((completion or {}).get("method", "")) or "unknown",
        "turn_id": str((completion or {}).get("turn_id", "")),
        "notification_count": len(notifications),
    }


def _codex_thread_compaction_complete(
    thread_id: str,
    response: dict[str, object],
    notifications: list[dict[str, object]],
) -> bool:
    return _codex_thread_compaction_completion(thread_id, response, notifications) is not None


def _codex_thread_compaction_completion(
    thread_id: str,
    response: dict[str, object],
    notifications: list[dict[str, object]],
) -> dict[str, object] | None:
    expected_turn_id = _codex_app_server_turn_id(response)
    for notification in reversed(notifications):
        method = str(notification.get("method", "")).strip()
        matches_thread = _codex_app_server_notification_matches_thread(notification, thread_id)
        if method == "thread/compacted" and matches_thread:
            return {"method": method}
        if method != "turn/completed" or not matches_thread:
            continue
        params = notification.get("params")
        turn = params.get("turn") if isinstance(params, dict) else None
        if not isinstance(turn, dict):
            return {"method": method}
        turn_id = str(turn.get("id", "")).strip()
        if expected_turn_id and turn_id and turn_id != expected_turn_id:
            continue
        status = str(turn.get("status", "")).strip()
        if status and status != "completed":
            error = turn.get("error")
            if isinstance(error, dict) and str(error.get("message", "")).strip():
                raise RuntimeError(f"Codex thread compact failed: {error['message']}")
            raise RuntimeError(f"Codex thread compact failed with turn status {status}.")
        return {"method": method, "turn_id": turn_id}
    return None


def _codex_app_server_turn_id(payload: dict[str, object]) -> str:
    turn = payload.get("turn")
    if isinstance(turn, dict):
        return str(turn.get("id", "")).strip()
    return ""


def _codex_app_server_notification_matches_thread(payload: dict[str, object], thread_id: str) -> bool:
    params = payload.get("params")
    if not isinstance(params, dict):
        return False
    return str(params.get("threadId", "")).strip() == thread_id


def _write_codex_app_server_message(process: subprocess.Popen[str], payload: dict[str, object]) -> None:
    if process.stdin is None:
        raise RuntimeError("codex app-server stdin is unavailable.")
    process.stdin.write(json.dumps(payload) + "\n")
    process.stdin.flush()


def _drain_app_server_stream(
    stream: object | None,
    stdout_queue: queue.Queue[object],
) -> None:
    if stream is None:
        stdout_queue.put(_APP_SERVER_STREAM_CLOSED)
        return
    try:
        while True:
            raw_line = stream.readline()
            if not raw_line:
                break
            stdout_queue.put(raw_line)
    finally:
        stdout_queue.put(_APP_SERVER_STREAM_CLOSED)


def _drain_app_server_stderr(
    stream: object | None,
    stderr_lines: list[str],
) -> None:
    if stream is None:
        return
    for raw_line in iter(stream.readline, ""):
        line = raw_line.rstrip("\n")
        if line:
            stderr_lines.append(line)


def _read_codex_app_server_response(
    process: subprocess.Popen[str],
    stdout_queue: queue.Queue[object],
    *,
    request_id: int,
    method_name: str,
    timeout_seconds: float,
    stderr_lines: list[str],
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        remaining = max(deadline - time.monotonic(), 0.0)
        if remaining <= 0.0:
            break
        try:
            item = stdout_queue.get(timeout=remaining)
        except queue.Empty:
            break
        if item is _APP_SERVER_STREAM_CLOSED:
            break
        stripped = str(item).strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if "method" in payload and "id" in payload and "result" not in payload and "error" not in payload:
            _write_codex_app_server_message(process, {"id": payload["id"], "result": {}})
            continue
        if payload.get("id") == request_id:
            return payload

    try:
        exit_code = process.wait(timeout=0.2)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Timed out waiting for {method_name} response from codex app-server.") from exc
    stderr_text = "\n".join(stderr_lines)
    if exit_code != 0:
        raise RuntimeError(_summarize_app_server_failure(stderr_text, exit_code=exit_code))
    raise RuntimeError(f"Missing {method_name} response from codex app-server.")


def _read_codex_app_server_response_until(
    process: subprocess.Popen[str],
    stdout_queue: queue.Queue[object],
    *,
    request_id: int,
    method_name: str,
    timeout_seconds: float,
    stderr_lines: list[str],
    until: Callable[[dict[str, object], list[dict[str, object]]], bool] | None = None,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    deadline = time.monotonic() + timeout_seconds
    response_payload: dict[str, object] | None = None
    notifications: list[dict[str, object]] = []
    while True:
        remaining = max(deadline - time.monotonic(), 0.0)
        if remaining <= 0.0:
            break
        try:
            item = stdout_queue.get(timeout=remaining)
        except queue.Empty:
            break
        if item is _APP_SERVER_STREAM_CLOSED:
            break
        stripped = str(item).strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if "method" in payload and "id" in payload and "result" not in payload and "error" not in payload:
            _write_codex_app_server_message(process, {"id": payload["id"], "result": {}})
            continue
        if payload.get("id") == request_id:
            response_payload = payload
            if until is None:
                return response_payload, notifications
            try:
                response = _unwrap_codex_app_server_response(method_name, response_payload)
            except RuntimeError:
                raise
            if until(response, notifications):
                return response_payload, notifications
            continue
        if "method" in payload and "id" not in payload:
            notifications.append(payload)
            if response_payload is None or until is None:
                continue
            response = _unwrap_codex_app_server_response(method_name, response_payload)
            if until(response, notifications):
                return response_payload, notifications

    try:
        exit_code = process.wait(timeout=0.2)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Timed out waiting for {method_name} response from codex app-server.") from exc
    stderr_text = "\n".join(stderr_lines)
    if exit_code != 0:
        raise RuntimeError(_summarize_app_server_failure(stderr_text, exit_code=exit_code))
    if response_payload is None:
        raise RuntimeError(f"Missing {method_name} response from codex app-server.")
    raise RuntimeError(f"Timed out waiting for {method_name} follow-up notifications from codex app-server.")


def _shutdown_codex_app_server_process(process: subprocess.Popen[str]) -> None:
    if process.stdin is not None and not process.stdin.closed:
        process.stdin.close()
    if process.stdout is not None and not process.stdout.closed:
        process.stdout.close()
    if process.stderr is not None and not process.stderr.closed:
        process.stderr.close()
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=0.2)
    except (OSError, subprocess.SubprocessError):
        try:
            process.kill()
            process.wait(timeout=0.2)
        except (OSError, subprocess.SubprocessError):
            return


def _unwrap_codex_app_server_response(method: str, response: dict[str, object]) -> dict[str, object]:
    if "result" in response and isinstance(response["result"], dict):
        return response["result"]
    if "error" in response:
        raise RuntimeError(_format_codex_app_server_error(response["error"]))
    raise RuntimeError(f"Malformed {method} response from codex app-server.")


def _format_codex_app_server_error(payload: object) -> str:
    if isinstance(payload, dict):
        message = str(payload.get("message", "")).strip()
        code = payload.get("code")
        if message and code is not None:
            return f"{message} (code {code})"
        if message:
            return message
    return "Unknown codex app-server error."


def _summarize_app_server_failure(stderr_text: str, *, exit_code: int) -> str:
    lines = [line.strip() for line in (stderr_text or "").splitlines() if line.strip()]
    filtered = [
        line
        for line in lines
        if not any(marker in line for marker in _RECURRING_STDERR_NOISE_MARKERS)
    ]
    details = filtered or lines
    if details:
        return f"codex app-server exited with code {exit_code}: {details[-1]}"
    return f"codex app-server exited with code {exit_code}."


def _can_verify_resumed_thread_turn_materialized(codex_bin: str) -> bool:
    return _can_use_native_codex_app_server(codex_bin)


def _can_use_native_codex_app_server(codex_bin: str) -> bool:
    if not codex_app_integration_enabled():
        return False
    if sys.platform != "darwin" or not _CODEX_APP_SERVER_BIN.exists():
        return False
    return _resolve_codex_app_server_bin(codex_bin) == str(_CODEX_APP_SERVER_BIN)


def _can_execute_native_turn_start(
    codex_bin: str,
    *,
    resume_session_id: str | None,
    stop_checker: Callable[[], str | None] | None = None,
) -> bool:
    del codex_bin, resume_session_id, stop_checker
    # `app-server turn/start` has not been reliable enough for bridge sessions that
    # need observable, reusable Codex threads. Prefer `codex exec ... resume <thread>`
    # as the stable default until native turn execution proves consistent.
    return False


def _run_codex_native_turn_with_polling(
    *,
    codex_bin: str,
    resume_session_id: str,
    prompt_text: str,
    last_message_path: Path,
    timeout_seconds: float | None,
    stop_checker: Callable[[], str | None] | None,
    stop_check_interval_seconds: float,
    heartbeat_callback: Callable[[], None] | None = None,
    progress_callback: Callable[[], None] | None = None,
) -> tuple[subprocess.CompletedProcess[str], str]:
    del stop_checker, stop_check_interval_seconds
    normalized_thread_id = str(resume_session_id or "").strip()
    if not normalized_thread_id:
        raise RuntimeError("Native turn/start requires a resumable Codex thread id.")

    resolved_codex_bin = _resolve_codex_app_server_bin(codex_bin)
    command = [resolved_codex_bin, "app-server", "turn/start", normalized_thread_id]
    native_timeout = timeout_seconds if timeout_seconds is not None else _DEFAULT_NATIVE_TURN_TIMEOUT_SECONDS

    with _CodexAppServerSession(codex_bin=codex_bin, timeout_seconds=native_timeout) as session:
        if heartbeat_callback is not None:
            heartbeat_callback()
        if progress_callback is not None:
            progress_callback()
        response, notifications = session.request_until(
            "turn/start",
            {
                "threadId": normalized_thread_id,
                "input": [{"type": "text", "text": prompt_text}],
            },
            until=_native_turn_until_complete,
        )
        if heartbeat_callback is not None:
            heartbeat_callback()
        if progress_callback is not None:
            progress_callback()
        thread_snapshot = session.request(
            "thread/read",
            {
                "threadId": normalized_thread_id,
                "includeTurns": True,
            },
        )
        stderr_text = "\n".join(session.stderr_lines)

    turn_payload = _select_native_turn_payload(
        thread_snapshot,
        turn_id=str((response.get("turn") or {}).get("id", "")).strip() if isinstance(response, dict) else "",
        prompt_text=prompt_text,
    )
    final_agent_message = _extract_native_agent_message(turn_payload, notifications)
    if final_agent_message:
        last_message_path.write_text(final_agent_message.rstrip() + "\n", encoding="utf-8")
    synthetic_stdout = _build_native_turn_exec_stdout(
        thread_id=normalized_thread_id,
        turn_payload=turn_payload,
        notifications=notifications,
        fallback_agent_message=final_agent_message,
    )
    return subprocess.CompletedProcess(command, 0, synthetic_stdout, stderr_text), ""


def _verify_resumed_thread_turn_materialized(
    *,
    codex_bin: str,
    thread_id: str,
    prompt_text: str,
    final_agent_message: str,
) -> None:
    if not _can_verify_resumed_thread_turn_materialized(codex_bin):
        return
    normalized_thread_id = str(thread_id or "").strip()
    normalized_prompt_text = _normalize_thread_turn_text(prompt_text)
    normalized_agent_message = _normalize_thread_turn_text(final_agent_message)
    if not normalized_thread_id or not normalized_prompt_text:
        return
    try:
        response = _run_codex_app_server_request(
            codex_bin=codex_bin,
            method="thread/read",
            params={
                "threadId": normalized_thread_id,
                "includeTurns": True,
            },
        )
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        raise RuntimeError(
            "same_thread run finished locally but verification via the native Codex app-server failed"
        ) from exc

    thread_payload = response.get("thread") if isinstance(response, dict) else None
    turns = thread_payload.get("turns") if isinstance(thread_payload, dict) else None
    if isinstance(turns, list):
        for turn in reversed(turns):
            if _resumed_thread_turn_matches(
                turn,
                prompt_text=normalized_prompt_text,
                final_agent_message=normalized_agent_message,
            ):
                return

    raise RuntimeError(
        "same_thread run finished locally but the resumed Codex turn did not materialize in official thread history"
    )


def execute_codex_prompt(
    *,
    prompt_path: Path,
    workdir: Path,
    artifacts_root: Path,
    thread_id: str,
    resume_session_id: str | None = None,
    codex_bin: str = "codex",
    observed_thread_name_hint: str = "",
    model: str | None = None,
    reasoning_effort: str | None = None,
    sandbox: str | None = None,
    profile: str | None = None,
    env: dict[str, str] | None = None,
    preflight_openai_reachability: bool = False,
    verify_resumed_thread_materialized: bool = True,
    timeout_seconds: float | None = None,
    progress_stall_seconds: float | None = _DEFAULT_CODEX_PROGRESS_STALL_SECONDS,
    compact_after_success: bool = False,
    compact_timeout_seconds: float | None = 300.0,
    stop_checker: Callable[[], str | None] | None = None,
    stop_check_interval_seconds: float = 0.25,
    heartbeat_callback: Callable[[], None] | None = None,
    progress_callback: Callable[[], None] | None = None,
) -> tuple[RunReport, dict[str, object]]:
    run_dir = _make_run_dir(artifacts_root, thread_id)
    prompt_copy_path = run_dir / "prompt.md"
    stdout_path = run_dir / "stdout.jsonl"
    stderr_path = run_dir / "stderr.txt"
    last_message_path = run_dir / "last_message.md"
    live_log_path = run_dir / "live_output.log"
    session_log = session_live_log_path(artifacts_root, thread_id)
    metadata_path = run_dir / "run_metadata.json"
    report_path = run_dir / "run_report.json"

    shutil.copyfile(prompt_path, prompt_copy_path)
    prompt_text = prompt_path.read_text(encoding="utf-8")
    session_log.parent.mkdir(parents=True, exist_ok=True)
    app_integration_enabled = codex_app_integration_enabled()
    auto_open_enabled = codex_app_auto_open_enabled()
    if resume_session_id and app_integration_enabled and auto_open_enabled:
        _open_codex_app_thread_once_best_effort(resume_session_id)
    thread_open_watcher = _start_codex_thread_open_watcher(
        workdir,
        enabled=app_integration_enabled and auto_open_enabled and not bool(resume_session_id),
    )

    skip_git_repo_check = not _is_git_repo(workdir)
    effective_model = str(model or _DEFAULT_CODEX_MODEL).strip() or _DEFAULT_CODEX_MODEL
    effective_reasoning_effort = (
        str(reasoning_effort or _DEFAULT_CODEX_REASONING_EFFORT).strip() or _DEFAULT_CODEX_REASONING_EFFORT
    )
    explicit_execution_settings = bool(str(model or "").strip() or str(reasoning_effort or "").strip())
    use_native_turn_start = _can_execute_native_turn_start(
        codex_bin,
        resume_session_id=resume_session_id,
        stop_checker=stop_checker,
    ) and not explicit_execution_settings
    resolved_codex_bin = _resolve_codex_app_server_bin(codex_bin)
    if use_native_turn_start:
        command = [resolved_codex_bin, "app-server", "turn/start", str(resume_session_id or "").strip()]
    else:
        command = [resolved_codex_bin, "exec"]
        if codex_exec_ignore_user_config_enabled():
            command.append("--ignore-user-config")
        command.extend(["-m", effective_model, "-c", f'model_reasoning_effort="{effective_reasoning_effort}"'])
        command.extend(_codex_exec_launch_flags(resolved_codex_bin, sandbox=sandbox))
        if profile:
            command.extend(["-p", profile])
        command.extend(["--json", "-o", str(last_message_path), "-C", str(workdir)])
        if skip_git_repo_check:
            command.append("--skip-git-repo-check")
        if resume_session_id:
            command.extend(["resume", resume_session_id])
        command.append("-")

    started_at = now_iso()
    live_log_paths = (live_log_path, session_log)
    _append_live_log_banner(
        live_log_paths,
        title=f"run started {started_at}",
        lines=[
            f"session_id={thread_id}",
            f"run_dir={run_dir}",
            f"workdir={workdir}",
            f"command={_display_command(command)}",
        ],
    )
    codex_env = _codex_exec_environment(env)
    ignored_workspace_roots = [artifacts_root, session_log.parent]
    workspace_before = _snapshot_workspace_files(
        workdir,
        ignored_roots=ignored_workspace_roots,
        max_duration_seconds=_WORKSPACE_SNAPSHOT_MAX_DURATION_SECONDS,
    )
    interruption_reason = ""
    if use_native_turn_start:
        completed, interruption_reason = _run_codex_native_turn_with_polling(
            codex_bin=codex_bin,
            resume_session_id=str(resume_session_id or "").strip(),
            prompt_text=prompt_text,
            last_message_path=last_message_path,
            timeout_seconds=timeout_seconds,
            stop_checker=stop_checker,
            stop_check_interval_seconds=stop_check_interval_seconds,
            heartbeat_callback=heartbeat_callback,
            progress_callback=progress_callback,
        )
    elif _should_preflight_openai_reachability(
        codex_bin=codex_bin,
        enabled=preflight_openai_reachability,
    ) and (preflight_stderr := _preflight_openai_api_reachability()):
        completed = subprocess.CompletedProcess(
            command,
            1,
            "",
            preflight_stderr,
        )
    elif stop_checker is None:
        try:
            completed = subprocess.run(
                args=command,
                input=prompt_text,
                text=True,
                capture_output=True,
                env=codex_env,
                check=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            completed = subprocess.CompletedProcess(
                command,
                124,
                _coerce_timeout_output(exc.stdout),
                _build_timeout_stderr(exc.stderr, timeout_seconds),
            )
    else:
        completed, interruption_reason = _run_codex_with_polling(
            command=command,
            prompt_text=prompt_text,
            timeout_seconds=timeout_seconds,
            progress_stall_seconds=progress_stall_seconds,
            env=codex_env,
            stop_checker=stop_checker,
            stop_check_interval_seconds=stop_check_interval_seconds,
            heartbeat_callback=heartbeat_callback,
            progress_callback=progress_callback,
            live_log_paths=live_log_paths,
        )
    _stop_codex_thread_open_watcher(thread_open_watcher)
    finished_at = now_iso()

    parsed_events = parse_exec_events(completed.stdout)
    if _turn_completed_before_timeout(parsed_events, completed):
        completed = subprocess.CompletedProcess(
            completed.args,
            0,
            completed.stdout,
            _strip_bridge_timeout_stderr(completed.stderr or "", timeout_seconds),
        )
        interruption_reason = ""

    stdout_path.write_text(completed.stdout, encoding="utf-8")
    stderr_path.write_text(completed.stderr or "No stderr output.\n", encoding="utf-8")
    if not last_message_path.exists():
        last_message_path.write_text("", encoding="utf-8")

    last_message = last_message_path.read_text(encoding="utf-8")
    workspace_after = _snapshot_workspace_files(
        workdir,
        ignored_roots=ignored_workspace_roots,
        max_duration_seconds=_WORKSPACE_SNAPSHOT_MAX_DURATION_SECONDS,
    )
    if resume_session_id and verify_resumed_thread_materialized and not use_native_turn_start:
        verification_message = parsed_events.final_agent_message or last_message
        # Fail closed if the official desktop thread history does not reflect the resumed turn.
        try:
            _verify_resumed_thread_turn_materialized(
                codex_bin=codex_bin,
                thread_id=resume_session_id,
                prompt_text=prompt_text,
                final_agent_message=verification_message,
            )
        except RuntimeError as exc:
            raise RuntimeError(f"{exc}. Review artifacts in {run_dir}.") from exc
    context_metrics = _estimate_context_metrics(parsed_events.usage, model=model)
    files_touched, explicit_checks = _extract_explicit_report_fields(last_message, parsed_events.final_agent_message)
    if not files_touched:
        files_touched = _infer_files_touched_from_snapshots(workspace_before, workspace_after)
    checks = explicit_checks or _infer_checks_from_commands(parsed_events.commands_observed)
    summary = _derive_summary(last_message, parsed_events.final_agent_message, completed.returncode, completed.stderr or "")
    blockers = _derive_blockers(completed.returncode, completed.stderr)
    risks = _derive_risks(completed.returncode)
    next_step = _derive_next_step(completed.returncode, completed.stderr or "")

    if interruption_reason == "stop_requested":
        summary = "Codex run was stopped by control request."
        blockers = ["Codex run was stopped by control request."]
        risks = ["Partial changes may need review before the next run."]
        next_step = "Inspect the partial artifacts before resuming or starting a fresh run."
    elif interruption_reason == "pause_requested":
        summary = "Codex run was paused by control request."
        blockers = ["Codex run was paused by control request."]
        risks = ["Partial changes may need review before resuming."]
        next_step = "Inspect the partial artifacts, then resume when ready."
    elif interruption_reason == "progress_stall":
        stall_window = f"{progress_stall_seconds} seconds" if progress_stall_seconds is not None else "the configured stall window"
        summary = "Codex run stalled without new output and was terminated for automatic retry."
        blockers = [f"Codex stalled without new output for {stall_window}."]
        risks = [
            "The interrupted run may already have changed files before the bridge retried it.",
            "Review the partial artifacts if repeated automatic retries stall on the same assistant turn.",
        ]
        next_step = "The bridge should rearm the same assistant turn and retry automatically."

    report = RunReport(
        timestamp=finished_at,
        thread_id=thread_id,
        summary=summary,
        requested_codex_thread_id=str(resume_session_id or ""),
        codex_thread_id=parsed_events.observed_codex_thread_id or str(resume_session_id or ""),
        observed_codex_thread_id=parsed_events.observed_codex_thread_id,
        final_agent_message=parsed_events.final_agent_message,
        visible_assistant_trace=list(parsed_events.assistant_messages),
        event_types=parsed_events.event_types,
        commands_observed=parsed_events.commands_observed,
        usage=parsed_events.usage,
        context_window_tokens=context_metrics["context_window_tokens"],
        context_used_tokens=context_metrics["context_used_tokens"],
        estimated_context_remaining_percent=context_metrics["estimated_context_remaining_percent"],
        context_signal_source=context_metrics["context_signal_source"],
        files_touched=files_touched
        or [
            f"Review raw last message: {last_message_path}",
            f"Review raw event stream: {stdout_path}",
        ],
        checks=checks or [_display_command(command)],
        blockers=blockers,
        risks=risks,
        artifacts_dir=str(run_dir),
        prompt_path=str(prompt_copy_path),
        raw_output_path=str(stdout_path),
        last_message_path=str(last_message_path),
        stderr_path=str(stderr_path),
        session_live_log_path=str(session_log),
        exit_code=completed.returncode,
        command=command,
        interruption_reason=interruption_reason,
        run_id=run_dir.name,
        next_step=next_step,
        workspace_path=str(workdir),
    )
    if compact_after_success and completed.returncode == 0 and not interruption_reason:
        compaction_thread_id = str(report.observed_codex_thread_id or report.codex_thread_id or resume_session_id or "").strip()
        if not compaction_thread_id:
            raise RuntimeError(f"Codex finished but no thread id was available for post-turn compaction. Review artifacts in {run_dir}.")
        report.codex_compaction = {
            "status": "running",
            "thread_id": compaction_thread_id,
            "method": "thread/compact/start",
            "started_at": now_iso(),
        }
        save_json(report_path, report.as_dict())
        _append_live_log_banner(
            live_log_paths,
            title="post-run compaction started",
            lines=[
                f"thread_id={compaction_thread_id}",
                "method=thread/compact/start",
                f"run_report_path={report_path}",
            ],
        )
        try:
            report.codex_compaction = compact_codex_thread_after_turn(
                codex_bin=codex_bin,
                thread_id=compaction_thread_id,
                workdir=workdir,
                timeout_seconds=compact_timeout_seconds,
            )
        except RuntimeError as exc:
            report.codex_compaction = {
                "status": "failed",
                "thread_id": compaction_thread_id,
                "method": "thread/compact/start",
                "error": str(exc),
                "finished_at": now_iso(),
            }
            save_json(report_path, report.as_dict())
            raise RuntimeError(f"Codex post-turn compaction failed: {exc}. Review artifacts in {run_dir}.") from exc
    if _can_use_native_codex_app_server(codex_bin) and parsed_events.observed_codex_thread_id:
        if resume_session_id:
            if auto_open_enabled:
                _open_codex_app_thread_once_best_effort(parsed_events.observed_codex_thread_id)
        else:
            register_codex_app_thread_best_effort(
                codex_bin=codex_bin,
                thread_id=parsed_events.observed_codex_thread_id,
                workdir=workdir,
                thread_name_hint=observed_thread_name_hint,
            )
    save_json(report_path, report.as_dict())
    _append_live_log_banner(
        live_log_paths,
        title=f"run finished {finished_at}",
        lines=[
            f"exit_code={completed.returncode}",
            f"interruption_reason={interruption_reason or 'none'}",
            f"summary={summary}",
            f"last_message_path={last_message_path}",
            f"stderr_path={stderr_path}",
        ],
    )

    metadata = {
        "started_at": started_at,
        "finished_at": finished_at,
        "thread_id": thread_id,
        "workdir": str(workdir),
        "command": command,
        "exit_code": completed.returncode,
        "skip_git_repo_check": skip_git_repo_check,
        "timeout_seconds": timeout_seconds,
        "progress_stall_seconds": progress_stall_seconds,
        "interruption_reason": interruption_reason,
        "prompt_path": str(prompt_copy_path),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "last_message_path": str(last_message_path),
        "live_log_path": str(live_log_path),
        "session_live_log_path": str(session_log),
        "run_report_path": str(report_path),
    }
    save_json(metadata_path, metadata)

    return report, {
        "run_dir": str(run_dir),
        "report_path": str(report_path),
        "metadata_path": str(metadata_path),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "last_message_path": str(last_message_path),
        "live_log_path": str(live_log_path),
        "session_live_log_path": str(session_log),
        "exit_code": completed.returncode,
    }


def _open_codex_app_thread_best_effort(thread_id: str) -> bool:
    if sys.platform != "darwin":
        return False
    normalized = str(thread_id or "").strip()
    if not normalized:
        return False
    deeplink = f"codex://threads/{normalized}"
    script = "\n".join(
        [
            f'tell application id "{_CODEX_APP_BUNDLE_ID}"',
            "  activate",
            "end tell",
            "delay 0.2",
            f'open location "{deeplink}"',
            "delay 0.2",
            f'tell application id "{_CODEX_APP_BUNDLE_ID}"',
            "  activate",
            "end tell",
        ]
    )
    try:
        subprocess.run(["osascript", "-"], input=script, text=True, check=True)
        return True
    except (OSError, subprocess.SubprocessError):
        try:
            subprocess.run(["open", "-b", _CODEX_APP_BUNDLE_ID], check=True)
            subprocess.run(["open", deeplink], check=True)
            return True
        except (OSError, subprocess.SubprocessError):
            return False


def _open_codex_app_thread_once_best_effort(thread_id: str) -> None:
    normalized = str(thread_id or "").strip()
    if not normalized:
        return
    if normalized in _OPENED_CODEX_APP_THREADS:
        return
    if _open_codex_app_thread_best_effort(normalized):
        _OPENED_CODEX_APP_THREADS.add(normalized)


def _start_codex_thread_open_watcher(
    workdir: Path,
    *,
    enabled: bool,
) -> tuple[threading.Event, threading.Thread] | None:
    if not enabled or sys.platform != "darwin":
        return None
    sessions_root = codex_home() / "sessions"
    if not sessions_root.exists():
        return None
    existing_paths = {str(path) for path in sessions_root.rglob("rollout-*.jsonl")}
    stop_event = threading.Event()

    def _watch() -> None:
        deadline = time.monotonic() + 90.0
        ignored_paths: set[str] = set()
        while not stop_event.is_set() and time.monotonic() < deadline:
            try:
                candidates = sorted(
                    sessions_root.rglob("rollout-*.jsonl"),
                    key=lambda item: item.stat().st_mtime,
                    reverse=True,
                )
            except OSError:
                time.sleep(0.5)
                continue
            for candidate in candidates:
                candidate_key = str(candidate)
                if candidate_key in existing_paths or candidate_key in ignored_paths:
                    continue
                thread_id = _extract_thread_id_from_rollout_session_path(candidate)
                if not thread_id:
                    ignored_paths.add(candidate_key)
                    continue
                matches = _thread_record_matches_workdir(thread_id, workdir)
                if matches is None:
                    continue
                if matches:
                    _open_codex_app_thread_once_best_effort(thread_id)
                    stop_event.set()
                    return
                ignored_paths.add(candidate_key)
            time.sleep(0.5)

    watcher = threading.Thread(target=_watch, daemon=True, name="codex-thread-open-watcher")
    watcher.start()
    return stop_event, watcher


def _stop_codex_thread_open_watcher(handle: tuple[threading.Event, threading.Thread] | None) -> None:
    if handle is None:
        return
    stop_event, watcher = handle
    stop_event.set()
    watcher.join(timeout=1.0)


def _extract_thread_id_from_rollout_session_path(path: Path) -> str:
    match = _ROLLOUT_SESSION_PATH_RE.search(path.name)
    if match is None:
        return ""
    return match.group(1)


def _thread_record_matches_workdir(thread_id: str, workdir: Path) -> bool | None:
    state_db = codex_home() / "state_5.sqlite"
    if not state_db.exists():
        return True
    try:
        connection = sqlite3.connect(state_db)
        try:
            row = connection.execute("select cwd from threads where id = ?", (thread_id,)).fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return True
    if row is None:
        return None
    recorded_cwd = str(row[0] or "").strip()
    if not recorded_cwd:
        return True
    return recorded_cwd == str(workdir)


def _run_codex_with_polling(
    *,
    command: list[str],
    prompt_text: str,
    timeout_seconds: float | None,
    progress_stall_seconds: float | None,
    env: dict[str, str] | None,
    stop_checker: Callable[[], str | None],
    stop_check_interval_seconds: float,
    heartbeat_callback: Callable[[], None] | None = None,
    progress_callback: Callable[[], None] | None = None,
    live_log_paths: tuple[Path, ...] = (),
) -> tuple[subprocess.CompletedProcess[str], str]:
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert process.stdin is not None
    process.stdin.write(prompt_text)
    process.stdin.close()

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    last_progress_at = time.monotonic()
    progress_lock = threading.Lock()
    effective_progress_stall_seconds = (
        progress_stall_seconds if progress_stall_seconds is not None and progress_stall_seconds > 0 else None
    )

    def _record_progress() -> None:
        nonlocal last_progress_at
        with progress_lock:
            last_progress_at = time.monotonic()
        if progress_callback is not None:
            progress_callback()

    stdout_thread = threading.Thread(
        target=_consume_stream,
        args=(process.stdout, stdout_chunks),
        kwargs={
            "live_log_paths": live_log_paths,
            "label": "STDOUT",
            "progress_callback": _record_progress,
        },
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_consume_stream,
        args=(process.stderr, stderr_chunks),
        kwargs={
            "live_log_paths": live_log_paths,
            "label": "STDERR",
            "progress_callback": _record_progress,
        },
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    interruption_reason = ""
    deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
    while process.poll() is None:
        if heartbeat_callback is not None:
            heartbeat_callback()
        stop_command = stop_checker()
        if stop_command in {"stop", "pause"}:
            interruption_reason = "pause_requested" if stop_command == "pause" else "stop_requested"
            _terminate_process(process)
            break
        if deadline is not None and time.monotonic() >= deadline:
            if effective_progress_stall_seconds is not None:
                with progress_lock:
                    seconds_since_progress = time.monotonic() - last_progress_at
                if seconds_since_progress < effective_progress_stall_seconds:
                    deadline = time.monotonic() + _ACTIVE_PROCESS_TIMEOUT_EXTENSION_SECONDS
                    time.sleep(max(stop_check_interval_seconds, 0.01))
                    continue
            if _process_tree_has_observable_activity(process):
                _record_progress()
                deadline = time.monotonic() + _ACTIVE_PROCESS_TIMEOUT_EXTENSION_SECONDS
                time.sleep(max(stop_check_interval_seconds, 0.01))
                continue
            interruption_reason = "timeout"
            _terminate_process(process)
            break
        if effective_progress_stall_seconds is not None:
            with progress_lock:
                seconds_since_progress = time.monotonic() - last_progress_at
            if seconds_since_progress >= effective_progress_stall_seconds:
                if _process_tree_has_observable_activity(process):
                    _record_progress()
                    time.sleep(max(stop_check_interval_seconds, 0.01))
                    continue
                interruption_reason = "progress_stall"
                _terminate_process(process)
                break
        time.sleep(max(stop_check_interval_seconds, 0.01))

    try:
        return_code = process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        return_code = process.wait(timeout=5)

    stdout_thread.join(timeout=5)
    stderr_thread.join(timeout=5)
    stdout_text = "".join(stdout_chunks)
    stderr_text = "".join(stderr_chunks)

    if interruption_reason == "timeout":
        return (
            subprocess.CompletedProcess(command, 124, stdout_text, _build_timeout_stderr(stderr_text, timeout_seconds)),
            interruption_reason,
        )
    if interruption_reason == "progress_stall":
        return (
            subprocess.CompletedProcess(
                command,
                124,
                stdout_text,
                _build_progress_stall_stderr(stderr_text, effective_progress_stall_seconds),
            ),
            interruption_reason,
        )
    if interruption_reason in {"stop_requested", "pause_requested"}:
        return (
            subprocess.CompletedProcess(
                command,
                130,
                stdout_text,
                _build_interruption_stderr(stderr_text, interruption_reason),
            ),
            interruption_reason,
        )

    return subprocess.CompletedProcess(command, return_code, stdout_text, stderr_text), ""


def _process_tree_has_observable_activity(process: subprocess.Popen[str]) -> bool:
    root_pid = getattr(process, "pid", None)
    if not isinstance(root_pid, int) or root_pid <= 0:
        return False
    try:
        completed = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,stat=,%cpu=,command="],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except Exception:
        return False
    if completed.returncode != 0:
        return False
    rows: dict[int, tuple[int, str, float, str]] = {}
    for line in completed.stdout.splitlines():
        parts = line.split(maxsplit=4)
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
            cpu = float(parts[3])
        except ValueError:
            continue
        stat = parts[2]
        command = parts[4] if len(parts) >= 5 else ""
        rows[pid] = (ppid, stat, cpu, command)
    if root_pid not in rows:
        return False
    descendants = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, (ppid, _stat, _cpu, _command) in rows.items():
            if pid not in descendants and ppid in descendants:
                descendants.add(pid)
                changed = True
    for pid in descendants:
        _ppid, stat, cpu, command = rows.get(pid, (0, "", 0.0, ""))
        if pid == root_pid:
            if cpu >= _PROCESS_ACTIVITY_CPU_THRESHOLD and _command_looks_like_codex_exec(command):
                return True
            continue
        if "R" in stat or cpu >= _PROCESS_ACTIVITY_CPU_THRESHOLD:
            return True
        if "Z" not in stat:
            normalized_command = command.casefold()
            if any(marker in normalized_command for marker in _PROTECTED_LONG_RUNNING_CHILD_COMMAND_MARKERS):
                return True
    return False


def _consume_stream(
    stream,
    chunks: list[str],
    *,
    live_log_paths: tuple[Path, ...] = (),
    label: str = "",
    progress_callback: Callable[[], None] | None = None,
) -> None:
    if stream is None:
        return
    try:
        while True:
            data = stream.readline()
            if not data:
                break
            chunks.append(data)
            if live_log_paths:
                _append_live_log_output(live_log_paths, label=label, text=data)
            if progress_callback is not None:
                progress_callback()
    finally:
        stream.close()


def _append_live_log_output(paths: tuple[Path, ...], *, label: str, text: str) -> None:
    if not text:
        return
    normalized_label = f"{label} | " if label else ""
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for line in text.splitlines(keepends=True):
                suffix = "\n" if not line.endswith("\n") else ""
                handle.write(f"{normalized_label}{line}{suffix}")


def _append_live_log_banner(paths: tuple[Path, ...], *, title: str, lines: list[str]) -> None:
    banner_lines = ["", f"=== {title} ===", *[line for line in lines if line], ""]
    text = "\n".join(banner_lines) + "\n"
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
