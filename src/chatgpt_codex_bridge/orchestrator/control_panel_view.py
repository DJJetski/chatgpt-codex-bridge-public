from __future__ import annotations

import html
import json
from typing import Any

from ..defaults import (
    CODEX_MODEL_OPTIONS,
    CODEX_REASONING_EFFORT_OPTIONS,
    DEFAULT_CODEX_MODEL,
    DEFAULT_CODEX_REASONING_EFFORT,
)

_DEFAULT_UI_CODEX_MODEL = DEFAULT_CODEX_MODEL
_DEFAULT_UI_REASONING_EFFORT = DEFAULT_CODEX_REASONING_EFFORT
_CODEX_MODEL_OPTIONS = (
    ("", f"Bridge default ({_DEFAULT_UI_CODEX_MODEL})"),
    *CODEX_MODEL_OPTIONS,
)
_CODEX_REASONING_EFFORT_OPTIONS = (
    ("", f"Bridge default ({_DEFAULT_UI_REASONING_EFFORT})"),
    *CODEX_REASONING_EFFORT_OPTIONS,
)


def _browser_label(channel: str) -> str:
    normalized = str(channel or "").strip().casefold()
    if normalized == "chrome":
        return "Google Chrome"
    if normalized == "brave":
        return "Brave Browser"
    if normalized == "msedge":
        return "Microsoft Edge"
    return "browser"


def _normalize_execution_setting(value: Any) -> str:
    return str(value or "").strip()


def _effective_codex_model(value: Any) -> str:
    normalized = _normalize_execution_setting(value)
    return normalized or _DEFAULT_UI_CODEX_MODEL


def _effective_codex_reasoning_effort(value: Any) -> str:
    normalized = _normalize_execution_setting(value).casefold()
    return normalized or _DEFAULT_UI_REASONING_EFFORT


def _render_select_options(options: tuple[tuple[str, str], ...], selected_value: str) -> str:
    selected = _normalize_execution_setting(selected_value)
    rendered: list[str] = []
    for value, label in options:
        option_value = html.escape(value)
        option_label = html.escape(label)
        selected_attr = ' selected="selected"' if selected == value else ""
        rendered.append(f'<option value="{option_value}"{selected_attr}>{option_label}</option>')
    return "".join(rendered)


def _display_int_or_na(value: Any, *, suffix: str = "") -> str:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return "n/a"
    if parsed < 0:
        return "n/a"
    return f"{parsed}{suffix}"


def _display_health_status(value: str) -> str:
    mapping = {
        "healthy": "Healthy",
        "inactive": "Inactive",
        "starting": "Starting",
        "waiting_for_chatgpt": "Waiting for ChatGPT",
        "running_quiet": "Running quietly",
        "post_run_pending": "Post-run pending",
        "blocked": "Blocked",
        "stalled": "Stalled",
        "suspected_hang": "Suspected hang",
    }
    normalized = str(value or "").strip()
    return mapping.get(normalized, normalized or "Unknown")


def _session_is_running_state(*, status: object, supervisor_status: object, auto_run_enabled: bool) -> bool:
    normalized_status = str(status or "").strip().casefold()
    normalized_supervisor = str(supervisor_status or "").strip().casefold()
    if auto_run_enabled:
        return True
    if normalized_supervisor in {"running", "starting"}:
        return True
    return (
        normalized_status == "active"
        and bool(normalized_supervisor)
        and normalized_supervisor not in {"blocked", "failed", "paused", "stopped", "completed", "idle"}
    )


def _session_can_resume_state(*, status: object, supervisor_status: object, auto_run_enabled: bool) -> bool:
    if _session_is_running_state(
        status=status,
        supervisor_status=supervisor_status,
        auto_run_enabled=auto_run_enabled,
    ):
        return False
    normalized_supervisor = str(supervisor_status or "").strip().casefold()
    return normalized_supervisor in {"paused", "blocked", "failed"}


