from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import webbrowser
from datetime import datetime
import time
from typing import Any
from uuid import uuid4

from .app_paths import bridge_artifacts_dir, bridge_config_dir, bridge_state_dir
from .executor import (
    codex_app_integration_enabled,
    execute_codex_prompt,
    prepare_native_codex_fork_thread,
    prepare_native_codex_start_thread,
)
from .launching import apply_launch_plan, build_launch_plan, render_launch_plan
from .lifecycle import (
    doctor_bridge,
    install_bridge,
    render_lifecycle_result,
    self_test_bridge,
    snapshot_bridge,
    uninstall_bridge,
)
from .models import DecisionContext, PromptRequest, RunReport, now_iso, repo_root
from .profiles import PROFILE_CHOICES, PROFILE_ENV, active_profile, profile_allows
from .orchestrator.browser import (
    RoutedChatAdapter,
    describe_browser_transport,
    normalize_stop_command_event,
    stop_command_already_processed,
)
from .orchestrator.contracts import build_codex_execution_prompt
from .orchestrator.control import BridgeControlParseError, extract_bridge_control_envelope
from .orchestrator.control_panel import (
    ControlPanelServer,
    ControlPanelService,
    _session_health_summary,
    _should_rearm_latest_assistant_message,
)
from .orchestrator.loop import LoopRunner
from .orchestrator.loop_support import _clear_delivery_retry_state
from .orchestrator.packets import build_return_packet, render_return_packet
from .orchestrator.models import ChatBinding, InstructionScopeUpdate, LoopPolicyDecision, OrchestratorSession
from .orchestrator.policy import resolve_instruction_texts
from .orchestrator.state import (
    load_chat_bindings,
    load_orchestrator_policy,
    load_session,
    list_sessions,
    read_orchestrator_policy,
    save_orchestrator_policy,
    save_session,
    session_path,
    upsert_chat_binding,
)
from .orchestrator.supervisor import SupervisorManager, describe_session_lock, terminate_locked_session_supervisor
from .policy import decide_actions
from .prompting import (
    apply_decision_to_request,
    build_reflection_prompt_request,
    build_return_prompt_request,
    render_prompt,
)
from .runtime_cleanup import cleanup_runtime_state
from .storage import (
    append_execution_log,
    enrich_report_with_registry_context,
    load_json,
    save_json,
    update_registry_with_decision,
    update_registry_with_launch_plan,
    update_registry_with_report,
)
from .v2.cli import register_v2_commands

_DEFAULT_ORCHESTRATOR_CODEX_TIMEOUT_SECONDS = 1800.0
_DEFAULT_ORCHESTRATOR_CODEX_PROGRESS_STALL_SECONDS = 300.0
_DEFAULT_ORCHESTRATOR_CODEX_COMPACT_TIMEOUT_SECONDS = 300.0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


