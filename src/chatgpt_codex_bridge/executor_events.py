from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

_DEFAULT_CONTEXT_WINDOW_TOKENS = 200_000
_MODEL_CONTEXT_WINDOW_HINTS = (
    ("gpt-5", 200_000),
    ("codex", 200_000),
    ("o3", 200_000),
    ("o4", 200_000),
)


@dataclass(slots=True)
class ParsedExecEvents:
    observed_codex_thread_id: str = ""
    event_types: list[str] = field(default_factory=list)
    assistant_messages: list[str] = field(default_factory=list)
    commands_observed: list[dict[str, object]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def final_agent_message(self) -> str:
        if not self.assistant_messages:
            return ""
        return self.assistant_messages[-1]


def parse_exec_events(stdout_text: str) -> ParsedExecEvents:
    parsed = ParsedExecEvents()
    command_items: dict[str, dict[str, object]] = {}
    command_order: list[str] = []
    for raw_line in stdout_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        payload = _load_event_payload(line)
        if payload is None:
            continue

        event_type = str(payload.get("type") or payload.get("event") or "")
        if event_type:
            parsed.event_types.append(event_type)

        if event_type == "thread.started":
            parsed.observed_codex_thread_id = str(payload.get("thread_id", ""))
        elif event_type in {"item.started", "item.completed"}:
            item = payload.get("item", {})
            if not isinstance(item, dict):
                continue
            if item.get("type") == "agent_message":
                message = str(item.get("text", "")).strip()
                if message:
                    parsed.assistant_messages.append(message)
            elif item.get("type") == "command_execution":
                item_id = str(item.get("id") or f"command_{len(command_order)}")
                if item_id not in command_items:
                    command_items[item_id] = {
                        "id": item_id,
                        "command": "",
                        "aggregated_output": "",
                        "exit_code": None,
                        "status": "",
                    }
                    command_order.append(item_id)
                command_entry = command_items[item_id]
                if item.get("command") is not None:
                    command_entry["command"] = str(item.get("command", ""))
                if item.get("aggregated_output") is not None:
                    command_entry["aggregated_output"] = str(item.get("aggregated_output", ""))
                if item.get("status") is not None:
                    command_entry["status"] = str(item.get("status", ""))
                if isinstance(item.get("exit_code"), int):
                    command_entry["exit_code"] = int(item["exit_code"])
        elif event_type == "turn.completed":
            usage = payload.get("usage", {})
            if isinstance(usage, dict):
                parsed.usage = {str(key): int(value) for key, value in usage.items() if isinstance(value, int)}

    parsed.commands_observed = [command_items[item_id] for item_id in command_order]
    return parsed


def _estimate_context_metrics(usage: dict[str, int], *, model: str | None) -> dict[str, int | str]:
    if not usage:
        return {
            "context_window_tokens": 0,
            "context_used_tokens": 0,
            "estimated_context_remaining_percent": -1,
            "context_signal_source": "",
        }

    input_tokens = max(int(usage.get("input_tokens", 0) or 0), 0)
    output_tokens = max(int(usage.get("output_tokens", 0) or 0), 0)
    used_tokens = max(input_tokens + output_tokens, input_tokens, output_tokens)
    if used_tokens <= 0:
        return {
            "context_window_tokens": 0,
            "context_used_tokens": 0,
            "estimated_context_remaining_percent": -1,
            "context_signal_source": "",
        }

    context_window_tokens, signal_source = _context_window_tokens_for_model(model)
    bounded_used_tokens = min(used_tokens, context_window_tokens)
    remaining_percent = int(((context_window_tokens - bounded_used_tokens) / context_window_tokens) * 100)
    return {
        "context_window_tokens": context_window_tokens,
        "context_used_tokens": bounded_used_tokens,
        "estimated_context_remaining_percent": max(0, min(100, remaining_percent)),
        "context_signal_source": signal_source,
    }


def _context_window_tokens_for_model(model: str | None) -> tuple[int, str]:
    override = str(os.environ.get("CODEX_CONTEXT_WINDOW_TOKENS", "")).strip()
    if override.isdigit():
        window = int(override)
        if window > 0:
            return window, "env:CODEX_CONTEXT_WINDOW_TOKENS"

    model_name = str(model or "").strip().casefold()
    for hint, window in _MODEL_CONTEXT_WINDOW_HINTS:
        if hint in model_name:
            return window, f"model_hint:{hint}"
    return _DEFAULT_CONTEXT_WINDOW_TOKENS, "default"


def _load_event_payload(line: str) -> dict[str, object] | None:
    for candidate in (line, _repair_truncated_json_line(line)):
        if not candidate:
            continue
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _repair_truncated_json_line(line: str) -> str | None:
    stripped = line.strip()
    if not stripped.startswith("{"):
        return None

    open_tokens: list[str] = []
    in_string = False
    escaped = False
    for character in stripped:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue

        if character == '"':
            in_string = True
            continue
        if character == "{":
            open_tokens.append("}")
            continue
        if character == "[":
            open_tokens.append("]")
            continue
        if character in {"}", "]"}:
            if not open_tokens or open_tokens[-1] != character:
                return None
            open_tokens.pop()

    repaired = stripped
    if in_string:
        if _has_odd_trailing_backslashes(repaired):
            repaired += "\\"
        repaired += '"'
    repaired += "".join(reversed(open_tokens))
    if repaired == stripped:
        return None
    return repaired


def _has_odd_trailing_backslashes(text: str) -> bool:
    trailing = 0
    for character in reversed(text):
        if character != "\\":
            break
        trailing += 1
    return trailing % 2 == 1


def _normalize_thread_turn_text(text: str) -> str:
    return str(text or "").replace("\r\n", "\n").strip()


def _native_turn_until_complete(response: dict[str, object], notifications: list[dict[str, object]]) -> bool:
    del response
    for payload in notifications:
        method = str(payload.get("method", "")).strip()
        params = payload.get("params", {})
        if method == "turn/completed":
            return True
        if method == "thread/status/changed" and str((params or {}).get("status", "")).strip() == "idle":
            return True
    return False


def _select_native_turn_payload(
    thread_snapshot: dict[str, object],
    *,
    turn_id: str,
    prompt_text: str,
) -> dict[str, object] | None:
    thread_payload = thread_snapshot.get("thread") if isinstance(thread_snapshot, dict) else None
    turns = thread_payload.get("turns") if isinstance(thread_payload, dict) else None
    if not isinstance(turns, list):
        return None

    normalized_turn_id = str(turn_id or "").strip()
    if normalized_turn_id:
        for turn in reversed(turns):
            if not isinstance(turn, dict):
                continue
            if str(turn.get("id", "")).strip() == normalized_turn_id:
                return turn

    normalized_prompt = _normalize_thread_turn_text(prompt_text)
    if normalized_prompt:
        for turn in reversed(turns):
            if _resumed_thread_turn_matches(turn, prompt_text=normalized_prompt, final_agent_message=""):
                return turn
    return None


def _extract_native_agent_message(
    turn_payload: dict[str, object] | None,
    notifications: list[dict[str, object]],
) -> str:
    if isinstance(turn_payload, dict):
        items = turn_payload.get("items")
        if isinstance(items, list):
            agent_messages: list[str] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                if str(item.get("type", "")).strip() != "agentMessage":
                    continue
                message = _normalize_thread_turn_text(item.get("text", ""))
                if message:
                    agent_messages.append(message)
            if agent_messages:
                return agent_messages[-1]

    delta_text = "".join(
        str((payload.get("params") or {}).get("delta", ""))
        for payload in notifications
        if str(payload.get("method", "")).strip() == "item/agentMessage/delta"
    )
    return _normalize_thread_turn_text(delta_text)


def _extract_native_command_items(turn_payload: dict[str, object] | None) -> list[dict[str, object]]:
    if not isinstance(turn_payload, dict):
        return []
    items = turn_payload.get("items")
    if not isinstance(items, list):
        return []

    commands: list[dict[str, object]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type", "")).strip()
        if item_type not in {"commandExecution", "command_execution"}:
            continue
        command_entry: dict[str, object] = {
            "id": str(item.get("id") or f"command_{len(commands)}"),
            "command": str(item.get("command", "")),
            "aggregated_output": str(item.get("aggregatedOutput", item.get("aggregated_output", ""))),
            "exit_code": None,
            "status": str(item.get("status", "")),
        }
        exit_code = item.get("exitCode", item.get("exit_code"))
        if isinstance(exit_code, int):
            command_entry["exit_code"] = exit_code
        commands.append(command_entry)
    return commands


def _extract_native_turn_usage(notifications: list[dict[str, object]]) -> dict[str, int]:
    for payload in reversed(notifications):
        if str(payload.get("method", "")).strip() != "turn/completed":
            continue
        params = payload.get("params", {})
        usage = params.get("usage") if isinstance(params, dict) else None
        if not isinstance(usage, dict):
            continue
        return {str(key): int(value) for key, value in usage.items() if isinstance(value, int)}
    return {}


def _build_native_turn_exec_stdout(
    *,
    thread_id: str,
    turn_payload: dict[str, object] | None,
    notifications: list[dict[str, object]],
    fallback_agent_message: str,
) -> str:
    events: list[dict[str, object]] = [{"type": "thread.started", "thread_id": thread_id}]
    commands = _extract_native_command_items(turn_payload)
    for item in commands:
        events.append(
            {
                "type": "item.completed",
                "item": {
                    "id": item.get("id"),
                    "type": "command_execution",
                    "command": item.get("command", ""),
                    "aggregated_output": item.get("aggregated_output", ""),
                    "exit_code": item.get("exit_code"),
                    "status": item.get("status", ""),
                },
            }
        )

    final_agent_message = _normalize_thread_turn_text(fallback_agent_message)
    if final_agent_message:
        events.append(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_agent_native",
                    "type": "agent_message",
                    "text": final_agent_message,
                },
            }
        )

    usage = _extract_native_turn_usage(notifications)
    if usage:
        events.append({"type": "turn.completed", "usage": usage})
    return "\n".join(json.dumps(event) for event in events) + ("\n" if events else "")


def _resumed_thread_turn_matches(
    turn: object,
    *,
    prompt_text: str,
    final_agent_message: str,
) -> bool:
    if not isinstance(turn, dict):
        return False
    items = turn.get("items")
    if not isinstance(items, list):
        return False

    observed_user_texts: list[str] = []
    observed_agent_texts: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type", "")).strip()
        if item_type == "userMessage":
            content_items = item.get("content")
            if not isinstance(content_items, list):
                continue
            for content_item in content_items:
                if not isinstance(content_item, dict):
                    continue
                if str(content_item.get("type", "")).strip() != "text":
                    continue
                normalized = _normalize_thread_turn_text(content_item.get("text", ""))
                if normalized:
                    observed_user_texts.append(normalized)
        elif item_type == "agentMessage":
            normalized = _normalize_thread_turn_text(item.get("text", ""))
            if normalized:
                observed_agent_texts.append(normalized)

    if prompt_text not in observed_user_texts:
        return False
    if final_agent_message and final_agent_message not in observed_agent_texts:
        return False
    return True
