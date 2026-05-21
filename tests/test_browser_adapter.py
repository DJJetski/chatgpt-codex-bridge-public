import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from mastermind_bridge.orchestrator.browser import (
    AppleScriptChromeChatAdapter,
    PlaywrightChatAdapter,
    RoutedChatAdapter,
    _combined_browser_transport_failure_message,
    enrich_browser_blocker_reason,
    _looks_like_chrome_applescript_transport_failure,
    detect_preferred_browser_channel,
    is_known_delivery_error,
    normalize_stop_command,
)
from mastermind_bridge.orchestrator.browser_support import canonical_delivery_error_signature
from mastermind_bridge.orchestrator.browser_support import assistant_message_looks_like_retryable_error


def _applescript_adapter_module():
    return sys.modules[AppleScriptChromeChatAdapter.__module__]


class _FakeNode:
    def __init__(
        self,
        text: str = "",
        *,
        element_id: str = "",
        on_click=None,
        on_press=None,
        visible: bool = True,
        clear_fill_raises: bool = False,
    ):
        self.text = text
        self.element_id = element_id
        self.visible = visible
        self.clear_fill_raises = clear_fill_raises
        self.filled_text = ""
        self.clicks = 0
        self.presses: list[str] = []
        self.on_click = on_click
        self.on_press = on_press


class _FakeElement:
    def __init__(self, page, selector: str, index: int):
        self._page = page
        self._selector = selector
        self._index = index

    def _node(self) -> _FakeNode:
        nodes = self._page._nodes(self._selector)
        if self._index < 0 or self._index >= len(nodes):
            raise AssertionError(f"No node for selector {self._selector} at index {self._index}")
        return nodes[self._index]

    def inner_text(self) -> str:
        return self._node().text

    def is_visible(self) -> bool:
        return bool(self._node().visible)

    def get_attribute(self, name: str) -> str | None:
        if name == "id":
            return self._node().element_id or None
        return None

    def fill(self, text: str) -> None:
        if not text and self._node().clear_fill_raises:
            raise RuntimeError("fill-clear-failed")
        self._node().filled_text = text

    def press(self, key: str) -> None:
        node = self._node()
        node.presses.append(key)
        if key in {"Backspace", "Delete"}:
            node.filled_text = ""
            node.text = ""
        if node.on_press is not None:
            node.on_press(self._page, key)

    def click(self) -> None:
        node = self._node()
        node.clicks += 1
        if node.on_click is not None:
            node.on_click(self._page)


class _FakeLocatorGroup:
    def __init__(self, page, selector: str):
        self._page = page
        self._selector = selector

    def count(self) -> int:
        return len(self._page._nodes(self._selector))

    @property
    def last(self) -> _FakeElement:
        return _FakeElement(self._page, self._selector, self.count() - 1)

    def nth(self, index: int) -> _FakeElement:
        return _FakeElement(self._page, self._selector, index)


class _FakePage:
    def __init__(self, nodes_by_selector: dict[str, list[_FakeNode]], *, wait_callback=None, title_text: str = ""):
        self._nodes_by_selector = nodes_by_selector
        self.wait_callback = wait_callback
        self.title_text = title_text

    def _nodes(self, selector: str) -> list[_FakeNode]:
        return self._nodes_by_selector.setdefault(selector, [])

    def locator(self, selector: str) -> _FakeLocatorGroup:
        return _FakeLocatorGroup(self, selector)

    def wait_for_timeout(self, timeout_ms: int) -> None:
        if self.wait_callback is not None:
            self.wait_callback(self, timeout_ms)

    def title(self) -> str:
        return self.title_text


class _FakeBrowserContext:
    def __init__(self):
        self.pages = []
        self.closed = False
        self.new_page_calls = 0
        self.last_page = None

    def new_page(self):
        self.new_page_calls += 1
        page = _FakeGotoPage()
        self.last_page = page
        self.pages.append(page)
        return page

    def close(self):
        self.closed = True


class _FakeGotoPage:
    def __init__(self):
        self.goto_calls = []
        self.url = ""

    def goto(self, url: str, wait_until: str = "") -> None:
        self.goto_calls.append((url, wait_until))
        self.url = url


def _node_composer_value(node: _FakeNode) -> str:
    return node.filled_text if node.filled_text else node.text


def _select_composer_node(
    selectors: list[str],
    nodes_by_selector: dict[str, list[_FakeNode]],
    *,
    prefer_visible_across_selectors: bool,
) -> tuple[str, _FakeNode] | None:
    if prefer_visible_across_selectors:
        for selector in selectors:
            nodes = nodes_by_selector.get(selector, [])
            visible_nodes = [node for node in nodes if node.visible]
            if visible_nodes:
                return selector, visible_nodes[-1]
        for selector in selectors:
            nodes = nodes_by_selector.get(selector, [])
            if nodes:
                return selector, nodes[-1]
        return None

    for selector in selectors:
        nodes = nodes_by_selector.get(selector, [])
        visible_nodes = [node for node in nodes if node.visible]
        candidate = visible_nodes[-1] if visible_nodes else (nodes[-1] if nodes else None)
        if candidate is not None:
            return selector, candidate
    return None


def _make_composer_dom_runner(adapter, nodes_by_selector: dict[str, list[_FakeNode]]):
    def runner(js_code: str):
        selectors = adapter._selector_values("composer")
        prefer_visible_across_selectors = (
            "findLastNode(composerSelectors, { visibleOnly: true }) || findLastNode(composerSelectors, { visibleOnly: false })"
            in js_code
        )
        selected = _select_composer_node(
            selectors,
            nodes_by_selector,
            prefer_visible_across_selectors=prefer_visible_across_selectors,
        )
        if "present: true, value" in js_code:
            if selected is None:
                return {"present": False, "value": ""}
            _, node = selected
            return {"present": True, "value": _node_composer_value(node)}
        if "return JSON.stringify({ cleared: true })" in js_code:
            if selected is None:
                return {"cleared": False}
            _, node = selected
            node.text = ""
            node.filled_text = ""
            return {"cleared": True}
        raise AssertionError(f"Unsupported composer DOM script: {js_code[:120]!r}")

    return runner


class _FakeChromium:
    def __init__(self):
        self.launch_calls = []
        self.launch_persistent_context_calls = []
        self.persistent_context = _FakeBrowserContext()
        self.browser = _FakeBrowser()

    def launch_persistent_context(self, profile_path: str, **kwargs):
        self.launch_persistent_context_calls.append((profile_path, kwargs))
        return self.persistent_context

    def launch(self, **kwargs):
        self.launch_calls.append(kwargs)
        return self.browser


class _FakeBrowser:
    def __init__(self):
        self.new_context_calls = 0
        self.context = _FakeBrowserContext()

    def new_context(self):
        self.new_context_calls += 1
        return self.context


class _FakePlaywrightRuntime:
    def __init__(self):
        self.chromium = _FakeChromium()
        self.stopped = False

    def stop(self):
        self.stopped = True


class _FakePlaywrightFactory:
    def __init__(self):
        self.runtime = _FakePlaywrightRuntime()

    def start(self):
        return self.runtime