def build_parser(prog: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog or _default_prog_name(),
        description=(
            "Supervisor V2 is the default runtime. "
            "Top-level non-v2 commands remain available as legacy compatibility surfaces."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    _register_lifecycle_commands(subparsers)
    register_v2_commands(subparsers)
    _register_orchestrator_state_commands(subparsers)
    _register_orchestrator_runtime_commands(subparsers)
    _register_core_bridge_commands(subparsers)
    return parser


def _default_prog_name() -> str:
    name = Path(sys.argv[0]).name
    if name in {"codex-bridge", "bridgectl"}:
        return name
    return "codex-bridge"


def _orchestrator_codex_timeout_seconds() -> float:
    raw_value = str(os.environ.get("BRIDGE_ORCHESTRATOR_CODEX_TIMEOUT_SECONDS", "")).strip()
    if not raw_value:
        return _DEFAULT_ORCHESTRATOR_CODEX_TIMEOUT_SECONDS
    try:
        parsed = float(raw_value)
    except ValueError:
        return _DEFAULT_ORCHESTRATOR_CODEX_TIMEOUT_SECONDS
    if parsed <= 0:
        return _DEFAULT_ORCHESTRATOR_CODEX_TIMEOUT_SECONDS
    return parsed


def _orchestrator_codex_progress_stall_seconds() -> float:
    raw_value = str(os.environ.get("BRIDGE_ORCHESTRATOR_CODEX_PROGRESS_STALL_SECONDS", "")).strip()
    if not raw_value:
        return _DEFAULT_ORCHESTRATOR_CODEX_PROGRESS_STALL_SECONDS
    try:
        parsed = float(raw_value)
    except ValueError:
        return _DEFAULT_ORCHESTRATOR_CODEX_PROGRESS_STALL_SECONDS
    if parsed <= 0:
        return _DEFAULT_ORCHESTRATOR_CODEX_PROGRESS_STALL_SECONDS
    return parsed


def _orchestrator_codex_compact_timeout_seconds() -> float:
    raw_value = str(os.environ.get("BRIDGE_ORCHESTRATOR_CODEX_COMPACT_TIMEOUT_SECONDS", "")).strip()
    if not raw_value:
        return _DEFAULT_ORCHESTRATOR_CODEX_COMPACT_TIMEOUT_SECONDS
    try:
        parsed = float(raw_value)
    except ValueError:
        return _DEFAULT_ORCHESTRATOR_CODEX_COMPACT_TIMEOUT_SECONDS
    if parsed <= 0:
        return _DEFAULT_ORCHESTRATOR_CODEX_COMPACT_TIMEOUT_SECONDS
    return parsed


def _register_lifecycle_commands(subparsers) -> None:
    install_parser = subparsers.add_parser(
        "install",
        help="Install bridge-owned local resources such as the bundled Codex skill.",
    )
    install_parser.add_argument("--dry-run", action="store_true")
    install_parser.add_argument("--force", action="store_true")
    install_parser.add_argument("--prefix", type=Path)
    install_parser.add_argument("--bridge-home", type=Path)
    install_parser.add_argument("--codex-home", type=Path)
    install_parser.add_argument(
        "--profile",
        choices=PROFILE_CHOICES,
        default="core-safe",
    )
    install_parser.add_argument("--json", action="store_true")
    install_parser.set_defaults(handler=handle_install)

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Check local Bridge/Codex readiness without performing external sends.",
    )
    doctor_parser.add_argument("--bridge-home", type=Path)
    doctor_parser.add_argument("--codex-home", type=Path)
    doctor_parser.add_argument(
        "--profile",
        choices=PROFILE_CHOICES,
        default=None,
    )
    doctor_parser.add_argument("--json", action="store_true")
    doctor_parser.add_argument("--no-redact", action="store_true")
    doctor_parser.set_defaults(handler=handle_doctor)

    self_test_parser = subparsers.add_parser(
        "self-test",
        help="Run local package/resource/store checks without external sends.",
    )
    self_test_parser.add_argument("--bridge-home", type=Path)
    self_test_parser.add_argument("--codex-home", type=Path)
    self_test_parser.add_argument("--json", action="store_true")
    self_test_parser.set_defaults(handler=handle_self_test)

    snapshot_parser = subparsers.add_parser(
        "snapshot",
        help="Print a redacted local environment snapshot for support/debugging.",
    )
    snapshot_parser.add_argument("--bridge-home", type=Path)
    snapshot_parser.add_argument("--codex-home", type=Path)
    snapshot_parser.add_argument("--json", action="store_true")
    snapshot_parser.add_argument("--no-redact", action="store_true")
    snapshot_parser.set_defaults(handler=handle_snapshot)

    uninstall_parser = subparsers.add_parser(
        "uninstall",
        help="Remove only files owned by the bridge install manifest.",
    )
    uninstall_parser.add_argument("--bridge-home", type=Path)
    uninstall_parser.add_argument("--dry-run", action="store_true")
    uninstall_parser.add_argument("--purge", action="store_true")
    uninstall_parser.add_argument("--json", action="store_true")
    uninstall_parser.set_defaults(handler=handle_uninstall)


def _register_orchestrator_state_commands(subparsers) -> None:
    bind_chat_parser = subparsers.add_parser(
        "bind-chat",
        help="Persist a repo-local binding to one ChatGPT Project orchestrator chat.",
    )
    bind_chat_parser.add_argument("--binding-id")
    bind_chat_parser.add_argument("--project-name", default="")
    bind_chat_parser.add_argument("--repo-path", required=True, type=Path)
    bind_chat_parser.add_argument("--workspace-path", type=Path)
    bind_chat_parser.add_argument("--chat-url", required=True)
    bind_chat_parser.add_argument("--browser-profile-path", default="")
    bind_chat_parser.add_argument("--browser-session-handle", default="")
    _add_bindings_argument(bind_chat_parser)
    bind_chat_parser.set_defaults(handler=handle_bind_chat)

    start_session_parser = subparsers.add_parser(
        "start-session",
        help="Create a persisted Orchestrator session with an explicit time budget.",
    )
    start_session_parser.add_argument("--session-id")
    start_session_parser.add_argument("--binding-id", required=True)
    start_session_parser.add_argument("--time-budget-minutes", type=int)
    _add_bindings_argument(start_session_parser)
    _add_policy_argument(start_session_parser)
    _add_sessions_dir_argument(start_session_parser)
    start_session_parser.set_defaults(handler=handle_start_session)

    status_parser = subparsers.add_parser(
        "status",
        help="Show the current binding/session summary for the orchestrator scaffolding.",
    )
    status_parser.add_argument("--binding-id")
    status_parser.add_argument("--session-id")
    _add_bindings_argument(status_parser)
    _add_policy_argument(status_parser)
    _add_sessions_dir_argument(status_parser)
    status_parser.set_defaults(handler=handle_status)

    pause_parser = subparsers.add_parser(
        "pause",
        help="Pause an orchestrator session without deleting local state.",
    )
    pause_parser.add_argument("--session-id", required=True)
    _add_sessions_dir_argument(pause_parser)
    pause_parser.set_defaults(handler=handle_pause)

    stop_parser = subparsers.add_parser(
        "stop",
        help="Stop an orchestrator session immediately or after the current cycle.",
    )
    stop_parser.add_argument("--session-id", required=True)
    stop_parser.add_argument("--after-cycle", action="store_true")
    _add_sessions_dir_argument(stop_parser)
    stop_parser.set_defaults(handler=handle_stop)

    resume_session_parser = subparsers.add_parser(
        "resume-session",
        help="Rearm a paused or blocked session so a later run-loop or supervisor can continue it.",
    )
    resume_session_parser.add_argument("--session-id", required=True)
    _add_sessions_dir_argument(resume_session_parser)
    resume_session_parser.set_defaults(handler=handle_resume_session)

    queue_instruction_parser = subparsers.add_parser(
        "queue-instruction",
        help="Queue a corrective instruction onto a persisted session for the next or every later run.",
    )
    queue_instruction_parser.add_argument("--session-id", required=True)
    queue_instruction_parser.add_argument("--text", required=True)
    queue_instruction_parser.add_argument("--scope", choices=("next_run", "session"), default="next_run")
    queue_instruction_parser.add_argument("--mode", choices=("append", "replace"), default="append")
    _add_sessions_dir_argument(queue_instruction_parser)
    queue_instruction_parser.set_defaults(handler=handle_queue_instruction)


def _register_orchestrator_runtime_commands(subparsers) -> None:
    run_loop_parser = subparsers.add_parser(
        "run-loop",
        help="Run one orchestrator cycle for a bound session.",
    )
    run_loop_parser.add_argument("--session-id", required=True)
    _add_bindings_argument(run_loop_parser)
    _add_policy_argument(run_loop_parser)
    _add_sessions_dir_argument(run_loop_parser)
    _add_artifacts_root_argument(run_loop_parser)
    _add_log_file_argument(run_loop_parser)
    _add_registry_argument(run_loop_parser)
    _add_codex_runtime_arguments(run_loop_parser, headless_default=False)
    run_loop_parser.set_defaults(handler=handle_run_loop)

    run_recovery_parser = subparsers.add_parser(
        "run-recovery",
        help="Run a local Codex recovery pass for a session without reading or posting through the ChatGPT browser transport.",
    )
    run_recovery_parser.add_argument("--session-id", required=True)
    _add_bindings_argument(run_recovery_parser)
    _add_policy_argument(run_recovery_parser)
    _add_sessions_dir_argument(run_recovery_parser)
    _add_artifacts_root_argument(run_recovery_parser)
    _add_log_file_argument(run_recovery_parser)
    _add_registry_argument(run_recovery_parser)
    _add_codex_runtime_arguments(run_recovery_parser)
    run_recovery_parser.set_defaults(handler=handle_run_recovery)

    supervise_session_parser = subparsers.add_parser(
        "supervise-session",
        help="Run a dedicated session supervisor without starting the web control panel.",
    )
    supervise_session_parser.add_argument("--session-id", required=True)
    _add_bindings_argument(supervise_session_parser)
    _add_policy_argument(supervise_session_parser)
    _add_sessions_dir_argument(supervise_session_parser)
    _add_artifacts_root_argument(supervise_session_parser)
    _add_log_file_argument(supervise_session_parser)
    _add_registry_argument(supervise_session_parser)
    _add_codex_runtime_arguments(supervise_session_parser, headless_default=False)
    supervise_session_parser.add_argument("--poll-interval-seconds", type=float, default=2.0)
    supervise_session_parser.set_defaults(handler=handle_supervise_session)

    control_panel_parser = subparsers.add_parser(
        "control-panel",
        help="Run the local web control panel for orchestrator sessions.",
    )
    control_panel_parser.add_argument("--host", default="127.0.0.1")
    control_panel_parser.add_argument("--port", type=int, default=8765)
    _add_bindings_argument(control_panel_parser)
    _add_policy_argument(control_panel_parser)
    _add_sessions_dir_argument(control_panel_parser)
    _add_artifacts_root_argument(control_panel_parser)
    _add_log_file_argument(control_panel_parser)
    _add_registry_argument(control_panel_parser)
    _add_codex_runtime_arguments(control_panel_parser, headless_default=True)
    control_panel_parser.add_argument("--open-browser", action=argparse.BooleanOptionalAction, default=True)
    control_panel_parser.set_defaults(handler=handle_control_panel)

    cleanup_parser = subparsers.add_parser(
        "cleanup-runtime-state",
        help="Remove safe local runtime caches without touching sessions, logs, or durable docs.",
    )
    cleanup_parser.add_argument("--repo-root", type=Path, default=repo_root())
    cleanup_parser.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=False)
    cleanup_parser.set_defaults(handler=handle_cleanup_runtime_state)


