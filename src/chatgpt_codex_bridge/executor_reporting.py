from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from datetime import datetime
from pathlib import Path

from .live_monitor import is_recurring_codex_noise_text

_RECURRING_STDERR_NOISE_MARKERS = (
    "codex_core_skills::loader: failed to stat skills entry",
    "codex_core_skills::loader: ignoring interface.icon_small:",
    "codex_core_skills::loader: ignoring interface.icon_large:",
    "codex_core::plugins::manifest: ignoring interface.defaultPrompt:",
    "codex_core_plugins::manifest: ignoring interface.defaultPrompt:",
    "codex_core::shell_snapshot: Failed to delete shell snapshot at",
    "codex_core::tools::router: error=exec_command failed",
    "codex_core::session::turn: after_agent hook failed; continuing",
    "codex_core::session: failed to record rollout items: thread",
    "codex_rmcp_client::stdio_server_launcher: Failed to terminate MCP process group",
)
_TEST_COMMAND_PREFIXES = (
    "python -m unittest",
    "python3 -m unittest",
    "python -m pytest",
    "python3 -m pytest",
    "pytest",
    "go test",
    "cargo test",
    "npm test",
    "pnpm test",
    "yarn test",
    "bun test",
    "bundle exec rspec",
    "rspec",
    "swift test",
    "xcodebuild test",
)
_IGNORED_WORKSPACE_DIR_NAMES = {
    ".dual-graph",
    ".dual-graph-context",
    ".build",
    ".git",
    ".hg",
    ".local",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".venv",
    "Chat GPT Exports",
    "__pycache__",
    "artifacts",
    "assistant-memory",
    "node_modules",
    "playwright-profile",
    "runtime_prompts",
    "session_locks",
    "state",
    "venv",
}
_IGNORED_WORKSPACE_FILE_NAMES = {
    ".DS_Store",
}
_IGNORED_WORKSPACE_FILE_SUFFIXES = {
    ".pyc",
    ".pyo",
}
_OPENAI_NETWORK_FAILURE_MARKERS = (
    "failed to lookup address information",
    "could not resolve host",
    "failed to connect to websocket",
    "error sending request for url",
    "stream disconnected",
)


def _extract_explicit_report_fields(*messages: str) -> tuple[list[str], list[str]]:
    files_touched: list[str] = []
    checks: list[str] = []
    for message in messages:
        if not message:
            continue
        lines = message.splitlines()
        files_touched = _merge_unique_items(
            files_touched,
            _filter_report_files_touched(_extract_report_section(lines, "Files touched:")),
        )
        checks = _merge_unique_items(checks, _extract_report_section(lines, "Checks run:"))
    return files_touched, checks


def _infer_checks_from_commands(commands_observed: list[dict[str, object]]) -> list[str]:
    checks: list[str] = []
    for item in commands_observed:
        command = _normalized_observed_command(str(item.get("command", "")))
        if not command:
            continue
        if not _command_finished(item):
            continue
        if not _looks_like_test_command(command):
            continue
        checks = _merge_unique_items(checks, [command])
    return checks


def _snapshot_workspace_files(
    workdir: Path,
    *,
    ignored_roots: list[Path] | None = None,
    max_duration_seconds: float | None = None,
) -> dict[str, tuple[int, int]]:
    snapshots: dict[str, tuple[int, int]] = {}
    resolved_workdir = workdir.resolve()
    ignored_prefixes = _normalized_ignored_roots(ignored_roots or [])
    deadline = (
        time.monotonic() + max(float(max_duration_seconds or 0.0), 0.0)
        if max_duration_seconds is not None
        else None
    )
    for root, dirnames, filenames in os.walk(resolved_workdir, topdown=True):
        root_path = Path(root)
        if _path_uses_ignored_root(root_path, ignored_prefixes):
            dirnames[:] = []
            continue
        dirnames[:] = [
            name
            for name in dirnames
            if not _should_ignore_workspace_directory(root_path / name, ignored_prefixes)
        ]
        for filename in filenames:
            if deadline is not None and time.monotonic() >= deadline:
                return snapshots
            path = root_path / filename
            if _should_ignore_workspace_path(path, ignored_prefixes):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            relative = path.relative_to(resolved_workdir).as_posix()
            snapshots[relative] = (stat.st_size, stat.st_mtime_ns)
        if deadline is not None and time.monotonic() >= deadline:
            return snapshots
    return snapshots


