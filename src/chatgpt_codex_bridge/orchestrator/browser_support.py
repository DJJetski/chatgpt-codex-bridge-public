from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

_TURN_ROLE_PREFIXES = {
    "assistant": ("chatgpt:", "assistant:"),
    "user": ("du:", "you:", "user:"),
}
_DEFAULT_POST_ACK_TIMEOUT_MS = 90000
_CHATGPT_IN_PAGE_SEND_FAILURE_SIGNATURE = "ChatGPT in-page send failed."
_CHATGPT_IN_PAGE_SEND_FAILURE_MARKERS = (
    "etwas ist schiefgegangen",
    "something went wrong",
    "an error occurred",
    "network error",
    "netzwerkfehler",
)
CHATGPT_RETRY_BUTTON_MARKERS = (
    "retry",
    "try again",
    "erneut versuchen",
    "erneut probieren",
    "regenerate",
    "erneut generieren",
    "antwort neu generieren",
    "antwort erneut generieren",
    "wiederholen",
)
_CHATGPT_RETRYABLE_ERROR_MARKERS = _CHATGPT_IN_PAGE_SEND_FAILURE_MARKERS + ("reasoning failed",)
_CHATGPT_RETRYABLE_ERROR_SURFACE_MARKERS = (
    "retry",
    "try again",
    "erneut versuchen",
)


def is_known_delivery_error(message: str, signatures: list[str]) -> bool:
    text = message.strip()
    return any(text == str(signature).strip() for signature in signatures if str(signature).strip())


def normalize_stop_command(text: str, stop_phrases: list[str]) -> str | None:
    normalized = text.strip().casefold()
    for phrase in stop_phrases:
        candidate = str(phrase).strip()
        if normalized == candidate.casefold():
            return candidate
    return None


def normalize_stop_command_event(payload: Any, stop_phrases: list[str]) -> dict[str, str] | None:
    if payload is None:
        return None

    if isinstance(payload, dict):
        text = str(payload.get("text", payload.get("command", ""))).strip()
        command = str(payload.get("command", "")).strip() or (normalize_stop_command(text, stop_phrases) or "")
        if not command:
            return None
        message_id = str(payload.get("message_id", ""))
        message_anchor = str(payload.get("message_anchor", "")) or message_id or _message_anchor(text or command)
        return {
            "command": command,
            "text": text or command,
            "message_id": message_id,
            "message_anchor": message_anchor,
            "message_hash": hashlib.sha1((text or command).encode("utf-8")).hexdigest(),
        }

    text = str(payload).strip()
    command = normalize_stop_command(text, stop_phrases)
    if not command:
        return None
    return {
        "command": command,
        "text": text,
        "message_id": "",
        "message_anchor": _message_anchor(text),
        "message_hash": hashlib.sha1(text.encode("utf-8")).hexdigest(),
    }


def stop_command_already_processed(session: Any, stop_event: dict[str, str]) -> bool:
    anchor = str(stop_event.get("message_anchor", ""))
    if anchor and anchor == str(getattr(session, "last_seen_user_control_anchor", "")):
        return True
    message_hash = str(stop_event.get("message_hash", ""))
    return bool(message_hash and message_hash == str(getattr(session, "latest_user_control_message_hash", "")))


def canonical_delivery_error_signature(message: str) -> str:
    text = str(message or "").strip()
    if _looks_like_chatgpt_in_page_send_failure(text):
        return _CHATGPT_IN_PAGE_SEND_FAILURE_SIGNATURE
    return text


def looks_like_retryable_chatgpt_error_surface(message: str) -> bool:
    normalized = normalize_composer_text(message).casefold()
    if not normalized:
        return False
    if not any(marker in normalized for marker in _CHATGPT_RETRYABLE_ERROR_MARKERS):
        return False
    return any(marker in normalized for marker in CHATGPT_RETRY_BUTTON_MARKERS)


def assistant_message_looks_like_retryable_error(message: str) -> bool:
    text = str(message or "").strip()
    if not text:
        return False
    if looks_like_retryable_chatgpt_error_surface(text):
        return True
    if canonical_delivery_error_signature(text) == _CHATGPT_IN_PAGE_SEND_FAILURE_SIGNATURE:
        return True
    normalized = text.casefold()
    if normalized == "reasoning failed":
        return True
    return "reasoning failed" in normalized and any(
        marker in normalized for marker in _CHATGPT_RETRYABLE_ERROR_SURFACE_MARKERS
    )