def _register_core_bridge_commands(subparsers) -> None:
    decide_parser = subparsers.add_parser("decide", help="Evaluate thread freshness and workspace actions.")
    decide_parser.add_argument("--context", required=True, type=Path)
    decide_parser.add_argument("--registry", required=True, type=Path)
    decide_parser.add_argument("--write", action="store_true")
    decide_parser.set_defaults(handler=handle_decide)

    prompt_parser = subparsers.add_parser("prompt", help="Render a prompt template from a structured request.")
    prompt_parser.add_argument("--request", required=True, type=Path)
    prompt_parser.add_argument("--output", required=True, type=Path)
    prompt_parser.set_defaults(handler=handle_prompt)

    prepare_cycle_parser = subparsers.add_parser(
        "prepare-cycle",
        help="Decide thread action, update the registry, and render the next Codex prompt in one step.",
    )
    prepare_cycle_parser.add_argument("--context", required=True, type=Path)
    prepare_cycle_parser.add_argument("--request", required=True, type=Path)
    prepare_cycle_parser.add_argument("--registry", required=True, type=Path)
    prepare_cycle_parser.add_argument("--output", required=True, type=Path)
    prepare_cycle_parser.add_argument("--write", action="store_true")
    prepare_cycle_parser.set_defaults(handler=handle_prepare_cycle)

    start_cycle_parser = subparsers.add_parser(
        "start-cycle",
        help="Prepare the next prompt and a workspace launch briefing in one step.",
    )
    start_cycle_parser.add_argument("--context", required=True, type=Path)
    start_cycle_parser.add_argument("--request", required=True, type=Path)
    start_cycle_parser.add_argument("--registry", required=True, type=Path)
    start_cycle_parser.add_argument("--prompt-output", required=True, type=Path)
    start_cycle_parser.add_argument("--launch-output", required=True, type=Path)
    start_cycle_parser.add_argument("--write", action="store_true")
    start_cycle_parser.add_argument("--apply-workspace", action="store_true")
    start_cycle_parser.set_defaults(handler=handle_start_cycle)

    log_parser = subparsers.add_parser("log", help="Append a run report and update thread metadata.")
    log_parser.add_argument("--report", required=True, type=Path)
    log_parser.add_argument("--log", required=True, type=Path)
    log_parser.add_argument("--registry", required=True, type=Path)
    log_parser.set_defaults(handler=handle_log)

    prepare_return_parser = subparsers.add_parser(
        "prepare-return",
        help="Render a packet that can be pasted back into the mastermind chat together with the raw Codex output.",
    )
    prepare_return_parser.add_argument("--report", required=True, type=Path)
    prepare_return_parser.add_argument("--output", required=True, type=Path)
    prepare_return_parser.set_defaults(handler=handle_prepare_return)

    reflect_parser = subparsers.add_parser(
        "reflect",
        help="Render a mastermind reflection prompt from the latest run report and durable state hints.",
    )
    reflect_parser.add_argument("--report", required=True, type=Path)
    reflect_parser.add_argument("--output", required=True, type=Path)
    reflect_parser.add_argument("--registry", type=Path)
    reflect_parser.set_defaults(handler=handle_reflect)

    execute_parser = subparsers.add_parser(
        "execute-codex",
        help="Run `codex exec` against a prepared prompt and capture the raw artifacts plus a draft report.",
    )
    execute_parser.add_argument("--prompt", required=True, type=Path)
    execute_parser.add_argument("--workdir", required=True, type=Path)
    execute_parser.add_argument("--artifacts-root", required=True, type=Path)
    execute_parser.add_argument("--thread-id", required=True)
    execute_parser.add_argument("--codex-bin", default="codex")
    execute_parser.add_argument("--model")
    execute_parser.add_argument("--reasoning-effort")
    execute_parser.add_argument("--sandbox")
    execute_parser.add_argument("--profile")
    execute_parser.add_argument("--timeout-seconds", type=float)
    execute_parser.add_argument("--log-file", type=Path)
    execute_parser.add_argument("--registry", type=Path)
    execute_parser.add_argument("--return-output", type=Path)
    execute_parser.set_defaults(handler=handle_execute_codex)


def _add_bindings_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--bindings", type=Path, default=_default_chat_bindings_path())


def _add_policy_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--policy", type=Path, default=_default_orchestrator_policy_path())


def _add_sessions_dir_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--sessions-dir", type=Path, default=_default_sessions_dir())


def _add_artifacts_root_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--artifacts-root", type=Path, default=bridge_artifacts_dir() / "runs")


def _add_log_file_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--log-file", type=Path)


def _add_registry_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--registry", type=Path)


def _add_codex_runtime_arguments(parser: argparse.ArgumentParser, *, headless_default: bool | None = None) -> None:
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--model")
    parser.add_argument("--reasoning-effort")
    parser.add_argument("--sandbox")
    parser.add_argument("--profile")
    if headless_default is not None:
        parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=headless_default)


def _default_chat_bindings_path() -> Path:
    return bridge_state_dir() / "CHAT_BINDINGS.json"


def _default_orchestrator_policy_path() -> Path:
    return bridge_config_dir() / "ORCHESTRATOR_POLICY.json"


def _default_sessions_dir() -> Path:
    return bridge_state_dir() / "sessions"


def _generated_id(prefix: str) -> str:
    timestamp = datetime.now().astimezone().strftime("%Y%m%d%H%M%S")
    return f"{prefix}-{timestamp}-{uuid4().hex[:8]}"


def handle_install(args: argparse.Namespace) -> int:
    result = install_bridge(
        bridge_home_path=args.bridge_home,
        codex_home_path=args.codex_home,
        prefix=args.prefix,
        profile=str(args.profile),
        dry_run=bool(args.dry_run),
        force=bool(args.force),
    )
    render_lifecycle_result(result, json_output=bool(args.json))
    return result.exit_code


def handle_doctor(args: argparse.Namespace) -> int:
    result = doctor_bridge(
        bridge_home_path=args.bridge_home,
        codex_home_path=args.codex_home,
        profile=args.profile,
        redact=not bool(args.no_redact),
    )
    render_lifecycle_result(result, json_output=bool(args.json))
    return result.exit_code


def handle_self_test(args: argparse.Namespace) -> int:
    result = self_test_bridge(bridge_home_path=args.bridge_home, codex_home_path=args.codex_home)
    render_lifecycle_result(result, json_output=bool(args.json))
    return result.exit_code


def handle_snapshot(args: argparse.Namespace) -> int:
    result = snapshot_bridge(
        bridge_home_path=args.bridge_home,
        codex_home_path=args.codex_home,
        redact=not bool(args.no_redact),
    )
    render_lifecycle_result(result, json_output=bool(args.json))
    return result.exit_code


def handle_uninstall(args: argparse.Namespace) -> int:
    result = uninstall_bridge(
        bridge_home_path=args.bridge_home,
        dry_run=bool(args.dry_run),
        purge=bool(args.purge),
    )
    render_lifecycle_result(result, json_output=bool(args.json))
    return result.exit_code


def handle_bind_chat(args: argparse.Namespace) -> int:
    binding = ChatBinding(
        binding_id=args.binding_id or _generated_id("binding"),
        project_name=str(args.project_name or args.repo_path.name),
        repo_path=str(args.repo_path),
        workspace_path=str(args.workspace_path or args.repo_path),
        chat_url=str(args.chat_url),
        browser_profile_path=str(args.browser_profile_path),
        browser_session_handle=str(args.browser_session_handle),
    )
    upsert_chat_binding(args.bindings, binding)
    print(json.dumps(binding.as_dict(), indent=2))
    return 0


