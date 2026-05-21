from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..app_paths import bridge_artifacts_dir, bridge_state_dir
from ..profiles import profile_allows
from .kernel import V2Kernel
from .store import (
    _DEFAULT_CHATGPT_MODEL,
    _DEFAULT_CHATGPT_REASONING_EFFORT,
    _DEFAULT_CODEX_EXECUTION_MODE,
    _DEFAULT_CODEX_MODEL,
    _DEFAULT_CODEX_REASONING_EFFORT,
)
from .workers import run_chatgpt_worker, run_codex_worker

_REASONING_EFFORT_CHOICES = ("minimal", "low", "medium", "high", "xhigh")
_CODEX_EXECUTION_MODE_CHOICES = ("cli_only", "allow_app")


def register_v2_commands(subparsers) -> None:
    parser = subparsers.add_parser(
        "v2",
        help="Terminal-first, kernel-first Supervisor V2.",
    )
    v2_subparsers = parser.add_subparsers(dest="v2_command", required=True)

    session_parser = v2_subparsers.add_parser("session", help="Manage Supervisor V2 sessions.")
    session_subparsers = session_parser.add_subparsers(dest="v2_session_command", required=True)

    create_parser = session_subparsers.add_parser("create", help="Create a new V2 session.")
    _add_v2_db_argument(create_parser)
    create_parser.add_argument("--repo-path", type=Path, default=Path.cwd())
    create_parser.add_argument("--workspace-path", type=Path)
    create_parser.add_argument("--operator-goal", required=True)
    create_parser.add_argument("--operator-notes", default="")
    create_parser.add_argument("--session-id")
    _add_v2_session_settings_arguments(create_parser, include_defaults=True)
    create_parser.set_defaults(handler=handle_v2_session_create)

    configure_parser = session_subparsers.add_parser("configure", help="Update persisted V2 session settings.")
    _add_v2_db_argument(configure_parser)
    _add_v2_session_id_argument(configure_parser)
    _add_v2_session_settings_arguments(configure_parser, include_defaults=False)
    configure_parser.add_argument("--clear-context-files", action="store_true")
    configure_parser.set_defaults(handler=handle_v2_session_configure)

    bootstrap_parser = session_subparsers.add_parser("bootstrap", help="Queue one manual bootstrap turn.")
    bootstrap_parser.add_argument("worker", choices=("chatgpt", "codex"))
    _add_v2_db_argument(bootstrap_parser)
    _add_v2_session_id_argument(bootstrap_parser)
    bootstrap_parser.add_argument("--prompt", default="")
    bootstrap_parser.add_argument(
        "--thread-mode",
        choices=("resume_current", "start_fresh"),
        default="resume_current",
    )
    bootstrap_parser.set_defaults(handler=handle_v2_session_bootstrap)

    arm_parser = session_subparsers.add_parser("arm", help="Switch a session from manual bootstrap to autonomous running.")
    _add_v2_db_argument(arm_parser)
    _add_v2_session_id_argument(arm_parser)
    arm_parser.set_defaults(handler=handle_v2_session_arm)

    start_parser = session_subparsers.add_parser("start", help="Run the V2 kernel loop in the current terminal.")
    _add_v2_db_argument(start_parser)
    _add_v2_artifacts_argument(start_parser)
    _add_v2_session_id_argument(start_parser)
    start_parser.add_argument("--codex-bin", default="codex")
    start_parser.add_argument("--max-turns", type=int)
    start_parser.add_argument("--poll-interval-seconds", type=float, default=0.5)
    start_parser.add_argument("--chatgpt-timeout-seconds", type=float, default=120.0)
    start_parser.add_argument("--codex-timeout-seconds", type=float, default=1800.0)
    start_parser.add_argument("--worker-lease-ttl-seconds", type=float, default=30.0)
    start_parser.add_argument("--kernel-lease-ttl-seconds", type=float, default=10.0)
    start_parser.set_defaults(handler=handle_v2_session_start)

    status_parser = session_subparsers.add_parser("status", help="Show a V2 session snapshot.")
    _add_v2_db_argument(status_parser)
    _add_v2_session_id_argument(status_parser)
    status_parser.add_argument("--format", choices=("json", "summary"), default="json")
    status_parser.set_defaults(handler=handle_v2_session_status)

    watch_parser = session_subparsers.add_parser("watch", help="Watch a V2 session snapshot stream.")
    _add_v2_db_argument(watch_parser)
    _add_v2_session_id_argument(watch_parser)
    watch_parser.add_argument("--poll-interval-seconds", type=float, default=1.0)
    watch_parser.add_argument("--format", choices=("json", "summary"), default="summary")
    watch_parser.set_defaults(handler=handle_v2_session_watch)

    pause_parser = session_subparsers.add_parser("pause", help="Pause a V2 session after the current turn.")
    _add_v2_db_argument(pause_parser)
    _add_v2_session_id_argument(pause_parser)
    pause_parser.set_defaults(handler=handle_v2_session_pause)

    resume_parser = session_subparsers.add_parser("resume", help="Resume a paused V2 session.")
    _add_v2_db_argument(resume_parser)
    _add_v2_session_id_argument(resume_parser)
    resume_parser.set_defaults(handler=handle_v2_session_resume)

    stop_parser = session_subparsers.add_parser("stop", help="Stop a V2 session.")
    _add_v2_db_argument(stop_parser)
    _add_v2_session_id_argument(stop_parser)
    stop_parser.set_defaults(handler=handle_v2_session_stop)

    abort_parser = session_subparsers.add_parser("abort-turn", help="Abort the currently running worker turn.")
    _add_v2_db_argument(abort_parser)
    _add_v2_session_id_argument(abort_parser)
    _add_v2_artifacts_argument(abort_parser)
    abort_parser.set_defaults(handler=handle_v2_session_abort_turn)

    internal_parser = v2_subparsers.add_parser("internal", help=argparse.SUPPRESS)
    internal_subparsers = internal_parser.add_subparsers(dest="v2_internal_command", required=True)

    run_chatgpt_parser = internal_subparsers.add_parser("run-chatgpt-turn", help=argparse.SUPPRESS)
    run_chatgpt_parser.add_argument("--input", required=True, type=Path)
    run_chatgpt_parser.add_argument("--output", required=True, type=Path)
    run_chatgpt_parser.set_defaults(handler=handle_v2_internal_run_chatgpt_turn)

    run_codex_parser = internal_subparsers.add_parser("run-codex-turn", help=argparse.SUPPRESS)
    run_codex_parser.add_argument("--input", required=True, type=Path)
    run_codex_parser.add_argument("--output", required=True, type=Path)
    run_codex_parser.set_defaults(handler=handle_v2_internal_run_codex_turn)