class BrowserAdapterTests(unittest.TestCase):
    def test_is_known_delivery_error_matches_exact_signatures(self):
        self.assertTrue(is_known_delivery_error("Reasoning failed", ["Reasoning failed"]))
        self.assertFalse(is_known_delivery_error("Temporary failure", ["Reasoning failed"]))

    def test_canonical_delivery_error_signature_normalizes_network_error_surface(self):
        self.assertEqual(
            canonical_delivery_error_signature("Network error\nErneut versuchen"),
            "ChatGPT in-page send failed.",
        )

    def test_playwright_latest_delivery_error_falls_back_to_retry_button_context(self):
        adapter = PlaywrightChatAdapter.__new__(PlaywrightChatAdapter)
        adapter.selectors = {"delivery_error": [".delivery-error"]}
        adapter._page = _FakePage({".delivery-error": []})
        adapter._run_json_script = lambda _js: {"text": "Reasoning failed\nErneut versuchen"}

        self.assertEqual(
            adapter._latest_delivery_error_text(),
            "Reasoning failed\nErneut versuchen",
        )

    def test_applescript_latest_delivery_error_falls_back_to_retry_button_context(self):
        adapter = AppleScriptChromeChatAdapter()
        adapter.selectors = {"delivery_error": [".delivery-error"]}
        adapter._run_json_script = lambda _js: {"text": "Reasoning failed\nErneut versuchen"}

        self.assertEqual(
            adapter._latest_delivery_error_text(),
            "Reasoning failed\nErneut versuchen",
        )

    def test_assistant_message_looks_like_retryable_error_surface(self):
        self.assertTrue(
            assistant_message_looks_like_retryable_error(
                "A network error occurred. Please check your connection and try again.\n\nErneut versuchen"
            )
        )
        self.assertTrue(assistant_message_looks_like_retryable_error("Reasoning failed"))
        self.assertFalse(
            assistant_message_looks_like_retryable_error("Assistant reply without DOM id.")
        )

    def test_normalize_stop_command_requires_exact_phrase(self):
        stop_phrases = ["stop", "pause", "stop after this cycle"]

        self.assertEqual(normalize_stop_command("pause", stop_phrases), "pause")
        self.assertIsNone(normalize_stop_command("please pause now", stop_phrases))

    def test_playwright_adapter_raises_clean_error_when_playwright_missing(self):
        import builtins

        original_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "playwright.sync_api":
                raise ModuleNotFoundError("No module named 'playwright'")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            with self.assertRaises(RuntimeError):
                PlaywrightChatAdapter(headless=True)

    def test_read_latest_assistant_message_uses_selector_fallbacks(self):
        adapter = PlaywrightChatAdapter.__new__(PlaywrightChatAdapter)
        adapter.selectors = {
            "assistant_message": ["[data-message-author-role='assistant']", ".assistant-fallback"],
        }
        adapter._page = _FakePage(
            {
                ".assistant-fallback": [_FakeNode("Assistant reply without DOM id.")],
            }
        )

        message = adapter.read_latest_assistant_message(session=None)

        self.assertEqual(message["text"], "Assistant reply without DOM id.")
        self.assertTrue(message["message_anchor"].startswith("assistant-0-"))

    def test_read_latest_assistant_message_disambiguates_repeated_text_without_dom_id(self):
        adapter = PlaywrightChatAdapter.__new__(PlaywrightChatAdapter)
        adapter.selectors = {
            "assistant_message": [".assistant-fallback"],
        }
        adapter._page = _FakePage(
            {
                ".assistant-fallback": [
                    _FakeNode("bridge-control"),
                    _FakeNode("bridge-control"),
                ],
            }
        )

        message = adapter.read_latest_assistant_message(session=None)

        self.assertEqual(message["text"], "bridge-control")
        self.assertTrue(message["message_anchor"].startswith("assistant-1-"))

    def test_read_latest_assistant_message_surfaces_chatgpt_challenge_page(self):
        adapter = PlaywrightChatAdapter.__new__(PlaywrightChatAdapter)
        adapter.selectors = {
            "assistant_message": ["[data-message-author-role='assistant']"],
        }
        adapter._page = _FakePage(
            {
                "body": [_FakeNode("Just a moment...")],
            },
            title_text="Just a moment...",
        )

        with self.assertRaisesRegex(RuntimeError, "interstitial or challenge page"):
            adapter.read_latest_assistant_message(session=None)

    def test_playwright_assistant_response_in_progress_checks_streaming_animation_nodes(self):
        adapter = PlaywrightChatAdapter.__new__(PlaywrightChatAdapter)
        adapter.selectors = {
            "assistant_message": [".assistant-fallback"],
        }
        captured = {}

        def fake_run_json_script(js_code: str):
            captured["js_code"] = js_code
            return {"in_progress": True}

        adapter._run_json_script = fake_run_json_script

        self.assertTrue(adapter.assistant_response_in_progress(session=None))
        self.assertIn("streaming-animation", captured["js_code"])

    def test_open_chat_prefers_binding_browser_channel_for_persistent_profile(self):
        binding = type(
            "Binding",
            (),
            {
                "chat_url": "https://chatgpt.com/c/project/test-chat",
                "browser_profile_path": "/tmp/profile",
                "browser_channel": "chrome",
            },
        )()
        factory = _FakePlaywrightFactory()
        adapter = PlaywrightChatAdapter.__new__(PlaywrightChatAdapter)
        adapter._sync_playwright = lambda: factory
        adapter._playwright_context = None
        adapter._browser_context = None
        adapter._page = None
        adapter.headless = False
        adapter.selectors = {}

        adapter.open_chat(binding)

        chromium = factory.runtime.chromium
        self.assertEqual(
            chromium.launch_persistent_context_calls,
            [("/tmp/profile", {"headless": False, "channel": "chrome"})],
        )
        self.assertEqual(factory.runtime.chromium.persistent_context.last_page.goto_calls, [("https://chatgpt.com/c/project/test-chat", "domcontentloaded")])

    def test_open_chat_retries_persistent_profile_without_channel_after_transport_failure(self):
        binding = type(
            "Binding",
            (),
            {
                "chat_url": "https://chatgpt.com/c/project/test-chat",
                "browser_profile_path": "/tmp/profile",
                "browser_channel": "chrome",
            },
        )()
        factory = _FakePlaywrightFactory()
        adapter = PlaywrightChatAdapter.__new__(PlaywrightChatAdapter)
        adapter._sync_playwright = lambda: factory
        adapter._playwright_context = None
        adapter._browser_context = None
        adapter._page = None
        adapter.headless = False
        adapter.selectors = {}

        chromium = factory.runtime.chromium
        original_launch = chromium.launch_persistent_context
        calls: list[tuple[str, dict[str, object]]] = []

        def fake_launch(profile_path: str, **kwargs):
            calls.append((profile_path, kwargs))
            if len(calls) == 1:
                raise RuntimeError(
                    "BrowserType.launch_persistent_context: Target page, context or browser has been closed\n"
                    "open /tmp/example-home/Library/Application Support/Google/Chrome/Crashpad/settings.dat: Operation not permitted (1)"
                )
            return original_launch(profile_path, **kwargs)

        chromium.launch_persistent_context = fake_launch

        adapter.open_chat(binding)

        self.assertEqual(
            calls,
            [
                ("/tmp/profile", {"headless": False, "channel": "chrome"}),
                ("/tmp/profile", {"headless": False}),
            ],
        )
        self.assertEqual(factory.runtime.chromium.persistent_context.last_page.goto_calls, [("https://chatgpt.com/c/project/test-chat", "domcontentloaded")])

    def test_playwright_current_chat_url_returns_page_url(self):
        adapter = PlaywrightChatAdapter.__new__(PlaywrightChatAdapter)
        page = _FakeGotoPage()
        page.goto("https://chatgpt.com/g/g-p-test/c/new-chat", wait_until="domcontentloaded")
        adapter._page = page

        self.assertEqual(
            adapter.current_chat_url(session=None),
            "https://chatgpt.com/g/g-p-test/c/new-chat",
        )

    def test_routed_adapter_uses_system_browser_backend_when_session_handle_is_present(self):
        binding = type(
            "Binding",
            (),
            {
                "chat_url": "https://chatgpt.com/c/project/test-chat",
                "browser_profile_path": "",
                "browser_channel": "chrome",
                "browser_session_handle": "default",
            },
        )()
        adapter = RoutedChatAdapter(headless=False)

        with patch("mastermind_bridge.orchestrator.browser.sys.platform", "darwin"), patch(
            "mastermind_bridge.orchestrator.browser.AppleScriptChromeChatAdapter.open_chat"
        ) as open_chat:
            adapter.open_chat(binding)

        self.assertIsInstance(adapter._active_adapter, AppleScriptChromeChatAdapter)
        open_chat.assert_called_once_with(binding)

    def test_routed_adapter_prefers_playwright_for_headless_runs_even_with_session_handle(self):
        binding = type(
            "Binding",
            (),
            {
                "chat_url": "https://chatgpt.com/c/project/test-chat",
                "browser_profile_path": "/tmp/profile",
                "browser_channel": "chrome",
                "browser_session_handle": "default",
            },
        )()
        adapter = RoutedChatAdapter(headless=True)

        with patch("mastermind_bridge.orchestrator.browser.sys.platform", "darwin"), patch(
            "mastermind_bridge.orchestrator.browser.PlaywrightChatAdapter.open_chat"
        ) as open_chat:
            adapter.open_chat(binding)

        self.assertIsInstance(adapter._active_adapter, PlaywrightChatAdapter)
        open_chat.assert_called_once_with(binding)

    def test_routed_adapter_retries_playwright_in_worker_thread_for_asyncio_loop_error(self):
        binding = type(
            "Binding",
            (),
            {
                "chat_url": "https://chatgpt.com/c/project/test-chat",
                "browser_profile_path": "/tmp/profile",
                "browser_channel": "chrome",
                "browser_session_handle": "default",
            },
        )()
        adapter = RoutedChatAdapter(headless=True)

        with patch("mastermind_bridge.orchestrator.browser.sys.platform", "darwin"), patch(
            "mastermind_bridge.orchestrator.browser.PlaywrightChatAdapter.open_chat",
            side_effect=[
                RuntimeError("It looks like you are using Playwright Sync API inside the asyncio loop."),
                None,
            ],
        ) as open_chat:
            adapter.open_chat(binding)

        self.assertEqual(type(adapter._active_adapter).__name__, "_ThreadedChatAdapter")
        self.assertEqual(open_chat.call_count, 2)
        adapter.close()

    def test_routed_adapter_respects_explicit_opt_out_when_applescript_open_fails(self):
        binding = type(
            "Binding",
            (),
            {
                "chat_url": "https://chatgpt.com/c/project/test-chat",
                "browser_profile_path": "/tmp/profile",
                "browser_channel": "chrome",
                "browser_session_handle": "default",
            },
        )()
        adapter = RoutedChatAdapter(headless=False)

        with patch.dict(
            "mastermind_bridge.orchestrator.browser.os.environ",
            {"BRIDGE_ENABLE_PLAYWRIGHT_APPLESCRIPT_FALLBACK": "0"},
            clear=False,
        ), patch("mastermind_bridge.orchestrator.browser.sys.platform", "darwin"), patch(
            "mastermind_bridge.orchestrator.browser.AppleScriptChromeChatAdapter.open_chat",
            side_effect=RuntimeError("Apple Events failed"),
        ) as applescript_open, patch(
            "mastermind_bridge.orchestrator.browser.PlaywrightChatAdapter.open_chat"
        ) as playwright_open:
            with self.assertRaisesRegex(RuntimeError, "Apple Events failed"):
                adapter.open_chat(binding)

        self.assertIsNone(adapter._active_adapter)
        applescript_open.assert_called_once_with(binding)
        playwright_open.assert_not_called()

    def test_routed_adapter_does_not_fall_back_to_playwright_when_applescript_open_fails_by_default(self):
        binding = type(
            "Binding",
            (),
            {
                "chat_url": "https://chatgpt.com/c/project/test-chat",
                "browser_profile_path": "/tmp/profile",
                "browser_channel": "chrome",
                "browser_session_handle": "default",
            },
        )()
        adapter = RoutedChatAdapter(headless=False)

        with patch("mastermind_bridge.orchestrator.browser.sys.platform", "darwin"), patch(
            "mastermind_bridge.orchestrator.browser.AppleScriptChromeChatAdapter.open_chat",
            side_effect=RuntimeError("Apple Events failed"),
        ) as applescript_open, patch(
            "mastermind_bridge.orchestrator.browser.PlaywrightChatAdapter.open_chat"
        ) as playwright_open:
            with self.assertRaisesRegex(RuntimeError, "Apple Events failed"):
                adapter.open_chat(binding)

        self.assertIsNone(adapter._active_adapter)
        applescript_open.assert_called_once_with(binding)
        playwright_open.assert_not_called()

    def test_routed_adapter_can_fall_back_to_playwright_when_applescript_open_fails_if_enabled(self):
        binding = type(
            "Binding",
            (),
            {
                "chat_url": "https://chatgpt.com/c/project/test-chat",
                "browser_profile_path": "/tmp/profile",
                "browser_channel": "chrome",
                "browser_session_handle": "default",
            },
        )()
        adapter = RoutedChatAdapter(headless=False)

        with patch.dict(
            "mastermind_bridge.orchestrator.browser.os.environ",
            {"BRIDGE_ENABLE_PLAYWRIGHT_APPLESCRIPT_FALLBACK": "1"},
            clear=False,
        ), patch("mastermind_bridge.orchestrator.browser.sys.platform", "darwin"), patch(
            "mastermind_bridge.orchestrator.browser.AppleScriptChromeChatAdapter.open_chat",
            side_effect=RuntimeError("Apple Events failed"),
        ) as applescript_open, patch(
            "mastermind_bridge.orchestrator.browser.PlaywrightChatAdapter.open_chat"
        ) as playwright_open:
            adapter.open_chat(binding)

        self.assertIsInstance(adapter._active_adapter, PlaywrightChatAdapter)
        applescript_open.assert_called_once_with(binding)
        playwright_open.assert_called_once_with(binding)

    def test_routed_adapter_falls_back_to_applescript_when_headless_playwright_persistent_launch_crashes(self):
        binding = type(
            "Binding",
            (),
            {
                "chat_url": "https://chatgpt.com/c/project/test-chat",
                "browser_profile_path": "/tmp/profile",
                "browser_channel": "chrome",
                "browser_session_handle": "default",
            },
        )()
        adapter = RoutedChatAdapter(headless=True)

        with patch("mastermind_bridge.orchestrator.browser.sys.platform", "darwin"), patch(
            "mastermind_bridge.orchestrator.browser.PlaywrightChatAdapter.open_chat",
            side_effect=Exception(
                "BrowserType.launch_persistent_context: Target page, context or browser has been closed\n"
                "open /tmp/example-home/Library/Application Support/Google/Chrome/Crashpad/settings.dat: Operation not permitted (1)"
            ),
        ) as playwright_open, patch(
            "mastermind_bridge.orchestrator.browser.AppleScriptChromeChatAdapter.open_chat"
        ) as applescript_open:
            adapter.open_chat(binding)

        self.assertIsInstance(adapter._active_adapter, AppleScriptChromeChatAdapter)
        playwright_open.assert_called_once_with(binding)
        applescript_open.assert_called_once_with(binding)

    def test_routed_adapter_does_not_fall_back_to_applescript_for_non_transport_playwright_errors(self):
        binding = type(
            "Binding",
            (),
            {
                "chat_url": "https://chatgpt.com/c/project/test-chat",
                "browser_profile_path": "/tmp/profile",
                "browser_channel": "chrome",
                "browser_session_handle": "default",
            },
        )()
        adapter = RoutedChatAdapter(headless=True)

        with patch("mastermind_bridge.orchestrator.browser.sys.platform", "darwin"), patch(
            "mastermind_bridge.orchestrator.browser.PlaywrightChatAdapter.open_chat",
            side_effect=RuntimeError("ChatGPT DOM contract missing `assistant_message` selector match."),
        ) as playwright_open, patch(
            "mastermind_bridge.orchestrator.browser.AppleScriptChromeChatAdapter.open_chat"
        ) as applescript_open:
            with self.assertRaisesRegex(RuntimeError, "assistant_message"):
                adapter.open_chat(binding)

        playwright_open.assert_called_once_with(binding)
        applescript_open.assert_not_called()

    def test_routed_adapter_respects_explicit_opt_out_for_runtime_applescript_playwright_fallback(self):
        binding = type(
            "Binding",
            (),
            {
                "chat_url": "https://chatgpt.com/c/project/test-chat",
                "browser_profile_path": "/tmp/profile",
                "browser_channel": "chrome",
                "browser_session_handle": "default",
            },
        )()
        adapter = RoutedChatAdapter(headless=False)
        active_adapter = AppleScriptChromeChatAdapter()
        active_adapter._binding = binding
        adapter._active_adapter = active_adapter
        adapter._binding = binding

        with patch.dict(
            "mastermind_bridge.orchestrator.browser.os.environ",
            {"BRIDGE_ENABLE_PLAYWRIGHT_APPLESCRIPT_FALLBACK": "0"},
            clear=False,
        ), patch("mastermind_bridge.orchestrator.browser.sys.platform", "darwin"), patch(
            "mastermind_bridge.orchestrator.browser.AppleScriptChromeChatAdapter.read_latest_assistant_message",
            side_effect=RuntimeError(
                "macOS browser Apple Events automation is not functioning on this host. "
                "The Bridge can see the configured browser app, but app/window/tab scripting is failing before DOM inspection. "
                "Use a host/browser setup where normal-browser Apple Events automation works, or resume through a different working browser transport."
            ),
        ), patch(
            "mastermind_bridge.orchestrator.browser.AppleScriptChromeChatAdapter.open_chat"
        ) as applescript_open, patch(
            "mastermind_bridge.orchestrator.browser.AppleScriptChromeChatAdapter.relaunch_chat"
        ) as applescript_relaunch, patch(
            "mastermind_bridge.orchestrator.browser.PlaywrightChatAdapter.open_chat"
        ) as playwright_open, patch(
            "mastermind_bridge.orchestrator.browser.PlaywrightChatAdapter.read_latest_assistant_message",
            return_value={"message_id": "assistant-1", "message_anchor": "assistant-1", "text": "bridge-control"},
        ) as playwright_read:
            with self.assertRaisesRegex(RuntimeError, "macOS browser Apple Events automation is not functioning on this host"):
                adapter.read_latest_assistant_message(session=None)

        self.assertIs(adapter._active_adapter, active_adapter)
        applescript_open.assert_called_once_with(binding)
        applescript_relaunch.assert_called_once_with(binding)
        playwright_open.assert_not_called()
        playwright_read.assert_not_called()

    def test_routed_adapter_recovers_runtime_applescript_transport_failure_via_normal_chrome(self):
        binding = type(
            "Binding",
            (),
            {
                "chat_url": "https://chatgpt.com/c/project/test-chat",
                "browser_profile_path": "/tmp/profile",
                "browser_channel": "chrome",
                "browser_session_handle": "default",
            },
        )()
        adapter = RoutedChatAdapter(headless=False)
        active_adapter = AppleScriptChromeChatAdapter()
        active_adapter._binding = binding
        adapter._active_adapter = active_adapter
        adapter._binding = binding

        with patch("mastermind_bridge.orchestrator.browser.sys.platform", "darwin"), patch(
            "mastermind_bridge.orchestrator.browser.AppleScriptChromeChatAdapter.read_latest_assistant_message",
            side_effect=[
                RuntimeError(
                    "macOS browser Apple Events automation is not functioning on this host. "
                    "The Bridge can see the configured browser app, but app/window/tab scripting is failing before DOM inspection. "
                    "Use a host/browser setup where normal-browser Apple Events automation works, or resume through a different working browser transport."
                ),
                {"message_id": "assistant-1", "message_anchor": "assistant-1", "text": "bridge-control"},
            ],
        ) as applescript_read, patch(
            "mastermind_bridge.orchestrator.browser.AppleScriptChromeChatAdapter.open_chat"
        ) as applescript_open, patch(
            "mastermind_bridge.orchestrator.browser.PlaywrightChatAdapter.open_chat"
        ) as playwright_open:
            result = adapter.read_latest_assistant_message(session=None)

        self.assertIs(adapter._active_adapter, active_adapter)
        applescript_open.assert_called_once_with(binding)
        playwright_open.assert_not_called()
        self.assertEqual(applescript_read.call_count, 2)
        self.assertEqual(result["text"], "bridge-control")

    def test_routed_adapter_relaunches_normal_chrome_when_soft_recovery_still_fails(self):
        binding = type(
            "Binding",
            (),
            {
                "chat_url": "https://chatgpt.com/c/project/test-chat",
                "browser_profile_path": "/tmp/profile",
                "browser_channel": "chrome",
                "browser_session_handle": "default",
            },
        )()
        adapter = RoutedChatAdapter(headless=False)
        active_adapter = AppleScriptChromeChatAdapter()
        active_adapter._binding = binding
        adapter._active_adapter = active_adapter
        adapter._binding = binding

        transport_error = RuntimeError(
            "macOS browser Apple Events automation is not functioning on this host. "
            "The Bridge reached the configured browser tab, but Chrome did not answer the Apple Event before macOS timed the call out (`-1712`). "
            "Use a host/browser setup where normal-browser Apple Events automation works, or resume through a different working browser transport."
        )
        with patch("mastermind_bridge.orchestrator.browser.sys.platform", "darwin"), patch(
            "mastermind_bridge.orchestrator.browser.AppleScriptChromeChatAdapter.read_latest_assistant_message",
            side_effect=[
                transport_error,
                transport_error,
                {"message_id": "assistant-1", "message_anchor": "assistant-1", "text": "bridge-control"},
            ],
        ) as applescript_read, patch(
            "mastermind_bridge.orchestrator.browser.AppleScriptChromeChatAdapter.open_chat"
        ) as applescript_open, patch(
            "mastermind_bridge.orchestrator.browser.AppleScriptChromeChatAdapter.relaunch_chat"
        ) as applescript_relaunch, patch(
            "mastermind_bridge.orchestrator.browser.PlaywrightChatAdapter.open_chat"
        ) as playwright_open:
            result = adapter.read_latest_assistant_message(session=None)

        self.assertIs(adapter._active_adapter, active_adapter)
        applescript_open.assert_called_once_with(binding)
        applescript_relaunch.assert_called_once_with(binding)
        playwright_open.assert_not_called()
        self.assertEqual(applescript_read.call_count, 3)
        self.assertEqual(result["text"], "bridge-control")

    def test_routed_adapter_can_recover_runtime_applescript_transport_failure_via_playwright_if_enabled(self):
        binding = type(
            "Binding",
            (),
            {
                "chat_url": "https://chatgpt.com/c/project/test-chat",
                "browser_profile_path": "/tmp/profile",
                "browser_channel": "chrome",
                "browser_session_handle": "default",
            },
        )()
        adapter = RoutedChatAdapter(headless=False)
        active_adapter = AppleScriptChromeChatAdapter()
        active_adapter._binding = binding
        adapter._active_adapter = active_adapter
        adapter._binding = binding

        with patch.dict(
            "mastermind_bridge.orchestrator.browser.os.environ",
            {"BRIDGE_ENABLE_PLAYWRIGHT_APPLESCRIPT_FALLBACK": "1"},
            clear=False,
        ), patch("mastermind_bridge.orchestrator.browser.sys.platform", "darwin"), patch(
            "mastermind_bridge.orchestrator.browser.AppleScriptChromeChatAdapter.read_latest_assistant_message",
            side_effect=RuntimeError(
                "macOS browser Apple Events automation is not functioning on this host. "
                "The Bridge can see the configured browser app, but app/window/tab scripting is failing before DOM inspection. "
                "Use a host/browser setup where normal-browser Apple Events automation works, or resume through a different working browser transport."
            ),
        ), patch(
            "mastermind_bridge.orchestrator.browser.AppleScriptChromeChatAdapter.open_chat",
            side_effect=RuntimeError("normal Chrome reopen failed"),
        ), patch(
            "mastermind_bridge.orchestrator.browser.PlaywrightChatAdapter.open_chat"
        ) as playwright_open, patch(
            "mastermind_bridge.orchestrator.browser.PlaywrightChatAdapter.read_latest_assistant_message",
            return_value={"message_id": "assistant-1", "message_anchor": "assistant-1", "text": "bridge-control"},
        ) as playwright_read:
            result = adapter.read_latest_assistant_message(session=None)

        self.assertIsInstance(adapter._active_adapter, PlaywrightChatAdapter)
        playwright_open.assert_called_once_with(binding)
        playwright_read.assert_called_once_with(None)
        self.assertEqual(result["text"], "bridge-control")

    def test_routed_adapter_surfaces_combined_host_failure_when_runtime_applescript_and_playwright_both_fail(self):
        binding = type(
            "Binding",
            (),
            {
                "chat_url": "https://chatgpt.com/c/project/test-chat",
                "browser_profile_path": "/tmp/profile",
                "browser_channel": "chrome",
                "browser_session_handle": "default",
            },
        )()
        adapter = RoutedChatAdapter(headless=False)
        active_adapter = AppleScriptChromeChatAdapter()
        active_adapter._binding = binding
        adapter._active_adapter = active_adapter
        adapter._binding = binding

        with patch.dict(
            "mastermind_bridge.orchestrator.browser.os.environ",
            {"BRIDGE_ENABLE_PLAYWRIGHT_APPLESCRIPT_FALLBACK": "1"},
            clear=False,
        ), patch("mastermind_bridge.orchestrator.browser.sys.platform", "darwin"), patch(
            "mastermind_bridge.orchestrator.browser.AppleScriptChromeChatAdapter.current_chat_url",
            side_effect=RuntimeError(
                "macOS browser Apple Events automation is not functioning on this host. "
                "The Bridge can see the configured browser app, but app/window/tab scripting is failing before DOM inspection. "
                "Use a host/browser setup where normal-browser Apple Events automation works, or resume through a different working browser transport."
            ),
        ), patch(
            "mastermind_bridge.orchestrator.browser.AppleScriptChromeChatAdapter.open_chat",
            side_effect=RuntimeError("normal Chrome reopen failed"),
        ), patch(
            "mastermind_bridge.orchestrator.browser.PlaywrightChatAdapter.open_chat",
            side_effect=RuntimeError(
                "BrowserType.launch_persistent_context: Target page, context or browser has been closed\n"
                "bootstrap_check_in org.chromium.Chromium.MachPortRendezvousServer.86655: Permission denied (1100)"
            ),
        ) as playwright_open:
            with self.assertRaisesRegex(RuntimeError, "macOS browser automation is not functioning on this host"):
                adapter.current_chat_url(session=None)

        playwright_open.assert_called_once_with(binding)

    def test_applescript_adapter_browser_javascript_timeout_becomes_transport_failure(self):
        binding = type(
            "Binding",
            (),
            {
                "chat_url": "https://chatgpt.com/c/project/test-chat",
                "browser_channel": "chrome",
            },
        )()
        adapter = AppleScriptChromeChatAdapter()
        adapter._binding = binding

        with patch(
            "mastermind_bridge.orchestrator.browser_applescript.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["/usr/bin/osascript"], timeout=8),
        ):
            with self.assertRaisesRegex(RuntimeError, "macOS browser Apple Events automation is not functioning on this host"):
                adapter._run_browser_javascript("(() => 'ok')()")

    def test_applescript_adapter_open_chat_uses_regular_google_chrome(self):
        binding = type(
            "Binding",
            (),
            {
                "chat_url": "https://chatgpt.com/c/project/test-chat",
                "browser_channel": "chrome",
            },
        )()
        adapter = AppleScriptChromeChatAdapter()

        with patch("mastermind_bridge.orchestrator.browser.AppleScriptChromeChatAdapter._focus_existing_chat_tab", return_value=False), patch(
            "mastermind_bridge.orchestrator.browser_applescript.subprocess.run"
        ) as run_mock:
            adapter.open_chat(binding)

        run_mock.assert_called_once_with(
            ["open", "-g", "-a", "Google Chrome", "https://chatgpt.com/c/project/test-chat"],
            check=True,
            text=True,
            capture_output=True,
        )

    def test_applescript_adapter_focuses_existing_chat_tab_instead_of_opening_duplicate(self):
        binding = type(
            "Binding",
            (),
            {
                "chat_url": "https://chatgpt.com/c/project/test-chat",
                "browser_channel": "chrome",
            },
        )()
        adapter = AppleScriptChromeChatAdapter()

        with patch("mastermind_bridge.orchestrator.browser.AppleScriptChromeChatAdapter._focus_existing_chat_tab", return_value=True) as focus_mock, patch(
            "mastermind_bridge.orchestrator.browser_applescript.subprocess.run"
        ) as run_mock:
            adapter.open_chat(binding)

        focus_mock.assert_called_once_with("Google Chrome", "https://chatgpt.com/c/project/test-chat")
        run_mock.assert_not_called()

    def test_applescript_adapter_activate_chat_opens_foreground_google_chrome(self):
        binding = type(
            "Binding",
            (),
            {
                "chat_url": "https://chatgpt.com/c/project/test-chat",
                "browser_channel": "chrome",
            },
        )()
        adapter = AppleScriptChromeChatAdapter()

        with patch("mastermind_bridge.orchestrator.browser_applescript.time.sleep") as sleep_mock, patch(
            "mastermind_bridge.orchestrator.browser_applescript.subprocess.run"
        ) as run_mock:
            adapter.activate_chat(binding)

        self.assertEqual(adapter._binding, binding)
        run_mock.assert_called_once_with(
            ["open", "-a", "Google Chrome", "https://chatgpt.com/c/project/test-chat"],
            check=True,
            text=True,
            capture_output=True,
        )
        sleep_mock.assert_called_once()

    def test_applescript_adapter_post_user_message_uses_temporary_foreground_js_lease(self):
        adapter = AppleScriptChromeChatAdapter()
        adapter._foreground_javascript = False

        def _assert_foreground(_session, _text, _return_packet_id):
            self.assertTrue(adapter._foreground_javascript)
            return {"status": "delivered", "return_packet_id": _return_packet_id}

        with patch.object(adapter, "_post_user_message_foreground", side_effect=_assert_foreground) as post_mock:
            result = adapter.post_user_message(session=None, text="packet", return_packet_id="packet-1")

        self.assertEqual(result["status"], "delivered")
        self.assertFalse(adapter._foreground_javascript)
        post_mock.assert_called_once_with(None, "packet", "packet-1")

    def test_applescript_post_user_message_falls_back_to_enter_when_clicked_send_does_not_post(self):
        adapter = AppleScriptChromeChatAdapter()
        state = {"enter_submitted": False}
        adapter._prepare_composer_for_post = lambda: {"status": "ready"}
        adapter._set_composer_text = lambda _text: {"status": "filled"}
        adapter._click_send_button = lambda: {"clicked": True}
        adapter._submit_via_enter = lambda: state.__setitem__("enter_submitted", True) or {"submitted": True}
        adapter._latest_delivery_error_text = lambda: ""
        adapter._clear_composer_draft = lambda: None
        adapter._latest_user_message_contains_packet = lambda _session, _packet_id: bool(state["enter_submitted"])
        adapter.read_latest_user_message = lambda _session: {
            "message_anchor": "msg-user-after-enter",
            "text": "posted packet-enter-fallback",
        }
        adapter.post_ack_timeout_ms = 1000
        adapter.poll_interval_ms = 10
        adapter.enter_submit_after_click_grace_ms = 0

        result = adapter.post_user_message(
            session=None,
            text="hello bridge",
            return_packet_id="packet-enter-fallback",
        )

        self.assertEqual(result["status"], "delivered")
        self.assertTrue(state["enter_submitted"])

    def test_applescript_adapter_does_not_open_duplicate_when_tab_focus_errors(self):
        binding = type(
            "Binding",
            (),
            {
                "chat_url": "https://chatgpt.com/c/project/test-chat",
                "browser_channel": "chrome",
            },
        )()
        adapter = AppleScriptChromeChatAdapter()

        with patch(
            "mastermind_bridge.orchestrator.browser.AppleScriptChromeChatAdapter._focus_existing_chat_tab",
            side_effect=RuntimeError("Apple Events focus failed"),
        ) as focus_mock, patch("mastermind_bridge.orchestrator.browser_applescript.subprocess.run") as run_mock:
            with self.assertRaisesRegex(RuntimeError, "Apple Events focus failed"):
                adapter.open_chat(binding)

        focus_mock.assert_called_once_with("Google Chrome", "https://chatgpt.com/c/project/test-chat")
        run_mock.assert_not_called()

    def test_applescript_adapter_falls_back_to_bundle_path_when_open_by_name_fails(self):
        binding = type(
            "Binding",
            (),
            {
                "chat_url": "https://chatgpt.com/c/project/test-chat",
                "browser_channel": "chrome",
            },
        )()
        adapter = AppleScriptChromeChatAdapter()
        open_by_name_error = subprocess.CalledProcessError(1, ["open", "-a", "Google Chrome"])
        applescript_module = _applescript_adapter_module()

        with patch(
            "mastermind_bridge.orchestrator.browser.AppleScriptChromeChatAdapter._focus_existing_chat_tab",
            return_value=False,
        ), patch.object(
            applescript_module,
            "_macos_browser_app_path",
            return_value=Path("/Applications/Google Chrome.app"),
        ), patch(
            "mastermind_bridge.orchestrator.browser_applescript.subprocess.run",
            side_effect=[open_by_name_error, None],
        ) as run_mock:
            adapter.open_chat(binding)

        self.assertEqual(
            run_mock.call_args_list,
            [
                unittest.mock.call(
                    ["open", "-g", "-a", "Google Chrome", "https://chatgpt.com/c/project/test-chat"],
                    check=True,
                    text=True,
                    capture_output=True,
                ),
                unittest.mock.call(
                    ["open", "-g", "-a", "/Applications/Google Chrome.app", "https://chatgpt.com/c/project/test-chat"],
                    check=True,
                    text=True,
                    capture_output=True,
                ),
            ],
        )

    def test_applescript_adapter_falls_back_to_browser_binary_when_open_launchservices_fails(self):
        binding = type(
            "Binding",
            (),
            {
                "chat_url": "https://chatgpt.com/c/project/test-chat",
                "browser_channel": "chrome",
            },
        )()
        adapter = AppleScriptChromeChatAdapter()
        open_by_name_error = subprocess.CalledProcessError(1, ["open", "-a", "Google Chrome"])
        open_by_path_error = subprocess.CalledProcessError(1, ["open", "-a", "/Applications/Google Chrome.app"])
        applescript_module = _applescript_adapter_module()

        with patch(
            "mastermind_bridge.orchestrator.browser.AppleScriptChromeChatAdapter._focus_existing_chat_tab",
            return_value=False,
        ), patch.object(
            applescript_module,
            "_macos_browser_app_path",
            return_value=Path("/Applications/Google Chrome.app"),
        ), patch.object(
            applescript_module,
            "_macos_browser_binary_path",
            return_value=Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ), patch(
            "mastermind_bridge.orchestrator.browser_applescript.subprocess.run",
            side_effect=[open_by_name_error, open_by_path_error],
        ) as run_mock, patch(
            "mastermind_bridge.orchestrator.browser_applescript.subprocess.Popen"
        ) as popen_mock:
            adapter.open_chat(binding)

        self.assertEqual(
            run_mock.call_args_list,
            [
                unittest.mock.call(
                    ["open", "-g", "-a", "Google Chrome", "https://chatgpt.com/c/project/test-chat"],
                    check=True,
                    text=True,
                    capture_output=True,
                ),
                unittest.mock.call(
                    ["open", "-g", "-a", "/Applications/Google Chrome.app", "https://chatgpt.com/c/project/test-chat"],
                    check=True,
                    text=True,
                    capture_output=True,
                ),
            ],
        )
        popen_mock.assert_called_once_with(
            ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "https://chatgpt.com/c/project/test-chat"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )

    def test_applescript_adapter_relaunch_chat_reloads_existing_tab_without_terminating_chrome(self):
        binding = type(
            "Binding",
            (),
            {
                "chat_url": "https://chatgpt.com/c/project/test-chat",
                "browser_channel": "chrome",
            },
        )()
        adapter = AppleScriptChromeChatAdapter()

        with patch(
            "mastermind_bridge.orchestrator.browser_applescript.AppleScriptChromeChatAdapter.reload_chat",
            return_value=True,
        ) as reload_mock, patch(
            "mastermind_bridge.orchestrator.browser_applescript.AppleScriptChromeChatAdapter._terminate_browser_app"
        ) as terminate_mock, patch(
            "mastermind_bridge.orchestrator.browser_applescript.AppleScriptChromeChatAdapter._open_chat_url"
        ) as open_mock:
            adapter.relaunch_chat(binding)

        self.assertEqual(adapter._binding, binding)
        reload_mock.assert_called_once_with(None)
        terminate_mock.assert_not_called()
        open_mock.assert_not_called()

    def test_applescript_adapter_relaunch_chat_can_force_relaunch_when_enabled(self):
        binding = type(
            "Binding",
            (),
            {
                "chat_url": "https://chatgpt.com/c/project/test-chat",
                "browser_channel": "chrome",
            },
        )()
        adapter = AppleScriptChromeChatAdapter()

        with patch.dict("mastermind_bridge.orchestrator.browser_applescript.os.environ", {"BRIDGE_ENABLE_NORMAL_BROWSER_FORCE_RELAUNCH": "1"}), patch(
            "mastermind_bridge.orchestrator.browser_applescript.AppleScriptChromeChatAdapter._terminate_browser_app"
        ) as terminate_mock, patch(
            "mastermind_bridge.orchestrator.browser_applescript.AppleScriptChromeChatAdapter.reload_chat"
        ) as reload_mock, patch(
            "mastermind_bridge.orchestrator.browser_applescript.AppleScriptChromeChatAdapter._open_chat_url"
        ) as open_mock:
            adapter.relaunch_chat(binding)

        terminate_mock.assert_called_once_with("Google Chrome")
        reload_mock.assert_not_called()
        open_mock.assert_called_once_with(
            "Google Chrome",
            unittest.mock.ANY,
            unittest.mock.ANY,
            "https://chatgpt.com/c/project/test-chat",
        )

    def test_applescript_adapter_surfaces_js_from_apple_events_requirement(self):
        binding = type(
            "Binding",
            (),
            {
                "chat_url": "https://chatgpt.com/c/project/test-chat",
                "browser_channel": "chrome",
            },
        )()
        adapter = AppleScriptChromeChatAdapter()
        adapter._binding = binding
        error = subprocess.CalledProcessError(
            1,
            ["osascript"],
            stderr="Google Chrome: Die Ausführung von JavaScript über AppleScript ist deaktiviert. Wenn du diese Funktion aktivieren möchtest, gehe zu „Ansicht“ > „Entwickler“ > „JavaScript von Apple Events erlauben“.",
        )

        with patch("mastermind_bridge.orchestrator.browser_applescript.subprocess.run", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "JavaScript from Apple Events"):
                adapter._run_browser_javascript("document.title")

    def test_applescript_adapter_surfaces_host_level_chrome_transport_failure_cleanly(self):
        binding = type(
            "Binding",
            (),
            {
                "chat_url": "https://chatgpt.com/c/project/test-chat",
                "browser_channel": "chrome",
            },
        )()
        adapter = AppleScriptChromeChatAdapter()
        adapter._binding = binding
        error = subprocess.CalledProcessError(
            1,
            ["osascript"],
            stderr=(
                "2026-04-17 01:32:57.700 osascript[45357:359587] Connection Invalid error for service "
                "com.apple.hiservices-xpcservice.\n"
                "2026-04-17 01:32:57.700 osascript[45357:359586] Error received in message reply handler: Connection invalid\n"
                "374:382: syntax error: Zeilenende erwartet, aber Identifier gefunden. (-2741)"
            ),
        )

        with patch("mastermind_bridge.orchestrator.browser_applescript.subprocess.run", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "macOS browser Apple Events automation is not functioning on this host"):
                adapter._run_browser_javascript("document.title")

    def test_applescript_adapter_front_window_url_surfaces_host_level_transport_failure_cleanly(self):
        binding = type(
            "Binding",
            (),
            {
                "chat_url": "https://chatgpt.com/c/project/test-chat",
                "browser_channel": "chrome",
            },
        )()
        adapter = AppleScriptChromeChatAdapter()
        adapter._binding = binding
        error = subprocess.CalledProcessError(
            1,
            ["osascript"],
            stderr=(
                "2026-04-17 03:17:06.615 osascript[54152:430253] Connection Invalid error for service "
                "com.apple.hiservices-xpcservice.\n"
                "2026-04-17 03:17:06.615 osascript[54152:430251] Error received in message reply handler: Connection invalid\n"
                "33:49: execution error: application \"Google Chrome\" kann nicht gelesen werden. (-1728)"
            ),
        )

        with patch("mastermind_bridge.orchestrator.browser_applescript.subprocess.run", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "macOS browser Apple Events automation is not functioning on this host"):
                adapter._front_window_active_tab_url()

    def test_detect_chrome_applescript_transport_failure_matches_connection_invalid_plus_tab_scripting(self):
        message = (
            "Connection Invalid error for service com.apple.hiservices-xpcservice.\n"
            "Error received in message reply handler: Connection invalid\n"
            "execution error: application \"Google Chrome\" kann nicht gelesen werden. (-1728)"
        )

        self.assertTrue(_looks_like_chrome_applescript_transport_failure(message))

    def test_detect_chrome_applescript_transport_failure_matches_brave_too(self):
        message = (
            "Connection Invalid error for service com.apple.hiservices-xpcservice.\n"
            "Error received in message reply handler: Connection invalid\n"
            "execution error: application \"Brave Browser\" kann nicht gelesen werden. (-1728)"
        )

        self.assertTrue(_looks_like_chrome_applescript_transport_failure(message))

    def test_detect_chrome_applescript_transport_failure_matches_launchservices_10827_browser_failures(self):
        message = (
            "Unable to find application named 'Google Chrome'\n"
            "The application /Applications/Google Chrome.app cannot be opened for an unexpected reason, "
            'error=Error Domain=NSOSStatusErrorDomain Code=-10827 "kLSNoExecutableErr: The executable is missing"'
        )

        self.assertTrue(_looks_like_chrome_applescript_transport_failure(message))

    def test_applescript_transport_failure_message_calls_out_launchservices_browser_bundle_failure(self):
        binding = type(
            "Binding",
            (),
            {
                "chat_url": "https://chatgpt.com/c/project/test-chat",
                "browser_channel": "chrome",
            },
        )()
        adapter = AppleScriptChromeChatAdapter()
        adapter._binding = binding
        error = subprocess.CalledProcessError(
            1,
            ["osascript"],
            stderr=(
                "Unable to find application named 'Google Chrome'\n"
                "The application /Applications/Google Chrome.app cannot be opened for an unexpected reason, "
                'error=Error Domain=NSOSStatusErrorDomain Code=-10827 "kLSNoExecutableErr: The executable is missing"'
            ),
        )

        with patch("mastermind_bridge.orchestrator.browser_applescript.subprocess.run", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "LaunchServices could not open Google Chrome"):
                adapter._run_browser_javascript("document.title")

    def test_combined_browser_transport_failure_message_preserves_launchservices_detail(self):
        primary_exc = RuntimeError(
            "Unable to find application named 'Google Chrome'\n"
            "The application /Applications/Google Chrome.app cannot be opened for an unexpected reason, "
            'error=Error Domain=NSOSStatusErrorDomain Code=-10827 "kLSNoExecutableErr: The executable is missing"'
        )
        fallback_exc = RuntimeError(
            "launch_persistent_context: Target page, context or browser has been closed"
        )

        message = enrich_browser_blocker_reason(
            _combined_browser_transport_failure_message(primary_exc, fallback_exc)
        )

        self.assertIn("LaunchServices could not open Google Chrome", message)

    def test_enrich_browser_blocker_reason_appends_host_probe_failures(self):
        message = (
            "macOS browser automation is not functioning on this host. "
            "The normal-browser Apple Events path failed during live tab inspection, and the Playwright "
            "persistent-profile fallback also failed to launch from this Codex process due to host or sandbox browser transport restrictions."
        )

        system_events_failure = subprocess.CompletedProcess(
            ["/usr/bin/osascript"],
            1,
            stdout="",
            stderr="40:97: execution error: Es ist ein Fehler „-10827“ aufgetreten. (-10827)",
        )
        screencapture_failure = subprocess.CompletedProcess(
            ["screencapture"],
            1,
            stdout="",
            stderr="could not create image from display 0",
        )

        with patch("mastermind_bridge.orchestrator.browser_support.sys.platform", "darwin"), patch(
            "mastermind_bridge.orchestrator.browser_support.subprocess.run",
            side_effect=[system_events_failure, screencapture_failure],
        ):
            enriched = enrich_browser_blocker_reason(message)

        self.assertIn("Host probes:", enriched)
        self.assertIn("system_events=-10827", enriched)
        self.assertIn("screencapture=display_capture_failed", enriched)

    def test_applescript_adapter_falls_back_to_generic_turn_sections_for_assistant_reads(self):
        adapter = AppleScriptChromeChatAdapter()
        payloads = iter(
            [
                {"missing_selector": True},
                {
                    "messages": [
                        {"text": "Du:\nstop", "message_id": "conversation-turn-1"},
                        {
                            "text": "ChatGPT:\nbridge-control\nsession_id: \"session-22bf7e07\"",
                            "message_id": "conversation-turn-2",
                        },
                    ]
                },
            ]
        )
        adapter._run_json_script = lambda _js: next(payloads)
        adapter._access_blocker_message = lambda: ""

        message = adapter.read_latest_assistant_message(session=None)

        self.assertEqual(message["message_id"], "conversation-turn-2")
        self.assertTrue(message["text"].startswith("bridge-control"))

    def test_applescript_adapter_uses_turn_text_when_assistant_node_is_empty(self):
        adapter = AppleScriptChromeChatAdapter()
        captured = {}

        def fake_run_json_script(js_code: str):
            captured["js_code"] = js_code
            return {
                "text": "Nachgedacht für 41s",
                "message_id": "conversation-turn-16",
                "message_index": 8,
            }

        adapter._run_json_script = fake_run_json_script

        message = adapter.read_latest_assistant_message(session=None)

        self.assertEqual(message["text"], "Nachgedacht für 41s")
        self.assertEqual(message["message_id"], "conversation-turn-16")
        self.assertIn("closest('section[data-testid^=\"conversation-turn-\"]", captured["js_code"])

    def test_applescript_adapter_generic_user_fallback_uses_user_anchor_when_message_id_is_missing(self):
        adapter = AppleScriptChromeChatAdapter()
        payloads = iter(
            [
                {"missing_selector": True},
                {
                    "messages": [
                        {"text": "ChatGPT:\ncontinue", "message_id": "", "message_index": 3},
                        {"text": "Du:\npause", "message_id": "", "message_index": 4},
                    ]
                },
            ]
        )
        adapter._run_json_script = lambda _js: next(payloads)

        message = adapter.read_latest_user_message(session=None)

        self.assertEqual(message["text"], "pause")
        self.assertTrue(message["message_anchor"].startswith("user-4-"))

    def test_applescript_adapter_generic_stop_fallback_preserves_message_index_without_message_id(self):
        adapter = AppleScriptChromeChatAdapter()
        payloads = iter(
            [
                {"messages": []},
                {
                    "messages": [
                        {"text": "ChatGPT:\ncontinue", "message_id": "", "message_index": 8},
                        {"text": "Du:\npause", "message_id": "", "message_index": 9},
                    ]
                },
            ]
        )
        adapter._run_json_script = lambda _js: next(payloads)
        adapter.recent_message_scan_limit = 8

        result = adapter.poll_stop_command(session=None, stop_phrases=["stop", "pause"])

        self.assertEqual(result["command"], "pause")
        self.assertTrue(result["message_anchor"].startswith("user-9-"))

    def test_applescript_assistant_response_in_progress_checks_streaming_animation_nodes(self):
        adapter = AppleScriptChromeChatAdapter()
        captured = {}

        def fake_run_json_script(js_code: str):
            captured["js_code"] = js_code
            return {"in_progress": True}

        adapter._run_json_script = fake_run_json_script
        adapter.selectors = {
            "assistant_message": [".assistant-fallback"],
        }

        self.assertTrue(adapter.assistant_response_in_progress(session=None))
        self.assertIn("streaming-animation", captured["js_code"])

    def test_applescript_current_chat_url_reads_window_location(self):
        adapter = AppleScriptChromeChatAdapter()
        adapter._run_json_script = lambda _js: {"href": "https://chatgpt.com/g/g-p-test/c/new-chat"}

        self.assertEqual(
            adapter.current_chat_url(session=None),
            "https://chatgpt.com/g/g-p-test/c/new-chat",
        )

    def test_applescript_current_chat_url_falls_back_to_active_tab_when_bound_tab_redirected(self):
        binding = type(
            "Binding",
            (),
            {
                "chat_url": "https://chatgpt.com/new",
                "browser_channel": "chrome",
            },
        )()
        adapter = AppleScriptChromeChatAdapter()
        adapter._binding = binding

        def raise_missing(_js: str):
            raise RuntimeError("The normal browser tab for this chat was not found. Open the chat in your normal browser and keep that tab available.")

        adapter._run_json_script = raise_missing

        with patch("mastermind_bridge.orchestrator.browser_applescript.subprocess.run") as run_mock:
            run_mock.return_value = subprocess.CompletedProcess(
                ["/usr/bin/osascript"],
                0,
                stdout="https://chatgpt.com/c/new-chat\n",
                stderr="",
            )
            self.assertEqual(
                adapter.current_chat_url(session=None),
                "https://chatgpt.com/c/new-chat",
            )
        run_mock.assert_called_once_with(
            [
                "/usr/bin/osascript",
                "-e",
                'tell application "Google Chrome" to return URL of active tab of front window',
            ],
            text=True,
            capture_output=True,
            check=True,
            timeout=adapter.apple_event_timeout_seconds,
        )

    def test_applescript_adapter_return_packet_visible_uses_generic_turn_fallback(self):
        packet_id = "packet-123"
        adapter = AppleScriptChromeChatAdapter()
        payloads = iter(
            [
                {"visible": False},
                {
                    "messages": [
                        {"text": f"Du:\nSession update\n{packet_id}", "message_id": "conversation-turn-7"},
                    ]
                },
            ]
        )
        adapter._run_json_script = lambda _js: next(payloads)

        self.assertTrue(adapter.return_packet_visible(session=None, return_packet_id=packet_id))

    def test_applescript_adapter_poll_stop_command_uses_generic_turn_fallback(self):
        adapter = AppleScriptChromeChatAdapter()
        payloads = iter(
            [
                {"messages": []},
                {
                    "messages": [
                        {"text": "ChatGPT:\ncontinue", "message_id": "conversation-turn-8"},
                        {"text": "Du:\npause", "message_id": "conversation-turn-9"},
                    ]
                },
            ]
        )
        adapter._run_json_script = lambda _js: next(payloads)
        adapter.recent_message_scan_limit = 8

        result = adapter.poll_stop_command(session=None, stop_phrases=["stop", "pause"])

        self.assertEqual(result["command"], "pause")
        self.assertEqual(result["message_anchor"], "conversation-turn-9")

    def test_chrome_osascript_uses_indexed_chrome_terms_without_foregrounding_by_default(self):
        from mastermind_bridge.orchestrator.browser import _chrome_osascript_source

        script = _chrome_osascript_source()

        self.assertIn('set jsCode to ""', script)
        self.assertIn('set jsCode to jsCode & ("")', script)
        self.assertIn('using terms from application "Google Chrome"', script)
        self.assertIn("repeat with windowIndex from 1 to (count of windows)", script)
        self.assertIn("repeat with tabIndex from 1 to tabCount", script)
        self.assertIn("bridgeUrlMatches((URL of tab tabIndex of window windowIndex) as text, targetUrl)", script)
        self.assertIn('if candidateUrl starts with (targetUrl & "?") then return true', script)
        self.assertIn('if candidateUrl starts with (targetUrl & "#") then return true', script)
        self.assertNotIn("set active tab index of window foundWindowIndex to foundTabIndex", script)
        self.assertNotIn("set index of window foundWindowIndex to 1", script)
        self.assertIn("return execute tab foundTabIndex of window foundWindowIndex javascript jsCode", script)
        self.assertNotIn("«event CrSuExJa»", script)

    def test_chrome_osascript_can_foreground_for_delivery_execution(self):
        from mastermind_bridge.orchestrator.browser import _chrome_osascript_source

        script = _chrome_osascript_source(foreground=True)

        self.assertIn("set active tab index of window foundWindowIndex to foundTabIndex", script)
        self.assertIn("set index of window foundWindowIndex to 1", script)
        self.assertIn("activate", script)
        self.assertIn("return execute tab foundTabIndex of window foundWindowIndex javascript jsCode", script)

    def test_applescript_js_expression_serializes_quotes_and_newlines_with_tokens(self):
        from mastermind_bridge.orchestrator.browser_support import _applescript_js_expression

        expression = _applescript_js_expression('alpha "beta"\ngamma')

        self.assertEqual(expression, '"alpha " & quote & "beta" & quote & linefeed & "gamma"')

    def test_applescript_js_expression_escapes_literal_backslashes(self):
        from mastermind_bridge.orchestrator.browser_support import _applescript_js_expression

        expression = _applescript_js_expression('a\\"b')

        self.assertEqual(expression, '"a\\\\" & quote & "b"')

    def test_chrome_focus_tab_osascript_finds_tab_without_foregrounding_by_default(self):
        from mastermind_bridge.orchestrator.browser_support import _chrome_focus_tab_osascript_source

        script = _chrome_focus_tab_osascript_source()

        self.assertIn('using terms from application "Google Chrome"', script)
        self.assertIn("repeat with windowIndex from 1 to (count of windows)", script)
        self.assertIn("bridgeUrlMatches((URL of tab tabIndex of window windowIndex) as text, targetUrl)", script)
        self.assertIn('if candidateUrl starts with (targetUrl & "?") then return true', script)
        self.assertIn('if candidateUrl starts with (targetUrl & "#") then return true', script)
        self.assertNotIn("set active tab index of window foundWindowIndex to foundTabIndex", script)
        self.assertNotIn("set index of window foundWindowIndex to 1", script)
        self.assertNotIn("«property acTI»", script)

    def test_chrome_focus_tab_osascript_can_foreground_explicitly(self):
        from mastermind_bridge.orchestrator.browser_support import _chrome_focus_tab_osascript_source

        script = _chrome_focus_tab_osascript_source(foreground=True)

        self.assertIn("set active tab index of window foundWindowIndex to foundTabIndex", script)
        self.assertIn("set index of window foundWindowIndex to 1", script)
        self.assertIn("activate", script)

    def test_applescript_adapter_embeds_large_js_in_script_chunks(self):
        binding = type(
            "Binding",
            (),
            {
                "chat_url": "https://chatgpt.com/c/project/test-chat",
                "browser_channel": "chrome",
            },
        )()
        adapter = AppleScriptChromeChatAdapter()
        adapter._binding = binding
        js_code = "(() => JSON.stringify({ text: " + json.dumps("x" * 200000) + " }))()"
        adapter.javascript_argv_chunk_chars = 4096
        captured_script: dict[str, str] = {}

        def fake_run(args, **kwargs):
            self.assertEqual(args, ["/usr/bin/osascript", "-", "Google Chrome", binding.chat_url])
            captured_script["text"] = str(kwargs.get("input", ""))
            return subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")

        with patch("mastermind_bridge.orchestrator.browser_applescript.subprocess.run", side_effect=fake_run):
            result = adapter._run_browser_javascript(js_code)

        self.assertEqual(result, "{}")
        script = captured_script["text"]
        self.assertGreater(script.count("set jsCode to jsCode & ("), 10)
        self.assertIn("quote", script)
        self.assertIn("return execute tab foundTabIndex of window foundWindowIndex javascript jsCode", script)

    def test_applescript_adapter_sets_large_composer_text_in_chunks(self):
        adapter = AppleScriptChromeChatAdapter()
        adapter.selectors = {"composer": ["textarea"]}
        adapter.composer_fill_chunk_chars = 10
        payload = "x" * 25
        seen_chunks: list[str] = []
        replace_flags: list[bool] = []
        renderer_seen: list[bool] = []

        def fake_run_json_script(js_code: str):
            chunk_marker = 'atob('
            chunk_start = js_code.index(chunk_marker) + len(chunk_marker)
            chunk_end = js_code.index(')', chunk_start)
            chunk_b64 = json.loads(js_code[chunk_start:chunk_end])
            seen_chunks.append(__import__("base64").b64decode(chunk_b64).decode("utf-8"))
            replace_flags.append("const replaceMode = true;" in js_code)
            renderer_seen.append("renderPlainTextToHtml" in js_code)
            return {"status": "filled"}

        adapter._run_json_script = fake_run_json_script
        adapter._composer_text_value = lambda: payload

        result = adapter._set_composer_text(payload)

        self.assertEqual(result, {"status": "filled"})
        self.assertEqual(seen_chunks, ["x" * 10, "x" * 10, "x" * 5])
        self.assertEqual(replace_flags, [True, False, False])
        self.assertEqual(renderer_seen, [True, True, True])

    def test_applescript_adapter_accepts_blank_line_collapse_after_fill(self):
        adapter = AppleScriptChromeChatAdapter()
        adapter.selectors = {"composer": ["textarea"]}
        adapter._run_json_script = lambda _js: {"status": "filled"}
        adapter._composer_text_value = lambda: "Header\nBody"

        result = adapter._set_composer_text("Header\n\nBody")

        self.assertEqual(result, {"status": "filled"})

    def test_applescript_adapter_rejects_missing_nonempty_line_after_fill(self):
        adapter = AppleScriptChromeChatAdapter()
        adapter.selectors = {"composer": ["textarea"]}
        adapter._run_json_script = lambda _js: {"status": "filled"}
        adapter._composer_text_value = lambda: "Header"

        result = adapter._set_composer_text("Header\n\nBody")

        self.assertEqual(result["status"], "failed")
        self.assertIn("did not preserve", result["error_signature"])

    def test_applescript_adapter_accepts_soft_wrap_linebreaks_after_fill(self):
        adapter = AppleScriptChromeChatAdapter()
        adapter.selectors = {"composer": ["textarea"]}
        adapter._run_json_script = lambda _js: {"status": "filled"}
        adapter._composer_text_value = (
            lambda: "- analyze everything Codex returned deeply;\nthis is the most important input"
        )

        result = adapter._set_composer_text(
            "- analyze everything Codex returned deeply; this is the most important input"
        )

        self.assertEqual(result, {"status": "filled"})

    def test_applescript_adapter_prefers_visible_contenteditable_over_hidden_textarea_when_reading_composer(self):
        adapter = AppleScriptChromeChatAdapter()
        adapter.selectors = {"composer": ["textarea", '[contenteditable="true"]']}
        nodes_by_selector = {
            "textarea": [_FakeNode("", visible=False)],
            '[contenteditable="true"]': [_FakeNode("Visible draft", visible=True)],
        }
        adapter._run_json_script = _make_composer_dom_runner(adapter, nodes_by_selector)

        value = adapter._composer_text_value()

        self.assertEqual(value, "Visible draft")

    def test_applescript_adapter_clears_visible_contenteditable_before_post(self):
        adapter = AppleScriptChromeChatAdapter()
        adapter.selectors = {"composer": ["textarea", '[contenteditable="true"]']}
        adapter.poll_interval_ms = 1
        nodes_by_selector = {
            "textarea": [_FakeNode("", visible=False)],
            '[contenteditable="true"]': [_FakeNode("Visible draft", visible=True)],
        }
        adapter._run_json_script = _make_composer_dom_runner(adapter, nodes_by_selector)

        result = adapter._prepare_composer_for_post()

        self.assertEqual(result, {"status": "ready"})
        self.assertEqual(nodes_by_selector['[contenteditable="true"]'][0].text, "")

    def test_playwright_adapter_sets_large_composer_text_in_chunks_and_verifies_structure(self):
        adapter = PlaywrightChatAdapter.__new__(PlaywrightChatAdapter)
        adapter.selectors = {"composer": ['[contenteditable="true"]']}
        adapter.composer_fill_chunk_chars = 10
        payload = "Header\n\nLine 1\nLine 2\n\nTail"
        seen_chunks: list[str] = []
        replace_flags: list[bool] = []
        renderer_seen: list[bool] = []

        def fake_run_json_script(js_code: str):
            chunk_marker = 'atob('
            chunk_start = js_code.index(chunk_marker) + len(chunk_marker)
            chunk_end = js_code.index(')', chunk_start)
            chunk_b64 = json.loads(js_code[chunk_start:chunk_end])
            seen_chunks.append(__import__("base64").b64decode(chunk_b64).decode("utf-8"))
            replace_flags.append("const replaceMode = true;" in js_code)
            renderer_seen.append("renderPlainTextToHtml" in js_code)
            return {"status": "filled"}

        adapter._run_json_script = fake_run_json_script
        adapter._composer_text_value = lambda: payload

        result = adapter._set_composer_text(payload)

        self.assertEqual(result, {"status": "filled"})

    def test_playwright_adapter_prefers_visible_contenteditable_over_hidden_textarea_when_reading_composer(self):
        adapter = PlaywrightChatAdapter.__new__(PlaywrightChatAdapter)
        adapter.selectors = {"composer": ["textarea", '[contenteditable="true"]']}
        nodes_by_selector = {
            "textarea": [_FakeNode("", visible=False)],
            '[contenteditable="true"]': [_FakeNode("Visible draft", visible=True)],
        }
        adapter._run_json_script = _make_composer_dom_runner(adapter, nodes_by_selector)

        value = adapter._composer_text_value()

        self.assertEqual(value, "Visible draft")

    def test_playwright_adapter_clears_visible_contenteditable_before_post(self):
        adapter = PlaywrightChatAdapter.__new__(PlaywrightChatAdapter)
        adapter.selectors = {"composer": ["textarea", '[contenteditable="true"]']}
        adapter.poll_interval_ms = 1
        nodes_by_selector = {
            "textarea": [_FakeNode("", visible=False)],
            '[contenteditable="true"]': [_FakeNode("Visible draft", visible=True)],
        }
        adapter._page = _FakePage(nodes_by_selector)
        adapter._run_json_script = _make_composer_dom_runner(adapter, nodes_by_selector)

        result = adapter._prepare_composer_for_post()

        self.assertEqual(result, {"status": "ready"})
        self.assertEqual(nodes_by_selector['[contenteditable="true"]'][0].text, "")

    def test_playwright_adapter_accepts_blank_line_collapse_after_fill(self):
        adapter = PlaywrightChatAdapter.__new__(PlaywrightChatAdapter)
        adapter.selectors = {"composer": ['[contenteditable="true"]']}
        adapter.composer_fill_chunk_chars = 4000
        adapter._run_json_script = lambda _js: {"status": "filled"}
        adapter._composer_text_value = lambda: "Header\nBody"

        result = adapter._set_composer_text("Header\n\nBody")

        self.assertEqual(result, {"status": "filled"})

    def test_playwright_adapter_rejects_missing_nonempty_line_after_fill(self):
        adapter = PlaywrightChatAdapter.__new__(PlaywrightChatAdapter)
        adapter.selectors = {"composer": ['[contenteditable="true"]']}
        adapter.composer_fill_chunk_chars = 4000
        adapter._run_json_script = lambda _js: {"status": "filled"}
        adapter._composer_text_value = lambda: "Header"

        result = adapter._set_composer_text("Header\n\nBody")

        self.assertEqual(result["status"], "failed")
        self.assertIn("did not preserve", result["error_signature"])

    def test_playwright_adapter_accepts_soft_wrap_linebreaks_after_fill(self):
        adapter = PlaywrightChatAdapter.__new__(PlaywrightChatAdapter)
        adapter.selectors = {"composer": ['[contenteditable="true"]']}
        adapter.composer_fill_chunk_chars = 4000
        adapter._run_json_script = lambda _js: {"status": "filled"}
        adapter._composer_text_value = (
            lambda: "- analyze everything Codex returned deeply;\nthis is the most important input"
        )

        result = adapter._set_composer_text(
            "- analyze everything Codex returned deeply; this is the most important input"
        )

        self.assertEqual(result, {"status": "filled"})

    def test_detect_preferred_browser_channel_prefers_installed_chrome(self):
        with patch("pathlib.Path.exists", return_value=True):
            self.assertEqual(detect_preferred_browser_channel(), "chrome")

    def test_detect_preferred_browser_channel_falls_back_to_brave(self):
        exists_by_path = {
            "/Applications/Google Chrome.app": False,
            "/Applications/Brave Browser.app": True,
            "/Applications/Microsoft Edge.app": False,
        }

        def fake_exists(path_obj):
            return exists_by_path.get(str(path_obj), False)

        with patch("pathlib.Path.exists", fake_exists):
            self.assertEqual(detect_preferred_browser_channel(), "brave")

    def test_post_user_message_uses_send_button_and_waits_for_packet_visibility(self):
        packet_id = "packet-123"
        composer = _FakeNode()
        send_button = _FakeNode(on_click=lambda page: page._nodes(".user-message").append(_FakeNode(f"posted {packet_id}")))
        adapter = PlaywrightChatAdapter.__new__(PlaywrightChatAdapter)
        adapter.selectors = {
            "composer": [".composer-fallback"],
            "send_button": [".send-button"],
            "user_message": [".user-message"],
            "delivery_error": [".delivery-error"],
        }
        adapter._page = _FakePage(
            {
                ".composer-fallback": [composer],
                ".send-button": [send_button],
                ".user-message": [_FakeNode("older user message")],
            }
        )
        adapter._prepare_composer_for_post = lambda: {"status": "ready"}
        adapter._set_composer_text = lambda text: {"status": "filled"} if not setattr(composer, "filled_text", text) else {"status": "filled"}
        adapter.post_ack_timeout_ms = 1000
        adapter.poll_interval_ms = 10

        result = adapter.post_user_message(session=None, text="hello bridge", return_packet_id=packet_id)

        self.assertEqual(result["status"], "delivered")
        self.assertEqual(composer.filled_text, "hello bridge")
        self.assertEqual(send_button.clicks, 1)
        self.assertEqual(composer.presses, [])

    def test_post_user_message_falls_back_to_enter_when_clicked_send_does_not_post(self):
        packet_id = "packet-enter-fallback"
        composer = _FakeNode(on_press=lambda page, key: page._nodes(".user-message").append(_FakeNode(f"posted {packet_id}")) if key == "Enter" else None)
        send_button = _FakeNode(on_click=lambda page: None)
        adapter = PlaywrightChatAdapter.__new__(PlaywrightChatAdapter)
        adapter.selectors = {
            "composer": [".composer-fallback"],
            "send_button": [".send-button"],
            "user_message": [".user-message"],
            "delivery_error": [".delivery-error"],
        }
        adapter._page = _FakePage(
            {
                ".composer-fallback": [composer],
                ".send-button": [send_button],
                ".user-message": [_FakeNode("older user message")],
            }
        )
        adapter._prepare_composer_for_post = lambda: {"status": "ready"}
        adapter._set_composer_text = lambda text: {"status": "filled"} if not setattr(composer, "filled_text", text) else {"status": "filled"}
        adapter.post_ack_timeout_ms = 1000
        adapter.poll_interval_ms = 10
        adapter.enter_submit_after_click_grace_ms = 0

        result = adapter.post_user_message(session=None, text="hello bridge", return_packet_id=packet_id)

        self.assertEqual(result["status"], "delivered")
        self.assertEqual(send_button.clicks, 1)
        self.assertEqual(composer.presses, ["Enter"])

    def test_post_user_message_does_not_treat_older_packet_match_as_delivery(self):
        packet_id = "packet-123"
        composer = _FakeNode()
        send_button = _FakeNode(on_click=lambda page: None)
        adapter = PlaywrightChatAdapter.__new__(PlaywrightChatAdapter)
        adapter.selectors = {
            "composer": [".composer-fallback"],
            "send_button": [".send-button"],
            "user_message": [".user-message"],
            "delivery_error": [".delivery-error"],
        }
        adapter._page = _FakePage(
            {
                ".composer-fallback": [composer],
                ".send-button": [send_button],
                ".user-message": [
                    _FakeNode(f"older {packet_id}"),
                    _FakeNode("latest user message without marker"),
                ],
            }
        )
        adapter._prepare_composer_for_post = lambda: {"status": "ready"}
        adapter._set_composer_text = lambda text: {"status": "filled"} if not setattr(composer, "filled_text", text) else {"status": "filled"}
        adapter.post_ack_timeout_ms = 20
        adapter.poll_interval_ms = 10

        result = adapter.post_user_message(session=None, text="hello bridge", return_packet_id=packet_id)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_signature"], "Message delivery confirmation timed out.")
        self.assertEqual(composer.filled_text, "")
        self.assertEqual(send_button.clicks, 1)

    def test_post_user_message_retries_visible_chatgpt_send_error_before_marking_delivered(self):
        packet_id = "packet-123"
        composer = _FakeNode()
        delivery_error = _FakeNode("Etwas ist schiefgegangen.\nErneut versuchen")

        def on_send(page):
            page._nodes(".user-message").append(_FakeNode(f"posted {packet_id}"))
            page._nodes(".delivery-error")[:] = [delivery_error]

        send_button = _FakeNode(on_click=on_send)
        adapter = PlaywrightChatAdapter.__new__(PlaywrightChatAdapter)
        adapter.selectors = {
            "composer": [".composer-fallback"],
            "send_button": [".send-button"],
            "user_message": [".user-message"],
            "delivery_error": [".delivery-error"],
        }
        adapter._page = _FakePage(
            {
                ".composer-fallback": [composer],
                ".send-button": [send_button],
                ".user-message": [_FakeNode("older user message")],
                ".delivery-error": [],
            }
        )
        adapter._prepare_composer_for_post = lambda: {"status": "ready"}
        adapter._set_composer_text = lambda text: {"status": "filled"} if not setattr(composer, "filled_text", text) else {"status": "filled"}
        adapter.post_ack_timeout_ms = 1000
        adapter.poll_interval_ms = 10
        adapter.delivery_error_retry_limit = 1
        retry_attempts = {"count": 0}

        def fake_retry():
            retry_attempts["count"] += 1
            adapter._page._nodes(".delivery-error").clear()
            return True

        adapter._retry_latest_delivery_error = fake_retry

        result = adapter.post_user_message(session=None, text="hello bridge", return_packet_id=packet_id)

        self.assertEqual(result["status"], "delivered")
        self.assertEqual(retry_attempts["count"], 1)
        self.assertEqual(send_button.clicks, 1)

    def test_post_user_message_does_not_mark_visible_packet_delivered_while_chatgpt_error_persists(self):
        packet_id = "packet-123"
        composer = _FakeNode()
        delivery_error = _FakeNode("Etwas ist schiefgegangen.\nErneut versuchen")

        def on_send(page):
            page._nodes(".user-message").append(_FakeNode(f"posted {packet_id}"))
            page._nodes(".delivery-error")[:] = [delivery_error]

        send_button = _FakeNode(on_click=on_send)
        adapter = PlaywrightChatAdapter.__new__(PlaywrightChatAdapter)
        adapter.selectors = {
            "composer": [".composer-fallback"],
            "send_button": [".send-button"],
            "user_message": [".user-message"],
            "delivery_error": [".delivery-error"],
        }
        adapter._page = _FakePage(
            {
                ".composer-fallback": [composer],
                ".send-button": [send_button],
                ".user-message": [_FakeNode("older user message")],
                ".delivery-error": [],
            }
        )
        adapter._prepare_composer_for_post = lambda: {"status": "ready"}
        adapter._set_composer_text = lambda text: {"status": "filled"} if not setattr(composer, "filled_text", text) else {"status": "filled"}
        adapter.post_ack_timeout_ms = 1000
        adapter.poll_interval_ms = 10
        adapter.delivery_error_retry_limit = 1
        adapter._retry_latest_delivery_error = lambda: False

        result = adapter.post_user_message(session=None, text="hello bridge", return_packet_id=packet_id)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_signature"], "ChatGPT in-page send failed.")
        self.assertEqual(send_button.clicks, 1)

    def test_applescript_post_user_message_retries_visible_chatgpt_send_error_before_marking_delivered(self):
        packet_id = "packet-123"
        adapter = AppleScriptChromeChatAdapter()
        adapter.delivery_error_retry_limit = 1
        adapter.post_ack_timeout_ms = 1000
        adapter.poll_interval_ms = 10
        send_state = {"sent": False, "retry_attempts": 0}

        adapter._prepare_composer_for_post = lambda: {"status": "ready"}
        adapter._set_composer_text = lambda text: {"status": "filled"}

        def fake_click_send_button():
            send_state["sent"] = True
            return {"clicked": True}

        adapter._click_send_button = fake_click_send_button
        adapter._submit_via_enter = lambda: {"submitted": False}
        adapter._latest_user_message_contains_packet = lambda session, return_packet_id: send_state["sent"]
        adapter.read_latest_user_message = lambda session: {
            "message_anchor": "user-1",
            "text": f"posted {packet_id}",
        }
        adapter._latest_delivery_error_text = lambda: (
            "Etwas ist schiefgegangen.\nErneut versuchen" if send_state["sent"] and send_state["retry_attempts"] == 0 else ""
        )

        def fake_retry():
            send_state["retry_attempts"] += 1
            return True

        adapter._retry_latest_delivery_error = fake_retry

        result = adapter.post_user_message(session=None, text="hello bridge", return_packet_id=packet_id)

        self.assertEqual(result["status"], "delivered")
        self.assertEqual(send_state["retry_attempts"], 1)

    def test_applescript_post_user_message_does_not_mark_visible_packet_delivered_while_chatgpt_error_persists(self):
        packet_id = "packet-123"
        adapter = AppleScriptChromeChatAdapter()
        adapter.delivery_error_retry_limit = 1
        adapter.post_ack_timeout_ms = 1000
        adapter.poll_interval_ms = 10
        send_state = {"sent": False}

        adapter._prepare_composer_for_post = lambda: {"status": "ready"}
        adapter._set_composer_text = lambda text: {"status": "filled"}
        adapter._click_send_button = lambda: send_state.update(sent=True) or {"clicked": True}
        adapter._submit_via_enter = lambda: {"submitted": False}
        adapter._latest_user_message_contains_packet = lambda session, return_packet_id: send_state["sent"]
        adapter.read_latest_user_message = lambda session: {
            "message_anchor": "user-1",
            "text": f"posted {packet_id}",
        }
        adapter._latest_delivery_error_text = lambda: (
            "Etwas ist schiefgegangen.\nErneut versuchen" if send_state["sent"] else ""
        )
        adapter._retry_latest_delivery_error = lambda: False

        result = adapter.post_user_message(session=None, text="hello bridge", return_packet_id=packet_id)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_signature"], "ChatGPT in-page send failed.")

    def test_clear_composer_draft_falls_back_to_select_all_and_backspace_when_fill_clear_fails(self):
        composer = _FakeNode("stale draft", clear_fill_raises=True)
        composer.filled_text = "stale draft"
        adapter = PlaywrightChatAdapter.__new__(PlaywrightChatAdapter)
        adapter.selectors = {
            "composer": [".composer-fallback"],
        }
        adapter._page = _FakePage(
            {
                ".composer-fallback": [composer],
            }
        )

        adapter._clear_composer_draft()

        self.assertEqual(composer.filled_text, "")
        self.assertIn("Meta+A", composer.presses)
        self.assertIn("Backspace", composer.presses)

    def test_optional_locator_prefers_visible_candidate_over_hidden_fallback(self):
        adapter = PlaywrightChatAdapter.__new__(PlaywrightChatAdapter)
        adapter.selectors = {
            "composer": [".composer-fallback"],
        }
        hidden = _FakeNode("hidden textarea", visible=False)
        visible = _FakeNode("visible composer", visible=True)
        adapter._page = _FakePage(
            {
                ".composer-fallback": [hidden, visible],
            }
        )

        locator = adapter._optional_locator("composer")

        self.assertIsNotNone(locator)
        self.assertEqual(locator.inner_text(), "visible composer")

    def test_poll_stop_command_returns_latest_matching_recent_user_message(self):
        adapter = PlaywrightChatAdapter.__new__(PlaywrightChatAdapter)
        adapter.selectors = {
            "user_message": [".user-message"],
        }
        adapter._page = _FakePage(
            {
                ".user-message": [
                    _FakeNode("bridge return packet"),
                    _FakeNode("pause", element_id="cmd-pause-1"),
                    _FakeNode("bridge return packet 2"),
                ],
            }
        )

        result = adapter.poll_stop_command(session=None, stop_phrases=["stop", "pause", "stop after this cycle"])

        self.assertEqual(result["command"], "pause")
        self.assertEqual(result["message_anchor"], "cmd-pause-1")


if __name__ == "__main__":
    unittest.main()
