from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..defaults import (
    DEFAULT_CHATGPT_MODEL,
    DEFAULT_CHATGPT_REASONING_EFFORT,
    DEFAULT_CODEX_MODEL,
    DEFAULT_CODEX_REASONING_EFFORT,
)
from ..codex_capabilities import codex_exec_capability_guidance_text
from ..executor import compact_codex_thread_after_turn, execute_codex_prompt
from ..profiles import profile_allows
from .types import ChatGPTTurnResult, CodexTurnResult

_OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
_DEFAULT_CHATGPT_MODEL = DEFAULT_CHATGPT_MODEL
_DEFAULT_CHATGPT_REASONING_EFFORT = DEFAULT_CHATGPT_REASONING_EFFORT
_DEFAULT_CODEX_MODEL = DEFAULT_CODEX_MODEL
_DEFAULT_CODEX_REASONING_EFFORT = DEFAULT_CODEX_REASONING_EFFORT
_CODEX_AUTO_COMPACT_ENV_VAR = "BRIDGE_V2_CODEX_AUTO_COMPACT"
_CODEX_COMPACT_TIMEOUT_ENV_VAR = "BRIDGE_V2_CODEX_COMPACT_TIMEOUT_SECONDS"
_CODEX_LOCAL_CAPABILITY_GUIDANCE = f"""# Local Capability Guidance

- Supervisor V2 stays terminal-first; do not make browser or desktop UI state a correctness dependency for ordinary V2 progress.
- The Browser Use Codex plugin is available when this codex exec runtime exposes it for browser tasks that benefit from the Codex in-app browser: local web targets such as localhost, 127.0.0.1, and file:// URLs, current-tab inspection, DOM snapshots, screenshots, and visual verification. Prefer it over ad hoc isolated Playwright when it fits.
- The Computer Use Codex plugin is the required escalation surface for real macOS GUI blockers when this runtime exposes it: Codex.app, the local control panel browser, Safari or Chrome sessions, permission dialogs, allow/OK dialogs, screenshots, app state, clicking, typing, and Accessibility-backed control. If it is not exposed, use equivalent real-session terminal/app automation before reporting the blocker.
- For logged-in web workflows, prefer the user's normal browser session through Computer Use when exposed or app-native automation; use Browser Use mainly for local or in-app browser verification where a separate in-app tab is appropriate.
- Use available local operator surfaces when they materially unblock progress: Computer Use, Accessibility, Apple Events/osascript, cliclick, Keyboard Maestro, screenshots, app inspection, Keychain, Passwords, already-authenticated apps, and Messages for one-time codes.
- If a macOS privacy, Automation, Accessibility, Screen Recording, Full Disk Access, browser-control, or helper permission is missing, open the relevant settings pane with CODEX_HOME/scripts/open_codex_privacy_settings.sh when present, report the exact permission, and continue after it is granted.
- Touch ID, hardware security-key taps, and other physical-presence prompts are blockers after you navigate to the required screen and report the exact prompt.
- Follow the normal confirmation and sensitive-data rules before submitting forms, sending messages, uploading files, changing OS security/privacy settings, changing cloud/account permissions, deleting data, creating persistent access keys, or entering/transmitting passwords, one-time codes, or other sensitive data.
- Never write raw passwords, one-time codes, API keys, tokens, or other secrets into the repo, logs, prompts, commits, or final answer.

{codex_exec_capability_guidance_text()}
"""


def validate_chatgpt_result(payload: dict[str, Any]) -> ChatGPTTurnResult:
    decision = str(payload.get("decision", "")).strip()
    thread_mode = str(payload.get("codex_thread_mode", "")).strip()
    codex_prompt = str(payload.get("codex_prompt", ""))
    summary = str(payload.get("summary", "")).strip()
    reasoning = str(payload.get("reasoning", "")).strip()
    needs_human_reason = str(payload.get("needs_human_reason", ""))
    if decision not in {"run_codex", "pause", "stop", "require_human"}:
        raise ValueError(f"invalid decision: {decision or '<missing>'}")
    if thread_mode not in {"resume_current", "start_fresh"}:
        raise ValueError(f"invalid codex_thread_mode: {thread_mode or '<missing>'}")
    if decision == "run_codex" and not codex_prompt.strip():
        raise ValueError("run_codex requires codex_prompt")
    if decision == "require_human" and not needs_human_reason.strip():
        raise ValueError("require_human requires needs_human_reason")
    if not summary:
        raise ValueError("summary is required")
    if not reasoning:
        raise ValueError("reasoning is required")
    return ChatGPTTurnResult(
        decision=decision,
        codex_thread_mode=thread_mode,
        codex_prompt=codex_prompt,
        summary=summary,
        reasoning=reasoning,
        needs_human_reason=needs_human_reason,
    )