def _infer_files_touched_from_snapshots(
    before: dict[str, tuple[int, int]],
    after: dict[str, tuple[int, int]],
) -> list[str]:
    return _filter_report_files_touched(
        [path for path in sorted(set(before) | set(after)) if before.get(path) != after.get(path)]
    )


def _extract_report_section(lines: list[str], marker: str) -> list[str]:
    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line.startswith(marker):
            continue

        trailing = line[len(marker) :].strip()
        if trailing:
            return [item.strip() for item in trailing.split(",") if item.strip()]

        values: list[str] = []
        for candidate in lines[index + 1 :]:
            stripped = candidate.strip()
            if not stripped:
                break
            if stripped.startswith(("Files touched:", "Checks run:")):
                break
            if stripped.startswith(("- ", "* ")):
                values.append(stripped[2:].strip())
                continue
            break
        return [item for item in values if item]

    return []


def _normalized_observed_command(command: str) -> str:
    stripped = command.strip()
    if not stripped:
        return ""

    try:
        parts = shlex.split(stripped)
    except ValueError:
        return stripped

    if len(parts) >= 3 and Path(parts[0]).name in {"sh", "bash", "zsh"} and parts[1] in {"-c", "-lc"}:
        return parts[2].strip()
    return stripped


def _command_finished(item: dict[str, object]) -> bool:
    status = str(item.get("status", "")).strip()
    return status in {"completed", "failed"} or isinstance(item.get("exit_code"), int)


def _looks_like_test_command(command: str) -> bool:
    return any(command == prefix or command.startswith(f"{prefix} ") for prefix in _TEST_COMMAND_PREFIXES)


def _normalized_ignored_roots(ignored_roots: list[Path]) -> tuple[Path, ...]:
    return tuple(path.resolve() for path in ignored_roots)


def _should_ignore_workspace_path(path: Path, ignored_prefixes: tuple[Path, ...]) -> bool:
    if _path_uses_ignored_root(path, ignored_prefixes):
        return True
    if _is_assistant_memory_compiled_path(path):
        return True
    if any(part in _IGNORED_WORKSPACE_DIR_NAMES for part in path.parts):
        return True
    if path.name in _IGNORED_WORKSPACE_FILE_NAMES:
        return True
    if path.suffix in _IGNORED_WORKSPACE_FILE_SUFFIXES:
        return True
    return False


def _should_ignore_workspace_directory(path: Path, ignored_prefixes: tuple[Path, ...]) -> bool:
    if _path_uses_ignored_root(path, ignored_prefixes):
        return True
    if _is_assistant_memory_compiled_path(path):
        return True
    return path.name in _IGNORED_WORKSPACE_DIR_NAMES


def _path_uses_ignored_root(path: Path, ignored_prefixes: tuple[Path, ...]) -> bool:
    return any(path == root or root in path.parents for root in ignored_prefixes)


def _filter_report_files_touched(paths: list[str]) -> list[str]:
    return [path for path in paths if not _is_generated_report_path(path)]


def _is_generated_report_path(path: str) -> bool:
    normalized = str(path or "").strip().replace("\\", "/")
    if not normalized:
        return False
    parts = [part for part in normalized.split("/") if part]
    for index, part in enumerate(parts[:-1]):
        if part == "assistant-memory" and parts[index + 1] == "compiled":
            return True
    return False


def _is_assistant_memory_compiled_path(path: Path) -> bool:
    parts = path.parts
    for index, part in enumerate(parts[:-1]):
        if part == "assistant-memory" and parts[index + 1] == "compiled":
            return True
    return False


def _make_run_dir(artifacts_root: Path, thread_id: str) -> Path:
    timestamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", thread_id).strip("-") or "adhoc"
    run_dir = artifacts_root / f"{timestamp}-{slug}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def _is_git_repo(workdir: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(workdir), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def _derive_summary(last_message: str, final_agent_message: str, exit_code: int, stderr: str = "") -> str:
    last_summary = _first_nonempty_line(last_message)
    final_summary = _first_nonempty_line(final_agent_message)
    if last_summary and final_summary:
        if final_summary.startswith(last_summary) and len(final_summary) > len(last_summary):
            return final_summary
        return last_summary
    if last_summary:
        return last_summary
    if final_summary:
        return final_summary
    if exit_code != 0:
        classified_reason = _classify_terminal_execution_failure(stderr)
        if classified_reason:
            return classified_reason
        return f"codex exec exited with code {exit_code}"
    return "Codex execution finished without a final message."