def normalize_composer_text(text: str) -> str:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.replace("\u200b", "").replace("\xa0", " ")
    return "\n".join(line.rstrip() for line in normalized.split("\n")).strip()


def _normalize_composer_nonempty_lines(text: str) -> tuple[str, ...]:
    normalized = normalize_composer_text(text)
    return tuple(line for line in normalized.split("\n") if line)


def _collapse_composer_whitespace(text: str) -> str:
    return " ".join(normalize_composer_text(text).split())


def composer_text_preserves_payload(expected: str, observed: str) -> bool:
    normalized_expected = normalize_composer_text(expected)
    normalized_observed = normalize_composer_text(observed)
    if normalized_expected == normalized_observed:
        return True
    if _normalize_composer_nonempty_lines(normalized_expected) == _normalize_composer_nonempty_lines(
        normalized_observed
    ):
        return True
    return _collapse_composer_whitespace(normalized_expected) == _collapse_composer_whitespace(normalized_observed)


def _looks_like_chatgpt_in_page_send_failure(message: str) -> bool:
    normalized = str(message or "").strip().casefold()
    if not normalized:
        return False
    return any(marker in normalized for marker in _CHATGPT_IN_PAGE_SEND_FAILURE_MARKERS)


def enrich_browser_blocker_reason(reason: str) -> str:
    message = str(reason or "").strip()
    if not _looks_like_host_browser_transport_failure_message(message):
        return message
    if "host probes:" in message.casefold():
        return message
    probe_failures = _probe_host_automation_failures()
    if not probe_failures:
        return message
    return f"{message} Host probes: {'; '.join(probe_failures)}."