def validate_codex_result(
    payload: dict[str, Any],
    *,
    expected_thread_mode: str,
    current_thread_id: str,
) -> CodexTurnResult:
    status = str(payload.get("status", "")).strip()
    summary = str(payload.get("summary", "")).strip()
    final_output = str(payload.get("final_output", ""))
    observed_thread_id = str(payload.get("observed_thread_id", "")).strip()
    exit_code = int(payload.get("exit_code", 0))
    files_touched = [str(item) for item in payload.get("files_touched", [])]
    checks = [str(item) for item in payload.get("checks", [])]
    blockers = [str(item) for item in payload.get("blockers", [])]
    estimated_context_remaining_percent = int(payload.get("estimated_context_remaining_percent", -1))
    artifacts_dir = str(payload.get("artifacts_dir", "")).strip()
    raw_codex_compaction = payload.get("codex_compaction", {})
    codex_compaction = dict(raw_codex_compaction) if isinstance(raw_codex_compaction, dict) else {}
    if status != "completed":
        raise ValueError(f"codex turn must complete successfully, got {status or '<missing>'}")
    if exit_code != 0:
        raise ValueError(f"codex exit_code must be 0, got {exit_code}")
    if not summary:
        raise ValueError("summary is required")
    if not observed_thread_id:
        raise ValueError("observed_thread_id is required")
    if estimated_context_remaining_percent < -1 or estimated_context_remaining_percent > 100:
        raise ValueError(
            "estimated_context_remaining_percent must be -1 or between 0 and 100"
        )
    if not artifacts_dir:
        raise ValueError("artifacts_dir is required")
    if expected_thread_mode == "resume_current" and current_thread_id and observed_thread_id and observed_thread_id != current_thread_id:
        raise ValueError("resume_current produced a different observed_thread_id")
    if expected_thread_mode == "start_fresh" and current_thread_id and observed_thread_id == current_thread_id:
        raise ValueError("start_fresh did not produce a fresh observed_thread_id")
    return CodexTurnResult(
        status=status,
        summary=summary,
        final_output=final_output,
        observed_thread_id=observed_thread_id,
        exit_code=exit_code,
        files_touched=files_touched,
        checks=checks,
        blockers=blockers,
        estimated_context_remaining_percent=estimated_context_remaining_percent,
        artifacts_dir=artifacts_dir,
        codex_compaction=codex_compaction,
    )


def _codex_prompt_with_local_capabilities(prompt: object) -> str:
    prompt_text = str(prompt or "").strip()
    if "Codex exec capability notes:" in prompt_text:
        return prompt_text + "\n"
    if not prompt_text:
        return _CODEX_LOCAL_CAPABILITY_GUIDANCE.strip() + "\n"
    return f"{_CODEX_LOCAL_CAPABILITY_GUIDANCE.strip()}\n\n# Task Prompt\n\n{prompt_text}\n"