def _derive_blockers(exit_code: int, stderr: str) -> list[str]:
    blockers: list[str] = []
    if exit_code != 0:
        classified_reason = _classify_terminal_execution_failure(stderr)
        blockers.append(classified_reason or f"codex exec exited with code {exit_code}")
    stderr_first_line = _first_actionable_stderr_line(stderr)
    if stderr_first_line and stderr_first_line not in blockers:
        blockers.append(stderr_first_line)
    return blockers


def _derive_risks(exit_code: int) -> list[str]:
    if exit_code != 0:
        return [
            "Codex execution failed; inspect stderr and raw output before trusting the run.",
            "Structured fields like files touched still need human review against the raw agent reply.",
        ]
    return [
        "Structured report fields are inferred; use the final Codex output and clean execution trace as the continuity source.",
    ]


def _derive_next_step(exit_code: int, stderr: str = "") -> str:
    if exit_code == 124:
        return "Inspect the partial Codex artifacts, adjust the timeout or task scope, and rerun the cycle."
    if exit_code != 0:
        classified_reason = _classify_terminal_execution_failure(stderr)
        if classified_reason:
            return (
                "Restore network/API reachability for the nested Codex process or run the recovery path "
                "in a host context with OpenAI access, then rerun the cycle."
            )
        return "Inspect the raw Codex artifacts, fix the execution issue, and rerun the cycle."
    return "Continue from the final Codex output and clean execution trace in the same ChatGPT chat."


def _classify_terminal_execution_failure(stderr: str) -> str:
    normalized = str(stderr or "").casefold()
    if not normalized:
        return ""
    mentions_openai_surface = any(
        marker in normalized
        for marker in (
            "api.openai.com",
            "responses_websocket",
            "openai api",
            "chatgpt.com/backend-api/plugins",
        )
    )
    if mentions_openai_surface and any(marker in normalized for marker in _OPENAI_NETWORK_FAILURE_MARKERS):
        return "Codex could not reach the OpenAI API because network or DNS access was unavailable from this process."
    return ""


def _display_command(command: list[str]) -> str:
    return " ".join(json.dumps(part) if " " in part else part for part in command)


def _coerce_timeout_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _build_timeout_stderr(stderr: str | bytes | None, timeout_seconds: float | None) -> str:
    timeout_message = f"codex exec timed out after {timeout_seconds} seconds.\n"
    existing = _coerce_timeout_output(stderr)
    if not existing:
        return timeout_message
    return timeout_message + existing


def _build_progress_stall_stderr(stderr: str | bytes | None, stall_seconds: float | None) -> str:
    stall_message = f"codex exec stalled without new output for {stall_seconds} seconds.\n"
    existing = _coerce_timeout_output(stderr)
    if not existing:
        return stall_message
    return stall_message + existing


def _build_interruption_stderr(stderr: str | bytes | None, interruption_reason: str) -> str:
    action = "paused" if interruption_reason == "pause_requested" else "stopped"
    interruption_message = f"codex exec was {action} by control request.\n"
    existing = _coerce_timeout_output(stderr)
    if not existing:
        return interruption_message
    return interruption_message + existing


def _first_actionable_stderr_line(stderr: str) -> str:
    for raw_line in stderr.splitlines():
        line = raw_line.strip()
        if not line or _is_recurring_codex_environment_noise(line):
            continue
        return line
    return ""


def _is_recurring_codex_environment_noise(line: str) -> bool:
    normalized = line.casefold()
    return any(marker.casefold() in normalized for marker in _RECURRING_STDERR_NOISE_MARKERS) or is_recurring_codex_noise_text(line)


def _merge_unique_items(existing: list[str], new_items: list[str]) -> list[str]:
    merged = list(existing)
    for item in new_items:
        if item not in merged:
            merged.append(item)
    return merged


def _first_nonempty_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""