def handle_v2_session_create(args: argparse.Namespace) -> int:
    blocked = _execution_mode_block_reason(str(args.codex_execution_mode))
    if blocked:
        return _print_v2_error(blocked)
    kernel = _v2_kernel(args)
    session = kernel.create_session(
        repo_path=args.repo_path,
        workspace_path=args.workspace_path or args.repo_path,
        operator_goal=str(args.operator_goal),
        operator_notes=str(args.operator_notes),
        chatgpt_model=str(args.chatgpt_model),
        chatgpt_reasoning_effort=str(args.chatgpt_reasoning_effort),
        codex_model=str(args.codex_model),
        codex_reasoning_effort=str(args.codex_reasoning_effort),
        codex_execution_mode=str(args.codex_execution_mode),
        context_files=[str(path) for path in args.context_file],
        session_id=str(args.session_id or ""),
    )
    print(json.dumps({"session": session.as_dict(), "db_path": str(args.db)}, indent=2))
    return 0


def handle_v2_session_configure(args: argparse.Namespace) -> int:
    kernel = _v2_kernel(args)
    fields = _session_settings_update_fields(args)
    blocked = _execution_mode_block_reason(str(fields.get("codex_execution_mode", "")))
    if blocked:
        return _print_v2_error(blocked)
    if args.clear_context_files:
        fields["context_files"] = []
    session = kernel.configure_session(args.session_id, **fields)
    print(json.dumps({"session": session.as_dict()}, indent=2))
    return 0