def run_chatgpt_worker(*, worker_input: dict[str, Any], output_path: Path) -> dict[str, Any]:
    fake_response = str(os.environ.get("BRIDGE_V2_FAKE_CHATGPT_RESPONSE", "")).strip()
    if fake_response:
        _maybe_sleep("BRIDGE_V2_FAKE_CHATGPT_SLEEP")
        payload = json.loads(fake_response)
        result = validate_chatgpt_result(payload).as_dict()
        output_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result

    api_key = str(os.environ.get("OPENAI_API_KEY", "")).strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required for the ChatGPT V2 worker")

    model = str(
        worker_input.get("chatgpt_model")
        or os.environ.get("BRIDGE_V2_CHATGPT_MODEL", _DEFAULT_CHATGPT_MODEL)
    ).strip() or _DEFAULT_CHATGPT_MODEL
    reasoning_effort = str(
        worker_input.get("chatgpt_reasoning_effort")
        or os.environ.get("BRIDGE_V2_CHATGPT_REASONING_EFFORT", _DEFAULT_CHATGPT_REASONING_EFFORT)
    ).strip() or _DEFAULT_CHATGPT_REASONING_EFFORT
    request_payload = {
        "model": model,
        "store": False,
        "reasoning": {"effort": reasoning_effort},
        "instructions": (
            "You are the ChatGPT orchestration worker for a terminal-first supervisor. "
            "Return exactly one JSON object that matches the requested schema. "
            "Do not add commentary."
        ),
        "input": json.dumps(worker_input, indent=2),
        "text": {
            "format": {
                "type": "json_schema",
                "name": "bridge_chatgpt_turn",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "decision": {
                            "type": "string",
                            "enum": ["run_codex", "pause", "stop", "require_human"],
                        },
                        "codex_thread_mode": {
                            "type": "string",
                            "enum": ["resume_current", "start_fresh"],
                        },
                        "codex_prompt": {"type": "string"},
                        "summary": {"type": "string", "minLength": 1},
                        "reasoning": {"type": "string", "minLength": 1},
                        "needs_human_reason": {"type": "string"},
                    },
                    "required": [
                        "decision",
                        "codex_thread_mode",
                        "codex_prompt",
                        "summary",
                        "reasoning",
                        "needs_human_reason",
                    ],
                    "additionalProperties": False,
                },
            }
        },
    }
    request = urllib.request.Request(
        _OPENAI_RESPONSES_URL,
        data=json.dumps(request_payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Client-Request-Id": str(uuid4()),
        },
        method="POST",
    )
    organization = str(os.environ.get("OPENAI_ORGANIZATION", "")).strip()
    project = str(os.environ.get("OPENAI_PROJECT", "")).strip()
    if organization:
        request.add_header("OpenAI-Organization", organization)
    if project:
        request.add_header("OpenAI-Project", project)

    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            raw_response = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"responses API request failed with HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"responses API request failed: {exc.reason}") from exc

    parsed_response = json.loads(raw_response)
    response_text = _extract_output_text(parsed_response)
    result = validate_chatgpt_result(json.loads(response_text)).as_dict()
    output_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def run_codex_worker(*, worker_input: dict[str, Any], output_path: Path) -> dict[str, Any]:
    codex_execution_mode = str(worker_input.get("codex_execution_mode", "")).strip() or "cli_only"
    _validate_codex_execution_mode_allowed(codex_execution_mode)
    if (
        str(worker_input.get("thread_mode", "")).strip() == "resume_current"
        and not str(worker_input.get("current_codex_thread_id", "")).strip()
    ):
        raise RuntimeError("resume_current requires current_codex_thread_id")

    fake_result = str(os.environ.get("BRIDGE_V2_FAKE_CODEX_RESULT", "")).strip()
    if fake_result:
        _maybe_sleep("BRIDGE_V2_FAKE_CODEX_SLEEP")
        payload = json.loads(fake_result)
        if not str(payload.get("artifacts_dir", "")).strip():
            payload["artifacts_dir"] = str(output_path.parent)
        result = validate_codex_result(
            payload,
            expected_thread_mode=str(worker_input.get("thread_mode", "")),
            current_thread_id=str(worker_input.get("current_codex_thread_id", "")),
        ).as_dict()
        output_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result

    artifacts_dir = output_path.parent
    prompt_path = artifacts_dir / "codex_prompt.md"
    prompt_path.write_text(
        _codex_prompt_with_local_capabilities(worker_input["codex_prompt"]),
        encoding="utf-8",
    )
    codex_model = str(worker_input.get("codex_model", "")).strip() or _DEFAULT_CODEX_MODEL
    codex_reasoning_effort = str(worker_input.get("codex_reasoning_effort", "")).strip() or _DEFAULT_CODEX_REASONING_EFFORT
    codex_bin = str(os.environ.get("BRIDGE_V2_CODEX_BIN", "codex")).strip() or "codex"
    resume_session_id = (
        str(worker_input.get("current_codex_thread_id", "")).strip()
        if str(worker_input.get("thread_mode", "")) == "resume_current"
        else None
    )
    with _codex_execution_environment(codex_execution_mode) as codex_env:
        report, _metadata = execute_codex_prompt(
            prompt_path=prompt_path,
            workdir=Path(str(worker_input["workspace_path"])),
            artifacts_root=artifacts_dir,
            thread_id=str(worker_input.get("current_codex_thread_id") or worker_input["session_id"]),
            resume_session_id=resume_session_id,
            codex_bin=codex_bin,
            model=codex_model,
            reasoning_effort=codex_reasoning_effort,
            env=codex_env,
            timeout_seconds=float(os.environ.get("BRIDGE_V2_CODEX_TIMEOUT_SECONDS", "1800")),
            verify_resumed_thread_materialized=False,
        )
    result = validate_codex_result(
        {
            "status": "completed" if report.exit_code == 0 else "failed",
            "summary": report.summary,
            "final_output": report.final_agent_message,
            "observed_thread_id": report.observed_codex_thread_id or report.codex_thread_id,
            "exit_code": report.exit_code,
            "files_touched": report.files_touched,
            "checks": report.checks,
            "blockers": report.blockers,
            "estimated_context_remaining_percent": report.estimated_context_remaining_percent,
            "artifacts_dir": report.artifacts_dir,
        },
        expected_thread_mode=str(worker_input.get("thread_mode", "")),
        current_thread_id=str(worker_input.get("current_codex_thread_id", "")),
    ).as_dict()
    if _codex_auto_compact_enabled(codex_execution_mode):
        result["codex_compaction"] = compact_codex_thread_after_turn(
            codex_bin=codex_bin,
            thread_id=str(result["observed_thread_id"]),
            workdir=Path(str(worker_input["workspace_path"])),
            timeout_seconds=float(os.environ.get(_CODEX_COMPACT_TIMEOUT_ENV_VAR, "300")),
        )
        result = validate_codex_result(
            result,
            expected_thread_mode=str(worker_input.get("thread_mode", "")),
            current_thread_id=str(worker_input.get("current_codex_thread_id", "")),
        ).as_dict()
    output_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


