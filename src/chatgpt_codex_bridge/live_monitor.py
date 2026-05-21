from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import textwrap
import time
from collections import deque
from pathlib import Path
from typing import Any

_DEFAULT_TAIL_LINES = 200
_DEFAULT_POLL_INTERVAL_SECONDS = 0.2
_STDOUT_PREFIX = "STDOUT | "
_STDERR_PREFIX = "STDERR | "
_STDERR_LEVEL_RE = re.compile(r"^(?P<timestamp>\S+)\s+(?P<level>WARN|ERROR|INFO)\s+(?P<body>.*)$")
_BANNER_RE = re.compile(r"^=== (?P<title>.+) ===$")
_KEY_VALUE_RE = re.compile(r"^(?P<key>[a-z_]+)=(?P<value>.*)$")
_SUCCESS_OUTPUT_PREVIEW_LINES = 2
_FAILURE_OUTPUT_PREVIEW_LINES = 6
_INLINE_OUTPUT_MAX_LINES = 3
_INLINE_OUTPUT_MAX_TOTAL_CHARS = 120
_COMMAND_PREVIEW_MAX_CHARS = 140
_OUTPUT_PREVIEW_MAX_CHARS = 160
_FILE_CHANGE_PREVIEW_LIMIT = 2
_EXPANDED_COMMAND_PREVIEW_MAX_CHARS = 4000
_EXPANDED_OUTPUT_PREVIEW_MAX_CHARS = 400
_EXPANDED_OUTPUT_HEAD_LINES = 24
_EXPANDED_OUTPUT_TAIL_LINES = 8
_RECURRING_STDERR_NOISE_MARKERS = (
    "codex_core_skills::loader: failed to stat skills entry",
    "codex_core_skills::loader: ignoring interface.icon_small:",
    "codex_core_skills::loader: ignoring interface.icon_large:",
    "codex_core::plugins::manifest: ignoring interface.defaultPrompt:",
    "codex_core_plugins::manifest: ignoring interface.defaultPrompt:",
    "codex_core::shell_snapshot: Failed to delete shell snapshot at",
    "codex_core::tools::router: error=exec_command failed",
    "codex_core::tools::router: error=agent with id",
    "codex_core::session::turn: after_agent hook failed; continuing",
    "codex_core::session::turn: stream disconnected - retrying sampling request",
    "codex_core::session: failed to record rollout items: thread",
    "codex_core_plugins::manager: failed to warm featured plugin ids cache",
    "codex_mcp::rmcp_client: failed to initialize MCP client during shutdown",
    "codex_otel::events::session_telemetry: metrics counter",
    "codex_rmcp_client::stdio_server_launcher: Failed to kill MCP process group",
    "codex_rmcp_client::stdio_server_launcher: Failed to terminate MCP process group",
)
_RECURRING_STDERR_NOISE_PATTERNS = (
    re.compile(r"\bWARN\s+codex_(?:core_)?plugins::manifest: ignoring interface\.", re.IGNORECASE),
    re.compile(r"\bWARN\s+codex_core_skills::loader: ignoring interface\.", re.IGNORECASE),
    re.compile(r"\bWARN\s+codex_rmcp_client::stdio_server_launcher: Failed to (?:kill|terminate) MCP process group\b", re.IGNORECASE),
    re.compile(r"\bWARN\s+codex_mcp::rmcp_client: failed to initialize MCP client during shutdown\b", re.IGNORECASE),
    re.compile(r"\bERROR\s+codex_core::tools::router: error=agent with id .+ not found\b", re.IGNORECASE),
)


def format_live_log_line(raw_line: str, *, detail: str = "compact") -> list[str]:
    line = raw_line.rstrip("\r\n")
    if not line:
        return [""]
    if line.startswith(_STDOUT_PREFIX):
        payload = _load_json_payload(line[len(_STDOUT_PREFIX) :])
        if payload is None:
            return ["stdout", *indent_block(line[len(_STDOUT_PREFIX) :])]
        return format_exec_event(payload, detail=detail)
    if line.startswith(_STDERR_PREFIX):
        return format_stderr_line(line[len(_STDERR_PREFIX) :], detail=detail)
    return format_plain_line(line, detail=detail)


