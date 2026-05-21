import errno
import json
import os
import subprocess
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


class _FakeServer:
    def __init__(self, host: str, port: int):
        self.server_address = (host, port)
        self.closed = False

    def shutdown(self):
        self.closed = True

    def server_close(self):
        self.closed = True


class DesktopLauncherTests(unittest.TestCase):
    def test_spawn_detached_launcher_starts_background_module_process(self):
        from mastermind_bridge.desktop_launcher import detached_launcher_log_path, spawn_detached_launcher

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "src" / "chatgpt_codex_bridge").mkdir(parents=True)
            os.environ["BRIDGE_HOME"] = str(root / "bridge-home")
            with (
                patch("mastermind_bridge.desktop_launcher.repo_root", return_value=root),
                patch("mastermind_bridge.desktop_launcher.sys.executable", "/usr/bin/python3"),
                patch("mastermind_bridge.desktop_launcher.subprocess.Popen") as popen,
            ):
                try:
                    log_path = spawn_detached_launcher(host="127.0.0.1", port=8765, headless=False, open_browser=True)
                    expected_log_path = detached_launcher_log_path()
                finally:
                    os.environ.pop("BRIDGE_HOME", None)

            self.assertEqual(log_path, expected_log_path)
            self.assertTrue(log_path.parent.exists())
            popen.assert_called_once()
            args = popen.call_args.kwargs["args"]
            self.assertEqual(args[:3], ["/usr/bin/python3", "-m", "chatgpt_codex_bridge.desktop_launcher"])
            self.assertIn("--open-browser", args)
            self.assertNotIn("--detach", args)
            pythonpath_entries = popen.call_args.kwargs["env"]["PYTHONPATH"].split(os.pathsep)
            self.assertEqual(pythonpath_entries[0], str(root / "src"))
            self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_app_bundle_launcher_uses_detach_mode(self):
        launcher_script = (
            Path(__file__).resolve().parents[1]
            / "Bridge Control Panel.app"
            / "Contents"
            / "MacOS"
            / "bridge-control-panel"
        )

        script = launcher_script.read_text()

        self.assertIn("--detach", script)

    def test_app_bundle_launcher_uses_relative_repo_root_and_dynamic_python_lookup(self):
        launcher_script = (
            Path(__file__).resolve().parents[1]
            / "Bridge Control Panel.app"
            / "Contents"
            / "MacOS"
            / "bridge-control-panel"
        )

        script = launcher_script.read_text()

        self.assertIn('repo_root="$(cd "$script_dir/../../.." && pwd)"', script)
        self.assertIn('resolve_python()', script)
        self.assertIn('export PYTHONPATH="$repo_root/src${PYTHONPATH:+:$PYTHONPATH}"', script)
        self.assertIn('export BRIDGE_HOME="${BRIDGE_HOME:-$repo_root}"', script)
        self.assertIn('export BRIDGE_PROFILE="${BRIDGE_PROFILE:-browser-extra}"', script)
        self.assertIn("-m chatgpt_codex_bridge.desktop_launcher", script)
        self.assertNotIn('/tmp/example-home/Codex/chatgpt-codex-bridge', script)
        self.assertNotIn('exec /opt/homebrew/bin/python3', script)

    def test_managed_browser_profile_path_defaults_to_bridge_home_state_directory(self):
        from mastermind_bridge.app_paths import bridge_state_dir
        from mastermind_bridge.desktop_launcher import managed_browser_profile_path

        with tempfile.TemporaryDirectory() as tmp_dir:
            os.environ["BRIDGE_HOME"] = str(Path(tmp_dir) / "bridge-home")

            try:
                profile_path = managed_browser_profile_path()
                expected_path = bridge_state_dir() / "playwright-profile"
            finally:
                os.environ.pop("BRIDGE_HOME", None)

            self.assertEqual(profile_path, expected_path)

    def test_managed_browser_profile_path_allows_explicit_legacy_base_directory(self):
        from mastermind_bridge.desktop_launcher import managed_browser_profile_path

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)

            profile_path = managed_browser_profile_path(root)

            self.assertEqual(profile_path, root / "state" / "playwright-profile")

    def test_resolve_codex_bin_prefers_native_app_binary_when_available(self):
        from mastermind_bridge.desktop_launcher import resolve_codex_bin

        with (
            patch("mastermind_bridge.desktop_launcher._normalize_executable_path") as normalize_mock,
            patch("mastermind_bridge.desktop_launcher._resolve_codex_bin_with_login_shell", return_value=""),
        ):
            normalize_mock.side_effect = [
                "",
                "/Applications/Codex.app/Contents/Resources/codex",
                "/tmp/test-home/.local/bin/codex",
                "/tmp/test-home/.dual-graph/codex",
            ]

            codex_bin = resolve_codex_bin()

        self.assertEqual(codex_bin, "/Applications/Codex.app/Contents/Resources/codex")

    def test_resolve_codex_bin_uses_shell_lookup_when_native_app_and_path_lookup_are_empty(self):
        from mastermind_bridge.desktop_launcher import resolve_codex_bin

        with (
            patch("mastermind_bridge.desktop_launcher._normalize_executable_path") as normalize_mock,
            patch(
                "mastermind_bridge.desktop_launcher._resolve_codex_bin_with_login_shell",
                return_value="/tmp/test-home/.dual-graph/codex",
            ),
        ):
            normalize_mock.side_effect = ["", "", "", "/tmp/test-home/.dual-graph/codex"]
            codex_bin = resolve_codex_bin()

        self.assertEqual(codex_bin, "/tmp/test-home/.dual-graph/codex")

    def test_probe_existing_panel_url_detects_running_panel(self):
        from mastermind_bridge.desktop_launcher import _probe_existing_panel_url

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.path != "/api/state":
                    self.send_response(404)
                    self.end_headers()
                    return
                payload = json.dumps(
                    {
                        "bindings": [],
                        "sessions": [],
                        "policy": {},
                        "supervisors": {},
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format, *args):  # noqa: A003
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        import threading

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            panel_url = _probe_existing_panel_url("127.0.0.1", server.server_address[1])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1)

        self.assertEqual(panel_url, f"http://127.0.0.1:{server.server_address[1]}")

    def test_create_desktop_runtime_reuses_existing_panel_when_port_is_busy(self):
        from mastermind_bridge.desktop_launcher import create_desktop_runtime

        with (
            patch("mastermind_bridge.desktop_launcher.list_sessions", return_value=[]),
            patch(
                "mastermind_bridge.desktop_launcher.ControlPanelServer",
                side_effect=OSError(errno.EADDRINUSE, "Address already in use"),
            ),
            patch(
                "mastermind_bridge.desktop_launcher._probe_existing_panel",
                return_value=type("Panel", (), {"panel_url": "http://127.0.0.1:8765", "server_fingerprint": "fresh"})(),
            ),
            patch("mastermind_bridge.desktop_launcher.control_panel_runtime_fingerprint", return_value="fresh"),
            patch("mastermind_bridge.desktop_launcher._request_panel_shutdown") as shutdown_mock,
        ):
            runtime = create_desktop_runtime(port=8765)

        self.assertIsNone(runtime.server)
        self.assertEqual(runtime.panel_url, "http://127.0.0.1:8765")
        shutdown_mock.assert_not_called()

    def test_create_desktop_runtime_restarts_existing_panel_when_existing_panel_is_stale(self):
        from mastermind_bridge.desktop_launcher import create_desktop_runtime

        server_calls = [
            OSError(errno.EADDRINUSE, "Address already in use"),
            _FakeServer("127.0.0.1", 8765),
        ]

        def fake_server(*, service, host, port):
            result = server_calls.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        with (
            patch("mastermind_bridge.desktop_launcher.list_sessions", return_value=[]),
            patch("mastermind_bridge.desktop_launcher.ControlPanelServer", side_effect=fake_server),
            patch(
                "mastermind_bridge.desktop_launcher._probe_existing_panel",
                return_value=type("Panel", (), {"panel_url": "http://127.0.0.1:8765", "server_fingerprint": None})(),
            ),
            patch("mastermind_bridge.desktop_launcher.control_panel_runtime_fingerprint", return_value="fresh"),
            patch("mastermind_bridge.desktop_launcher._request_panel_shutdown", return_value=True),
        ):
            runtime = create_desktop_runtime(port=8765)

        self.assertIsNotNone(runtime.server)
        self.assertEqual(runtime.panel_url, "http://127.0.0.1:8765")

    def test_create_desktop_runtime_falls_back_to_random_port_when_busy_port_is_not_panel(self):
        from mastermind_bridge.desktop_launcher import create_desktop_runtime

        server_calls = [
            OSError(errno.EADDRINUSE, "Address already in use"),
            _FakeServer("127.0.0.1", 43123),
        ]

        def fake_server(*, service, host, port):
            result = server_calls.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        with (
            patch("mastermind_bridge.desktop_launcher.list_sessions", return_value=[]),
            patch("mastermind_bridge.desktop_launcher.ControlPanelServer", side_effect=fake_server),
            patch("mastermind_bridge.desktop_launcher._probe_existing_panel", return_value=None),
        ):
            runtime = create_desktop_runtime(port=8765)

        self.assertIsNotNone(runtime.server)
        self.assertEqual(runtime.panel_url, "http://127.0.0.1:43123")

    def test_create_desktop_runtime_marks_auto_run_session_blocked_when_runner_boot_fails(self):
        from mastermind_bridge.desktop_launcher import create_desktop_runtime

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            sessions_dir = root / "sessions"
            sessions_dir.mkdir(parents=True)
            (sessions_dir / "session-1.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "session": {
                            "session_id": "session-1",
                            "binding_id": "binding-1",
                            "repo_path": "/tmp/repo",
                            "workspace_path": "/tmp/repo",
                            "chat_url": "https://chatgpt.com/c/project/binding-1",
                            "status": "active",
                            "loop_state": "idle",
                            "auto_run_enabled": True,
                            "supervisor_status": "running",
                            "time_budget_minutes": 30,
                            "budget_remaining_minutes": 30,
                        },
                    }
                )
            )

            with (
                patch("mastermind_bridge.desktop_launcher.repo_root", return_value=root),
                patch("mastermind_bridge.desktop_launcher._default_chat_bindings_path", return_value=root / "CHAT_BINDINGS.json"),
                patch("mastermind_bridge.desktop_launcher._default_orchestrator_policy_path", return_value=root / "ORCHESTRATOR_POLICY.json"),
                patch("mastermind_bridge.desktop_launcher._default_sessions_dir", return_value=sessions_dir),
                patch("mastermind_bridge.desktop_launcher.SupervisorManager.ensure_session", side_effect=RuntimeError("Playwright is not installed.")),
            ):
                runtime = create_desktop_runtime(port=0)

            try:
                payload = json.loads((sessions_dir / "session-1.json").read_text())["session"]
                self.assertEqual(payload["status"], "blocked")
                self.assertEqual(payload["supervisor_status"], "blocked")
                self.assertEqual(payload["loop_state"], "requires_human")
                self.assertFalse(payload["auto_run_enabled"])
                self.assertIn("Playwright is not installed", payload["last_error"])
            finally:
                runtime.close()

    def test_create_desktop_runtime_uses_resolved_codex_binary_for_auto_run_sessions(self):
        from mastermind_bridge.desktop_launcher import create_desktop_runtime

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            sessions_dir = root / "sessions"
            sessions_dir.mkdir(parents=True)
            (sessions_dir / "session-1.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "session": {
                            "session_id": "session-1",
                            "binding_id": "binding-1",
                            "repo_path": "/tmp/repo",
                            "workspace_path": "/tmp/repo",
                            "chat_url": "https://chatgpt.com/c/project/binding-1",
                            "status": "active",
                            "loop_state": "idle",
                            "auto_run_enabled": True,
                            "supervisor_status": "running",
                            "time_budget_minutes": 30,
                            "budget_remaining_minutes": 30,
                        },
                    }
                )
            )

            with (
                patch("mastermind_bridge.desktop_launcher.repo_root", return_value=root),
                patch("mastermind_bridge.desktop_launcher._default_chat_bindings_path", return_value=root / "CHAT_BINDINGS.json"),
                patch("mastermind_bridge.desktop_launcher._default_orchestrator_policy_path", return_value=root / "ORCHESTRATOR_POLICY.json"),
                patch("mastermind_bridge.desktop_launcher._default_sessions_dir", return_value=sessions_dir),
                patch("mastermind_bridge.desktop_launcher.resolve_codex_bin", return_value="/tmp/test-home/.dual-graph/codex"),
                patch("mastermind_bridge.desktop_launcher._build_loop_runner", side_effect=RuntimeError("Codex bootstrap check.")) as build_loop_runner,
            ):
                runtime = create_desktop_runtime(port=0)

            try:
                self.assertEqual(build_loop_runner.call_args.kwargs["codex_bin"], "/tmp/test-home/.dual-graph/codex")
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