def _render_session_card(session: dict[str, Any]) -> str:
    session_id = html.escape(str(session.get("session_id", "")))
    repo_path = html.escape(str(session.get("repo_path", "")))
    supervisor_status = html.escape(str(session.get("supervisor_status", session.get("loop_state", ""))))
    status = html.escape(str(session.get("status", "")))
    loop_state = html.escape(str(session.get("loop_state", "")))
    budget = html.escape(
        f'{session.get("budget_remaining_minutes", 0)} / {session.get("time_budget_minutes", 0)} min'
    )
    budget_semantics = html.escape(str(session.get("budget_semantics", "")))
    configured_model_raw = _normalize_execution_setting(session.get("codex_model", ""))
    configured_reasoning_raw = _normalize_execution_setting(session.get("codex_reasoning_effort", ""))
    effective_model = html.escape(_effective_codex_model(configured_model_raw))
    effective_reasoning = html.escape(_effective_codex_reasoning_effort(configured_reasoning_raw))
    cycles = html.escape(str(session.get("cycles_completed", 0)))
    human_attention_reason = html.escape(str(session.get("human_attention_reason", "")))
    last_error = html.escape(str(session.get("last_error", "")))
    degraded_mode = html.escape(str(session.get("degraded_mode", "")))
    degraded_reason = html.escape(str(session.get("degraded_reason", "")))
    browser_transport_mode = html.escape(str(session.get("browser_transport_mode", "")))
    policy_decision = session.get("policy_decision", {})
    if not human_attention_reason and isinstance(policy_decision, dict):
        reasons = policy_decision.get("reasons", [])
        if isinstance(reasons, list):
            reason_text = " ".join(str(item) for item in reasons if str(item).strip())
            human_attention_reason = html.escape(reason_text)
    session_status_raw = str(session.get("status", "")).strip().casefold()
    supervisor_status_raw = str(session.get("supervisor_status", session.get("loop_state", ""))).strip().casefold()
    loop_state_raw = str(session.get("loop_state", "")).strip().casefold()
    session_running = _session_is_running_state(
        status=session.get("status", ""),
        supervisor_status=session.get("supervisor_status", session.get("loop_state", "")),
        auto_run_enabled=bool(session.get("auto_run_enabled", False)),
    )
    session_can_resume = _session_can_resume_state(
        status=session.get("status", ""),
        supervisor_status=session.get("supervisor_status", session.get("loop_state", "")),
        auto_run_enabled=bool(session.get("auto_run_enabled", False)),
    )
    latest_run = session.get("latest_run")
    health = session.get("health", {})
    css_class = "session session-running"
    if session_status_raw != "active" or supervisor_status_raw in {"blocked", "failed", "paused", "stopped", "completed", "idle"}:
        css_class = "session session-muted"
    notes = ""
    if human_attention_reason and not session_running:
        notes += f'<p class="hint"><strong>Needs human:</strong> {human_attention_reason}</p>'
    if last_error:
        notes += f'<p class="hint"><strong>Last error:</strong> {last_error}</p>'
    if degraded_mode:
        degraded_text = degraded_mode
        if degraded_reason:
            degraded_text = f"{degraded_text} ({degraded_reason})"
        notes += f'<p class="hint"><strong>Degraded:</strong> {degraded_text}</p>'
    detail_blocks: list[str] = []
    if isinstance(latest_run, dict):
        latest_status = html.escape(str(latest_run.get("status", "")))
        latest_thread = html.escape(
            str(
                latest_run.get("codex_thread_id")
                or latest_run.get("observed_codex_thread_id")
                or session.get("current_codex_thread_id")
                or session.get("current_codex_run_id")
                or ""
            )
        )
        latest_thread_action = html.escape(str(latest_run.get("thread_action", "")))
        latest_thread_operation = html.escape(str(latest_run.get("thread_operation", "")))
        latest_delivery = html.escape(str(latest_run.get("delivery_status", "")))
        latest_packet = html.escape(str(latest_run.get("return_packet_id", "")))
        latest_summary = html.escape(str(latest_run.get("summary", "")))
        latest_next_step = html.escape(str(latest_run.get("next_step", "")))
        latest_context_remaining = html.escape(
            _display_int_or_na(latest_run.get("estimated_context_remaining_percent", -1), suffix="%")
        )
        latest_continuity = html.escape(
            _display_int_or_na(latest_run.get("context_continuity_percent", -1), suffix="%")
        )
        latest_continuity_band = html.escape(str(latest_run.get("continuity_band", "")))
        detail_blocks.append(
            '<div class="hint">'
            f"<strong>Latest Codex run:</strong> {latest_status or 'completed'}<br>"
            f"<strong>Codex thread:</strong> {latest_thread or 'n/a'}<br>"
            f"<strong>Thread action:</strong> {latest_thread_action or 'n/a'}<br>"
            f"<strong>Thread operation:</strong> {latest_thread_operation or 'n/a'}<br>"
            f"<strong>Context left:</strong> {latest_context_remaining}<br>"
            f"<strong>Continuity:</strong> {latest_continuity}"
            f"{f' ({latest_continuity_band})' if latest_continuity_band else ''}<br>"
            f"<strong>Delivery:</strong> {latest_delivery or 'n/a'}<br>"
            f"<strong>Return packet:</strong> {latest_packet or 'n/a'}<br>"
            f"<strong>Summary:</strong> {latest_summary or 'No summary yet.'}<br>"
            f"<strong>Next:</strong> {latest_next_step or 'No next step recorded.'}"
            "</div>"
        )
    health_status = html.escape(_display_health_status(str(health.get("status", ""))))
    health_reason = html.escape(str(health.get("reason", "")))
    supervisor_heartbeat_at = html.escape(str(session.get("supervisor_heartbeat_at", "")))
    phase_started_at = html.escape(str(session.get("phase_started_at", "")))
    last_chat_activity_at = html.escape(str(session.get("last_chat_activity_at", "")))
    last_codex_activity_at = html.escape(str(session.get("last_codex_activity_at", "")))
    last_delivery_at = html.escape(str(session.get("last_delivery_at", "")))
    detail_blocks.append(
        '<div class="hint">'
        f"<strong>Health:</strong> {health_status or 'unknown'}<br>"
        f"<strong>Heartbeat:</strong> {supervisor_heartbeat_at or 'n/a'}<br>"
        f"<strong>Phase started:</strong> {phase_started_at or 'n/a'}<br>"
        f"<strong>Last ChatGPT activity:</strong> {last_chat_activity_at or 'n/a'}<br>"
        f"<strong>Last Codex activity:</strong> {last_codex_activity_at or 'n/a'}<br>"
        f"<strong>Last delivery:</strong> {last_delivery_at or 'n/a'}<br>"
        f"<strong>Health detail:</strong> {health_reason or 'No immediate issues detected.'}"
        "</div>"
    )
    detail_blocks.append(
        '<div class="hint">'
        f"<strong>Codex model:</strong> {effective_model}{'' if configured_model_raw else ' (bridge default)'}<br>"
        f"<strong>Reasoning effort:</strong> {effective_reasoning}{'' if configured_reasoning_raw else ' (bridge default)'}<br>"
        f"<strong>Budget semantics:</strong> {budget_semantics or 'n/a'}<br>"
        f"<strong>Browser transport:</strong> {browser_transport_mode or 'n/a'}<br>"
        "<strong>Chat persistence:</strong> The bridge stays on the bound ChatGPT chat and retries there first."
        "</div>"
    )
    prompt_preview = html.escape(str(session.get("last_productive_prompt", "")))
    if prompt_preview:
        detail_blocks.append(
            '<div class="hint">'
            "<strong>Last ChatGPT -> Codex prompt:</strong>"
            f"<pre>{prompt_preview}</pre>"
            "</div>"
        )
    if isinstance(latest_run, dict):
        final_output_preview = html.escape(str(latest_run.get("final_output_preview", "")))
        if final_output_preview:
            detail_blocks.append(
                '<div class="hint">'
                "<strong>Last Codex -> ChatGPT reply:</strong>"
                f"<pre>{final_output_preview}</pre>"
                "</div>"
            )
    details_html = (
        '<details class="session-details"><summary>Details</summary>'
        + "".join(detail_blocks)
        + "</details>"
    )
    delete_title = "Stop the session before deleting it" if session_running else "Delete session"
    delete_button = (
        f'<button class="danger session-delete" type="button" disabled title="{html.escape(delete_title)}">&times;</button>'
        if session_running
        else f'<button class="danger session-delete" type="button" title="{html.escape(delete_title)}" onclick="controlSession(\'{session_id}\', \'delete\')">&times;</button>'
    )
    control_buttons = f'<button onclick="controlSession(\'{session_id}\', \'start\')">Start</button>'
    if session_running:
        control_buttons = (
            f'<button class="secondary" onclick="controlSession(\'{session_id}\', \'pause\')">Pause</button>'
            f'<button class="danger" onclick="controlSession(\'{session_id}\', \'stop\')">Stop</button>'
        )
    elif session_can_resume:
        control_buttons = (
            f'<button onclick="controlSession(\'{session_id}\', \'resume\')">Start</button>'
            f'<button class="danger" onclick="controlSession(\'{session_id}\', \'stop\')">Stop</button>'
        )
    codex_thread_button = (
        f'<button class="secondary" onclick="controlSession(\'{session_id}\', \'open-codex-thread\')">Open Live Monitor</button>'
    )
    config_form = (
        '<div class="config-grid">'
        f'<label>Codex Model<select id="session-model-{session_id}" class="session-model-select">'
        f"{_render_select_options(_CODEX_MODEL_OPTIONS, configured_model_raw)}"
        "</select></label>"
        f'<label>Reasoning Effort<select id="session-reasoning-{session_id}" class="session-reasoning-select">'
        f"{_render_select_options(_CODEX_REASONING_EFFORT_OPTIONS, configured_reasoning_raw)}"
        "</select></label>"
        f'<button class="secondary" type="button" onclick="applySessionExecutionSettings(\'{session_id}\')">Apply Execution Settings</button>'
        "</div>"
    )
    return (
        f'<section class="{css_class}" data-session-id="{session_id}">'
        f"<header><div><strong>{session_id}</strong><div class=\"hint\">{repo_path}</div></div>"
        f'<div class="session-header-actions"><span class="pill">{supervisor_status}</span>{delete_button}</div></header>'
        "<dl>"
        f"<dt>Status</dt><dd>{status}</dd>"
        f"<dt>Loop</dt><dd>{loop_state}</dd>"
        f"<dt>Budget</dt><dd>{budget}</dd>"
        f"<dt>Cycles</dt><dd>{cycles}</dd>"
        "</dl>"
        f"{notes}"
        f"{config_form}"
        f"{details_html}"
        f'<div class="actions"><button class="secondary" onclick="controlSession(\'{session_id}\', \'open-chat\')">Open Chat</button>'
        f"{codex_thread_button}"
        f'<button class="secondary" onclick="controlSession(\'{session_id}\', \'open-run\')">Artifacts</button>'
        f"{control_buttons}</div>"
        "</section>"
    )