def format_exec_event(payload: dict[str, Any], *, detail: str = "compact") -> list[str]:
    event_type = str(payload.get("type") or payload.get("event") or "").strip()
    if detail == "terminal" and event_type in {"thread.started", "turn.started", "turn.completed"}:
        return []
    if event_type == "thread.started":
        thread_id = str(payload.get("thread_id", "")).strip()
        return ["", f"Thread started: {thread_id}"] if thread_id else ["", "Thread started"]
    if event_type == "turn.started":
        return ["", "Turn started"]
    if event_type == "turn.completed":
        usage = payload.get("usage")
        if isinstance(usage, dict) and usage:
            usage_bits = ", ".join(f"{key}={value}" for key, value in usage.items())
            return ["", f"Turn completed: {usage_bits}"]
        return ["", "Turn completed"]
    if event_type in {"item.started", "item.completed"}:
        item = payload.get("item")
        if isinstance(item, dict):
            return format_item_event(event_type, item, detail=detail)
    if detail == "terminal":
        return []
    if event_type:
        return ["", f"Event: {event_type}", *indent_block(json.dumps(payload, ensure_ascii=True, sort_keys=True))]
    return ["", "stdout", *indent_block(json.dumps(payload, ensure_ascii=True, sort_keys=True))]


def format_item_event(event_type: str, item: dict[str, Any], *, detail: str = "compact") -> list[str]:
    item_type = str(item.get("type", "")).strip()
    if item_type == "agent_message":
        text = str(item.get("text", "")).strip()
        if detail == "terminal":
            return ["", *wrap_text_block(text, indent="")] if text else []
        label = "Agent update" if event_type == "item.completed" else "Agent message"
        item_id = str(item.get("id", "")).strip()
        header = f"{label} [{item_id}]" if item_id else label
        return ["", header, *wrap_text_block(text)]
    if item_type == "command_execution":
        return format_command_event(event_type, item, detail=detail)
    if item_type == "file_change":
        return format_file_change_event(event_type, item, detail=detail)
    if item_type == "collab_tool_call":
        return format_collab_tool_call_event(event_type, item, detail=detail)
    if detail == "terminal":
        return []
    item_id = str(item.get("id", "")).strip()
    header = f"Item {item_type or 'event'}"
    if item_id:
        header += f" [{item_id}]"
    return ["", header, *indent_block(json.dumps(item, ensure_ascii=True, sort_keys=True))]


def format_command_event(event_type: str, item: dict[str, Any], *, detail: str = "compact") -> list[str]:
    item_id = str(item.get("id", "")).strip()
    status = str(item.get("status", "")).strip()
    exit_code = item.get("exit_code")
    command = str(item.get("command", "")).strip()
    command_summary = summarize_command(command, detail=detail)
    if event_type == "item.started":
        if detail != "expanded":
            return []
        header = f"Started command [{item_id}]" if item_id else "Started command"
        lines = ["", header]
        if command_summary:
            lines.append(f"  {command_summary}")
        return lines

    if status == "failed":
        label = "Command failed"
    else:
        label = "Ran command"
    header = f"{label} [{item_id}]" if item_id else label
    if status == "failed" and isinstance(exit_code, int):
        header += f" exit={exit_code}"
    lines = ["", header]
    if command_summary:
        lines.append(f"  {command_summary}")
    aggregated_output = str(item.get("aggregated_output", ""))
    lines.extend(summarize_command_output(aggregated_output, failed=status == "failed", detail=detail))
    return lines


def format_file_change_event(event_type: str, item: dict[str, Any], *, detail: str = "compact") -> list[str]:
    item_id = str(item.get("id", "")).strip()
    status = str(item.get("status", "")).strip()
    if event_type == "item.started":
        if detail != "expanded":
            return []
        header = f"Started file change [{item_id}]" if item_id else "Started file change"
        return ["", header]

    changes = item.get("changes")
    valid_changes = [change for change in changes if isinstance(change, dict)] if isinstance(changes, list) else []
    header = summarize_file_change_header(item_id=item_id, status=status, changes=valid_changes)
    lines = ["", header]
    preview_limit = len(valid_changes) if detail == "expanded" else _FILE_CHANGE_PREVIEW_LIMIT
    max_chars = _EXPANDED_OUTPUT_PREVIEW_MAX_CHARS if detail == "expanded" else _OUTPUT_PREVIEW_MAX_CHARS
    for change in valid_changes[:preview_limit]:
        kind = str(change.get("kind", "")).strip() or "update"
        path = str(change.get("path", "")).strip()
        path_summary = shorten_text(path, max_chars=max_chars)
        if path_summary:
            lines.append(f"  {kind}: {path_summary}")
    remaining = len(valid_changes) - min(len(valid_changes), preview_limit)
    if remaining > 0:
        lines.append(f"  … {remaining} more changes")
    return lines