def handle_start_session(args: argparse.Namespace) -> int:
    if args.time_budget_minutes is None or args.time_budget_minutes <= 0:
        print(
            json.dumps(
                {
                    "error": "An explicit time budget is required. Pass --time-budget-minutes <minutes>."
                },
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2

    bindings = load_chat_bindings(args.bindings)
    binding = next((item for item in bindings if item.binding_id == args.binding_id), None)
    if binding is None:
        print(json.dumps({"error": f"Unknown binding_id: {args.binding_id}"}, indent=2), file=sys.stderr)
        return 1

    policy = load_orchestrator_policy(args.policy)
    if policy.get("require_explicit_budget", True) and args.time_budget_minutes <= 0:
        print(json.dumps({"error": "Policy requires an explicit time budget."}, indent=2), file=sys.stderr)
        return 2

    session_id = args.session_id or _generated_id("session")
    path = session_path(args.sessions_dir, session_id)
    if path.exists():
        print(json.dumps({"error": f"Session already exists: {session_id}"}, indent=2), file=sys.stderr)
        return 1

    policy_decision = LoopPolicyDecision(
        policy_outcome="allow",
        reasons=["Explicit time budget provided."],
        time_budget_minutes=args.time_budget_minutes,
        time_budget_remaining_minutes=args.time_budget_minutes,
    )
    session = OrchestratorSession(
        session_id=session_id,
        binding_id=binding.binding_id,
        repo_path=binding.repo_path,
        workspace_path=binding.workspace_path,
        chat_url=binding.chat_url,
        time_budget_minutes=args.time_budget_minutes,
        budget_remaining_minutes=args.time_budget_minutes,
        policy_decision=policy_decision,
    )
    save_session(path, session)

    binding.last_session_id = session_id
    binding.updated_at = session.updated_at
    upsert_chat_binding(args.bindings, binding)

    print(
        json.dumps(
            {
                "session_id": session.session_id,
                "binding_id": session.binding_id,
                "time_budget_minutes": session.time_budget_minutes,
                "policy_outcome": session.policy_decision.policy_outcome,
                "session_path": str(path),
            },
            indent=2,
        )
    )
    return 0


def handle_status(args: argparse.Namespace) -> int:
    bindings = load_chat_bindings(args.bindings)
    policy = read_orchestrator_policy(args.policy)
    sessions = list_sessions(args.sessions_dir)
    session_lock_dir = args.sessions_dir.parent / "session_locks"

    selected_session = None
    if args.session_id:
        path = session_path(args.sessions_dir, args.session_id)
        if not path.exists():
            print(json.dumps({"error": f"Unknown session_id: {args.session_id}"}, indent=2), file=sys.stderr)
            return 1
        selected_session = load_session(path)

    selected_binding = None
    binding_id = args.binding_id or (selected_session.binding_id if selected_session else "")
    if binding_id:
        selected_binding = next((item for item in bindings if item.binding_id == binding_id), None)
        if selected_binding is None:
            print(json.dumps({"error": f"Unknown binding_id: {binding_id}"}, indent=2), file=sys.stderr)
            return 1

    if selected_session is None and selected_binding is not None:
        selected_session = next(
            (
                session
                for session in sessions
                if session.binding_id == selected_binding.binding_id and session.status == "active"
            ),
            None,
        )

    session_summaries = []
    for session in sessions:
        session_lock = describe_session_lock(session_lock_dir, session.session_id)
        item = session.as_dict()
        item["health"] = _session_health_summary(session, session_lock=session_lock)
        item["session_lock"] = session_lock
        session_summaries.append(item)

    selected_session_lock = (
        describe_session_lock(session_lock_dir, selected_session.session_id)
        if selected_session
        else None
    )
    selected_session_payload = (
        {
            **selected_session.as_dict(),
            "health": _session_health_summary(selected_session, session_lock=selected_session_lock),
            "session_lock": selected_session_lock,
        }
        if selected_session
        else {}
    )

    payload = {
        "bindings_count": len(bindings),
        "sessions_count": len(sessions),
        "sessions": session_summaries,
        "binding": (
            {
                **selected_binding.as_dict(),
                "browser_transport_mode": describe_browser_transport(selected_binding),
            }
            if selected_binding
            else {}
        ),
        "session": selected_session_payload,
        "policy": policy,
        "session_lock": selected_session_lock,
    }
    print(json.dumps(payload, indent=2))
    return 0


def handle_pause(args: argparse.Namespace) -> int:
    session = load_session(session_path(args.sessions_dir, args.session_id))
    if session.loop_state in {"starting_codex", "posting_return_packet", "waiting_for_chatgpt_response"}:
        session.latest_user_control_command = "pause"
        session.auto_run_enabled = True
        session.supervisor_status = "running"
        reasons = ["Pause requested from the CLI; the active turn will drain before pausing."]
    else:
        session.status = "paused"
        session.loop_state = "paused"
        session.auto_run_enabled = False
        session.supervisor_status = "paused"
        _clear_delivery_retry_state(session)
        reasons = ["Pause requested from the CLI."]
    session.policy_decision = LoopPolicyDecision(
        policy_outcome="paused",
        reasons=reasons,
        time_budget_minutes=session.time_budget_minutes,
        time_budget_remaining_minutes=session.budget_remaining_minutes,
    )
    save_session(session_path(args.sessions_dir, args.session_id), session)
    print(json.dumps({"session_id": session.session_id, "status": session.status}, indent=2))
    return 0


def handle_stop(args: argparse.Namespace) -> int:
    session = load_session(session_path(args.sessions_dir, args.session_id))
    supervisor_termination: dict[str, Any] | None = None
    if args.after_cycle or session.loop_state in {"starting_codex", "posting_return_packet", "waiting_for_chatgpt_response"}:
        session.latest_user_control_command = "stop"
        session.stop_after_cycle_requested = True
        session.loop_state = session.loop_state or "idle"
        session.auto_run_enabled = True
        session.supervisor_status = "running"
    else:
        session.stop_after_cycle_requested = False
        session.status = "completed"
        session.loop_state = "completed"
        session.auto_run_enabled = False
        session.supervisor_status = "stopped"
    save_session(session_path(args.sessions_dir, args.session_id), session)
    if not args.after_cycle and not session.stop_after_cycle_requested:
        supervisor_termination = terminate_locked_session_supervisor(
            args.sessions_dir.parent / "session_locks",
            session.session_id,
        )
    payload = {
        "session_id": session.session_id,
        "status": session.status,
        "stop_after_cycle_requested": session.stop_after_cycle_requested,
    }
    if supervisor_termination is not None:
        payload["supervisor_termination"] = supervisor_termination
    print(
        json.dumps(
            payload,
            indent=2,
        )
    )
    return 0


def handle_resume_session(args: argparse.Namespace) -> int:
    session = load_session(session_path(args.sessions_dir, args.session_id))
    session.status = "active"
    session.loop_state = "idle"
    session.auto_run_enabled = True
    session.supervisor_status = "running"
    session.latest_user_control_command = ""
    session.stop_after_cycle_requested = False
    session.human_attention_reason = ""
    session.last_error = ""
    session.policy_decision = LoopPolicyDecision(
        policy_outcome="allow",
        reasons=["Resume requested; autoloop rearmed for the next run-loop or supervisor pass."],
        time_budget_minutes=session.time_budget_minutes,
        time_budget_remaining_minutes=session.budget_remaining_minutes,
    )
    if _should_rearm_latest_assistant_message(session):
        session.last_seen_chat_message_anchor = ""
        session.latest_assistant_message_id = ""
        session.latest_assistant_message_hash = ""
    save_session(session_path(args.sessions_dir, args.session_id), session)
    print(
        json.dumps(
            {
                "session_id": session.session_id,
                "status": session.status,
                "next_step": (
                    f"Run `python3 -m mastermind_bridge.cli supervise-session --session-id {session.session_id}` "
                    f"or `python3 -m mastermind_bridge.cli run-loop --session-id {session.session_id}` to continue it."
                ),
            },
            indent=2,
        )
    )
    return 0


def handle_queue_instruction(args: argparse.Namespace) -> int:
    session = load_session(session_path(args.sessions_dir, args.session_id))
    updates = [item.as_dict() for item in session.instruction_updates]
    if args.mode == "replace":
        updates = [item for item in updates if str(item.get("scope", "")) != args.scope]
    update = InstructionScopeUpdate(
        scope=str(args.scope),
        mode=str(args.mode),
        text=str(args.text),
    )
    updates.append(update.as_dict())
    session.instruction_updates = [InstructionScopeUpdate.from_dict(item) for item in updates]
    save_session(session_path(args.sessions_dir, args.session_id), session)
    print(
        json.dumps(
            {
                "session_id": session.session_id,
                "scope": update.scope,
                "mode": update.mode,
                "instruction_count": len(session.instruction_updates),
                "text": update.text,
            },
            indent=2,
        )
    )
    return 0


def handle_run_loop(args: argparse.Namespace) -> int:
    runner = _build_loop_runner(
        bindings_path=args.bindings,
        policy_path=args.policy,
        sessions_dir=args.sessions_dir,
        artifacts_root=args.artifacts_root,
        log_file=args.log_file,
        registry_path=args.registry,
        codex_bin=args.codex_bin,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        sandbox=args.sandbox,
        profile=args.profile,
        headless=args.headless,
    )
    try:
        result = runner.run_once(args.session_id)
    finally:
        close = getattr(runner, "close", None)
        if callable(close):
            close()
    print(json.dumps(result, indent=2))
    return 0 if result.get("policy_outcome") not in {"require_human", "budget_exhausted"} else 1


def handle_run_recovery(args: argparse.Namespace) -> int:
    session, binding = _load_session_and_binding(
        session_id=args.session_id,
        sessions_dir=args.sessions_dir,
        bindings_path=args.bindings,
    )
    policy_state = load_orchestrator_policy(args.policy)
    envelope_like, prompt_source = _build_local_recovery_envelope(session)
    instructions = resolve_instruction_texts(session, policy_state)
    runtime_prompts_dir = args.sessions_dir.parent / "runtime_prompts"
    report = _execute_session_prompt(
        prompt=envelope_like["prompt"],
        thread_action=envelope_like["thread_action"],
        session=session,
        binding=binding,
        instructions=instructions,
        runtime_prompts_dir=runtime_prompts_dir,
        sessions_dir=args.sessions_dir,
        policy_path=args.policy,
        artifacts_root=args.artifacts_root,
        log_file=args.log_file,
        registry_path=args.registry,
        codex_bin=args.codex_bin,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        sandbox=args.sandbox,
        profile=args.profile,
        env=_local_recovery_codex_env(session.session_id, thread_action=envelope_like["thread_action"]),
        adapter=None,
    )
    refreshed = load_session(session_path(args.sessions_dir, session.session_id))
    refreshed.last_codex_activity_at = now_iso()
    refreshed.supervisor_heartbeat_at = refreshed.last_codex_activity_at
    refreshed.current_codex_thread_id = (
        report.codex_thread_id
        or report.observed_codex_thread_id
        or refreshed.current_codex_thread_id
        or refreshed.current_codex_run_id
    )
    refreshed.current_codex_run_id = refreshed.current_codex_thread_id or refreshed.current_codex_run_id
    refreshed.last_thread_action = envelope_like["thread_action"]
    refreshed.degraded_mode = str(report.degraded_mode or "")
    refreshed.degraded_reason = "; ".join(str(item) for item in report.degraded_reasons if str(item).strip())
    if report.exit_code == 0:
        refreshed.status = "active"
        refreshed.auto_run_enabled = True
        refreshed.loop_state = "starting_codex"
        refreshed.supervisor_status = "running"
        refreshed.human_attention_reason = ""
        refreshed.last_error = ""
    else:
        blocker_reason = ""
        if report.blockers:
            blocker_reason = str(report.blockers[0]).strip()
        if not blocker_reason:
            blocker_reason = str(report.summary or "").strip()
        if blocker_reason:
            refreshed.status = "blocked"
            refreshed.auto_run_enabled = False
            refreshed.loop_state = "requires_human"
            refreshed.supervisor_status = "blocked"
            refreshed.human_attention_reason = blocker_reason
            refreshed.last_error = blocker_reason
            refreshed.policy_decision = LoopPolicyDecision(
                policy_outcome="require_human",
                reasons=[blocker_reason],
                human_gate_required=True,
                human_gate_reason=blocker_reason,
                human_gate_category="codex_execution_failure",
                time_budget_minutes=refreshed.time_budget_minutes,
                time_budget_remaining_minutes=refreshed.budget_remaining_minutes,
            )
    save_session(session_path(args.sessions_dir, refreshed.session_id), refreshed)
    print(
        json.dumps(
            {
                "session_id": refreshed.session_id,
                "runner_action": "offline_recovery_executed",
                "prompt_source": prompt_source,
                "task_label": envelope_like["task_label"],
                "thread_action": envelope_like["thread_action"],
                "codex_thread_id": refreshed.current_codex_thread_id,
                "run_id": report.run_id,
                "artifacts_dir": report.artifacts_dir,
                "summary": report.summary,
                "exit_code": report.exit_code,
            },
            indent=2,
        )
    )
    return 0 if report.exit_code == 0 else 1


def handle_supervise_session(args: argparse.Namespace) -> int:
    manager = SupervisorManager(
        sessions_dir=args.sessions_dir,
        runner_factory=lambda: _build_loop_runner(
            bindings_path=args.bindings,
            policy_path=args.policy,
            sessions_dir=args.sessions_dir,
            artifacts_root=args.artifacts_root,
            log_file=args.log_file,
            registry_path=args.registry,
            codex_bin=args.codex_bin,
            model=args.model,
            reasoning_effort=args.reasoning_effort,
            sandbox=args.sandbox,
            profile=args.profile,
            headless=args.headless,
        ),
        poll_interval_seconds=float(args.poll_interval_seconds),
    )
    result = manager.ensure_session(args.session_id)
    print(json.dumps(result, indent=2))
    supervisor = manager._supervisors.get(args.session_id)
    if supervisor is None:
        return 1
    try:
        while supervisor.is_alive():
            supervisor.join(timeout=0.5)
    except KeyboardInterrupt:
        manager.stop_session(args.session_id)
        supervisor.join(timeout=2.0)
    return 0


def handle_control_panel(args: argparse.Namespace) -> int:
    bridge_profile = active_profile()
    if not profile_allows("control-panel", bridge_profile.name):
        print(
            json.dumps(
                {
                    "error": "control_panel_profile_required",
                    "profile": bridge_profile.name,
                    "hint": f"Set {PROFILE_ENV}=browser-extra or {PROFILE_ENV}=macos-app to enable the optional control panel.",
                },
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2
    if str(args.host) not in {"127.0.0.1", "localhost", "::1"} and str(
        os.environ.get("BRIDGE_ALLOW_REMOTE_CONTROL_PANEL", "")
    ).strip().casefold() not in {"1", "true", "yes"}:
        print(
            json.dumps(
                {
                    "error": "Refusing non-local control panel bind without BRIDGE_ALLOW_REMOTE_CONTROL_PANEL=1.",
                },
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1
    manager = SupervisorManager(
        sessions_dir=args.sessions_dir,
        runner_factory=lambda: _build_loop_runner(
            bindings_path=args.bindings,
            policy_path=args.policy,
            sessions_dir=args.sessions_dir,
            artifacts_root=args.artifacts_root,
            log_file=args.log_file,
            registry_path=args.registry,
            codex_bin=args.codex_bin,
            model=args.model,
            reasoning_effort=args.reasoning_effort,
            sandbox=args.sandbox,
            profile=args.profile,
            headless=args.headless,
        ),
    )
    service = ControlPanelService(
        bindings_path=args.bindings,
        policy_path=args.policy,
        sessions_dir=args.sessions_dir,
        artifacts_root=args.artifacts_root,
        supervisor_manager=manager,
        default_codex_model=args.model,
        default_codex_reasoning_effort=args.reasoning_effort,
    )
    for session in list_sessions(args.sessions_dir):
        if session.status == "active" and session.auto_run_enabled:
            try:
                manager.ensure_session(session.session_id)
            except RuntimeError:
                continue

    server = ControlPanelServer(service=service, host=str(args.host), port=int(args.port))
    panel_url = f"http://{args.host}:{server.server_address[1]}"
    print(json.dumps({"control_panel_url": panel_url}, indent=2))
    if args.open_browser:
        webbrowser.open(panel_url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def handle_cleanup_runtime_state(args: argparse.Namespace) -> int:
    result = cleanup_runtime_state(Path(args.repo_root), dry_run=bool(args.dry_run))
    print(json.dumps(result.as_dict(), indent=2))
    return 0


def handle_decide(args: argparse.Namespace) -> int:
    context = DecisionContext.from_dict(load_json(args.context))
    decision = decide_actions(context)
    if args.write:
        registry = load_json(args.registry)
        updated = update_registry_with_decision(registry, context, decision)
        save_json(args.registry, updated)
    print(json.dumps(decision.as_dict(), indent=2))
    return 0


def _build_loop_runner(
    *,
    bindings_path: Path,
    policy_path: Path,
    sessions_dir: Path,
    artifacts_root: Path,
    log_file: Path | None,
    registry_path: Path | None,
    codex_bin: str,
    model: str | None,
    reasoning_effort: str | None = None,
    sandbox: str | None,
    profile: str | None,
    headless: bool,
) -> LoopRunner:
    adapter = RoutedChatAdapter(headless=headless)
    runtime_prompts_dir = sessions_dir.parent / "runtime_prompts"

    def _executor(*, prompt: str, thread_action: str, session: OrchestratorSession, binding: ChatBinding, instructions: list[str]) -> RunReport:
        return _execute_session_prompt(
            prompt=prompt,
            thread_action=thread_action,
            session=session,
            binding=binding,
            instructions=instructions,
            runtime_prompts_dir=runtime_prompts_dir,
            sessions_dir=sessions_dir,
            policy_path=policy_path,
            artifacts_root=artifacts_root,
            log_file=log_file,
            registry_path=registry_path,
            codex_bin=codex_bin,
            model=model,
            reasoning_effort=reasoning_effort,
            sandbox=sandbox,
            profile=profile,
            env=None,
            adapter=adapter,
        )

    return LoopRunner(
        adapter=adapter,
        executor=_executor,
        bindings_path=bindings_path,
        policy_path=policy_path,
        sessions_dir=sessions_dir,
    )


def _runtime_prompt_path(runtime_prompts_dir: Path, session_id: str) -> Path:
    return runtime_prompts_dir / session_id / "NEXT_PROMPT.md"


def _load_session_and_binding(*, session_id: str, sessions_dir: Path, bindings_path: Path) -> tuple[OrchestratorSession, ChatBinding]:
    session = load_session(session_path(sessions_dir, session_id))
    bindings = load_chat_bindings(bindings_path)
    binding = next((item for item in bindings if item.binding_id == session.binding_id), None)
    if binding is None:
        raise ValueError(f"Unknown binding_id for session {session_id}: {session.binding_id}")
    return session, binding


def _execute_session_prompt(
    *,
    prompt: str,
    thread_action: str,
    session: OrchestratorSession,
    binding: ChatBinding,
    instructions: list[str],
    runtime_prompts_dir: Path,
    sessions_dir: Path,
    policy_path: Path,
    artifacts_root: Path,
    log_file: Path | None,
    registry_path: Path | None,
    codex_bin: str,
    model: str | None,
    reasoning_effort: str | None,
    sandbox: str | None,
    profile: str | None,
    env: dict[str, str] | None,
    adapter: RoutedChatAdapter | None,
) -> RunReport:
    composed_prompt = build_codex_execution_prompt(
        prompt,
        instructions,
        repo_path=str(binding.repo_path),
        workspace_path=str(binding.workspace_path),
        session_id=session.session_id,
        thread_action=thread_action,
    )
    prompt_file = _runtime_prompt_path(runtime_prompts_dir, session.session_id)
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_file.write_text(composed_prompt, encoding="utf-8")
    stop_phrases = [
        str(item)
        for item in load_orchestrator_policy(policy_path).get("stop_phrases", [])
        if str(item).strip()
    ]

    def _stop_checker() -> str | None:
        latest_session = load_session(session_path(sessions_dir, session.session_id))
        if latest_session.status == "paused":
            latest_session.latest_user_control_command = "pause"
            save_session(session_path(sessions_dir, latest_session.session_id), latest_session)
            return None
        if latest_session.status == "completed":
            latest_session.latest_user_control_command = "stop"
            latest_session.stop_after_cycle_requested = True
            save_session(session_path(sessions_dir, latest_session.session_id), latest_session)
            return None
        if adapter is None:
            return None
        try:
            stop_event = normalize_stop_command_event(adapter.poll_stop_command(latest_session, stop_phrases), stop_phrases)
        except Exception:
            return None
        if stop_event is None or stop_command_already_processed(latest_session, stop_event):
            return None
        stop_command = stop_event["command"]
        latest_session.latest_user_control_command = stop_command
        latest_session.last_seen_user_control_anchor = stop_event["message_anchor"]
        latest_session.latest_user_control_message_hash = stop_event["message_hash"]
        if stop_command == "stop after this cycle":
            latest_session.stop_after_cycle_requested = True
            save_session(session_path(sessions_dir, latest_session.session_id), latest_session)
            return None
        if stop_command == "pause":
            latest_session.auto_run_enabled = True
            latest_session.supervisor_status = "running"
            save_session(session_path(sessions_dir, latest_session.session_id), latest_session)
            return None
        if stop_command == "stop":
            latest_session.stop_after_cycle_requested = True
            latest_session.auto_run_enabled = True
            latest_session.supervisor_status = "running"
            save_session(session_path(sessions_dir, latest_session.session_id), latest_session)
            return None
        return None

    current_codex_thread_id = str(session.current_codex_thread_id or session.current_codex_run_id or "").strip()
    resume_session_id: str | None = None
    requested_codex_thread_id = ""
    parent_codex_thread_id = ""
    thread_operation = ""
    degraded_mode = ""
    degraded_reasons: list[str] = []
    app_integration_enabled = codex_app_integration_enabled()
    last_execution_heartbeat_persist_at = 0.0
    last_execution_activity_persist_at = 0.0

    def _persist_execution_signal(*, record_codex_activity: bool) -> None:
        nonlocal last_execution_heartbeat_persist_at, last_execution_activity_persist_at
        current_time = time.monotonic()
        last_persist_at = (
            last_execution_activity_persist_at if record_codex_activity else last_execution_heartbeat_persist_at
        )
        if (current_time - last_persist_at) < 5.0:
            return
        latest_session = load_session(session_path(sessions_dir, session.session_id))
        latest_session.supervisor_heartbeat_at = now_iso()
        if record_codex_activity:
            latest_session.last_codex_activity_at = latest_session.supervisor_heartbeat_at
        save_session(
            session_path(sessions_dir, latest_session.session_id),
            latest_session,
            touch_updated_at=False,
        )
        if record_codex_activity:
            last_execution_activity_persist_at = current_time
            last_execution_heartbeat_persist_at = current_time
        else:
            last_execution_heartbeat_persist_at = current_time

    def _execution_heartbeat_callback() -> None:
        _persist_execution_signal(record_codex_activity=False)

    def _execution_progress_callback() -> None:
        _persist_execution_signal(record_codex_activity=True)

    if thread_action == "same_thread":
        if not current_codex_thread_id:
            raise RuntimeError("same_thread was requested but no resumable Codex thread is recorded for this session.")
        resume_session_id = current_codex_thread_id
        requested_codex_thread_id = current_codex_thread_id
        thread_operation = "resume_existing_thread"
    elif thread_action == "fork_thread":
        if not current_codex_thread_id:
            raise RuntimeError("fork_thread was requested but no parent Codex thread is recorded for this session.")
        parent_codex_thread_id = current_codex_thread_id
        if app_integration_enabled:
            requested_codex_thread_id = (
                prepare_native_codex_fork_thread(
                    codex_bin=codex_bin,
                    source_thread_id=current_codex_thread_id,
                    workdir=Path(binding.workspace_path),
                    thread_name_hint=f"{Path(binding.workspace_path).name} {session.session_id}",
                )
                or ""
            )
            if not requested_codex_thread_id:
                raise RuntimeError("fork_thread could not be created via the Codex app-server; refusing to fake lineage.")
            resume_session_id = requested_codex_thread_id
            thread_operation = "app_server_fork"
        else:
            thread_operation = "fresh_exec_fork_thread"
    elif thread_action == "new_thread":
        if app_integration_enabled:
            requested_codex_thread_id = (
                prepare_native_codex_start_thread(
                    codex_bin=codex_bin,
                    workdir=Path(binding.workspace_path),
                    thread_name_hint=f"{Path(binding.workspace_path).name} {session.session_id}",
                )
                or ""
            )
            if requested_codex_thread_id:
                resume_session_id = requested_codex_thread_id
                thread_operation = "app_server_start"
            else:
                thread_operation = "cli_fresh_exec"
                degraded_mode = "app_server_start_unavailable"
                degraded_reasons.append("Fell back to fresh `codex exec` because `thread/start` was unavailable or failed.")
        else:
            thread_operation = "fresh_exec_new_thread"
    if requested_codex_thread_id:
        session.current_codex_thread_id = requested_codex_thread_id
        session.current_codex_run_id = requested_codex_thread_id
        save_session(session_path(sessions_dir, session.session_id), session)
    effective_model = str(session.codex_model or model or "").strip() or None
    effective_reasoning_effort = str(session.codex_reasoning_effort or reasoning_effort or "").strip() or None
    report, _execution = execute_codex_prompt(
        prompt_path=prompt_file,
        workdir=Path(binding.workspace_path),
        artifacts_root=artifacts_root,
        thread_id=session.session_id,
        resume_session_id=resume_session_id,
        codex_bin=codex_bin,
        observed_thread_name_hint=(
            f"{Path(binding.workspace_path).name} {session.session_id}" if app_integration_enabled and thread_action == "new_thread" else ""
        ),
        model=effective_model,
        reasoning_effort=effective_reasoning_effort,
        sandbox=sandbox,
        profile=profile,
        env=env,
        preflight_openai_reachability=adapter is None,
        timeout_seconds=_orchestrator_codex_timeout_seconds(),
        progress_stall_seconds=_orchestrator_codex_progress_stall_seconds(),
        compact_after_success=app_integration_enabled,
        compact_timeout_seconds=_orchestrator_codex_compact_timeout_seconds(),
        stop_checker=_stop_checker,
        heartbeat_callback=_execution_heartbeat_callback,
        progress_callback=_execution_progress_callback,
    )
    report.session_id = session.session_id
    report.bridge_session_id = session.session_id
    report.binding_id = binding.binding_id
    report.workspace_path = str(binding.workspace_path)
    report.thread_action = thread_action
    report.parent_thread_id = report.parent_thread_id or parent_codex_thread_id
    report.requested_codex_thread_id = requested_codex_thread_id
    report.codex_thread_id = report.observed_codex_thread_id or requested_codex_thread_id
    report.thread_operation = thread_operation
    report.degraded_mode = degraded_mode
    report.degraded_reasons = list(degraded_reasons)
    if registry_path:
        registry = load_json(registry_path)
        updated = update_registry_with_report(registry, report)
        report = enrich_report_with_registry_context(updated, report)
        save_json(registry_path, updated)
    if report.artifacts_dir:
        save_json(Path(report.artifacts_dir) / "run_report.json", report.as_dict())
    if log_file:
        append_execution_log(log_file, report)
    return report


def _build_local_recovery_envelope(session: OrchestratorSession) -> tuple[dict[str, str], str]:
    thread_action = _local_recovery_thread_action(session)
    queued_next_run = [
        item.text.strip()
        for item in session.instruction_updates
        if str(item.scope) == "next_run" and str(item.text).strip()
    ]
    if queued_next_run:
        repo_path = str(session.workspace_path or session.repo_path).strip()
        prompt = "\n".join(
            [
                "Continue working only in the bound repo:",
                repo_path,
                "",
                "This is a local recovery Codex execution because the ChatGPT browser transport for this session is currently blocked.",
                "Do not wait for browser recovery before taking the next safe repo, runtime, or product step.",
                "Use the persisted orchestrator instructions attached below as the primary frontier for this run.",
                "Inspect the latest repo-native truth and latest relevant run artifacts, then implement and verify the largest safe forward step.",
                "Keep documentation changes secondary and only touch docs after durable truth really changed.",
            ]
        )
        return {
            "prompt": prompt,
            "thread_action": thread_action,
            "task_label": "offline_recovery",
        }, "queued_instruction"

    candidate = str(session.in_progress_assistant_text or "").strip()
    if candidate:
        try:
            envelope = extract_bridge_control_envelope(candidate)
        except BridgeControlParseError:
            envelope = None
        if envelope is not None and envelope.session_id == session.session_id and envelope.decision == "run_codex":
            return {
                "prompt": envelope.prompt,
                "thread_action": thread_action,
                "task_label": envelope.task_label,
            }, "stored_assistant"

    repo_path = str(session.workspace_path or session.repo_path).strip()
    prompt = "\n".join(
        [
            "Continue working only in the bound repo:",
            repo_path,
            "",
            "This is a local recovery Codex execution because the ChatGPT browser transport for this session is currently blocked.",
            "Re-establish the next honest repo, runtime, or product frontier from the current repo state and latest run artifacts.",
            "Take the largest safe forward step you can verify locally, and update docs only after durable truth changes.",
        ]
    )
    return {
        "prompt": prompt,
        "thread_action": thread_action,
        "task_label": "offline_recovery",
    }, "synthetic"


def _local_recovery_thread_action(session: OrchestratorSession) -> str:
    if str(session.current_codex_thread_id or session.current_codex_run_id or "").strip():
        return "same_thread"
    return "new_thread"


def _local_recovery_codex_env(session_id: str, *, thread_action: str) -> dict[str, str] | None:
    if thread_action == "same_thread" or codex_app_integration_enabled():
        return None
    codex_home = Path("/tmp") / "bridge-codex-home" / str(session_id or "session").strip()
    codex_home.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    return env


def handle_prompt(args: argparse.Namespace) -> int:
    request = PromptRequest.from_dict(load_json(args.request))
    prompt = render_prompt(request)
    args.output.write_text(prompt)
    print(json.dumps({"output": str(args.output), "mode": request.mode}, indent=2))
    return 0


def handle_prepare_cycle(args: argparse.Namespace) -> int:
    context = DecisionContext.from_dict(load_json(args.context))
    decision = decide_actions(context)
    request = PromptRequest.from_dict(load_json(args.request))
    request = apply_decision_to_request(request, context, decision)
    prompt = render_prompt(request)
    args.output.write_text(prompt)
    if args.write:
        registry = load_json(args.registry)
        updated = update_registry_with_decision(registry, context, decision)
        save_json(args.registry, updated)
    print(
        json.dumps(
            {
                "thread_action": decision.thread_action,
                "context_continuity_percent": decision.context_continuity_percent,
                "continuity_band": decision.continuity_band,
                "selected_thread_id": decision.selected_thread_id,
                "prompt_mode": request.mode,
                "output": str(args.output),
            },
            indent=2,
        )
    )
    return 0


def handle_start_cycle(args: argparse.Namespace) -> int:
    context = DecisionContext.from_dict(load_json(args.context))
    decision = decide_actions(context)
    request = PromptRequest.from_dict(load_json(args.request))
    request = apply_decision_to_request(request, context, decision)
    prompt = render_prompt(request)
    args.prompt_output.write_text(prompt)

    registry = load_json(args.registry)
    updated = registry
    if args.write:
        updated = update_registry_with_decision(registry, context, decision)

    launch_plan = build_launch_plan(context, decision, updated, args.prompt_output)
    if args.apply_workspace:
        launch_plan = apply_launch_plan(context, decision, launch_plan)
    if args.write:
        updated = update_registry_with_launch_plan(updated, launch_plan)
        save_json(args.registry, updated)
    args.launch_output.write_text(render_launch_plan(launch_plan))

    status_code = 1 if launch_plan.workspace_apply_status == "failed" else 0
    print(
        json.dumps(
            {
                "thread_action": decision.thread_action,
                "worktree_action": decision.worktree_action,
                "branch_action": decision.branch_action,
                "context_continuity_percent": decision.context_continuity_percent,
                "continuity_band": decision.continuity_band,
                "selected_thread_id": decision.selected_thread_id,
                "prompt_mode": request.mode,
                "prompt_output": str(args.prompt_output),
                "launch_output": str(args.launch_output),
                "commands": launch_plan.commands,
                "warnings": launch_plan.warnings,
                "workspace_apply_status": launch_plan.workspace_apply_status,
                "workspace_apply_commands": launch_plan.workspace_apply_commands,
                "workspace_apply_warnings": launch_plan.workspace_apply_warnings,
            },
            indent=2,
        )
    )
    return status_code


def handle_log(args: argparse.Namespace) -> int:
    report = RunReport.from_dict(load_json(args.report))
    registry = load_json(args.registry)
    updated = update_registry_with_report(registry, report)
    report = enrich_report_with_registry_context(updated, report)
    save_json(args.report, report.as_dict())
    append_execution_log(args.log, report)
    save_json(args.registry, updated)
    print(json.dumps({"thread_id": report.thread_id, "summary": report.summary}, indent=2))
    return 0


def handle_prepare_return(args: argparse.Namespace) -> int:
    report = RunReport.from_dict(load_json(args.report))
    packet = build_return_packet(report)
    args.output.write_text(render_return_packet(packet), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "thread_id": report.thread_id}, indent=2))
    return 0


def handle_reflect(args: argparse.Namespace) -> int:
    report = RunReport.from_dict(load_json(args.report))
    state_files: list[str] = []
    if args.registry:
        registry = load_json(args.registry)
        report = enrich_report_with_registry_context(registry, report)
        project = registry.get("project", {})
        if isinstance(project, dict):
            state_files = [str(item) for item in project.get("canonical_state_files", [])]
    request = build_reflection_prompt_request(
        report,
        state_files=state_files,
        report_path=str(args.report),
    )
    prompt = render_prompt(request)
    args.output.write_text(prompt)
    print(json.dumps({"output": str(args.output), "thread_id": report.thread_id}, indent=2))
    return 0


def handle_execute_codex(args: argparse.Namespace) -> int:
    report, execution = execute_codex_prompt(
        prompt_path=args.prompt,
        workdir=args.workdir,
        artifacts_root=args.artifacts_root,
        thread_id=args.thread_id,
        codex_bin=args.codex_bin,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        sandbox=args.sandbox,
        profile=args.profile,
        timeout_seconds=args.timeout_seconds,
    )

    if args.registry:
        registry = load_json(args.registry)
        updated = update_registry_with_report(registry, report)
        report = enrich_report_with_registry_context(updated, report)
        save_json(Path(str(execution["report_path"])), report.as_dict())
        save_json(args.registry, updated)
    if args.log_file:
        append_execution_log(args.log_file, report)
    if args.return_output:
        packet = build_return_packet(report)
        args.return_output.write_text(render_return_packet(packet), encoding="utf-8")

    payload = {
        **execution,
        "thread_id": report.thread_id,
        "summary": report.summary,
        "return_output": str(args.return_output) if args.return_output else "",
    }
    print(json.dumps(payload, indent=2))
    return 0 if report.exit_code == 0 else 1


def _compose_codex_prompt(prompt: str, instructions: list[str]) -> str:
    return build_codex_execution_prompt(
        prompt,
        instructions,
        repo_path="",
        workspace_path="",
        session_id="",
        thread_action="",
    )


if __name__ == "__main__":
    raise SystemExit(main())
