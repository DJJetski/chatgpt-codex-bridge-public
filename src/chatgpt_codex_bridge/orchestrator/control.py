from __future__ import annotations

import json
import re
import textwrap

from .models import BridgeControlEnvelope, _normalize_codex_thread_action

_CONTROL_BLOCK_RE = re.compile(r"```bridge-control\s*(\{.*?\})\s*```", re.DOTALL)
_JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)
_TEXT_BLOCK_RE = re.compile(r"```(?:text|prompt|md|markdown)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_RENDERED_CONTROL_LABEL_RE = re.compile(r"(?mi)^bridge-control\s*$")
_THREAD_ACTION_HINT_RE = re.compile(r'(?mi)^\s*(?:codex_thread_action|thread_action)\s*:\s*["\']?([a-zA-Z_]+)["\']?\s*$')
_REQUIRED_FIELDS = {
    "protocol_version",
    "session_id",
    "decision",
    "codex_thread_action",
    "prompt",
    "task_label",
}
_HUMAN_READABLE_TOP_LEVEL_KEYS = _REQUIRED_FIELDS | {
    "human_gate",
    "instruction_updates",
    "time_budget_remaining_hint",
    "notes_for_audit",
    "delivery_attempts",
}


class BridgeControlParseError(ValueError):
    """Raised when a bridge-control block is missing or invalid."""


def extract_bridge_control_envelope(text: str) -> BridgeControlEnvelope:
    payload = _extract_payload(text)
    if payload is None:
        raise BridgeControlParseError("Missing bridge-control block.")

    if not isinstance(payload, dict):
        raise BridgeControlParseError("bridge-control payload must be a JSON object.")

    missing = sorted(field for field in _REQUIRED_FIELDS if field not in payload)
    if missing:
        raise BridgeControlParseError(f"Missing required bridge-control fields: {', '.join(missing)}")

    envelope = BridgeControlEnvelope.from_dict(payload)
    if not envelope.session_id:
        raise BridgeControlParseError("bridge-control session_id must not be empty.")
    if not envelope.decision:
        raise BridgeControlParseError("bridge-control decision must not be empty.")
    if not envelope.task_label:
        raise BridgeControlParseError("bridge-control task_label must not be empty.")
    if not str(envelope.prompt or "").strip():
        raise BridgeControlParseError("bridge-control prompt must not be empty.")
    return envelope


def infer_bridge_control_envelope(
    text: str,
    *,
    session_id: str,
    default_thread_action: str,
    parse_error: str = "",
) -> BridgeControlEnvelope | None:
    raw_candidate = str(text or "").strip()
    if not raw_candidate:
        return None
    first_line = raw_candidate.splitlines()[0].strip()
    if not first_line:
        return None
    lowered_first_line = first_line.casefold()
    if lowered_first_line in {"bridge-control", "json"}:
        return None
    if lowered_first_line.startswith("[repair-") or lowered_first_line.startswith("[recovery-"):
        return None
    if len(raw_candidate) < 8:
        return None
    task_excerpt = _preferred_followup_prompt(raw_candidate) or raw_candidate
    thread_action = _thread_action_hint(raw_candidate) or _normalize_codex_thread_action(default_thread_action) or "same_thread"
    notes_for_audit = [
        "Assistant reply was forwarded in plain-language passthrough mode because no valid bridge-control block was present."
    ]
    if parse_error.strip():
        notes_for_audit.append(f"Original bridge-control parse failure: {parse_error.strip()}")
    return BridgeControlEnvelope(
        protocol_version="1",
        session_id=str(session_id or "").strip(),
        decision="run_codex",
        codex_thread_action=thread_action,
        prompt=raw_candidate,
        task_label=_infer_task_label(task_excerpt),
        notes_for_audit=notes_for_audit,
    )


def _thread_action_hint(text: str) -> str:
    matches = list(_THREAD_ACTION_HINT_RE.finditer(str(text or "")))
    for match in reversed(matches):
        hint = _normalize_codex_thread_action(str(match.group(1) or ""))
        if hint in {"same_thread", "new_thread", "fork_thread"}:
            return hint
    return ""


def _preferred_followup_prompt(text: str) -> str:
    candidate = str(text or "").strip()
    if not candidate:
        return ""
    lowered = candidate.casefold()
    if "schick codex jetzt genau das" in lowered or "send codex this" in lowered or "exactly this" in lowered:
        block = _largest_text_block(candidate)
        if block:
            return block
    return candidate


def _largest_text_block(text: str) -> str:
    blocks = []
    for match in _TEXT_BLOCK_RE.finditer(text):
        block = str(match.group(1) or "").strip()
        if not block:
            continue
        if block.lstrip().startswith("{") or block.casefold().startswith("bridge-control"):
            continue
        blocks.append(block)
    if not blocks:
        return ""
    return max(blocks, key=len)


def _extract_payload(text: str) -> dict | None:
    label_matches = list(_RENDERED_CONTROL_LABEL_RE.finditer(text))
    label_match = label_matches[-1] if label_matches else None
    control_matches = list(_CONTROL_BLOCK_RE.finditer(text))
    control_match = control_matches[-1] if control_matches else None

    if control_match is not None or label_match is not None:
        control_start = control_match.start() if control_match is not None else -1
        label_start = label_match.start() if label_match is not None else -1
        if control_start >= label_start and control_match is not None:
            return _parse_fenced_json_match(control_match, label="bridge-control")
        if label_match is None:
            return None
        return _extract_rendered_control_payload(text, label_match)

    payload = _extract_fenced_json_payload(text=text, pattern=_JSON_BLOCK_RE, label="json")
    if payload is not None:
        return payload

    return _extract_raw_json_payload(text)


def _infer_task_label(text: str) -> str:
    first_meaningful_line = next((line.strip() for line in text.splitlines() if line.strip()), "chatgpt followup")
    slug = re.sub(r"[^a-z0-9]+", "_", first_meaningful_line.casefold()).strip("_")
    if not slug:
        return "chatgpt_followup"
    if slug[0].isdigit():
        slug = f"chatgpt_{slug}"
    return slug[:48] or "chatgpt_followup"


def _extract_rendered_control_payload(text: str, label_match: re.Match[str]) -> dict | None:
    start = text.find("{", label_match.end())
    if start < 0:
        payload = _parse_human_readable_payload(text[label_match.end() :])
        if payload:
            return payload
        raise BridgeControlParseError("Missing bridge-control payload after header.")
    decoder = json.JSONDecoder()
    try:
        payload, _end = decoder.raw_decode(text[start:])
    except json.JSONDecodeError as exc:
        human_payload = _parse_human_readable_payload(text[label_match.end() :])
        if human_payload:
            return human_payload
        raise BridgeControlParseError(f"Invalid bridge-control JSON: {exc}") from exc
    if isinstance(payload, dict):
        return payload
    raise BridgeControlParseError("bridge-control payload must be a JSON object.")


def _extract_fenced_json_payload(*, text: str, pattern: re.Pattern[str], label: str) -> dict | None:
    matches = list(pattern.finditer(text))
    match = matches[-1] if matches else None
    if match is None:
        return None
    return _parse_fenced_json_match(match, label=label)


def _parse_fenced_json_match(match: re.Match[str], *, label: str) -> dict:
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise BridgeControlParseError(f"Invalid {label} JSON: {exc}") from exc
    if isinstance(payload, dict):
        return payload
    raise BridgeControlParseError(f"{label} payload must be a JSON object.")


def _extract_raw_json_payload(text: str) -> dict | None:
    candidate = str(text or "").strip()
    if candidate and "\n" in candidate:
        first_line, remainder = candidate.split("\n", 1)
        if first_line.strip().casefold() == "json":
            candidate = remainder.lstrip()
    if not candidate.startswith("{"):
        return None
    decoder = json.JSONDecoder()
    try:
        payload, end_index = decoder.raw_decode(candidate)
    except json.JSONDecodeError:
        return None
    if candidate[end_index:].strip():
        return None
    if isinstance(payload, dict):
        return payload
    raise BridgeControlParseError("JSON payload must be a JSON object.")


def _parse_human_readable_payload(text: str) -> dict[str, object]:
    payload: dict[str, object] = {}
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].rstrip()
        if not line.strip():
            index += 1
            continue
        match = re.match(r"^([a-zA-Z0-9_]+):\s*(.*)$", line)
        if match is None:
            break
        key, raw_value = match.groups()
        if raw_value == "|":
            block_lines: list[str] = []
            index += 1
            while index < len(lines):
                block_line = lines[index]
                if _looks_like_indented_block_line(block_line):
                    block_lines.append(_normalize_block_line_indentation(block_line))
                    index += 1
                    continue
                if _looks_like_human_readable_top_level_field(block_line):
                    break
                if not block_line.strip():
                    block_lines.append("")
                    index += 1
                    continue
                block_lines.append(block_line.rstrip())
                index += 1
            payload[key] = textwrap.dedent("\n".join(block_lines)).rstrip()
            continue
        payload[key] = _parse_scalar_value(raw_value)
        index += 1
    return payload


def _parse_scalar_value(raw_value: str) -> object:
    value = raw_value.strip()
    if not value:
        return ""
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _looks_like_indented_block_line(line: str) -> bool:
    return bool(line) and line[0].isspace()


def _normalize_block_line_indentation(line: str) -> str:
    prefix_width = 0
    while prefix_width < len(line) and line[prefix_width].isspace():
        prefix_width += 1
    return (" " * prefix_width) + line[prefix_width:]


def _looks_like_human_readable_top_level_field(line: str) -> bool:
    match = re.match(r"^([a-zA-Z0-9_]+):\s*(.*)$", line.rstrip())
    if match is None:
        return False
    return str(match.group(1) or "").strip() in _HUMAN_READABLE_TOP_LEVEL_KEYS


def render_bridge_control_block(envelope: BridgeControlEnvelope) -> str:
    payload = json.dumps(envelope.as_dict(), indent=2)
    return f"```bridge-control\n{payload}\n```"