def format_collab_tool_call_event(event_type: str, item: dict[str, Any], *, detail: str = "compact") -> list[str]:
    if detail == "terminal" or event_type != "item.completed":
        return []
    tool = str(item.get("tool", "")).strip()
    item_id = str(item.get("id", "")).strip()
    status = str(item.get("status", "")).strip()
    label = "Subagent update"
    if tool == "close_agent":
        label = "Subagent closed"
    elif tool == "wait_agent":
        label = "Subagent result"
    elif tool:
        label = f"Subagent tool {tool}"
    if item_id:
        label += f" [{item_id}]"
    if status:
        label += f" ({status})"

    lines = ["", label]
    states = item.get("agents_states")
    if isinstance(states, dict) and states:
        rendered = 0
        for agent_id, state in states.items():
            if not isinstance(state, dict):
                continue
            agent_status = str(state.get("status", "")).strip() or "unknown"
            message = first_meaningful_line(str(state.get("message", "") or ""))
            agent_label = shorten_text(str(agent_id), max_chars=24)
            summary = f"  {agent_label}: {agent_status}"
            if message:
                summary += f" - {shorten_text(message, max_chars=_OUTPUT_PREVIEW_MAX_CHARS)}"
            lines.append(summary)
            rendered += 1
            if rendered >= 3:
                break
        remaining = len(states) - rendered
        if remaining > 0:
            lines.append(f"  … {remaining} more subagents")
    return lines


def format_stderr_line(raw_text: str, *, detail: str = "compact") -> list[str]:
    text = raw_text.strip()
    if not text:
        return [""]
    if _is_recurring_stderr_noise(text):
        return []
    match = _STDERR_LEVEL_RE.match(text)
    if match is None:
        if detail == "terminal":
            lowered = text.casefold()
            if not lowered.startswith(("error", "fatal", "exception", "traceback", "caused by:")):
                return []
            return ["", *wrap_text_block(text, indent="")]
        return ["", "stderr", *wrap_text_block(text)]
    level = match.group("level").lower()
    timestamp = match.group("timestamp")
    body = match.group("body").strip()
    if detail == "terminal" and level != "error":
        return []
    title = {
        "warn": f"Warning {timestamp}",
        "error": f"Error {timestamp}",
        "info": f"Info {timestamp}",
    }.get(level, f"stderr {timestamp}")
    return ["", title, *wrap_text_block(body)]


def _is_recurring_stderr_noise(text: str) -> bool:
    return is_recurring_codex_noise_text(text)


def is_recurring_codex_noise_text(text: str) -> bool:
    normalized = str(text or "").casefold()
    if any(marker.casefold() in normalized for marker in _RECURRING_STDERR_NOISE_MARKERS):
        return True
    return any(pattern.search(text) for pattern in _RECURRING_STDERR_NOISE_PATTERNS)


def format_plain_line(line: str, *, detail: str = "compact") -> list[str]:
    if detail == "terminal":
        return []
    banner_match = _BANNER_RE.match(line)
    if banner_match is not None:
        title = banner_match.group("title").strip()
        if title.startswith("run started "):
            return ["", f"Run started: {title.removeprefix('run started ').strip()}"]
        if title.startswith("run finished "):
            return ["", f"Run finished: {title.removeprefix('run finished ').strip()}"]
        return ["", title]

    kv_match = _KEY_VALUE_RE.match(line)
    if kv_match is None:
        return [line]

    key = kv_match.group("key").strip()
    value = kv_match.group("value").strip()
    if key == "command":
        return [f"Command: {summarize_command(value, detail=detail)}"]
    if key == "summary":
        return ["Summary:", *wrap_text_block(value)]

    title = {
        "session_id": "Session",
        "run_dir": "Run dir",
        "workdir": "Workspace",
        "exit_code": "Exit code",
        "interruption_reason": "Interruption",
        "last_message_path": "Last message",
        "stderr_path": "stderr log",
    }.get(key)
    if title is None:
        return [line]
    return [f"{title}: {value}"]


