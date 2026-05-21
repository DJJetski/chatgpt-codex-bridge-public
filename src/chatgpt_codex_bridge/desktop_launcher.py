from __future__ import annotations

import argparse
import errno
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from .app_paths import bridge_artifacts_dir, bridge_logs_dir, bridge_state_dir
from .control_panel_runtime import control_panel_runtime_fingerprint
from .cli import (
    _build_loop_runner,
    _default_chat_bindings_path,
    _default_orchestrator_policy_path,
    _default_sessions_dir,
)
from .models import repo_root
from .orchestrator.control_panel import ControlPanelServer, ControlPanelService
from .orchestrator.models import LoopPolicyDecision
from .orchestrator.state import list_sessions, load_session, save_session, session_path
from .orchestrator.supervisor import SupervisorManager


@dataclass(slots=True)
class DesktopRuntime:
    server: ControlPanelServer | None
    panel_url: str

    def shutdown(self) -> None:
        if self.server is None:
            return
        self.server.shutdown()

    def close(self) -> None:
        if self.server is None:
            return
        self.server.server_close()


@dataclass(slots=True)
class ExistingPanelInfo:
    panel_url: str
    server_fingerprint: str | None


def managed_browser_profile_path(base_dir: Path | None = None) -> Path:
    if base_dir is not None:
        return base_dir / "state" / "playwright-profile"
    return bridge_state_dir() / "playwright-profile"


def detached_launcher_log_path(base_dir: Path | None = None) -> Path:
    if base_dir is not None:
        return base_dir / "state" / "desktop-launcher.log"
    return bridge_logs_dir() / "desktop-launcher.log"


def resolve_codex_bin() -> str:
    candidates = [
        _normalize_executable_path(os.environ.get("CODEX_BIN")),
        _normalize_executable_path("/Applications/Codex.app/Contents/Resources/codex"),
        _normalize_executable_path(shutil.which("codex")),
        _resolve_codex_bin_with_login_shell(),
        _normalize_executable_path(str(Path.home() / ".dual-graph" / "codex")),
    ]
    for candidate in candidates:
        if candidate:
            return candidate
    return "codex"


def spawn_detached_launcher(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    headless: bool = False,
    open_browser: bool = True,
) -> Path:
    log_path = detached_launcher_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    launcher_repo_root = repo_root()
    args = [
        sys.executable,
        "-m",
        "chatgpt_codex_bridge.desktop_launcher",
        "--host",
        str(host),
        "--port",
        str(port),
        "--open-browser" if open_browser else "--no-open-browser",
    ]
    if headless:
        args.append("--headless")
    env = _launcher_env(launcher_repo_root)
    with log_path.open("ab") as log_handle:
        subprocess.Popen(
            args=args,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            cwd=str(launcher_repo_root),
            env=env,
            start_new_session=True,
            close_fds=True,
        )
    return log_path


