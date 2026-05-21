from __future__ import annotations

from pathlib import Path

from ..live_monitor import format_live_log_line, is_recurring_codex_noise_text
from ..models import ReturnPacket, RunReport, derive_return_packet_id
from .contracts import _chatgpt_supervisor_header_lines

_MAX_COMMAND_SUMMARY_LINES = 20
_MAX_FOOTER_ITEMS = 40
_MAX_RETURN_PACKET_CHARS = 180000
_MAX_COMPACT_TRACE_LINES = 2500
_MAX_COMPACT_TRACE_CHARS = 140000
_MAX_SUMMARY_CHARS = 20000
_MAX_SUPERVISOR_PROMPT_CHARS = 40000


def build_return_packet(report: RunReport) -> ReturnPacket:
    final_output = str(report.final_agent_message or report.summary or "").strip()
    artifacts = [
        path
        for path in [
            report.artifacts_dir,
            report.prompt_path,
            report.raw_output_path,
            report.last_message_path,
            report.stderr_path,
        ]
        if path
    ]
    commands_observed = _command_summary_lines(report.commands_observed)
    return ReturnPacket(
        return_packet_id=derive_return_packet_id(report),
        thread_id=report.thread_id,
        session_id=report.session_id,
        binding_id=report.binding_id,
        run_id=report.run_id or report.artifacts_dir.rsplit("/", 1)[-1],
        summary=report.summary,
        final_output=final_output,
        visible_trace=_visible_trace_lines(report.visible_assistant_trace, final_output),
        commands_observed=commands_observed,
        files_touched=_packet_files_touched(report.files_touched),
        checks=list(report.checks),
        blockers=_packet_detail_items(report.blockers),
        risks=_packet_detail_items(report.risks),
        next_step=_packet_next_step(report.next_step),
        artifacts=artifacts,
        session_live_log_path=report.session_live_log_path,
        workspace_path=report.workspace_path,
        observed_codex_thread_id=report.observed_codex_thread_id,
        thread_action=report.thread_action,
        parent_thread_id=report.parent_thread_id,
        lineage_root_thread_id=report.lineage_root_thread_id,
        lineage_path=list(report.lineage_path),
        workspace_apply_status=report.workspace_apply_status,
        workspace_apply_commands=list(report.workspace_apply_commands),
        workspace_apply_warnings=list(report.workspace_apply_warnings),
        usage=dict(report.usage),
        context_window_tokens=report.context_window_tokens,
        context_used_tokens=report.context_used_tokens,
        estimated_context_remaining_percent=report.estimated_context_remaining_percent,
        context_signal_source=report.context_signal_source,
        context_continuity_percent=report.context_continuity_percent,
        continuity_band=report.continuity_band,
        delivery_status=report.delivery_status,
        delivery_attempt_count=report.delivery_attempt_count,
        budget_snapshot=dict(report.budget_snapshot),
        policy_outcome=report.policy_outcome,
        requested_codex_thread_id=report.requested_codex_thread_id,
        codex_thread_id=report.codex_thread_id,
        thread_operation=report.thread_operation,
        degraded_mode=report.degraded_mode,
        degraded_reasons=list(report.degraded_reasons),
    )


def render_return_packet(packet: ReturnPacket, *, compact: bool = False) -> str:
    codex_trace = _render_compact_codex_trace(packet)
    supervisor_prompt = _render_supervisor_prompt(packet)
    final_output = str(packet.final_output or packet.summary or "none").strip() or "none"
    lines = _chatgpt_supervisor_header_lines(
        session_id=packet.session_id or "none",
        return_packet_id=packet.return_packet_id or "none",
    )
    lines.extend(
        [
            "- stay in this same ChatGPT chat; if your current reply fails, use the chat's retry button instead of opening a new chat",
            "- write your whole actionable reply as the next plain-language prompt for Codex",
            "- make that prompt detailed enough for a full productive run, not a tiny stub or short checklist",
            "- do not emit bridge-control, JSON, YAML, or any transport wrapper",
            "- if the latest Codex output is messy but salvageable, recover the real next step and keep moving instead of starting a meta loop",
            "",
            "Supervisor prompt sent to Codex:",
            "",
            supervisor_prompt or "none",
            "",
            "Here is what Codex wrote:",
            "",
            final_output,
        ]
    )
    if codex_trace:
        lines.extend(
            [
                "",
                "Execution trace excerpt for ChatGPT continuity (use this to understand the real steps, commands, checks, and decisions):",
                "",
            ]
        )
        lines.extend(codex_trace.splitlines())
        lines.extend(_compact_packet_footer_lines(packet))
    else:
        lines.extend(_legacy_packet_detail_lines(packet, include_final_output=False))
    rendered = "\n".join(lines) + "\n"
    if len(rendered) <= _MAX_RETURN_PACKET_CHARS:
        return rendered
    return _render_size_capped_packet(packet)