def wrap_text_block(text: str, *, indent: str = "  ", width: int | None = None) -> list[str]:
    normalized = str(text or "").strip()
    if not normalized:
        return [indent.rstrip()]
    effective_width = width or terminal_width()
    lines: list[str] = []
    for paragraph in normalized.splitlines():
        stripped = paragraph.strip()
        if not stripped:
            lines.append(indent.rstrip())
            continue
        lines.extend(
            textwrap.wrap(
                stripped,
                width=max(effective_width, len(indent) + 20),
                initial_indent=indent,
                subsequent_indent=indent,
                break_long_words=False,
                break_on_hyphens=False,
            )
        )
    return lines


def indent_block(text: str, *, indent: str = "  ") -> list[str]:
    if not text:
        return [indent.rstrip()]
    return [f"{indent}{line}" if line else indent.rstrip() for line in text.splitlines()]


def shorten_text(text: str, *, max_chars: int) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 1].rstrip() + "…"


def first_meaningful_line(text: str) -> str:
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped.lstrip("-*# ").strip()
    return ""


def summarize_command(command: str, *, detail: str = "compact") -> str:
    normalized = str(command or "").strip()
    if normalized.startswith("/bin/zsh -lc "):
        shell_payload = normalized[len("/bin/zsh -lc ") :].strip()
        if len(shell_payload) >= 2 and shell_payload[0] == shell_payload[-1] and shell_payload[0] in {'"', "'"}:
            normalized = shell_payload[1:-1]
        else:
            normalized = shell_payload
    max_chars = _EXPANDED_COMMAND_PREVIEW_MAX_CHARS if detail == "expanded" else _COMMAND_PREVIEW_MAX_CHARS
    return shorten_text(normalized, max_chars=max_chars)


def summarize_command_output(output: str, *, failed: bool, detail: str = "compact") -> list[str]:
    normalized = str(output or "").strip("\n")
    if not normalized.strip():
        return []
    lines = [line.rstrip() for line in normalized.splitlines()]
    if detail == "expanded":
        return render_expanded_command_output(lines)
    visible_limit = _FAILURE_OUTPUT_PREVIEW_LINES if failed else _SUCCESS_OUTPUT_PREVIEW_LINES
    total_chars = sum(len(line) for line in lines)
    if (
        not failed
        and len(lines) <= _INLINE_OUTPUT_MAX_LINES
        and total_chars <= _INLINE_OUTPUT_MAX_TOTAL_CHARS
    ):
        return ["  Result:", *[f"    {shorten_text(line, max_chars=_OUTPUT_PREVIEW_MAX_CHARS)}" for line in lines]]

    if not failed:
        descriptor = summarize_output_shape(lines)
        return [f"  Result: {descriptor}"]

    preview_lines = [shorten_text(line, max_chars=_OUTPUT_PREVIEW_MAX_CHARS) for line in lines[:visible_limit]]
    summary = f"  Error output: {len(lines)} lines"
    if len(lines) > visible_limit:
        summary += f" (showing {visible_limit})"
    rendered = [summary, *[f"    {line}" for line in preview_lines]]
    if len(lines) > visible_limit:
        rendered.append("    …")
    return rendered


def render_expanded_command_output(lines: list[str]) -> list[str]:
    if not lines:
        return []
    total_lines = len(lines)
    if total_lines <= _EXPANDED_OUTPUT_HEAD_LINES + _EXPANDED_OUTPUT_TAIL_LINES:
        return [
            "  Result:",
            *[
                f"    {shorten_text(line, max_chars=_EXPANDED_OUTPUT_PREVIEW_MAX_CHARS)}"
                for line in lines
            ],
        ]

    head = lines[:_EXPANDED_OUTPUT_HEAD_LINES]
    tail = lines[-_EXPANDED_OUTPUT_TAIL_LINES:]
    omitted = total_lines - len(head) - len(tail)
    rendered = [f"  Result: {total_lines} lines (showing first {len(head)} and last {len(tail)})"]
    rendered.extend(
        f"    {shorten_text(line, max_chars=_EXPANDED_OUTPUT_PREVIEW_MAX_CHARS)}" for line in head
    )
    rendered.append(f"    … {omitted} lines omitted …")
    rendered.extend(
        f"    {shorten_text(line, max_chars=_EXPANDED_OUTPUT_PREVIEW_MAX_CHARS)}" for line in tail
    )
    return rendered