@contextmanager
def _codex_execution_environment(execution_mode: str):
    child_env = os.environ.copy()
    if execution_mode != "cli_only":
        yield child_env
        return
    original_values = {
        "BRIDGE_ENABLE_CODEX_APP_INTEGRATION": os.environ.get("BRIDGE_ENABLE_CODEX_APP_INTEGRATION"),
        "BRIDGE_AUTO_OPEN_CODEX_APP_THREADS": os.environ.get("BRIDGE_AUTO_OPEN_CODEX_APP_THREADS"),
    }
    os.environ["BRIDGE_ENABLE_CODEX_APP_INTEGRATION"] = "0"
    os.environ["BRIDGE_AUTO_OPEN_CODEX_APP_THREADS"] = "0"
    child_env["BRIDGE_ENABLE_CODEX_APP_INTEGRATION"] = "0"
    child_env["BRIDGE_AUTO_OPEN_CODEX_APP_THREADS"] = "0"
    try:
        yield child_env
    finally:
        for key, value in original_values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _validate_codex_execution_mode_allowed(execution_mode: str) -> None:
    if execution_mode not in {"cli_only", "allow_app"}:
        raise ValueError(f"invalid codex_execution_mode: {execution_mode}")
    if execution_mode == "allow_app" and not profile_allows("macos-app"):
        raise PermissionError("codex_execution_mode=allow_app requires BRIDGE_PROFILE=macos-app")


def _codex_auto_compact_enabled(execution_mode: str = "cli_only") -> bool:
    normalized = str(os.environ.get(_CODEX_AUTO_COMPACT_ENV_VAR, "")).strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return execution_mode == "allow_app"


def _extract_output_text(payload: dict[str, Any]) -> str:
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text
    output_items = payload.get("output")
    if isinstance(output_items, list):
        parts: list[str] = []
        for item in output_items:
            if not isinstance(item, dict):
                continue
            content_items = item.get("content")
            if not isinstance(content_items, list):
                continue
            for content in content_items:
                if not isinstance(content, dict):
                    continue
                text_value = content.get("text")
                if isinstance(text_value, str) and text_value:
                    parts.append(text_value)
        if parts:
            return "".join(parts)
    raise RuntimeError("responses API returned no parseable output text")


def _maybe_sleep(env_var: str) -> None:
    seconds = str(os.environ.get(env_var, "")).strip()
    if not seconds:
        return
    time.sleep(float(seconds))