def _bullet_lines(items: list[str]) -> list[str]:
    if not items:
        return ["- none"]
    visible = items[:_MAX_FOOTER_ITEMS]
    lines = [f"- {item}" for item in visible]
    remaining = len(items) - len(visible)
    if remaining > 0:
        lines.append(f"- … {remaining} more")
    return lines


def _packet_files_touched(paths: list[str]) -> list[str]:
    return [path for path in paths if not _is_generated_packet_path(path)]


def _packet_detail_items(items: list[str]) -> list[str]:
    return [item for item in items if not _is_generated_packet_detail_noise(item)]


def _is_generated_packet_detail_noise(text: str) -> bool:
    normalized = str(text or "").strip()
    if not normalized:
        return False
    if is_recurring_codex_noise_text(normalized):
        return True
    return normalized in {
        "Paste the full raw Codex output back into the mastermind chat for deep analysis.",
        "Structured fields like files touched still need human review against the raw agent reply.",
    }


def _packet_next_step(next_step: str) -> str:
    normalized = str(next_step or "").strip()
    if normalized == "Review the raw Codex artifacts, then paste the return packet and raw output into the mastermind chat.":
        return "Continue from the final Codex output and clean execution trace in the same ChatGPT chat."
    return normalized


def _is_generated_packet_path(path: str) -> bool:
    normalized = str(path or "").strip().replace("\\", "/")
    if not normalized:
        return False
    parts = [part for part in normalized.split("/") if part]
    for index, part in enumerate(parts[:-1]):
        if part == "assistant-memory" and parts[index + 1] == "compiled":
            return True
    return False


def _command_summary_lines(commands: list[dict[str, object]]) -> list[str]:
    if not commands:
        return []
    lines: list[str] = []
    for item in commands:
        command = str(item.get("command", "")).strip()
        if not command:
            continue
        status = str(item.get("status", "") or "unknown")
        exit_code = item.get("exit_code")
        exit_text = "none" if exit_code is None else str(exit_code)
        lines.append(f"{command} | status: {status} | exit: {exit_text}")
        if len(lines) >= _MAX_COMMAND_SUMMARY_LINES:
            break
    remaining = max(len(commands) - len(lines), 0)
    if remaining > 0:
        lines.append(f"… {remaining} more commands")
    return lines


def _visible_trace_lines(trace: list[str], final_output: str) -> list[str]:
    if not trace:
        return []
    normalized_final = str(final_output or "").strip()
    deduped: list[str] = []
    seen: set[str] = set()
    for item in trace:
        text = str(item or "").strip()
        if not text:
            continue
        if normalized_final and text == normalized_final:
            continue
        if text in seen:
            continue
        seen.add(text)
        deduped.append(text)
    return deduped


def _render_compact_codex_trace(packet: ReturnPacket) -> str:
    path = _compact_trace_source_path(packet)
    if path is None:
        return ""
    if not path.exists():
        return ""

    rendered: list[str] = []
    last_blank = False
    total_chars = 0
    truncated = False
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for raw_line in handle:
                for line in format_live_log_line(raw_line, detail="compact"):
                    is_blank = line == ""
                    if is_blank and last_blank:
                        continue
                    candidate_chars = len(line) + 1
                    if len(rendered) >= _MAX_COMPACT_TRACE_LINES or total_chars + candidate_chars > _MAX_COMPACT_TRACE_CHARS:
                        truncated = True
                        break
                    rendered.append(line)
                    last_blank = is_blank
                    total_chars += candidate_chars
                if truncated:
                    break
    except OSError:
        return ""

    while rendered and rendered[0] == "":
        rendered.pop(0)
    while rendered and rendered[-1] == "":
        rendered.pop()
    if truncated:
        rendered.append("… additional live Codex trace omitted for chat delivery size; continue from the final response plus the included execution trace.")
    return "\n".join(rendered)


def _compact_trace_source_path(packet: ReturnPacket) -> Path | None:
    run_dir = _packet_run_dir(packet)
    if run_dir is not None:
        run_live_log = run_dir / "live_output.log"
        return run_live_log if run_live_log.exists() else None
    session_log_path = str(packet.session_live_log_path or "").strip()
    return Path(session_log_path) if session_log_path else None