def summarize_output_shape(lines: list[str]) -> str:
    non_empty = [line.strip() for line in lines if line.strip()]
    if not non_empty:
        return "no output"

    sample = non_empty[: min(len(non_empty), 12)]
    path_like_count = sum(1 for line in sample if looks_like_path_line(line))
    if path_like_count >= max(3, int(len(sample) * 0.7)):
        return f"{len(non_empty)} paths"
    return f"{len(non_empty)} lines"


def looks_like_path_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith(("/", "./", "../", "~")):
        return True
    if "/" in stripped and not stripped.startswith(("http://", "https://")):
        return True
    return False


def summarize_file_change_header(*, item_id: str, status: str, changes: list[dict[str, Any]]) -> str:
    count = len(changes)
    noun = "file" if count == 1 else "files"
    if status == "failed":
        label = f"File change failed [{item_id}]" if item_id else "File change failed"
        if count:
            return f"{label} ({count} {noun})"
        return label
    if count:
        label = f"Edited {count} {noun}"
    else:
        label = "Edited files"
    return f"{label} [{item_id}]" if item_id else label


def terminal_width(default: int = 100) -> int:
    try:
        if getattr(sys.stdout, "isatty", lambda: False)():
            return max(shutil.get_terminal_size().columns, 60)
        return default
    except Exception:
        return default


def _load_json_payload(text: str) -> dict[str, Any] | None:
    candidate = text.strip()
    if not candidate:
        return None
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


class LiveMonitor:
    def __init__(self, *, stream=None, detail: str = "compact") -> None:
        self.stream = stream or sys.stdout
        self.detail = detail
        self._last_blank = False

    def emit(self, lines: list[str]) -> None:
        for line in lines:
            is_blank = line == ""
            if is_blank and self._last_blank:
                continue
            self.stream.write(line + "\n")
            self.stream.flush()
            self._last_blank = is_blank

    def render_line(self, raw_line: str) -> None:
        self.emit(format_live_log_line(raw_line, detail=self.detail))

    def render_initial_tail(self, path: Path, *, tail_lines: int) -> int:
        if tail_lines > 0 and path.exists():
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in deque(handle, maxlen=tail_lines):
                    self.render_line(line)
        return path.stat().st_size if path.exists() else 0

    def follow(self, path: Path, *, start_offset: int, poll_interval: float) -> None:
        offset = max(start_offset, 0)
        pending = ""
        while True:
            if not path.exists():
                time.sleep(poll_interval)
                continue
            size = path.stat().st_size
            if size < offset:
                offset = 0
                pending = ""
            if size == offset:
                time.sleep(poll_interval)
                continue
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(offset)
                chunk = handle.read()
                offset = handle.tell()
            if not chunk:
                time.sleep(poll_interval)
                continue
            data = pending + chunk
            pending = ""
            for fragment in data.splitlines(keepends=True):
                if fragment.endswith("\n") or fragment.endswith("\r"):
                    self.render_line(fragment)
                else:
                    pending = fragment


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render a human-readable live view of a bridge session log.")
    parser.add_argument("--log", required=True, help="Path to the session log file.")
    parser.add_argument("--tail-lines", type=int, default=_DEFAULT_TAIL_LINES, help="Number of existing lines to render before following.")
    parser.add_argument(
        "--detail",
        choices=("compact", "expanded", "terminal"),
        default="compact",
        help="Rendering detail level.",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=_DEFAULT_POLL_INTERVAL_SECONDS,
        help="Polling interval in seconds while following the log.",
    )
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True, write_through=True)

    log_path = Path(args.log).expanduser()
    monitor = LiveMonitor(detail=str(args.detail))
    start_offset = monitor.render_initial_tail(log_path, tail_lines=max(args.tail_lines, 0))
    try:
        monitor.follow(log_path, start_offset=start_offset, poll_interval=max(args.poll_interval, 0.05))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