def _message_anchor(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def _indexed_message_anchor(role: str, index: int, text: str) -> str:
    return f"{role}-{max(index, 0)}-{_message_anchor(text)}"


def _classify_turn_role(text: str) -> str:
    first_line = text.strip().splitlines()[0].strip().casefold() if text.strip() else ""
    for role, prefixes in _TURN_ROLE_PREFIXES.items():
        if first_line in prefixes:
            return role
    return ""


def _strip_turn_role_label(text: str, role: str) -> str:
    stripped = text.strip()
    if not stripped:
        return ""
    lines = stripped.splitlines()
    first_line = lines[0].strip().casefold()
    if first_line in _TURN_ROLE_PREFIXES.get(role, ()):
        remainder = "\n".join(lines[1:]).strip()
        return remainder or stripped
    return stripped


def _macos_browser_app_name(binding: Any) -> str:
    normalized = str(getattr(binding, "browser_channel", "") or detect_preferred_browser_channel()).strip().casefold()
    if normalized == "chrome":
        return "Google Chrome"
    if normalized == "brave":
        return "Brave Browser"
    if normalized == "msedge":
        return "Microsoft Edge"
    return ""


def _macos_browser_app_path(binding: Any) -> Path | None:
    normalized = str(getattr(binding, "browser_channel", "") or detect_preferred_browser_channel()).strip().casefold()
    candidates = {
        "chrome": Path("/Applications/Google Chrome.app"),
        "brave": Path("/Applications/Brave Browser.app"),
        "msedge": Path("/Applications/Microsoft Edge.app"),
    }
    path = candidates.get(normalized)
    if path and path.exists():
        return path
    return None


def _macos_browser_binary_path(binding: Any) -> Path | None:
    normalized = str(getattr(binding, "browser_channel", "") or detect_preferred_browser_channel()).strip().casefold()
    candidates = {
        "chrome": Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        "brave": Path("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
        "msedge": Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
    }
    path = candidates.get(normalized)
    if path and path.exists():
        return path
    return None


def describe_browser_transport(binding: Any) -> str:
    browser_session_handle = str(getattr(binding, "browser_session_handle", "") or "").strip()
    if sys.platform == "darwin" and browser_session_handle:
        app_name = _macos_browser_app_name(binding) or "browser"
        return f"applescript_tab:{app_name}"
    profile_path = str(getattr(binding, "browser_profile_path", "") or "").strip()
    if profile_path:
        return "playwright_persistent_profile"
    return "playwright_ephemeral"


def _applescript_js_expression(text: str) -> str:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    tokens: list[str] = []
    chunk: list[str] = []

    def flush_chunk() -> None:
        if chunk:
            # AppleScript string literals need literal backslashes doubled.
            tokens.append(f'"{"".join(chunk).replace("\\", "\\\\")}"')
            chunk.clear()

    for character in normalized:
        if character == '"':
            flush_chunk()
            tokens.append("quote")
            continue
        if character == "\n":
            flush_chunk()
            tokens.append("linefeed")
            continue
        chunk.append(character)

    flush_chunk()
    if not tokens:
        return '""'
    return " & ".join(tokens)


def _chrome_osascript_source(
    js_chunks: list[str] | None = None,
    *,
    terms_app_name: str = "Google Chrome",
    foreground: bool = False,
) -> str:
    chunk_lines = "\n".join(
        f"    set jsCode to jsCode & ({_applescript_js_expression(chunk)})"
        for chunk in (js_chunks or [""])
    )
    foreground_lines = (
        """
                set active tab index of window foundWindowIndex to foundTabIndex
                set index of window foundWindowIndex to 1
                        activate
"""
        if foreground
        else ""
    )
    return f"""
on run argv
    set appName to item 1 of argv
    set targetUrl to item 2 of argv
    set jsCode to ""
{chunk_lines}
    using terms from application "{terms_app_name}"
        tell application appName
            set foundWindowIndex to 0
            set foundTabIndex to 0
            repeat with windowIndex from 1 to (count of windows)
                set tabCount to count of tabs of window windowIndex
                repeat with tabIndex from 1 to tabCount
                    if my bridgeUrlMatches((URL of tab tabIndex of window windowIndex) as text, targetUrl) then
                        if foundWindowIndex is 0 then
                            set foundWindowIndex to windowIndex
                            set foundTabIndex to tabIndex
                        end if
                    end if
                end repeat
            end repeat
            if foundWindowIndex is not 0 then
                repeat with windowIndex from (count of windows) to 1 by -1
                    set tabCount to count of tabs of window windowIndex
                    repeat with tabIndex from tabCount to 1 by -1
                        if windowIndex is not foundWindowIndex or tabIndex is not foundTabIndex then
                            if my bridgeUrlMatches((URL of tab tabIndex of window windowIndex) as text, targetUrl) then
                                close tab tabIndex of window windowIndex
                            end if
                        end if
                    end repeat
                end repeat
{foreground_lines.rstrip()}
                return execute tab foundTabIndex of window foundWindowIndex javascript jsCode
            end if
        end tell
    end using terms from
    error "Bridge tab not found for " & targetUrl
end run

on bridgeUrlMatches(candidateUrl, targetUrl)
    if candidateUrl is targetUrl then return true
    if candidateUrl starts with (targetUrl & "?") then return true
    if candidateUrl starts with (targetUrl & "#") then return true
    return false
end bridgeUrlMatches
""".strip()


def _chrome_focus_tab_osascript_source(*, terms_app_name: str = "Google Chrome", foreground: bool = False) -> str:
    foreground_lines = (
        """
                set active tab index of window foundWindowIndex to foundTabIndex
                set index of window foundWindowIndex to 1
                        activate
"""
        if foreground
        else ""
    )
    return f"""
on run argv
    set appName to item 1 of argv
    set targetUrl to item 2 of argv
    using terms from application "{terms_app_name}"
        tell application appName
            set foundWindowIndex to 0
            set foundTabIndex to 0
            repeat with windowIndex from 1 to (count of windows)
                set tabCount to count of tabs of window windowIndex
                repeat with tabIndex from 1 to tabCount
                    if my bridgeUrlMatches((URL of tab tabIndex of window windowIndex) as text, targetUrl) then
                        if foundWindowIndex is 0 then
                            set foundWindowIndex to windowIndex
                            set foundTabIndex to tabIndex
                        end if
                    end if
                end repeat
            end repeat
            if foundWindowIndex is not 0 then
                repeat with windowIndex from (count of windows) to 1 by -1
                    set tabCount to count of tabs of window windowIndex
                    repeat with tabIndex from tabCount to 1 by -1
                        if windowIndex is not foundWindowIndex or tabIndex is not foundTabIndex then
                            if my bridgeUrlMatches((URL of tab tabIndex of window windowIndex) as text, targetUrl) then
                                close tab tabIndex of window windowIndex
                            end if
                        end if
                    end repeat
                end repeat
{foreground_lines.rstrip()}
                return "FOUND"
            end if
        end tell
    end using terms from
    error "Bridge tab not found for " & targetUrl
end run

on bridgeUrlMatches(candidateUrl, targetUrl)
    if candidateUrl is targetUrl then return true
    if candidateUrl starts with (targetUrl & "?") then return true
    if candidateUrl starts with (targetUrl & "#") then return true
    return false
end bridgeUrlMatches
""".strip()


def detect_preferred_browser_channel() -> str:
    candidates = [
        ("chrome", Path("/Applications/Google Chrome.app")),
        ("brave", Path("/Applications/Brave Browser.app")),
        ("msedge", Path("/Applications/Microsoft Edge.app")),
    ]
    for channel, app_path in candidates:
        if app_path.exists():
            return channel
    return ""


def _looks_like_chrome_applescript_transport_failure(message: str) -> bool:
    normalized = str(message or "").casefold()
    if not normalized:
        return False
    launchservices_markers = (
        "(-10827)",
        "klsnoexecutableerr",
        "cannot be opened for an unexpected reason",
    )
    browser_identity_markers = (
        'application "google chrome"',
        'application "brave browser"',
        'application "safari"',
        "/applications/google chrome.app",
        "/applications/brave browser.app",
        "/applications/safari.app",
        "unable to find application named 'google chrome'",
        "unable to find application named 'brave browser'",
        "unable to find application named 'safari'",
    )
    if any(marker in normalized for marker in launchservices_markers) and any(
        marker in normalized for marker in browser_identity_markers
    ):
        return True
    required_markers = (
        "com.apple.hiservices-xpcservice",
        "connection invalid",
    )
    transport_markers = (
        'application "google chrome"',
        'application "brave browser"',
        "active tab",
        "front window",
        "window 1",
        "syntax error",
        "kann nicht gelesen werden",
    )
    return all(marker in normalized for marker in required_markers) and any(
        marker in normalized for marker in transport_markers
    )


def _looks_like_chrome_applescript_timeout_failure(message: str) -> bool:
    normalized = str(message or "").casefold()
    if not normalized:
        return False
    timeout_markers = (
        "appleevent lieferte eine zeitüberschreitung",
        "appleevent timed out",
        "apple event timed out",
        "(-1712)",
    )
    browser_markers = (
        "google chrome",
        "brave browser",
        "microsoft edge",
    )
    return any(marker in normalized for marker in timeout_markers) and any(
        marker in normalized for marker in browser_markers
    )


def _looks_like_applescript_runtime_transport_failure(exc: Exception) -> bool:
    message = str(exc or "").strip()
    if not message:
        return False
    normalized = message.casefold()
    if "macos browser apple events automation is not functioning on this host" in normalized:
        return True
    return _looks_like_chrome_applescript_transport_failure(message) or _looks_like_chrome_applescript_timeout_failure(message)


def _browser_bundle_label_from_error(message: str) -> str:
    normalized = str(message or "").casefold()
    if "/applications/google chrome.app" in normalized or "google chrome" in normalized:
        return "Google Chrome (`/Applications/Google Chrome.app`)"
    if "/applications/brave browser.app" in normalized or "brave browser" in normalized:
        return "Brave Browser (`/Applications/Brave Browser.app`)"
    if "/applications/microsoft edge.app" in normalized or "microsoft edge" in normalized:
        return "Microsoft Edge (`/Applications/Microsoft Edge.app`)"
    return "the configured normal browser app bundle"


def _looks_like_browser_launchservices_boot_failure(message: str) -> bool:
    normalized = str(message or "").casefold()
    if not normalized:
        return False
    if "-10827" not in normalized and "klsnoexecutableerr" not in normalized:
        return False
    return any(
        marker in normalized
        for marker in (
            "unable to find application named",
            "cannot be opened for an unexpected reason",
            "/applications/google chrome.app",
            "/applications/brave browser.app",
            "/applications/microsoft edge.app",
        )
    )


def _applescript_transport_failure_detail(message: str) -> str:
    if _looks_like_browser_launchservices_boot_failure(message):
        bundle_label = _browser_bundle_label_from_error(message)
        return f"LaunchServices could not open {bundle_label}; macOS returned `-10827` before tab inspection could begin."
    if _looks_like_chrome_applescript_timeout_failure(message):
        return "The Bridge reached the configured browser tab, but Chrome did not answer the Apple Event before macOS timed the call out (`-1712`)."
    if _looks_like_chrome_applescript_transport_failure(message):
        return "The Bridge can see the configured browser app, but app/window/tab scripting is failing before DOM inspection."
    return ""


def _applescript_transport_failure_message(message: str) -> str:
    detail = _applescript_transport_failure_detail(message) or (
        "The Bridge can see the configured browser app, but app/window/tab scripting is failing before DOM inspection."
    )
    return (
        "macOS browser Apple Events automation is not functioning on this host. "
        f"{detail} "
        "Use a host/browser setup where normal-browser Apple Events automation works, or resume through a different working browser transport."
    )


def _combined_browser_transport_failure_message(primary_exc: Exception, fallback_exc: Exception) -> str:
    primary_message = str(primary_exc or "").strip()
    fallback_message = str(fallback_exc or "").strip()
    if _looks_like_playwright_launch_transport_failure(fallback_message):
        primary_detail = _applescript_transport_failure_detail(primary_message) or (
            "The normal-browser Apple Events path failed during live tab inspection."
        )
        return (
            "macOS browser automation is not functioning on this host. "
            f"{primary_detail} "
            "The Playwright "
            "persistent-profile fallback also failed to launch from this Codex process due to host or sandbox browser transport restrictions. "
            "Use a host/browser setup where either normal-browser Apple Events or sandbox-compatible Playwright browser launch works."
        )
    return primary_message or fallback_message or "Browser automation failed."


def _looks_like_playwright_launch_transport_failure(message: str) -> bool:
    normalized = str(message or "").casefold()
    if not normalized:
        return False
    launch_markers = (
        "launch_persistent_context",
        "target page, context or browser has been closed",
        "crashpad/settings.dat",
        "bootstrap_check_in",
        "machportrendezvousserver",
        "mach_port_rendezvous",
        "connection invalid error for service com.apple.hiservices-xpcservice",
        "permission denied (1100)",
    )
    return any(marker in normalized for marker in launch_markers)


def _looks_like_host_browser_transport_failure_message(message: str) -> bool:
    normalized = str(message or "").casefold()
    if not normalized:
        return False
    if "macos browser automation is not functioning on this host" in normalized:
        return True
    if "macos browser apple events automation is not functioning on this host" in normalized:
        return True
    return (
        _looks_like_chrome_applescript_transport_failure(message)
        or _looks_like_chrome_applescript_timeout_failure(message)
        or _looks_like_playwright_launch_transport_failure(message)
    )


def _probe_host_automation_failures() -> list[str]:
    if sys.platform != "darwin":
        return []
    failures: list[str] = []
    system_events_failure = _probe_system_events_failure()
    if system_events_failure:
        failures.append(system_events_failure)
    screencapture_failure = _probe_screencapture_failure()
    if screencapture_failure:
        failures.append(screencapture_failure)
    return failures


def _probe_system_events_failure() -> str:
    try:
        result = subprocess.run(
            [
                "/usr/bin/osascript",
                "-e",
                'tell application "System Events" to get name of first application process whose frontmost is true',
            ],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        return f"system_events=os_error:{type(exc).__name__}"
    if result.returncode == 0:
        return ""
    return f"system_events={_summarize_host_probe_message(result.stderr or result.stdout)}"


def _probe_screencapture_failure() -> str:
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="bridge-host-probe-", suffix=".png", delete=False) as handle:
            temp_path = Path(handle.name)
        result = subprocess.run(
            ["screencapture", "-x", str(temp_path)],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        return f"screencapture=os_error:{type(exc).__name__}"
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    if result.returncode == 0:
        return ""
    return f"screencapture={_summarize_host_probe_message(result.stderr or result.stdout)}"


def _summarize_host_probe_message(message: str) -> str:
    normalized = " ".join(str(message or "").strip().split())
    lowered = normalized.casefold()
    if "-10827" in lowered:
        return "-10827"
    if "could not create image from display" in lowered:
        return "display_capture_failed"
    if "operation not permitted" in lowered:
        return "operation_not_permitted"
    if "connection invalid" in lowered:
        return "connection_invalid"
    if not normalized:
        return "unknown"
    return normalized[:80]