def handle_v2_session_bootstrap(args: argparse.Namespace) -> int:
    kernel = _v2_kernel(args)
    turn = kernel.bootstrap_turn(
        args.session_id,
        worker=str(args.worker),
        prompt=str(args.prompt),
        thread_mode=str(args.thread_mode),
    )
    print(json.dumps({"queued_turn": turn.as_dict()}, indent=2))
    return 0


def handle_v2_session_arm(args: argparse.Namespace) -> int:
    kernel = _v2_kernel(args)
    session = kernel.arm_session(args.session_id)
    print(json.dumps({"session": session.as_dict()}, indent=2))
    return 0


def handle_v2_session_start(args: argparse.Namespace) -> int:
    kernel = V2Kernel(
        db_path=args.db,
        artifacts_root=_resolved_v2_artifacts_root(args.db, args.artifacts_root),
        codex_bin=str(args.codex_bin),
        poll_interval_seconds=float(args.poll_interval_seconds),
        chatgpt_timeout_seconds=float(args.chatgpt_timeout_seconds),
        codex_timeout_seconds=float(args.codex_timeout_seconds),
        worker_lease_ttl_seconds=float(args.worker_lease_ttl_seconds),
        kernel_lease_ttl_seconds=float(args.kernel_lease_ttl_seconds),
    )
    blocked = _execution_mode_block_reason(kernel.store.get_session(args.session_id).codex_execution_mode)
    if blocked:
        return _print_v2_error(blocked)
    snapshot = kernel.start(
        args.session_id,
        max_turns=args.max_turns,
        poll_interval_seconds=float(args.poll_interval_seconds),
    )
    print(json.dumps(snapshot, indent=2))
    return 0


def handle_v2_session_status(args: argparse.Namespace) -> int:
    kernel = _v2_kernel(args)
    if args.format == "summary":
        print(kernel.render_summary(args.session_id))
        return 0
    print(json.dumps(kernel.status_snapshot(args.session_id), indent=2))
    return 0


def handle_v2_session_watch(args: argparse.Namespace) -> int:
    kernel = _v2_kernel(args)
    kernel.watch(
        args.session_id,
        poll_interval_seconds=float(args.poll_interval_seconds),
        output_format=str(args.format),
    )
    return 0


def handle_v2_session_pause(args: argparse.Namespace) -> int:
    kernel = _v2_kernel(args)
    session = kernel.pause_session(args.session_id)
    print(json.dumps({"session": session.as_dict()}, indent=2))
    return 0


def handle_v2_session_resume(args: argparse.Namespace) -> int:
    kernel = _v2_kernel(args)
    session = kernel.resume_session(args.session_id)
    print(json.dumps({"session": session.as_dict()}, indent=2))
    return 0


def handle_v2_session_stop(args: argparse.Namespace) -> int:
    kernel = _v2_kernel(args)
    session = kernel.stop_session(args.session_id)
    print(json.dumps({"session": session.as_dict()}, indent=2))
    return 0


def handle_v2_session_abort_turn(args: argparse.Namespace) -> int:
    kernel = V2Kernel(
        db_path=args.db,
        artifacts_root=_resolved_v2_artifacts_root(args.db, args.artifacts_root),
    )
    payload = kernel.abort_turn(args.session_id)
    print(json.dumps(payload, indent=2))
    return 0


def handle_v2_internal_run_chatgpt_turn(args: argparse.Namespace) -> int:
    worker_input = json.loads(args.input.read_text(encoding="utf-8"))
    result = run_chatgpt_worker(worker_input=worker_input, output_path=args.output)
    print(json.dumps(result, indent=2))
    return 0