def render_dashboard_html(
    *,
    sessions: list[dict[str, Any]],
    default_browser_channel: str,
    default_codex_model: str,
    default_codex_reasoning_effort: str,
) -> str:
    session_cards = "\n".join(_render_session_card(item) for item in sessions) or "<p>No sessions yet.</p>"
    managed_browser_label = html.escape(_browser_label(default_browser_channel))
    quickstart_model_options = _render_select_options(_CODEX_MODEL_OPTIONS, default_codex_model)
    quickstart_reasoning_options = _render_select_options(
        _CODEX_REASONING_EFFORT_OPTIONS,
        default_codex_reasoning_effort,
    )
    model_options_json = json.dumps(
        [{"value": value, "label": label} for value, label in _CODEX_MODEL_OPTIONS]
    )
    reasoning_options_json = json.dumps(
        [{"value": value, "label": label} for value, label in _CODEX_REASONING_EFFORT_OPTIONS]
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Bridge Control Panel</title>
  <meta http-equiv="Cache-Control" content="no-store, no-cache, must-revalidate, max-age=0">
  <meta http-equiv="Pragma" content="no-cache">
  <meta http-equiv="Expires" content="0">
  <style>
    :root {{
      --bg: #f4f0e8;
      --ink: #1d1a17;
      --muted: #6a6157;
      --panel: rgba(255, 251, 245, 0.92);
      --line: rgba(29, 26, 23, 0.14);
      --accent: #165d52;
      --danger: #9f2d21;
      --shadow: 0 24px 80px rgba(41, 32, 22, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: "Avenir Next", "Segoe UI", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(22, 93, 82, 0.16), transparent 38%),
        linear-gradient(180deg, #fbf7f0 0%, var(--bg) 100%);
    }}
    main {{
      width: min(1120px, calc(100vw - 48px));
      margin: 0 auto;
      padding: 48px 0 72px;
    }}
    h1, h2 {{
      font-family: "Iowan Old Style", "Palatino Linotype", serif;
      font-weight: 600;
      letter-spacing: -0.03em;
      margin: 0 0 12px;
    }}
    .hero {{
      display: grid;
      gap: 16px;
      margin-bottom: 32px;
    }}
    .hero p {{
      max-width: 760px;
      margin: 0;
      color: var(--muted);
      line-height: 1.5;
    }}
    .grid {{
      display: grid;
      grid-template-columns: 1.05fr 1.4fr;
      gap: 24px;
      align-items: start;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 24px;
      box-shadow: var(--shadow);
      padding: 24px;
      backdrop-filter: blur(10px);
    }}
    form {{
      display: grid;
      gap: 12px;
    }}
    label {{
      display: grid;
      gap: 6px;
      font-size: 0.92rem;
      color: var(--muted);
    }}
    input, select, textarea {{
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 12px 14px;
      font: inherit;
      background: rgba(255, 255, 255, 0.78);
      color: var(--ink);
    }}
    textarea {{
      min-height: 180px;
      resize: vertical;
      line-height: 1.45;
    }}
    button {{
      border: 0;
      border-radius: 999px;
      padding: 11px 16px;
      font: inherit;
      background: var(--accent);
      color: #fff;
      cursor: pointer;
      transition: transform 160ms ease, opacity 160ms ease;
    }}
    button:hover {{ transform: translateY(-1px); }}
    button:disabled {{
      cursor: not-allowed;
      opacity: 0.48;
      transform: none;
    }}
    button.secondary {{ background: rgba(29, 26, 23, 0.08); color: var(--ink); }}
    button.danger {{ background: var(--danger); }}
    button.session-delete {{
      min-width: 38px;
      padding: 8px 0;
      line-height: 1;
      font-size: 1rem;
    }}
    .actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 16px;
    }}
    .config-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 10px;
      margin-top: 14px;
    }}
    .config-grid button {{
      align-self: end;
    }}
    .stack {{
      display: grid;
      gap: 16px;
    }}
    .session {{
      padding: 18px;
      border-radius: 20px;
      border: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.72);
      transform: translateY(0);
      animation: rise 280ms ease both;
    }}
    .session.session-no-animate {{ animation: none; }}
    .session.session-muted {{
      opacity: 0.58;
      background: rgba(242, 238, 231, 0.9);
    }}
    .session.session-running {{
      border-color: rgba(22, 93, 82, 0.22);
      background: rgba(255, 255, 255, 0.86);
    }}
    .session header {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 10px;
    }}
    .session-header-actions {{
      display: flex;
      align-items: flex-start;
      gap: 8px;
    }}
    .pill {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 6px 10px;
      border-radius: 999px;
      background: rgba(22, 93, 82, 0.1);
      color: var(--accent);
      font-size: 0.82rem;
    }}
    dl {{
      display: grid;
      grid-template-columns: max-content 1fr;
      gap: 8px 12px;
      margin: 0;
      font-size: 0.92rem;
    }}
    dt {{ color: var(--muted); }}
    .hint {{
      font-size: 0.88rem;
      color: var(--muted);
      margin-top: 10px;
    }}
    pre {{
      margin: 10px 0 0;
      padding: 12px 14px;
      border-radius: 14px;
      background: rgba(29, 26, 23, 0.05);
      border: 1px solid var(--line);
      color: var(--ink);
      white-space: pre-wrap;
      word-break: break-word;
      font: 0.84rem/1.45 "SFMono-Regular", "Menlo", monospace;
    }}
    .session-details {{
      margin-top: 12px;
      border-top: 1px solid var(--line);
      padding-top: 12px;
    }}
    .session-details summary {{
      cursor: pointer;
      color: var(--muted);
      font-size: 0.9rem;
    }}
    @keyframes rise {{
      from {{ opacity: 0; transform: translateY(10px); }}
      to {{ opacity: 1; transform: translateY(0); }}
    }}
    @media (max-width: 860px) {{
      main {{ width: min(100vw - 24px, 680px); padding-top: 28px; }}
      .grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <main>
    <section class="hero">
      <h1>Bridge Control Panel</h1>
      <p>Use one persistent ChatGPT chat, watch the latest prompts and replies here, steer manually when you want, then press Go when the supervisor should continue the same chat autonomously.</p>
    </section>
    <section class="grid">
      <div class="stack">
        <article class="panel">
          <h2>Session</h2>
          <p class="hint">Use this for almost everything. It creates the internal repo/chat mapping and the session in one step, bound to your real ChatGPT chat.</p>
          <form id="quickstart-form">
            <label>Chat URL<input name="chat_url" placeholder="https://chatgpt.com/..."></label>
            <label>Time Budget (minutes)<input name="time_budget_minutes" type="number" min="1" value="30"></label>
            <label>Repo Path (optional override)<input name="repo_path" placeholder="/path/to/project"></label>
            <label>Workspace Path (optional override)<input name="workspace_path" placeholder="/path/to/project"></label>
            <label>Codex Model<select name="codex_model">{quickstart_model_options}</select></label>
            <label>Reasoning Effort<select name="codex_reasoning_effort">{quickstart_reasoning_options}</select></label>
            <label>Optional Codex Thread Title<input name="seed_codex_thread_title" placeholder="Repo gron analysieren"></label>
            <label>Optional Codex Thread ID<input name="seed_codex_thread_id" placeholder="019d..."></label>
            <button type="submit">Create Session</button>
          </form>
          <div class="actions">
            <button class="secondary" type="button" onclick="copyBootstrapPrompt()">Copy Bootstrap Prompt</button>
          </div>
          <p class="hint">You never need to create a binding by hand here. The bridge saves and reuses that internal mapping automatically.</p>
          <label class="hint">Bootstrap Prompt
            <textarea id="bootstrap-prompt" readonly placeholder="Create a quick test session first. The bootstrap prompt will appear here automatically."></textarea>
          </label>
          <p class="hint">The bridge copies a bootstrap prompt for ChatGPT. Open that same chat in {managed_browser_label}, paste the prompt there, wait for ChatGPT's reply, keep that tab available, then click Go on the session card below. If Chrome blocks local page scripting, enable View -> Developer -> Allow JavaScript from Apple Events once.</p>
        </article>
      </div>
      <article class="panel">
        <h2>Sessions</h2>
        <div id="sessions" class="stack">{session_cards}</div>
      </article>
    </section>
  </main>
  <script>
    const codexModelOptions = {model_options_json};
    const codexReasoningOptions = {reasoning_options_json};
    const sessionUiState = new Map();
    let hasLiveRefreshedSessions = false;
    async function postJson(url, payload) {{
      const response = await fetch(url, {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(payload || {{}})
      }});
      if (!response.ok) {{
        const text = await response.text();
        throw new Error(text || response.statusText);
      }}
      return response.json();
    }}
    function formPayload(form) {{
      return Object.fromEntries(new FormData(form).entries());
    }}
    function captureSessionUiState() {{
      document.querySelectorAll('#sessions .session').forEach((card) => {{
        const sessionId = String(card.dataset.sessionId || '');
        if (!sessionId) return;
        const details = card.querySelector('.session-details');
        const modelSelect = card.querySelector('.session-model-select');
        const reasoningSelect = card.querySelector('.session-reasoning-select');
        sessionUiState.set(sessionId, {{
          detailsOpen: Boolean(details && details.open),
          pendingModel: String(modelSelect && modelSelect.value || ''),
          pendingReasoning: String(reasoningSelect && reasoningSelect.value || ''),
        }});
      }});
    }}
    function restoreSessionUiState(container) {{
      container.querySelectorAll('.session').forEach((card) => {{
        const sessionId = String(card.dataset.sessionId || '');
        if (!sessionId) return;
        const savedState = sessionUiState.get(sessionId);
        if (!savedState) return;
        const details = card.querySelector('.session-details');
        if (details) {{
          details.open = Boolean(savedState.detailsOpen);
        }}
        const modelSelect = card.querySelector('.session-model-select');
        if (modelSelect && typeof savedState.pendingModel === 'string') {{
          modelSelect.value = savedState.pendingModel;
        }}
        const reasoningSelect = card.querySelector('.session-reasoning-select');
        if (reasoningSelect && typeof savedState.pendingReasoning === 'string') {{
          reasoningSelect.value = savedState.pendingReasoning;
        }}
      }});
    }}
    async function refreshSessions() {{
      captureSessionUiState();
      const state = await fetch(`/api/state?ts=${{Date.now()}}`, {{ cache: 'no-store' }}).then((response) => response.json());
      const sessionsElement = document.getElementById('sessions');
      sessionsElement.innerHTML = state.sessions.map((session) => renderSession(session, {{
        detailsOpen: Boolean(sessionUiState.get(session.session_id)?.detailsOpen),
        suppressAnimation: hasLiveRefreshedSessions,
      }})).join('') || '<p>No sessions yet.</p>';
      restoreSessionUiState(sessionsElement);
      hasLiveRefreshedSessions = true;
    }}
    function showUiError(error) {{
      const message = error instanceof Error ? error.message : String(error || 'Unknown error');
      window.alert(message);
    }}
    async function writeBootstrapPrompt(promptText) {{
      const target = document.getElementById('bootstrap-prompt');
      target.value = promptText || '';
      if (!promptText) return;
      if (navigator.clipboard && navigator.clipboard.writeText) {{
        try {{
          await navigator.clipboard.writeText(promptText);
        }} catch (error) {{
          console.warn('Clipboard write failed', error);
        }}
      }}
    }}
    async function copyBootstrapPrompt() {{
      const promptText = document.getElementById('bootstrap-prompt').value;
      if (!promptText) return;
      if (navigator.clipboard && navigator.clipboard.writeText) {{
        await navigator.clipboard.writeText(promptText);
      }}
    }}
    function sessionIsMuted(session) {{
      const status = String(session.status || '').toLowerCase();
      const supervisor = String(session.supervisor_status || '').toLowerCase();
      return status !== 'active' || ['blocked', 'failed', 'paused', 'stopped', 'completed', 'idle'].includes(supervisor);
    }}
    function sessionNeedsHuman(session) {{
      const supervisor = String(session.supervisor_status || '').toLowerCase();
      const loop = String(session.loop_state || '').toLowerCase();
      return ['blocked', 'failed'].includes(supervisor) || loop === 'requires_human';
    }}
    function sessionIsRunning(session) {{
      const status = String(session.status || '').toLowerCase();
      const supervisor = String(session.supervisor_status || session.loop_state || '').toLowerCase();
      const autoRun = Boolean(session.auto_run_enabled);
      if (autoRun) return true;
      if (['running', 'starting'].includes(supervisor)) return true;
      return status === 'active' && Boolean(supervisor) && !['blocked', 'failed', 'paused', 'stopped', 'completed', 'idle'].includes(supervisor);
    }}
    function sessionCanResume(session) {{
      if (sessionIsRunning(session)) return false;
      const supervisor = String(session.supervisor_status || session.loop_state || '').toLowerCase();
      return ['paused', 'blocked', 'failed'].includes(supervisor);
    }}
    function escapeHtml(value) {{
      return String(value ?? '').replace(/[&<>"']/g, (char) => ({{
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;'
      }}[char]));
    }}
    function renderSelectOptions(options, currentValue) {{
      return options.map((option) => {{
        const optionValue = String(option.value || '');
        const selected = String(currentValue || '') === optionValue ? ' selected' : '';
        return `<option value="${{escapeHtml(optionValue)}}"${{selected}}>${{escapeHtml(option.label || '')}}</option>`;
      }}).join('');
    }}
    async function applySessionExecutionSettings(sessionId) {{
      const modelElement = document.getElementById(`session-model-${{sessionId}}`);
      const reasoningElement = document.getElementById(`session-reasoning-${{sessionId}}`);
      await postJson(`/api/sessions/${{sessionId}}/execution-config`, {{
        codex_model: modelElement ? modelElement.value : '',
        codex_reasoning_effort: reasoningElement ? reasoningElement.value : '',
      }});
    }}
    function renderSession(session, options = {{}}) {{
      const detailsOpen = Boolean(options.detailsOpen);
      const suppressAnimation = Boolean(options.suppressAnimation);
      const sessionClass = sessionIsMuted(session) ? 'session session-muted' : 'session session-running';
      const sessionClassName = suppressAnimation ? `${{sessionClass}} session-no-animate` : sessionClass;
      const running = sessionIsRunning(session);
      const canResume = sessionCanResume(session);
      const deleteButton = sessionIsRunning(session)
        ? `<button class="danger session-delete" type="button" disabled title="Stop the session before deleting it">&times;</button>`
        : `<button class="danger session-delete" type="button" title="Delete session" onclick="controlSession('${{session.session_id}}', 'delete')">&times;</button>`;
      const humanAttention = !running && session.human_attention_reason
        ? `<p class="hint"><strong>Needs human:</strong> ${{escapeHtml(session.human_attention_reason)}}</p>`
        : '';
      const lastError = session.last_error
        ? `<p class="hint"><strong>Last error:</strong> ${{escapeHtml(session.last_error)}}</p>`
        : '';
      const degradedText = session.degraded_mode
        ? `${{session.degraded_mode}}${{session.degraded_reason ? ` (${{session.degraded_reason}})` : ''}}`
        : '';
      const degraded = degradedText
        ? `<p class="hint"><strong>Degraded:</strong> ${{escapeHtml(degradedText)}}</p>`
        : '';
      const latestRun = session.latest_run || null;
      const health = session.health || {{}};
      const budgetSemantics = session.budget_semantics ? String(session.budget_semantics) : 'n/a';
      const browserTransport = session.browser_transport_mode ? String(session.browser_transport_mode) : 'n/a';
      const configuredModel = String(session.codex_model || '');
      const configuredReasoning = String(session.codex_reasoning_effort || '');
      const effectiveModel = configuredModel || '{_DEFAULT_UI_CODEX_MODEL}';
      const effectiveReasoning = configuredReasoning || '{_DEFAULT_UI_REASONING_EFFORT}';
      const latestContextRemaining = latestRun && Number.isFinite(Number(latestRun.estimated_context_remaining_percent)) && Number(latestRun.estimated_context_remaining_percent) >= 0
        ? `${{latestRun.estimated_context_remaining_percent}}%`
        : 'n/a';
      const latestContinuity = latestRun && Number.isFinite(Number(latestRun.context_continuity_percent)) && Number(latestRun.context_continuity_percent) >= 0
        ? `${{latestRun.context_continuity_percent}}%`
        : 'n/a';
      const latestContinuityBand = latestRun && latestRun.continuity_band
        ? ` (${{latestRun.continuity_band}})`
        : '';
      const latestRunHtml = latestRun ? `
        <div class="hint">
          <strong>Latest Codex run:</strong> ${{latestRun.status || 'completed'}}<br>
          <strong>Codex thread:</strong> ${{escapeHtml(latestRun.codex_thread_id || latestRun.observed_codex_thread_id || session.current_codex_thread_id || session.current_codex_run_id || 'n/a')}}<br>
          <strong>Thread action:</strong> ${{escapeHtml(latestRun.thread_action || 'n/a')}}<br>
          <strong>Context left:</strong> ${{latestContextRemaining}}<br>
          <strong>Continuity:</strong> ${{latestContinuity}}${{latestContinuityBand}}<br>
          <strong>Compaction:</strong> ${{escapeHtml(latestRun.compaction_status || 'n/a')}}<br>
          <strong>Delivery:</strong> ${{escapeHtml(latestRun.delivery_status || 'n/a')}}<br>
          <strong>Return packet:</strong> ${{escapeHtml(latestRun.return_packet_id || 'n/a')}}<br>
          <strong>Summary:</strong> ${{escapeHtml(latestRun.summary || 'No summary yet.')}}<br>
          <strong>Next:</strong> ${{escapeHtml(latestRun.next_step || 'No next step recorded.')}}
        </div>
      ` : '';
      const healthStatusMap = {{
        healthy: 'Healthy',
        inactive: 'Inactive',
        starting: 'Starting',
        waiting_for_chatgpt: 'Waiting for ChatGPT',
        running_quiet: 'Running quietly',
        post_run_pending: 'Post-run pending',
        blocked: 'Blocked',
        stalled: 'Stalled',
        suspected_hang: 'Suspected hang',
        unknown: 'Unknown'
      }};
      const healthStatus = healthStatusMap[String(health.status || 'unknown')] || String(health.status || 'unknown');
      const healthHtml = `
        <div class="hint">
          <strong>Health:</strong> ${{healthStatus}}<br>
          <strong>Heartbeat:</strong> ${{session.supervisor_heartbeat_at || 'n/a'}}<br>
          <strong>Phase started:</strong> ${{session.phase_started_at || 'n/a'}}<br>
          <strong>Last ChatGPT activity:</strong> ${{session.last_chat_activity_at || 'n/a'}}<br>
          <strong>Last Codex activity:</strong> ${{session.last_codex_activity_at || 'n/a'}}<br>
          <strong>Last delivery:</strong> ${{session.last_delivery_at || 'n/a'}}<br>
          <strong>Health detail:</strong> ${{escapeHtml(health.reason || 'No immediate issues detected.')}}
        </div>
      `;
      const metadataHtml = `
        <div class="hint">
          <strong>Codex model:</strong> ${{effectiveModel}}${{configuredModel ? '' : ' (bridge default)'}}<br>
          <strong>Reasoning effort:</strong> ${{effectiveReasoning}}${{configuredReasoning ? '' : ' (bridge default)'}}<br>
          <strong>Budget semantics:</strong> ${{budgetSemantics}}<br>
          <strong>Browser transport:</strong> ${{browserTransport}}<br>
          <strong>Chat persistence:</strong> The bridge stays on the bound ChatGPT chat and retries there first.
        </div>
      `;
      const promptPreviewHtml = session.last_productive_prompt ? `
        <div class="hint">
          <strong>Last ChatGPT -> Codex prompt:</strong>
          <pre>${{escapeHtml(session.last_productive_prompt)}}</pre>
        </div>
      ` : '';
      const finalOutputPreviewHtml = latestRun && latestRun.final_output_preview ? `
        <div class="hint">
          <strong>Last Codex -> ChatGPT reply:</strong>
          <pre>${{escapeHtml(latestRun.final_output_preview)}}</pre>
        </div>
      ` : '';
      const configHtml = `
        <div class="config-grid">
          <label>Codex Model
            <select id="session-model-${{session.session_id}}" class="session-model-select">
              ${{renderSelectOptions(codexModelOptions, configuredModel)}}
            </select>
          </label>
          <label>Reasoning Effort
            <select id="session-reasoning-${{session.session_id}}" class="session-reasoning-select">
              ${{renderSelectOptions(codexReasoningOptions, configuredReasoning)}}
            </select>
          </label>
          <button class="secondary" type="button" onclick="applySessionExecutionSettings('${{session.session_id}}')">Apply Execution Settings</button>
        </div>
      `;
      const detailsHtml = `
        <details class="session-details" ${{detailsOpen ? 'open' : ''}}>
          <summary>Details</summary>
          ${{healthHtml}}
          ${{latestRunHtml}}
          ${{metadataHtml}}
          ${{promptPreviewHtml}}
          ${{finalOutputPreviewHtml}}
        </details>
      `;
      const codexThreadButton = `<button class="secondary" onclick="controlSession('${{session.session_id}}', 'open-codex-thread')">Open Live Monitor</button>`;
      let controlButtons = `
        <button onclick="controlSession('${{session.session_id}}', 'start')">Start</button>
      `;
      if (running) {{
        controlButtons = `
          <button class="secondary" onclick="controlSession('${{session.session_id}}', 'pause')">Pause</button>
          <button class="danger" onclick="controlSession('${{session.session_id}}', 'stop')">Stop</button>
        `;
      }} else if (canResume) {{
        controlButtons = `
          <button onclick="controlSession('${{session.session_id}}', 'resume')">Start</button>
          <button class="danger" onclick="controlSession('${{session.session_id}}', 'stop')">Stop</button>
        `;
      }}
      return `
        <section class="${{sessionClassName}}" data-session-id="${{session.session_id}}">
          <header>
            <div>
              <strong>${{session.session_id}}</strong>
              <div class="hint">${{escapeHtml(session.repo_path || 'No repo path recorded.')}}</div>
            </div>
            <div class="session-header-actions">
              <span class="pill">${{escapeHtml(session.supervisor_status || session.loop_state)}}</span>
              ${{deleteButton}}
            </div>
          </header>
          <dl>
            <dt>Status</dt><dd>${{escapeHtml(session.status)}}</dd>
            <dt>Loop</dt><dd>${{escapeHtml(session.loop_state)}}</dd>
            <dt>Budget</dt><dd>${{session.budget_remaining_minutes}} / ${{session.time_budget_minutes}} min</dd>
            <dt>Cycles</dt><dd>${{session.cycles_completed || 0}}</dd>
          </dl>
          ${{humanAttention}}
          ${{lastError}}
          ${{degraded}}
          ${{configHtml}}
          ${{detailsHtml}}
          <div class="actions">
            <button class="secondary" onclick="controlSession('${{session.session_id}}', 'open-chat')">Open Chat</button>
            ${{codexThreadButton}}
            <button class="secondary" onclick="controlSession('${{session.session_id}}', 'open-run')">Artifacts</button>
            ${{controlButtons}}
          </div>
        </section>
      `;
    }}
    async function controlSession(sessionId, action) {{
      try {{
        if (action === 'open-chat') {{
          await postJson(`/api/sessions/${{sessionId}}/open-chat`, {{}});
          return;
        }}
        if (action === 'open-run') {{
          await postJson(`/api/sessions/${{sessionId}}/open-run`, {{}});
          return;
        }}
        if (action === 'open-codex-thread') {{
          await postJson(`/api/sessions/${{sessionId}}/open-codex-thread`, {{}});
          return;
        }}
        if (action === 'open-codex-app-thread') {{
          await postJson(`/api/sessions/${{sessionId}}/open-codex-app-thread`, {{}});
          return;
        }}
        if (action === 'delete') {{
          if (!window.confirm('Delete this session from the dashboard? Run artifacts stay on disk.')) return;
          await postJson(`/api/sessions/${{sessionId}}/delete`, {{}});
          return;
        }}
        if (action === 'start') await postJson(`/api/sessions/${{sessionId}}/start`, {{ single_cycle: false }});
        if (action === 'pause') await postJson(`/api/sessions/${{sessionId}}/pause`, {{}});
        if (action === 'resume') await postJson(`/api/sessions/${{sessionId}}/resume`, {{ single_cycle: false }});
        if (action === 'stop') await postJson(`/api/sessions/${{sessionId}}/stop`, {{ after_cycle: false }});
      }} catch (error) {{
        showUiError(error);
      }} finally {{
        await refreshSessions();
      }}
    }}
    document.getElementById('quickstart-form').addEventListener('submit', async (event) => {{
      event.preventDefault();
      try {{
        const payload = formPayload(event.target);
        payload.time_budget_minutes = Number(payload.time_budget_minutes || 0);
        const result = await postJson('/api/quickstart', payload);
        await writeBootstrapPrompt(result.bootstrap_prompt || '');
      }} catch (error) {{
        showUiError(error);
      }} finally {{
        await refreshSessions();
      }}
    }});
    setInterval(refreshSessions, 3000);
  </script>
</body>
</html>"""
