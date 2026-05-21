from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import time
from typing import Any

from .browser_support import (
    CHATGPT_RETRY_BUTTON_MARKERS,
    _DEFAULT_POST_ACK_TIMEOUT_MS,
    _applescript_transport_failure_message,
    _chrome_focus_tab_osascript_source,
    _chrome_osascript_source,
    _classify_turn_role,
    _indexed_message_anchor,
    _looks_like_chrome_applescript_transport_failure,
    _macos_browser_app_name,
    _macos_browser_app_path,
    _macos_browser_binary_path,
    _strip_turn_role_label,
    canonical_delivery_error_signature,
    composer_text_preserves_payload,
    normalize_stop_command,
)


class AppleScriptChromeChatAdapter:
    """macOS adapter that drives a normal Chrome/Edge tab via Apple Events."""

    def __init__(self, *, selectors: dict[str, str] | None = None) -> None:
        self.selectors = {
            "assistant_message": [
                '[data-message-author-role="assistant"]',
                '[data-testid="conversation-turn-assistant"]',
            ],
            "user_message": [
                '[data-message-author-role="user"]',
                '[data-testid="conversation-turn-user"]',
            ],
            "composer": [
                "textarea",
                '[contenteditable="true"]',
            ],
            "send_button": [
                'button[data-testid="send-button"]',
                'button[aria-label="Send message"]',
            ],
            "delivery_error": [
                '[role="alert"]',
                '[data-testid="toast"]',
            ],
        }
        if selectors:
            self.selectors.update(selectors)
        self.post_ack_timeout_ms = _DEFAULT_POST_ACK_TIMEOUT_MS
        self.poll_interval_ms = 250
        self.enter_submit_after_click_grace_ms = 1500
        self.recent_message_scan_limit = 8
        self.composer_fill_chunk_chars = 4000
        self.delivery_error_retry_limit = 2
        self.javascript_argv_chunk_chars = 16000
        self.apple_event_timeout_seconds = 20.0
        self._foreground_javascript = False
        self._binding = None

    def open_chat(self, binding: Any) -> None:
        app_name = _macos_browser_app_name(binding)
        app_path = _macos_browser_app_path(binding)
        browser_binary_path = _macos_browser_binary_path(binding)
        if not app_name:
            raise RuntimeError("No supported normal browser is configured for this binding.")
        self._binding = binding
        if self._focus_existing_chat_tab(app_name, str(binding.chat_url)):
            return
        self._open_chat_url(app_name, app_path, browser_binary_path, str(binding.chat_url))

    def relaunch_chat(self, binding: Any | None = None) -> None:
        target_binding = binding or self._binding
        if target_binding is None:
            raise RuntimeError("No browser binding is open.")
        app_name = _macos_browser_app_name(target_binding)
        app_path = _macos_browser_app_path(target_binding)
        browser_binary_path = _macos_browser_binary_path(target_binding)
        chat_url = str(getattr(target_binding, "chat_url", "") or "").strip()
        if not app_name or not chat_url:
            raise RuntimeError("No supported normal browser is configured for this binding.")
        self._binding = target_binding
        if self._allow_force_browser_relaunch():
            self._terminate_browser_app(app_name)
        else:
            try:
                if self.reload_chat(None):
                    return
            except RuntimeError:
                pass
        self._open_chat_url(app_name, app_path, browser_binary_path, chat_url)

    def _open_chat_url(
        self,
        app_name: str,
        app_path: Any,
        browser_binary_path: Any,
        chat_url: str,
        *,
        foreground: bool = False,
    ) -> None:
        open_args = ["open"]
        if not foreground:
            open_args.append("-g")
        open_args.extend(["-a", app_name, chat_url])
        try:
            subprocess.run(
                open_args,
                check=True,
                text=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError:
            if app_path:
                open_path_args = ["open"]
                if not foreground:
                    open_path_args.append("-g")
                open_path_args.extend(["-a", str(app_path), chat_url])
                try:
                    subprocess.run(
                        open_path_args,
                        check=True,
                        text=True,
                        capture_output=True,
                    )
                    return
                except subprocess.CalledProcessError:
                    pass
            if not browser_binary_path:
                raise
            subprocess.Popen(
                [str(browser_binary_path), chat_url],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )

    def activate_chat(self, binding: Any | None = None) -> None:
        target_binding = binding or self._binding
        if target_binding is None:
            raise RuntimeError("No browser binding is open.")
        app_name = _macos_browser_app_name(target_binding)
        app_path = _macos_browser_app_path(target_binding)
        browser_binary_path = _macos_browser_binary_path(target_binding)
        chat_url = str(getattr(target_binding, "chat_url", "") or "").strip()
        if not app_name or not chat_url:
            raise RuntimeError("No supported normal browser is configured for this binding.")
        self._binding = target_binding
        self._open_chat_url(app_name, app_path, browser_binary_path, chat_url, foreground=True)
        time.sleep(max(int(getattr(self, "poll_interval_ms", 250)), 10) / 1000.0)

    def _terminate_browser_app(self, app_name: str) -> None:
        try:
            subprocess.run(
                ["/usr/bin/osascript", "-e", f'tell application "{app_name}" to quit'],
                text=True,
                capture_output=True,
                check=False,
                timeout=3.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        time.sleep(1.0)
        if not self._browser_process_running(app_name):
            return
        try:
            subprocess.run(
                ["/usr/bin/pkill", "-TERM", "-x", app_name],
                text=True,
                capture_output=True,
                check=False,
            )
        except OSError:
            return
        time.sleep(1.0)

    def _browser_process_running(self, app_name: str) -> bool:
        try:
            result = subprocess.run(
                ["/usr/bin/pgrep", "-x", app_name],
                text=True,
                capture_output=True,
                check=False,
            )
        except OSError:
            return False
        return result.returncode == 0

    def _allow_force_browser_relaunch(self) -> bool:
        value = str(os.environ.get("BRIDGE_ENABLE_NORMAL_BROWSER_FORCE_RELAUNCH", "") or "").strip().casefold()
        return value in {"1", "true", "yes", "on"}

    def current_chat_url(self, session: Any) -> str:
        try:
            payload = self._run_json_script(
                """
                (() => JSON.stringify({ href: String(window.location.href || '') }))()
                """
            )
            return str(payload.get("href", "")).strip()
        except RuntimeError as exc:
            if "tab for this chat was not found" not in str(exc):
                raise
            return self._front_window_active_tab_url()

    def reload_chat(self, session: Any) -> bool:
        if self._binding is None:
            raise RuntimeError("No browser binding is open.")
        app_name = _macos_browser_app_name(self._binding)
        chat_url = str(getattr(self._binding, "chat_url", "") or "").strip()
        if not app_name or not chat_url:
            raise RuntimeError("No supported normal browser is configured for this binding.")
        script = """
on run argv
  set termsAppName to item 1 of argv
  set boundUrl to item 2 of argv
  tell application termsAppName
    repeat with w in windows
      set tabCount to number of tabs in w
      repeat with i from 1 to tabCount
        set t to tab i of w
        if (URL of t as text) starts with boundUrl then
          tell t to reload
          return "reloaded"
        end if
      end repeat
    end repeat
  end tell
  error "Bridge tab not found."
end run
"""
        try:
            result = subprocess.run(
                ["/usr/bin/osascript", "-", app_name, chat_url],
                input=script,
                text=True,
                capture_output=True,
                check=True,
                timeout=max(float(getattr(self, "apple_event_timeout_seconds", 0.0) or 0.0), 0.1),
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                _applescript_transport_failure_message(f"{app_name} AppleEvent timed out (-1712).")
            ) from exc
        except subprocess.CalledProcessError as exc:
            message = (exc.stderr or exc.stdout or str(exc)).strip()
            if "Bridge tab not found" in message:
                raise RuntimeError(
                    "The normal browser tab for this chat was not found. Open the chat in your normal browser and keep that tab available."
                ) from exc
            if _looks_like_chrome_applescript_transport_failure(message):
                raise RuntimeError(_applescript_transport_failure_message(message)) from exc
            raise RuntimeError(message or "Browser tab reload via Apple Events failed.") from exc
        time.sleep(max(int(getattr(self, "poll_interval_ms", 250)), 10) / 1000.0)
        return str(result.stdout or "").strip() == "reloaded"

    def close(self) -> None:
        return None

    def read_latest_assistant_message(self, session: Any) -> dict[str, str]:
        payload = self._run_json_script(
            """
            (() => {
              const selectors = __ASSISTANT_SELECTORS__;
              for (const selector of selectors) {
                const nodes = Array.from(document.querySelectorAll(selector));
                const last = nodes[nodes.length - 1];
                if (!last) continue;
                const ownText = String(last.innerText || '').trim();
                const turn = last.closest('section[data-testid^="conversation-turn-"], [data-testid^="conversation-turn-"]');
                const turnText = turn ? String(turn.innerText || '').trim() : '';
                const text = ownText || turnText;
                if (!text) continue;
                const messageId = last.id || (turn ? turn.id || turn.getAttribute('data-testid') || '' : '');
                return JSON.stringify({ text, message_id: messageId, message_index: nodes.length - 1 });
              }
              return JSON.stringify({ missing_selector: true });
            })()
            """.replace("__ASSISTANT_SELECTORS__", json.dumps(self._selector_values("assistant_message")))
        )
        if payload.get("missing_selector"):
            fallback_message = self._fallback_latest_turn("assistant")
            if fallback_message is not None:
                return fallback_message
            access_blocker = self._access_blocker_message()
            if access_blocker:
                raise RuntimeError(access_blocker)
            raise RuntimeError("ChatGPT DOM contract missing `assistant_message` selector match.")
        text = str(payload.get("text", "")).strip()
        message_id = str(payload.get("message_id", ""))
        message_index = int(payload.get("message_index", 0) or 0)
        return {
            "message_id": message_id,
            "message_anchor": message_id or _indexed_message_anchor("assistant", message_index, text),
            "text": text,
        }

    def read_latest_user_message(self, session: Any) -> dict[str, str]:
        payload = self._run_json_script(
            """
            (() => {
              const selectors = __USER_SELECTORS__;
              for (const selector of selectors) {
                const nodes = Array.from(document.querySelectorAll(selector));
                const last = nodes[nodes.length - 1];
                if (!last) continue;
                const text = String(last.innerText || '').trim();
                if (!text) continue;
                return JSON.stringify({ text, message_id: last.id || '', message_index: nodes.length - 1 });
              }
              return JSON.stringify({ missing_selector: true });
            })()
            """.replace("__USER_SELECTORS__", json.dumps(self._selector_values("user_message")))
        )
        if payload.get("missing_selector"):
            fallback_message = self._fallback_latest_turn("user")
            if fallback_message is not None:
                return fallback_message
            raise RuntimeError("ChatGPT DOM contract missing `user_message` selector match.")
        text = str(payload.get("text", "")).strip()
        message_id = str(payload.get("message_id", ""))
        message_index = int(payload.get("message_index", 0) or 0)
        return {
            "message_id": message_id,
            "message_anchor": message_id or _indexed_message_anchor("user", message_index, text),
            "text": text,
        }

    def read_recent_user_messages(self, session: Any, limit: int = 8) -> list[dict[str, str]]:
        payload = self._run_json_script(
            """
            (() => {
              const selectors = __USER_SELECTORS__;
              const limit = __LIMIT__;
              const messages = [];
              for (const selector of selectors) {
                const nodes = Array.from(document.querySelectorAll(selector));
                const recent = nodes.slice(Math.max(0, nodes.length - limit));
                for (const node of recent) {
                  const text = String(node.innerText || '').trim();
                  if (!text) continue;
                  messages.push({ text, message_id: node.id || '', message_index: Math.max(0, nodes.length - limit) + messages.length });
                }
                if (messages.length) break;
              }
              return JSON.stringify({ messages });
            })()
            """
            .replace("__USER_SELECTORS__", json.dumps(self._selector_values("user_message")))
            .replace("__LIMIT__", str(max(limit, 1)))
        )
        messages = [
            {
                "text": str(item.get("text", "")).strip(),
                "message_id": str(item.get("message_id", "")),
                "message_anchor": str(item.get("message_id", "")) or _indexed_message_anchor(
                    "user",
                    int(item.get("message_index", 0) or 0),
                    str(item.get("text", "")).strip(),
                ),
            }
            for item in payload.get("messages", [])
            if str(item.get("text", "")).strip()
        ]
        if messages:
            return messages
        fallback = self._fallback_recent_turns("user", limit=max(limit, 1))
        return [
            {
                "text": item["text"],
                "message_id": item["message_id"],
                "message_anchor": item["message_id"] or _indexed_message_anchor(
                    "user",
                    int(item.get("message_index", 0) or 0),
                    item["text"],
                ),
            }
            for item in fallback
        ]

    def assistant_response_in_progress(self, session: Any) -> bool:
        payload = self._run_json_script(
            """
            (() => {
              const assistantSelectors = __ASSISTANT_SELECTORS__;
              const latestAssistant = (() => {
                for (const selector of assistantSelectors) {
                  const nodes = Array.from(document.querySelectorAll(selector));
                  const last = nodes[nodes.length - 1];
                  if (!last) continue;
                  return {
                    text: String(last.innerText || '').trim(),
                    streaming: Boolean(
                      (last.classList && last.classList.contains('streaming-animation')) ||
                      String(last.className || '').toLowerCase().includes('streaming-animation') ||
                      last.querySelector('.streaming-animation')
                    ),
                  };
                }
                return { text: '', streaming: false };
              })();
              const latestText = latestAssistant.text || '';
              const normalized = latestText.toLowerCase();
              const buttons = Array.from(document.querySelectorAll('button')).map((button) =>
                `${String(button.innerText || '').trim()} ${String(button.getAttribute('aria-label') || '').trim()} ${String(button.getAttribute('data-testid') || '').trim()}`.toLowerCase()
              );
              const stopVisible = buttons.some((text) => /(^|\\s)(stop|stopp)(\\s|$)|stop-button/.test(text));
              const thinkingVisible = ['thinking…', 'thinking...', 'denke nach…', 'denke nach...'].includes(normalized);
              return JSON.stringify({ in_progress: Boolean(stopVisible || thinkingVisible || latestAssistant.streaming) });
            })()
            """
            .replace("__ASSISTANT_SELECTORS__", json.dumps(self._selector_values("assistant_message")))
        )
        return bool(payload.get("in_progress"))

    def cancel_assistant_response(self, session: Any) -> bool:
        payload = self._run_json_script(
            """
            (() => {
              const buttons = Array.from(document.querySelectorAll('button'));
              const stopButton = buttons.findLast((button) => {
                const text = `${String(button.innerText || '').trim()} ${String(button.getAttribute('aria-label') || '').trim()} ${String(button.getAttribute('data-testid') || '').trim()}`.toLowerCase();
                return /(^|\\s)(stop|stopp)(\\s|$)|stop-button/.test(text) && !button.disabled;
              });
              if (!stopButton) return JSON.stringify({ cancelled: false });
              stopButton.click();
              return JSON.stringify({ cancelled: true });
            })()
            """
        )
        if payload.get("cancelled"):
            time.sleep(max(int(getattr(self, "poll_interval_ms", 250)), 10) / 1000.0)
            return True
        return False

    def retry_latest_assistant_response(self, session: Any) -> bool:
        if self._retry_latest_delivery_error():
            time.sleep(max(int(getattr(self, "poll_interval_ms", 250)), 10) / 1000.0)
            return True
        return False

    def latest_assistant_response_error(self, session: Any) -> str:
        return self._latest_delivery_error_text()

    def prepare_return_packet_delivery(self, session: Any) -> dict[str, str]:
        return self._prepare_composer_for_post()

    def post_user_message(self, session: Any, text: str, return_packet_id: str) -> dict[str, str]:
        previous_foreground = self._foreground_javascript
        self._foreground_javascript = True
        try:
            return self._post_user_message_foreground(session, text, return_packet_id)
        finally:
            self._foreground_javascript = previous_foreground

    def _post_user_message_foreground(self, session: Any, text: str, return_packet_id: str) -> dict[str, str]:
        composer_ready = self._prepare_composer_for_post()
        if composer_ready.get("status") != "ready":
            return {
                "status": "failed",
                "error_signature": str(composer_ready.get("error_signature", "")).strip()
                or "Return packet delivery preflight failed.",
                "return_packet_id": return_packet_id,
            }
        submit = self._set_composer_text(text)
        if submit.get("status") != "filled":
            error_signature = str(submit.get("error_signature", "")).strip()
            if submit.get("status") == "missing_composer" and not error_signature:
                access_blocker = self._access_blocker_message()
                if access_blocker:
                    error_signature = access_blocker
                else:
                    error_signature = "ChatGPT DOM contract missing `composer` selector match."
            return {
                "status": "failed",
                "error_signature": error_signature or "ChatGPT composer did not preserve the prepared packet text.",
                "return_packet_id": return_packet_id,
            }
        deadline = time.monotonic() + max(float(getattr(self, "post_ack_timeout_ms", 5000)) / 1000.0, 0.1)
        poll_interval_ms = max(int(getattr(self, "poll_interval_ms", 250)), 10)
        delivery_candidate_anchor = ""
        send_attempted = False
        send_attempted_at = 0.0
        enter_submit_after_click_attempted = False
        delivery_error_retry_count = 0
        while True:
            packet_visible = self._latest_user_message_contains_packet(session, return_packet_id)
            if packet_visible:
                latest_user = self.read_latest_user_message(session)
                candidate_anchor = str(latest_user.get("message_anchor", ""))
            else:
                latest_user = {}
                candidate_anchor = ""
            error_text = self._latest_delivery_error_text()
            if error_text and send_attempted:
                if delivery_error_retry_count < max(int(getattr(self, "delivery_error_retry_limit", 2)), 0):
                    if self._retry_latest_delivery_error():
                        delivery_error_retry_count += 1
                        delivery_candidate_anchor = ""
                        time.sleep(poll_interval_ms / 1000.0)
                        continue
                self._clear_composer_draft()
                return {
                    "status": "failed",
                    "error_signature": canonical_delivery_error_signature(error_text),
                    "return_packet_id": return_packet_id,
                }
            if packet_visible:
                if candidate_anchor and candidate_anchor == delivery_candidate_anchor:
                    return {"status": "delivered", "message_anchor": return_packet_id, "return_packet_id": return_packet_id}
                delivery_candidate_anchor = candidate_anchor
                if time.monotonic() >= deadline:
                    break
                time.sleep(poll_interval_ms / 1000.0)
                continue
            else:
                delivery_candidate_anchor = ""

            if not send_attempted:
                click_state = self._click_send_button()
                if click_state.get("clicked"):
                    send_attempted = True
                    send_attempted_at = time.monotonic()
                elif time.monotonic() >= deadline:
                    submit_state = self._submit_via_enter()
                    if submit_state.get("submitted"):
                        send_attempted = True
                        send_attempted_at = time.monotonic()
                        enter_submit_after_click_attempted = True
            elif not enter_submit_after_click_attempted:
                enter_grace_seconds = max(
                    float(getattr(self, "enter_submit_after_click_grace_ms", 1500) or 0.0) / 1000.0,
                    0.0,
                )
                if send_attempted_at > 0.0 and time.monotonic() - send_attempted_at >= enter_grace_seconds:
                    submit_state = self._submit_via_enter()
                    if submit_state.get("submitted"):
                        enter_submit_after_click_attempted = True
                        send_attempted_at = time.monotonic()
            error_text = self._latest_delivery_error_text()
            if error_text and send_attempted:
                if delivery_error_retry_count < max(int(getattr(self, "delivery_error_retry_limit", 2)), 0):
                    if self._retry_latest_delivery_error():
                        delivery_error_retry_count += 1
                        delivery_candidate_anchor = ""
                        time.sleep(poll_interval_ms / 1000.0)
                        continue
                self._clear_composer_draft()
                return {
                    "status": "failed",
                    "error_signature": canonical_delivery_error_signature(error_text),
                    "return_packet_id": return_packet_id,
                }
            if time.monotonic() >= deadline:
                break
            time.sleep(poll_interval_ms / 1000.0)
        error_text = self._latest_delivery_error_text()
        if error_text and send_attempted:
            self._clear_composer_draft()
            return {
                "status": "failed",
                "error_signature": canonical_delivery_error_signature(error_text),
                "return_packet_id": return_packet_id,
            }
        if delivery_candidate_anchor and self._latest_user_message_contains_packet(session, return_packet_id):
            latest_user = self.read_latest_user_message(session)
            if str(latest_user.get("message_anchor", "")) == delivery_candidate_anchor:
                return {"status": "delivered", "message_anchor": return_packet_id, "return_packet_id": return_packet_id}
        self._clear_composer_draft()
        return {
            "status": "failed",
            "error_signature": "Message delivery confirmation timed out.",
            "return_packet_id": return_packet_id,
        }

    def _click_send_button(self) -> dict[str, Any]:
        return self._run_json_script(
            """
            (() => {
              const sendSelectors = __SEND_SELECTORS__;
              const isVisible = (node) => {
                if (!node) return false;
                const style = window.getComputedStyle(node);
                if (!style) return false;
                if (style.display === 'none' || style.visibility === 'hidden') return false;
                return node.getClientRects().length > 0;
              };
              for (const selector of sendSelectors) {
                const nodes = Array.from(document.querySelectorAll(selector)).filter(isVisible);
                const button = nodes[nodes.length - 1] || null;
                if (!button) continue;
                if (button.disabled) return JSON.stringify({ clicked: false, disabled: true });
                button.click();
                return JSON.stringify({ clicked: true });
              }
              return JSON.stringify({ clicked: false });
            })()
            """.replace("__SEND_SELECTORS__", json.dumps(self._selector_values("send_button")))
        )

    def _submit_via_enter(self) -> dict[str, Any]:
        return self._run_json_script(
            """
            (() => {
              const composerSelectors = __COMPOSER_SELECTORS__;
              const isVisible = (node) => {
                if (!node) return false;
                const style = window.getComputedStyle(node);
                if (!style) return false;
                if (style.display === 'none' || style.visibility === 'hidden') return false;
                return node.getClientRects().length > 0;
              };
              for (const selector of composerSelectors) {
                const nodes = Array.from(document.querySelectorAll(selector)).filter(isVisible);
                const composer = nodes[nodes.length - 1] || null;
                if (!composer) continue;
                composer.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', which: 13, keyCode: 13, bubbles: true }));
                composer.dispatchEvent(new KeyboardEvent('keypress', { key: 'Enter', code: 'Enter', which: 13, keyCode: 13, bubbles: true }));
                composer.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', code: 'Enter', which: 13, keyCode: 13, bubbles: true }));
                return JSON.stringify({ submitted: true });
              }
              return JSON.stringify({ submitted: false });
            })()
            """.replace("__COMPOSER_SELECTORS__", json.dumps(self._selector_values("composer")))
        )

    def _set_composer_text(self, text: str) -> dict[str, Any]:
        composer_selectors = json.dumps(self._selector_values("composer"))
        chunks = [text[index : index + self.composer_fill_chunk_chars] for index in range(0, len(text), self.composer_fill_chunk_chars)]
        if not chunks:
            chunks = [""]
        for index, chunk in enumerate(chunks):
            chunk_b64 = base64.b64encode(chunk.encode("utf-8")).decode("ascii")
            prepared_value = "".join(chunks[: index + 1])
            prepared_b64 = base64.b64encode(prepared_value.encode("utf-8")).decode("ascii")
            payload = self._run_json_script(
                """
                (() => {
                  const composerSelectors = __COMPOSER_SELECTORS__;
                  const payloadChunk = new TextDecoder().decode(
                    Uint8Array.from(atob(__MESSAGE_CHUNK_B64__), (character) => character.charCodeAt(0))
                  );
                  const preparedValue = new TextDecoder().decode(
                    Uint8Array.from(atob(__PREPARED_VALUE_B64__), (character) => character.charCodeAt(0))
                  );
                  const replaceMode = __REPLACE_MODE__;
                  const isVisible = (node) => {
                    if (!node) return false;
                    const style = window.getComputedStyle(node);
                    if (!style) return false;
                    if (style.display === 'none' || style.visibility === 'hidden') return false;
                    return node.getClientRects().length > 0;
                  };
                  const renderPlainTextToHtml = (value) => {
                    const normalized = String(value || '').replace(/\\r\\n/g, '\\n').replace(/\\r/g, '\\n');
                    if (!normalized) return '<p><br class="ProseMirror-trailingBreak"></p>';
                    const escapeHtml = (input) => String(input)
                      .replace(/&/g, '&amp;')
                      .replace(/</g, '&lt;')
                      .replace(/>/g, '&gt;');
                    const blocks = [];
                    let paragraph = [];
                    const flushParagraph = () => {
                      if (!paragraph.length) {
                        blocks.push('<p><br class="ProseMirror-trailingBreak"></p>');
                        return;
                      }
                      blocks.push(`<p>${paragraph.map(escapeHtml).join('<br>')}</p>`);
                      paragraph = [];
                    };
                    for (const line of normalized.split('\\n')) {
                      if (line === '') {
                        flushParagraph();
                        paragraph = [];
                        continue;
                      }
                      paragraph.push(line);
                    }
                    if (paragraph.length || normalized.endsWith('\\n')) {
                      flushParagraph();
                    }
                    return blocks.join('');
                  };
                  const findLastNode = (selectors, { visibleOnly }) => {
                    for (const selector of selectors) {
                      const nodes = Array.from(document.querySelectorAll(selector));
                      const matches = visibleOnly ? nodes.filter(isVisible) : nodes;
                      const candidate = matches[matches.length - 1] || null;
                      if (candidate) return candidate;
                    }
                    return null;
                  };
                  const composer = findLastNode(composerSelectors, { visibleOnly: true }) || findLastNode(composerSelectors, { visibleOnly: false });
                  if (!composer) return JSON.stringify({ status: 'missing_composer' });
                  const nextValue = preparedValue;
                  if (composer.tagName === 'TEXTAREA') {
                    const prototype = Object.getPrototypeOf(composer);
                    const descriptor = Object.getOwnPropertyDescriptor(prototype, 'value');
                    if (descriptor && descriptor.set) {
                      descriptor.set.call(composer, nextValue);
                    } else {
                      composer.value = nextValue;
                    }
                    composer.dispatchEvent(new Event('input', { bubbles: true }));
                    composer.dispatchEvent(new Event('change', { bubbles: true }));
                  } else {
                    composer.focus();
                    composer.innerHTML = renderPlainTextToHtml(nextValue);
                    composer.dispatchEvent(new InputEvent('input', { bubbles: true, data: payloadChunk, inputType: 'insertText' }));
                    composer.dispatchEvent(new Event('change', { bubbles: true }));
                  }
                  return JSON.stringify({ status: 'filled' });
                })()
                """
                .replace("__COMPOSER_SELECTORS__", composer_selectors)
                .replace("__MESSAGE_CHUNK_B64__", json.dumps(chunk_b64))
                .replace("__PREPARED_VALUE_B64__", json.dumps(prepared_b64))
                .replace("__REPLACE_MODE__", "true" if index == 0 else "false")
            )
            if payload.get("status") != "filled":
                return payload
        observed_text = self._composer_text_value()
        if observed_text is None:
            return {"status": "missing_composer"}
        if not composer_text_preserves_payload(text, observed_text):
            return {
                "status": "failed",
                "error_signature": "ChatGPT composer did not preserve the prepared packet text.",
            }
        return {"status": "filled"}

    def _clear_composer_draft(self) -> None:
        try:
            self._run_json_script(
                """
                (() => {
                  const composerSelectors = __COMPOSER_SELECTORS__;
                  const isVisible = (node) => {
                    if (!node) return false;
                    const style = window.getComputedStyle(node);
                    if (!style) return false;
                    if (style.display === 'none' || style.visibility === 'hidden') return false;
                    return node.getClientRects().length > 0;
                  };
                  const findLastNode = (selectors, { visibleOnly }) => {
                    for (const selector of selectors) {
                      const nodes = Array.from(document.querySelectorAll(selector));
                      const matches = visibleOnly ? nodes.filter(isVisible) : nodes;
                      const candidate = matches[matches.length - 1] || null;
                      if (candidate) return candidate;
                    }
                    return null;
                  };
                  const composer = findLastNode(composerSelectors, { visibleOnly: true }) || findLastNode(composerSelectors, { visibleOnly: false });
                  if (!composer) return JSON.stringify({ cleared: false });
                  if (composer.tagName === 'TEXTAREA') {
                    const prototype = Object.getPrototypeOf(composer);
                    const descriptor = Object.getOwnPropertyDescriptor(prototype, 'value');
                    if (descriptor && descriptor.set) {
                      descriptor.set.call(composer, '');
                    } else {
                      composer.value = '';
                    }
                    composer.dispatchEvent(new Event('input', { bubbles: true }));
                    composer.dispatchEvent(new Event('change', { bubbles: true }));
                  } else {
                    composer.focus();
                    const selection = window.getSelection ? window.getSelection() : null;
                    if (selection) {
                      const range = document.createRange();
                      range.selectNodeContents(composer);
                      selection.removeAllRanges();
                      selection.addRange(range);
                    }
                    let cleared = false;
                    if (document.execCommand) {
                      try {
                        cleared = Boolean(document.execCommand('insertText', false, ''));
                      } catch (error) {
                        cleared = false;
                      }
                      if (!cleared) {
                        try {
                          cleared = Boolean(document.execCommand('delete', false, null));
                        } catch (error) {
                          cleared = false;
                        }
                      }
                    }
                    composer.textContent = '';
                    if (String(composer.className || '').includes('ProseMirror')) {
                      composer.innerHTML = '<p><br class="ProseMirror-trailingBreak"></p>';
                    }
                    composer.dispatchEvent(new InputEvent('input', { bubbles: true, data: '', inputType: 'deleteContentBackward' }));
                    composer.dispatchEvent(new Event('change', { bubbles: true }));
                    if (selection) {
                      selection.removeAllRanges();
                    }
                  }
                  return JSON.stringify({ cleared: true });
                })()
                """.replace("__COMPOSER_SELECTORS__", json.dumps(self._selector_values("composer")))
            )
        except Exception:
            return

    def _prepare_composer_for_post(self) -> dict[str, str]:
        for _ in range(3):
            self._clear_composer_draft()
            composer_text = self._composer_text_value()
            if composer_text is None:
                access_blocker = self._access_blocker_message()
                if access_blocker:
                    return {"status": "failed", "error_signature": access_blocker}
                return {
                    "status": "failed",
                    "error_signature": "ChatGPT DOM contract missing `composer` selector match.",
                }
            normalized = composer_text.replace("\u200b", "").replace("\xa0", " ").strip()
            if not normalized:
                return {"status": "ready"}
            time.sleep(max(int(getattr(self, "poll_interval_ms", 250)), 10) / 1000.0)
        return {
            "status": "failed",
            "error_signature": "ChatGPT composer still contains draft text after clear verification.",
        }

    def _composer_text_value(self) -> str | None:
        payload = self._run_json_script(
            """
            (() => {
              const composerSelectors = __COMPOSER_SELECTORS__;
              const isVisible = (node) => {
                if (!node) return false;
                const style = window.getComputedStyle(node);
                if (!style) return false;
                if (style.display === 'none' || style.visibility === 'hidden') return false;
                return node.getClientRects().length > 0;
              };
              const findLastNode = (selectors, { visibleOnly }) => {
                for (const selector of selectors) {
                  const nodes = Array.from(document.querySelectorAll(selector));
                  const matches = visibleOnly ? nodes.filter(isVisible) : nodes;
                  const candidate = matches[matches.length - 1] || null;
                  if (candidate) return candidate;
                }
                return null;
              };
              const composer = findLastNode(composerSelectors, { visibleOnly: true }) || findLastNode(composerSelectors, { visibleOnly: false });
              if (composer) {
                const value = composer.tagName === 'TEXTAREA'
                  ? String(composer.value || '')
                  : String(composer.innerText || composer.textContent || '');
                return JSON.stringify({ present: true, value });
              }
              return JSON.stringify({ present: false, value: '' });
            })()
            """.replace("__COMPOSER_SELECTORS__", json.dumps(self._selector_values("composer")))
        )
        if not payload.get("present"):
            return None
        return str(payload.get("value", ""))

    def return_packet_visible(self, session: Any, return_packet_id: str) -> bool:
        payload = self._run_json_script(
            """
            (() => {
              const selectors = __USER_SELECTORS__;
              const packetId = __PACKET_ID__;
              for (const selector of selectors) {
                const nodes = Array.from(document.querySelectorAll(selector));
                if (nodes.some((node) => String(node.innerText || '').includes(packetId))) {
                  return JSON.stringify({ visible: true });
                }
              }
              return JSON.stringify({ visible: false });
            })()
            """
            .replace("__USER_SELECTORS__", json.dumps(self._selector_values("user_message")))
            .replace("__PACKET_ID__", json.dumps(return_packet_id))
        )
        if payload.get("visible"):
            return True
        return any(return_packet_id in item["text"] for item in self._fallback_recent_turns("user", limit=8))

    def _latest_user_message_contains_packet(self, session: Any, return_packet_id: str) -> bool:
        try:
            latest = self.read_latest_user_message(session)
        except RuntimeError:
            return False
        return return_packet_id in str(latest.get("text", ""))

    def poll_stop_command(self, session: Any, stop_phrases: list[str]) -> dict[str, str] | None:
        payload = self._run_json_script(
            """
            (() => {
              const selectors = __USER_SELECTORS__;
              const limit = __LIMIT__;
              const messages = [];
              for (const selector of selectors) {
                const nodes = Array.from(document.querySelectorAll(selector));
                const recent = nodes.slice(Math.max(0, nodes.length - limit));
                for (const node of recent) {
                  const text = String(node.innerText || '').trim();
                  if (!text) continue;
                  messages.push({ text, message_id: node.id || '' });
                }
                if (messages.length) break;
              }
              return JSON.stringify({ messages });
            })()
            """
            .replace("__USER_SELECTORS__", json.dumps(self._selector_values("user_message")))
            .replace("__LIMIT__", str(int(getattr(self, "recent_message_scan_limit", 8))))
        )
        messages = [
            {
                "text": str(item.get("text", "")).strip(),
                "message_id": str(item.get("message_id", "")),
            }
            for item in payload.get("messages", [])
            if str(item.get("text", "")).strip()
        ]
        if not messages:
            messages = self._fallback_recent_turns("user", limit=int(getattr(self, "recent_message_scan_limit", 8)))
        for item in reversed(messages):
            command = normalize_stop_command(str(item.get("text", "")), stop_phrases)
            if command:
                text = str(item.get("text", "")).strip()
                message_id = str(item.get("message_id", ""))
                return {
                    "command": command,
                    "text": text,
                    "message_id": message_id,
                    "message_anchor": message_id or _indexed_message_anchor(
                        "user",
                        int(item.get("message_index", 0) or 0),
                        text,
                    ),
                    "message_hash": hashlib.sha1(text.encode("utf-8")).hexdigest(),
                }
        return None

    def _latest_delivery_error_text(self) -> str:
        try:
            payload = self._run_json_script(
                """
                (() => {
                  const selectors = __ERROR_SELECTORS__;
                  for (const selector of selectors) {
                    const nodes = Array.from(document.querySelectorAll(selector));
                    const last = nodes[nodes.length - 1];
                    if (!last) continue;
                    const text = String(last.innerText || '').trim();
                    if (text) return JSON.stringify({ text });
                  }
                  const retryMarkers = __RETRY_MARKERS__;
                  const buttons = Array.from(document.querySelectorAll('button'));
                  const retryButton = buttons.findLast((button) => {
                    const label = `${String(button.innerText || '').trim()} ${String(button.getAttribute('aria-label') || '').trim()} ${String(button.getAttribute('data-testid') || '').trim()}`.trim().toLowerCase();
                    if (!label) return false;
                    if (button.disabled) return false;
                    return retryMarkers.some((marker) => label.includes(marker));
                  });
                  if (!retryButton) return JSON.stringify({ text: '' });
                  const label = `${String(retryButton.innerText || '').trim()} ${String(retryButton.getAttribute('aria-label') || '').trim()}`.trim();
                  const container = retryButton.closest('[data-testid^="conversation-turn-"], [data-message-author-role], [role="alert"], article, section');
                  const context = container ? String(container.innerText || '').trim() : '';
                  return JSON.stringify({ text: context || label });
                })()
                """
                .replace("__ERROR_SELECTORS__", json.dumps(self._selector_values("delivery_error")))
                .replace("__RETRY_MARKERS__", json.dumps([marker.casefold() for marker in CHATGPT_RETRY_BUTTON_MARKERS]))
            )
        except RuntimeError:
            return ""
        return str(payload.get("text", "")).strip()

    def _retry_latest_delivery_error(self) -> bool:
        payload = self._run_json_script(
            """
            (() => {
              const buttons = Array.from(document.querySelectorAll('button'));
              const retryMarkers = __RETRY_MARKERS__;
              const retryButton = buttons.findLast((button) => {
                const label = `${String(button.innerText || '').trim()} ${String(button.getAttribute('aria-label') || '').trim()} ${String(button.getAttribute('data-testid') || '').trim()}`.trim().toLowerCase();
                if (!label) return false;
                if (button.disabled) return false;
                return retryMarkers.some((marker) => label.includes(marker));
              });
              if (!retryButton) return JSON.stringify({ retried: false });
              retryButton.click();
              return JSON.stringify({ retried: true });
            })()
            """.replace("__RETRY_MARKERS__", json.dumps([marker.casefold() for marker in CHATGPT_RETRY_BUTTON_MARKERS]))
        )
        return bool(payload.get("retried"))

    def _fallback_latest_turn(self, role: str) -> dict[str, str] | None:
        recent = self._fallback_recent_turns(role, limit=12)
        if not recent:
            return None
        latest = recent[-1]
        text = latest["text"]
        message_id = latest["message_id"]
        return {
            "message_id": message_id,
            "message_anchor": message_id or _indexed_message_anchor(role, int(latest.get("message_index", 0) or 0), text),
            "text": text,
        }

    def _fallback_recent_turns(self, role: str, limit: int) -> list[dict[str, str]]:
        payload = self._run_json_script(
            """
            (() => {
              const limit = __LIMIT__;
              const nodes = Array.from(document.querySelectorAll('section[data-testid^="conversation-turn-"]'));
              const recent = nodes.slice(Math.max(0, nodes.length - limit));
              const startIndex = Math.max(0, nodes.length - limit);
              const messages = recent.map((node, idx) => ({
                text: String(node.innerText || '').trim(),
                message_id: node.id || node.getAttribute('data-testid') || '',
                message_index: startIndex + idx
              })).filter((item) => item.text);
              return JSON.stringify({ messages });
            })()
            """.replace("__LIMIT__", str(max(limit, 1)))
        )
        messages: list[dict[str, str]] = []
        for item in payload.get("messages", []):
            original_text = str(item.get("text", "")).strip()
            if not original_text:
                continue
            detected_role = _classify_turn_role(original_text)
            if detected_role != role:
                continue
            messages.append(
                {
                    "text": _strip_turn_role_label(original_text, detected_role),
                    "message_id": str(item.get("message_id", "")),
                    "message_index": int(item.get("message_index", 0) or 0),
                }
            )
        return messages

    def _access_blocker_message(self) -> str:
        payload = self._run_json_script(
            """
            (() => JSON.stringify({
              title: String(document.title || '').trim(),
              body: String(document.body ? document.body.innerText || '' : '').trim().slice(0, 4000)
            }))()
            """
        )
        title_lower = str(payload.get("title", "")).casefold()
        body_lower = str(payload.get("body", "")).casefold()
        if "just a moment" in title_lower or "just a moment" in body_lower or "verify you are human" in body_lower:
            return (
                "ChatGPT is showing an interstitial or challenge page (`Just a moment...`) in your normal browser tab. "
                "Wait until the real conversation UI is visible in that tab, then start again."
            )
        if "log in" in title_lower or "sign in" in title_lower or "log in" in body_lower or "sign in" in body_lower:
            return (
                "ChatGPT is not fully authenticated in the normal browser tab. "
                "Open the chat there, log in, and start again."
            )
        return ""

    def _selector_values(self, key: str) -> list[str]:
        raw_value = self.selectors.get(key, [])
        if isinstance(raw_value, str):
            values = [raw_value]
        else:
            values = [str(item) for item in raw_value]
        return [value for value in values if value.strip()]

    def _run_json_script(self, js_code: str) -> dict[str, Any]:
        raw = self._run_browser_javascript(js_code)
        text = str(raw or "").strip()
        if not text:
            return {}
        return json.loads(text)

    def _run_browser_javascript(self, js_code: str) -> str:
        if self._binding is None:
            raise RuntimeError("No browser binding is open.")
        app_name = _macos_browser_app_name(self._binding)
        if not app_name:
            raise RuntimeError("No supported normal browser is configured for this binding.")
        js_chunks = [
            js_code[index : index + max(int(self.javascript_argv_chunk_chars or 0), 1)]
            for index in range(0, len(js_code), max(int(self.javascript_argv_chunk_chars or 0), 1))
        ] or [""]
        script = _chrome_osascript_source(
            js_chunks,
            terms_app_name=app_name,
            foreground=bool(getattr(self, "_foreground_javascript", False)),
        )
        try:
            result = subprocess.run(
                ["/usr/bin/osascript", "-", app_name, str(self._binding.chat_url)],
                input=script,
                text=True,
                capture_output=True,
                check=True,
                timeout=max(float(getattr(self, "apple_event_timeout_seconds", 0.0) or 0.0), 0.1),
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                _applescript_transport_failure_message(f'{app_name} AppleEvent timed out (-1712).')
            ) from exc
        except OSError as exc:
            raise RuntimeError(str(exc) or "Browser automation via Apple Events failed.") from exc
        except subprocess.CalledProcessError as exc:
            message = (exc.stderr or exc.stdout or str(exc)).strip()
            if "JavaScript" in message and "Apple Events" in message:
                raise RuntimeError(
                    "Chrome blocks local DOM automation until `JavaScript from Apple Events` is enabled. "
                    "In Chrome: View -> Developer -> Allow JavaScript from Apple Events, then try again."
                ) from exc
            if "Bridge tab not found" in message:
                raise RuntimeError(
                    "The normal browser tab for this chat was not found. Open the chat in your normal browser and keep that tab available."
                ) from exc
            if _looks_like_chrome_applescript_transport_failure(message):
                raise RuntimeError(_applescript_transport_failure_message(message)) from exc
            raise RuntimeError(message or "Browser automation via Apple Events failed.") from exc
        return result.stdout

    def _focus_existing_chat_tab(self, app_name: str, chat_url: str) -> bool:
        script = _chrome_focus_tab_osascript_source(terms_app_name=app_name)
        try:
            result = subprocess.run(
                ["/usr/bin/osascript", "-", app_name, chat_url],
                input=script,
                text=True,
                capture_output=True,
                check=False,
                timeout=max(float(getattr(self, "apple_event_timeout_seconds", 0.0) or 0.0), 0.1),
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(_applescript_transport_failure_message(f'{app_name} AppleEvent timed out (-1712).')) from exc
        if result.returncode == 0:
            return True
        message = (result.stderr or result.stdout or "").strip()
        if "Bridge tab not found" in message:
            return False
        if _looks_like_chrome_applescript_transport_failure(message):
            raise RuntimeError(_applescript_transport_failure_message(message))
        raise RuntimeError(message or "Browser tab focus via Apple Events failed.")

    def _front_window_active_tab_url(self) -> str:
        app_name = _macos_browser_app_name(self._binding)
        if not app_name:
            raise RuntimeError("No supported normal browser is configured for this binding.")
        try:
            result = subprocess.run(
                [
                    "/usr/bin/osascript",
                    "-e",
                    f'tell application "{app_name}" to return URL of active tab of front window',
                ],
                text=True,
                capture_output=True,
                check=True,
                timeout=max(float(getattr(self, "apple_event_timeout_seconds", 0.0) or 0.0), 0.1),
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(_applescript_transport_failure_message(f'{app_name} AppleEvent timed out (-1712).')) from exc
        except subprocess.CalledProcessError as exc:
            message = (exc.stderr or exc.stdout or str(exc)).strip()
            if _looks_like_chrome_applescript_transport_failure(message):
                raise RuntimeError(_applescript_transport_failure_message(message)) from exc
            raise RuntimeError(message or "Browser tab inspection via Apple Events failed.") from exc
        return str(result.stdout or "").strip()