def create_desktop_runtime(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    headless: bool = False,
) -> DesktopRuntime:
    repo_path = repo_root()
    expected_server_fingerprint = control_panel_runtime_fingerprint(repo_path)
    bindings_path = _default_chat_bindings_path()
    policy_path = _default_orchestrator_policy_path()
    sessions_dir = _default_sessions_dir()
    artifacts_root = bridge_artifacts_dir() / "runs"
    codex_bin = resolve_codex_bin()

    manager = SupervisorManager(
        sessions_dir=sessions_dir,
        runner_factory=lambda: _build_loop_runner(
            bindings_path=bindings_path,
            policy_path=policy_path,
            sessions_dir=sessions_dir,
            artifacts_root=artifacts_root,
            log_file=None,
            registry_path=None,
            codex_bin=codex_bin,
            model=None,
            reasoning_effort=None,
            sandbox=None,
            profile=None,
            headless=headless,
        ),
    )
    service = ControlPanelService(
        bindings_path=bindings_path,
        policy_path=policy_path,
        sessions_dir=sessions_dir,
        artifacts_root=artifacts_root,
        supervisor_manager=manager,
        default_repo_path=str(repo_path),
        default_workspace_path=str(repo_path),
        default_browser_profile_path=str(managed_browser_profile_path()),
    )
    for session in list_sessions(sessions_dir):
        if session.status == "active" and session.auto_run_enabled:
            try:
                manager.ensure_session(session.session_id)
            except Exception as exc:
                _mark_session_blocked_on_boot(sessions_dir, session.session_id, str(exc))

    try:
        server = ControlPanelServer(service=service, host=host, port=port)
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        existing_panel = _probe_existing_panel(host, port)
        if existing_panel and existing_panel.server_fingerprint == expected_server_fingerprint:
            return DesktopRuntime(server=None, panel_url=existing_panel.panel_url)
        if existing_panel and _request_panel_shutdown(existing_panel.panel_url):
            server = _wait_for_restarted_server(service, host, port)
        elif existing_panel:
            server = ControlPanelServer(service=service, host=host, port=0)
        else:
            server = ControlPanelServer(service=service, host=host, port=0)
    panel_url = f"http://{host}:{server.server_address[1]}"
    return DesktopRuntime(server=server, panel_url=panel_url)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bridge-desktop-launcher")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--open-browser", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--detach", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.detach:
        spawn_detached_launcher(
            host=str(args.host),
            port=int(args.port),
            headless=bool(args.headless),
            open_browser=bool(args.open_browser),
        )
        return 0
    runtime = create_desktop_runtime(host=str(args.host), port=int(args.port), headless=bool(args.headless))

    def _shutdown(_signum=None, _frame=None) -> None:
        threading.Thread(target=runtime.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    if args.open_browser:
        _open_panel_url(runtime.panel_url)
    if runtime.server is None:
        return 0
    try:
        runtime.server.serve_forever()
    finally:
        runtime.close()
    return 0


def _probe_existing_panel_url(host: str, port: int) -> str | None:
    panel = _probe_existing_panel(host, port)
    if panel is None:
        return None
    return panel.panel_url


def _probe_existing_panel(host: str, port: int) -> ExistingPanelInfo | None:
    panel_url = f"http://{host}:{port}"
    try:
        with urlopen(f"{panel_url}/api/state", timeout=1.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, TimeoutError, ValueError, URLError, json.JSONDecodeError):
        return None
    required_keys = {"bindings", "sessions", "policy", "supervisors"}
    if not required_keys.issubset(payload):
        return None
    fingerprint = payload.get("server_fingerprint")
    normalized_fingerprint = str(fingerprint).strip() if fingerprint else None
    return ExistingPanelInfo(panel_url=panel_url, server_fingerprint=normalized_fingerprint)


def _launcher_env(launcher_repo_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    source_path = launcher_repo_root / "src"
    if (source_path / "chatgpt_codex_bridge").is_dir():
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            f"{source_path}{os.pathsep}{existing_pythonpath}" if existing_pythonpath else str(source_path)
        )
    return env


def _open_panel_url(panel_url: str) -> None:
    if sys.platform == "darwin":
        try:
            subprocess.run(
                ["/usr/bin/open", "-a", "Safari", panel_url],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        except (OSError, subprocess.CalledProcessError):
            pass
    webbrowser.open(panel_url)


def _request_panel_shutdown(panel_url: str) -> bool:
    request = Request(f"{panel_url}/api/control/shutdown", method="POST")
    try:
        with urlopen(request, timeout=1.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, TimeoutError, ValueError, URLError, json.JSONDecodeError):
        return False
    return payload.get("status") == "shutting_down"


def _wait_for_restarted_server(service: ControlPanelService, host: str, port: int) -> ControlPanelServer:
    deadline = time.monotonic() + 3.0
    while True:
        try:
            return ControlPanelServer(service=service, host=host, port=port)
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
            if time.monotonic() >= deadline:
                return ControlPanelServer(service=service, host=host, port=0)
            time.sleep(0.1)


def _mark_session_blocked_on_boot(sessions_dir: Path, session_id: str, message: str) -> None:
    path = session_path(sessions_dir, session_id)
    session = load_session(path)
    session.status = "blocked"
    session.auto_run_enabled = False
    session.supervisor_status = "blocked"
    session.loop_state = "requires_human"
    session.human_attention_reason = message
    session.last_error = message
    session.policy_decision = LoopPolicyDecision(
        policy_outcome="require_human",
        reasons=[message],
        human_gate_required=True,
        human_gate_reason=message,
        human_gate_category="runtime_start_failure",
        time_budget_minutes=session.time_budget_minutes,
        time_budget_remaining_minutes=session.budget_remaining_minutes,
    )
    save_session(path, session)


def _resolve_codex_bin_with_login_shell() -> str | None:
    try:
        completed = subprocess.run(
            ["/bin/zsh", "-lc", "command -v codex"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return _normalize_executable_path(completed.stdout)


def _normalize_executable_path(candidate: str | None) -> str | None:
    if not candidate:
        return None
    first_line = candidate.strip().splitlines()[0].strip()
    if not first_line:
        return None
    path = Path(first_line).expanduser()
    if not path.exists() or not path.is_file():
        return None
    return str(path.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