def handle_v2_internal_run_codex_turn(args: argparse.Namespace) -> int:
    worker_input = json.loads(args.input.read_text(encoding="utf-8"))
    result = run_codex_worker(worker_input=worker_input, output_path=args.output)
    print(json.dumps(result, indent=2))
    return 0


def _add_v2_db_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", type=Path, default=_default_v2_db_path())


def _add_v2_session_id_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--session-id", required=True)


def _add_v2_artifacts_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--artifacts-root",
        type=Path,
        default=None,
        help="Artifact root. Defaults to repo artifacts for the default DB, or beside a custom --db.",
    )


def _add_v2_session_settings_arguments(parser: argparse.ArgumentParser, *, include_defaults: bool) -> None:
    default_model = _DEFAULT_CHATGPT_MODEL if include_defaults else None
    default_chatgpt_reasoning = _DEFAULT_CHATGPT_REASONING_EFFORT if include_defaults else None
    default_codex_model = _DEFAULT_CODEX_MODEL if include_defaults else None
    default_codex_reasoning = _DEFAULT_CODEX_REASONING_EFFORT if include_defaults else None
    default_execution_mode = _DEFAULT_CODEX_EXECUTION_MODE if include_defaults else None
    default_context_files: list[Path] | None = [] if include_defaults else None
    parser.add_argument("--chatgpt-model", default=default_model)
    parser.add_argument("--chatgpt-reasoning-effort", choices=_REASONING_EFFORT_CHOICES, default=default_chatgpt_reasoning)
    parser.add_argument("--codex-model", default=default_codex_model)
    parser.add_argument("--codex-reasoning-effort", choices=_REASONING_EFFORT_CHOICES, default=default_codex_reasoning)
    parser.add_argument("--codex-execution-mode", choices=_CODEX_EXECUTION_MODE_CHOICES, default=default_execution_mode)
    parser.add_argument("--context-file", action="append", type=Path, default=default_context_files)


def _session_settings_update_fields(args: argparse.Namespace) -> dict[str, object]:
    fields: dict[str, object] = {}
    for field_name in (
        "chatgpt_model",
        "chatgpt_reasoning_effort",
        "codex_model",
        "codex_reasoning_effort",
        "codex_execution_mode",
    ):
        value = getattr(args, field_name, None)
        if value is None:
            continue
        fields[field_name] = str(value)
    if getattr(args, "context_file", None) is not None:
        fields["context_files"] = [str(path) for path in args.context_file]
    return fields


def _execution_mode_block_reason(execution_mode: str) -> dict[str, str] | None:
    if execution_mode != "allow_app":
        return None
    if profile_allows("macos-app"):
        return None
    return {
        "error": "codex_execution_mode_profile_required",
        "codex_execution_mode": execution_mode,
        "required_profile": "macos-app",
    }


def _print_v2_error(payload: dict[str, str]) -> int:
    print(json.dumps(payload, indent=2, sort_keys=True), file=sys.stderr)
    return 2


def _default_v2_db_path() -> Path:
    return bridge_state_dir() / "supervisor_v2.sqlite3"


def _default_v2_artifacts_root() -> Path:
    return bridge_artifacts_dir() / "v2"


def _v2_kernel(args: argparse.Namespace) -> V2Kernel:
    return V2Kernel(
        db_path=args.db,
        artifacts_root=_resolved_v2_artifacts_root(args.db, getattr(args, "artifacts_root", None)),
    )


def _resolved_v2_artifacts_root(db_path: Path, artifacts_root: Path | None = None) -> Path:
    if artifacts_root is not None:
        return artifacts_root

    resolved_db = Path(db_path).expanduser().resolve()
    if resolved_db == _default_v2_db_path().resolve():
        return _default_v2_artifacts_root()

    base_dir = resolved_db.parent
    if base_dir.name == "state":
        base_dir = base_dir.parent
    return base_dir / "artifacts" / "v2"