def _render_supervisor_prompt(packet: ReturnPacket) -> str:
    path = _supervisor_prompt_source_path(packet)
    if path is None or not path.exists():
        return ""
    try:
        return _truncate_text(path.read_text(encoding="utf-8", errors="replace"), _MAX_SUPERVISOR_PROMPT_CHARS)
    except OSError:
        return ""


def _supervisor_prompt_source_path(packet: ReturnPacket) -> Path | None:
    for artifact in packet.artifacts:
        path = Path(str(artifact or ""))
        if path.name in {"prompt.md", "NEXT_PROMPT.md", "codex_prompt.md"}:
            return path
    run_dir = _packet_run_dir(packet)
    if run_dir is None:
        return None
    for name in ("prompt.md", "NEXT_PROMPT.md", "codex_prompt.md"):
        path = run_dir / name
        if path.exists():
            return path
    return None


def _packet_run_dir(packet: ReturnPacket) -> Path | None:
    for artifact in packet.artifacts:
        path = Path(str(artifact or ""))
        if path.name == "live_output.log":
            return path.parent
        if path.is_dir():
            return path
    return None


def _compact_packet_footer_lines(packet: ReturnPacket) -> list[str]:
    return [
        "",
        "Files touched:",
        *_bullet_lines(packet.files_touched),
        "",
        "Checks:",
        *_bullet_lines(packet.checks),
        "",
        "Blockers:",
        *_bullet_lines(packet.blockers),
        "",
        "Risks:",
        *_bullet_lines(packet.risks),
        "",
        f"Recommended next step: {packet.next_step or 'none'}",
    ]


def _legacy_packet_detail_lines(packet: ReturnPacket, *, include_final_output: bool = True) -> list[str]:
    lines: list[str] = []
    if include_final_output:
        lines.extend([packet.final_output or "none", ""])
    lines.extend(
        [
            "Visible Codex trace:",
            *_bullet_lines(packet.visible_trace),
            "",
            "Commands observed:",
            *_bullet_lines(packet.commands_observed),
            "",
            "Files touched:",
            *_bullet_lines(packet.files_touched),
            "",
            "Checks:",
            *_bullet_lines(packet.checks),
            "",
            "Blockers:",
            *_bullet_lines(packet.blockers),
            "",
            "Risks:",
            *_bullet_lines(packet.risks),
            "",
            f"Recommended next step: {packet.next_step or 'none'}",
        ]
    )
    return lines


def _truncate_text(text: str, limit: int) -> str:
    normalized = str(text or "").strip()
    if limit <= 0:
        return ""
    if len(normalized) <= limit:
        return normalized
    marker = "\n[... omitted for chat delivery size limit ...]"
    if limit <= len(marker):
        return marker[:limit]
    return normalized[: limit - len(marker)].rstrip() + marker


def _render_size_capped_packet(packet: ReturnPacket) -> str:
    summary_source = packet.final_output or packet.summary or packet.next_step or "none"
    supervisor_prompt = _render_supervisor_prompt(packet)
    summary_limit = _MAX_SUMMARY_CHARS
    while True:
        lines = _chatgpt_supervisor_header_lines(
            session_id=packet.session_id or "none",
            return_packet_id=packet.return_packet_id or "none",
        )
        lines.extend(
            [
                "- stay in this same ChatGPT chat; if your current reply fails, use the chat's retry button instead of opening a new chat",
                "- write your whole actionable reply as the next plain-language prompt for Codex",
                "- the full Codex trace was larger than this chat can reliably accept, so use the concise status below",
                "- do not emit bridge-control, JSON, YAML, or any transport wrapper",
                "",
                "Supervisor prompt sent to Codex:",
                _truncate_text(supervisor_prompt, min(_MAX_SUPERVISOR_PROMPT_CHARS, 8000)) or "none",
                "",
                "Condensed Codex status:",
                _truncate_text(summary_source, summary_limit) or "none",
                "",
                "Files touched:",
                *_bullet_lines(packet.files_touched),
                "",
                "Checks:",
                *_bullet_lines(packet.checks),
                "",
                "Blockers:",
                *_bullet_lines(packet.blockers),
                "",
                "Risks:",
                *_bullet_lines(packet.risks),
                "",
                f"Recommended next step: {packet.next_step or 'none'}",
            ]
        )
        rendered = "\n".join(lines) + "\n"
        if len(rendered) <= _MAX_RETURN_PACKET_CHARS or summary_limit <= 512:
            return rendered
        overflow = len(rendered) - _MAX_RETURN_PACKET_CHARS
        summary_limit = max(summary_limit - overflow - 256, 512)
