from __future__ import annotations

import os
import queue
import sys
import threading
from collections.abc import Callable
from typing import Any

from .browser_applescript import AppleScriptChromeChatAdapter
from .browser_playwright import PlaywrightChatAdapter
from .browser_support import (
    _chrome_osascript_source,
    _combined_browser_transport_failure_message,
    _looks_like_applescript_runtime_transport_failure,
    _looks_like_chrome_applescript_transport_failure,
    _looks_like_playwright_launch_transport_failure,
    describe_browser_transport,
    detect_preferred_browser_channel,
    enrich_browser_blocker_reason,
    is_known_delivery_error,
    normalize_stop_command,
    normalize_stop_command_event,
    stop_command_already_processed,
)


_PLAYWRIGHT_SYNC_API_ASYNCIO_LOOP_MARKER = "using Playwright Sync API inside the asyncio loop"


def _looks_like_playwright_sync_api_asyncio_loop_failure(exc: Exception) -> bool:
    return _PLAYWRIGHT_SYNC_API_ASYNCIO_LOOP_MARKER.casefold() in str(exc or "").casefold()


class _ThreadedChatAdapter:
    def __init__(self, adapter_factory: Callable[[], Any]) -> None:
        self._adapter_factory = adapter_factory
        self._requests: queue.Queue[tuple[str, tuple[Any, ...], dict[str, Any], queue.Queue[tuple[bool, Any]]]] = (
            queue.Queue()
        )
        self._ready: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)
        self._closed = False
        self._thread = threading.Thread(target=self._run, daemon=True, name="bridge-playwright-adapter")
        self._thread.start()
        ok, payload = self._ready.get()
        if not ok:
            raise payload

    def __getattr__(self, method_name: str):
        if method_name.startswith("_"):
            raise AttributeError(method_name)

        def _method(*args: Any, **kwargs: Any):
            return self._call(method_name, *args, **kwargs)

        return _method

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._call("__close__")
        finally:
            self._thread.join(timeout=2.0)

    def _call(self, method_name: str, *args: Any, **kwargs: Any):
        if self._closed and method_name != "__close__":
            raise RuntimeError("Threaded browser adapter is closed.")
        result_queue: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)
        self._requests.put((method_name, args, kwargs, result_queue))
        ok, payload = result_queue.get()
        if ok:
            return payload
        raise payload

    def _run(self) -> None:
        try:
            adapter = self._adapter_factory()
        except BaseException as exc:  # pragma: no cover - defensive startup guard
            self._ready.put((False, exc))
            return
        self._ready.put((True, None))
        while True:
            method_name, args, kwargs, result_queue = self._requests.get()
            try:
                if method_name == "__close__":
                    close = getattr(adapter, "close", None)
                    if callable(close):
                        close()
                    result_queue.put((True, None))
                    return
                method = getattr(adapter, method_name)
                result_queue.put((True, method(*args, **kwargs)))
            except BaseException as exc:
                result_queue.put((False, exc))


