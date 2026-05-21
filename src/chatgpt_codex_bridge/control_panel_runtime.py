from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

from .executor import session_live_log_path
from .live_monitor import main as live_monitor_main
from .models import repo_root

def control_panel_runtime_fingerprint(base_dir: Path | None = None) -> str:
    root = Path(base_dir) if base_dir is not None else repo_root()
    digest = hashlib.sha256()
    for relative_path in _control_panel_runtime_files(root):
        digest.update(relative_path.as_posix().encode("utf-8"))
        digest.update(b"\0")
        path = root / relative_path
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<missing>")
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def _control_panel_runtime_files(root: Path) -> list[Path]:
    canonical_root = root / "src" / "chatgpt_codex_bridge"
    if canonical_root.exists():
        return sorted(path.relative_to(root) for path in canonical_root.rglob("*.py"))
    legacy_root = root / "mastermind_bridge"
    if legacy_root.exists():
        return sorted(path.relative_to(root) for path in legacy_root.rglob("*.py"))
    package_root = root / "chatgpt_codex_bridge"
    if package_root.exists():
        return sorted(path.relative_to(root) for path in package_root.rglob("*.py"))
    return [
        Path("src/chatgpt_codex_bridge/control_panel_runtime.py"),
        Path("src/chatgpt_codex_bridge/executor.py"),
        Path("src/chatgpt_codex_bridge/orchestrator/control_panel.py"),
        Path("src/chatgpt_codex_bridge/orchestrator/control_panel_view.py"),
    ]


def run_terminal_live_monitor(
    *,
    session_id: str,
    workspace_path: str,
    artifacts_root: Path,
    tail_lines: int,
    poll_interval: float,
    emit_initial_prompt: bool = True,
) -> int:
    log_path = session_live_log_path(artifacts_root, session_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.touch(exist_ok=True)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True, write_through=True)

    print(f"[bridge] watching formatted live session log: {log_path}")
    print()
    print(f"[bridge] observed workspace: {workspace_path}")
    print()
    if emit_initial_prompt:
        _emit_initial_prompt(session_id=session_id, artifacts_root=artifacts_root)

    return live_monitor_main(
        [
            "--log",
            str(log_path),
            "--tail-lines",
            str(max(tail_lines, 0)),
            "--detail",
            "terminal",
            "--poll-interval",
            str(max(poll_interval, 0.05)),
        ]
    )


def _emit_initial_prompt(*, session_id: str, artifacts_root: Path) -> None:
    prompt_path = _latest_prompt_path_for_session(artifacts_root, session_id)
    if prompt_path is None:
        return
    try:
        prompt_text = prompt_path.read_text(encoding="utf-8", errors="replace").rstrip()
    except OSError:
        return
    if not prompt_text:
        return

    print(f"[bridge] prompt sent to Codex: {prompt_path}")
    print()
    print("=== prompt sent to Codex ===")
    print(prompt_text)
    print()


def _latest_prompt_path_for_session(artifacts_root: Path, session_id: str) -> Path | None:
    if not artifacts_root.exists():
        return None
    candidates = []
    for run_dir in sorted(artifacts_root.glob(f"*-{session_id}")):
        prompt_path = run_dir / "prompt.md"
        if prompt_path.exists():
            candidates.append(prompt_path)
    return candidates[-1] if candidates else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Launch the bridge live monitor from the bridge runtime.")
    parser.add_argument("--session-id", required=True, help="Bridge session id.")
    parser.add_argument("--workspace", required=True, help="Observed workspace path.")
    parser.add_argument("--artifacts-root", required=True, help="Bridge artifacts root for session logs and runs.")
    parser.add_argument("--tail-lines", type=int, default=200, help="Existing line tail to render before following.")
    parser.add_argument("--poll-interval", type=float, default=0.2, help="Log polling interval in seconds.")
    parser.add_argument(
        "--no-initial-prompt",
        action="store_true",
        help="Do not print the latest prompt before following the live log.",
    )
    parser.add_argument(
        "--follow-from-end",
        action="store_true",
        help="Ignore existing log lines and render only new live output.",
    )
    args = parser.parse_args(argv)

    return run_terminal_live_monitor(
        session_id=str(args.session_id),
        workspace_path=str(args.workspace),
        artifacts_root=Path(args.artifacts_root).expanduser(),
        tail_lines=0 if bool(args.follow_from_end) else int(args.tail_lines),
        poll_interval=float(args.poll_interval),
        emit_initial_prompt=not bool(args.no_initial_prompt),
    )


if __name__ == "__main__":
    raise SystemExit(main())