class RoutedChatAdapter:
    def __init__(self, *, headless: bool = True, selectors: dict[str, str] | None = None) -> None:
        self.headless = headless
        self.selectors = selectors
        self._active_adapter = None
        self._binding = None

    def open_chat(self, binding: Any) -> None:
        self._binding = binding
        adapter = self._select_adapter(binding)
        try:
            adapter.open_chat(binding)
            self._active_adapter = adapter
            return
        except Exception as exc:
            if isinstance(adapter, PlaywrightChatAdapter) and _looks_like_playwright_sync_api_asyncio_loop_failure(exc):
                threaded_adapter = self._threaded_playwright_adapter()
                threaded_adapter.open_chat(binding)
                self._active_adapter = threaded_adapter
                return
            if isinstance(adapter, AppleScriptChromeChatAdapter):
                profile_path = str(getattr(binding, "browser_profile_path", "") or "").strip()
                if not profile_path or not self._allow_playwright_fallback_from_applescript():
                    raise
                fallback_adapter = self._open_playwright_adapter(binding)
                self._active_adapter = fallback_adapter
                return
            if not self._can_fallback_from_playwright_to_applescript(binding, exc):
                raise
            fallback_adapter = AppleScriptChromeChatAdapter(selectors=self.selectors)
            fallback_adapter.open_chat(binding)
            self._active_adapter = fallback_adapter

    def close(self) -> None:
        if self._active_adapter is None:
            return
        close = getattr(self._active_adapter, "close", None)
        if callable(close):
            close()
        self._active_adapter = None
        self._binding = None

    def read_latest_assistant_message(self, session: Any) -> dict[str, str]:
        return self._call_active_adapter_method("read_latest_assistant_message", session)

    def read_latest_user_message(self, session: Any) -> dict[str, str]:
        return self._call_active_adapter_method("read_latest_user_message", session)

    def read_recent_user_messages(self, session: Any, limit: int = 8) -> list[dict[str, str]]:
        read_recent = getattr(self._require_active_adapter(), "read_recent_user_messages", None)
        if not callable(read_recent):
            latest = self.read_latest_user_message(session)
            return [latest] if latest else []
        return self._call_active_adapter_method("read_recent_user_messages", session, limit=limit)

    def assistant_response_in_progress(self, session: Any) -> bool:
        checker = getattr(self._require_active_adapter(), "assistant_response_in_progress", None)
        if not callable(checker):
            return False
        return bool(self._call_active_adapter_method("assistant_response_in_progress", session))

    def cancel_assistant_response(self, session: Any) -> bool:
        canceller = getattr(self._require_active_adapter(), "cancel_assistant_response", None)
        if not callable(canceller):
            return False
        return bool(self._call_active_adapter_method("cancel_assistant_response", session))

    def retry_latest_assistant_response(self, session: Any) -> bool:
        retrier = getattr(self._require_active_adapter(), "retry_latest_assistant_response", None)
        if not callable(retrier):
            return False
        return bool(self._call_active_adapter_method("retry_latest_assistant_response", session))

    def latest_assistant_response_error(self, session: Any) -> str:
        reader = getattr(self._require_active_adapter(), "latest_assistant_response_error", None)
        if not callable(reader):
            return ""
        return str(self._call_active_adapter_method("latest_assistant_response_error", session) or "")

    def prepare_return_packet_delivery(self, session: Any) -> dict[str, str]:
        preparer = getattr(self._require_active_adapter(), "prepare_return_packet_delivery", None)
        if not callable(preparer):
            return {"status": "ready"}
        return dict(self._call_active_adapter_method("prepare_return_packet_delivery", session))

    def post_user_message(self, session: Any, text: str, return_packet_id: str) -> dict[str, str]:
        return self._call_active_adapter_method("post_user_message", session, text, return_packet_id)

    def activate_chat(self, binding: Any | None = None) -> bool:
        adapter = self._require_active_adapter()
        target_binding = binding or self._binding
        activator = getattr(adapter, "activate_chat", None)
        if callable(activator):
            activator(target_binding)
            return True
        opener = getattr(adapter, "open_chat", None)
        if callable(opener) and target_binding is not None:
            opener(target_binding)
            return True
        return False

    def return_packet_visible(self, session: Any, return_packet_id: str) -> bool:
        return bool(self._call_active_adapter_method("return_packet_visible", session, return_packet_id))

    def current_chat_url(self, session: Any) -> str:
        reader = getattr(self._require_active_adapter(), "current_chat_url", None)
        if not callable(reader):
            return ""
        return str(self._call_active_adapter_method("current_chat_url", session) or "")

    def poll_stop_command(self, session: Any, stop_phrases: list[str]) -> dict[str, str] | None:
        return self._call_active_adapter_method("poll_stop_command", session, stop_phrases)

    def _require_active_adapter(self):
        if self._active_adapter is None:
            raise RuntimeError("No browser adapter is active.")
        return self._active_adapter

    def _call_active_adapter_method(self, method_name: str, *args: Any, **kwargs: Any):
        adapter = self._require_active_adapter()
        method = getattr(adapter, method_name, None)
        if not callable(method):
            raise RuntimeError(f"Active browser adapter does not implement `{method_name}`.")
        try:
            return method(*args, **kwargs)
        except Exception as exc:
            if not self._recover_from_runtime_browser_transport_failure(exc):
                raise
        adapter = self._require_active_adapter()
        method = getattr(adapter, method_name, None)
        if not callable(method):
            raise RuntimeError(f"Recovered browser adapter does not implement `{method_name}`.")
        try:
            return method(*args, **kwargs)
        except Exception as retry_exc:
            if not self._recover_from_runtime_browser_transport_failure(
                retry_exc,
                relaunch_normal_browser=True,
            ):
                raise
        adapter = self._require_active_adapter()
        method = getattr(adapter, method_name, None)
        if not callable(method):
            raise RuntimeError(f"Recovered browser adapter does not implement `{method_name}`.")
        return method(*args, **kwargs)

    def _select_adapter(self, binding: Any):
        browser_session_handle = str(getattr(binding, "browser_session_handle", "")).strip()
        if sys.platform == "darwin" and browser_session_handle and not self.headless:
            return AppleScriptChromeChatAdapter(selectors=self.selectors)
        return PlaywrightChatAdapter(headless=self.headless, selectors=self.selectors)

    def _recover_from_runtime_browser_transport_failure(
        self,
        exc: Exception,
        *,
        relaunch_normal_browser: bool = False,
    ) -> bool:
        binding = self._binding
        if not isinstance(self._active_adapter, AppleScriptChromeChatAdapter) or binding is None:
            return False
        if (
            sys.platform != "darwin"
            or not _looks_like_applescript_runtime_transport_failure(exc)
        ):
            return False

        normal_browser_error: Exception | None = None
        try:
            if relaunch_normal_browser and self._allow_normal_browser_relaunch_recovery():
                relaunch_chat = getattr(self._active_adapter, "relaunch_chat", None)
                if not callable(relaunch_chat):
                    raise RuntimeError("Active normal-browser adapter does not implement `relaunch_chat`.")
                relaunch_chat(binding)
            else:
                self._active_adapter.open_chat(binding)
            return True
        except Exception as recovery_exc:
            normal_browser_error = recovery_exc

        profile_path = str(getattr(binding, "browser_profile_path", "") or "").strip()
        if not profile_path or not self._allow_playwright_fallback_from_applescript():
            raise RuntimeError(
                f"{str(exc or '').strip()} Normal Chrome recovery also failed: {normal_browser_error}"
            ) from normal_browser_error

        try:
            fallback_adapter = self._open_playwright_adapter(binding)
        except Exception as fallback_exc:
            raise RuntimeError(_combined_browser_transport_failure_message(exc, fallback_exc)) from fallback_exc
        self._active_adapter = fallback_adapter
        return True

    def _can_fallback_from_playwright_to_applescript(self, binding: Any, exc: Exception) -> bool:
        browser_session_handle = str(getattr(binding, "browser_session_handle", "") or "").strip()
        if sys.platform != "darwin" or not browser_session_handle:
            return False
        return _looks_like_playwright_launch_transport_failure(str(exc or ""))

    def _allow_playwright_fallback_from_applescript(self) -> bool:
        value = str(os.environ.get("BRIDGE_ENABLE_PLAYWRIGHT_APPLESCRIPT_FALLBACK", "") or "").strip().casefold()
        return value in {"1", "true", "yes", "on"}

    def _allow_normal_browser_relaunch_recovery(self) -> bool:
        value = str(os.environ.get("BRIDGE_DISABLE_NORMAL_BROWSER_RELAUNCH_RECOVERY", "") or "").strip().casefold()
        return value not in {"1", "true", "yes", "on"}

    def _open_playwright_adapter(self, binding: Any):
        adapter = PlaywrightChatAdapter(headless=self.headless, selectors=self.selectors)
        try:
            adapter.open_chat(binding)
            return adapter
        except Exception as exc:
            close = getattr(adapter, "close", None)
            if callable(close):
                close()
            if not _looks_like_playwright_sync_api_asyncio_loop_failure(exc):
                raise
        threaded_adapter = self._threaded_playwright_adapter()
        threaded_adapter.open_chat(binding)
        return threaded_adapter

    def _threaded_playwright_adapter(self) -> _ThreadedChatAdapter:
        return _ThreadedChatAdapter(lambda: PlaywrightChatAdapter(headless=self.headless, selectors=self.selectors))
