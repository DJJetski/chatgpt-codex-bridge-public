from __future__ import annotations

import base64
import hashlib
import json
import time
from typing import Any

from .browser_support import (
    CHATGPT_RETRY_BUTTON_MARKERS,
    _DEFAULT_POST_ACK_TIMEOUT_MS,
    _indexed_message_anchor,
    _looks_like_playwright_launch_transport_failure,
    canonical_delivery_error_signature,
    composer_text_preserves_payload,
    detect_preferred_browser_channel,
    normalize_stop_command,
)


class PlaywrightChatAdapter:
    """Optional Playwright-backed adapter for the bound ChatGPT chat."""

    def __init__(self, *, headless: bool = True, selectors: dict[str, str] | None = None) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # pragma: no cover - exercised through the import guard test
            raise RuntimeError(
                "Playwright is not installed. Install the optional browser dependency to use run-loop."
            ) from exc

        self._sync_playwright = sync_playwright
        self._playwright_context = None
        self._browser_context = None
        self._page = None
        self.headless = headless
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

    def open_chat(self, binding: Any) -> None:
        if self._browser_context is None:
            self._playwright_context = self._sync_playwright().start()
            chromium = self._playwright_context.chromium
            profile_path = getattr(binding, "browser_profile_path", "") or None
            browser_channel = getattr(binding, "browser_channel", "") or detect_preferred_browser_channel() or None
            if profile_path:
                self._browser_context = self._launch_persistent_context(
                    chromium,
                    profile_path,
                    browser_channel=browser_channel,
                )
            else:
                launch_kwargs = {"headless": self.headless}
                if browser_channel:
                    launch_kwargs["channel"] = browser_channel
                browser = chromium.launch(**launch_kwargs)
                self._browser_context = browser.new_context()
            self._page = self._browser_context.pages[0] if self._browser_context.pages else self._browser_context.new_page()
        self._page.goto(binding.chat_url, wait_until="domcontentloaded")

    def _launch_persistent_context(self, chromium: Any, profile_path: str, *, browser_channel: str | None):
        launch_kwargs = {"headless": self.headless}
        if browser_channel:
            launch_kwargs["channel"] = browser_channel
        try:
            return chromium.launch_persistent_context(profile_path, **launch_kwargs)
        except Exception as exc:
            if not browser_channel or not _looks_like_playwright_launch_transport_failure(str(exc or "")):
                raise
        return chromium.launch_persistent_context(profile_path, headless=self.headless)

    def current_chat_url(self, session: Any) -> str:
        if self._page is None:
            raise RuntimeError("No browser page is open.")
        return str(getattr(self._page, "url", "") or "")

    def reload_chat(self, session: Any) -> bool:
        if self._page is None:
            raise RuntimeError("No browser page is open.")
        self._page.reload(wait_until="domcontentloaded")
        self._page.wait_for_timeout(max(int(getattr(self, "poll_interval_ms", 250)), 10))
        return True

    def close(self) -> None:
        if self._browser_context is not None:
            self._browser_context.close()
            self._browser_context = None
            self._page = None
        if self._playwright_context is not None:
            self._playwright_context.stop()
            self._playwright_context = None

    def read_latest_assistant_message(self, session: Any) -> dict[str, str]:
        for selector in self._selector_values("assistant_message"):
            locator = self._page.locator(selector)
            count = locator.count()
            if count <= 0:
                continue
            item = locator.nth(count - 1)
            text = item.inner_text().strip()
            if not text:
                continue
            message_id = item.get_attribute("id") or ""
            anchor = message_id or _indexed_message_anchor("assistant", count - 1, text)
            return {"message_id": message_id, "message_anchor": anchor, "text": text}
        locator = self._required_locator("assistant_message")
        text = locator.inner_text().strip()
        message_id = locator.get_attribute("id") or ""
        anchor = message_id or _indexed_message_anchor("assistant", 0, text)
        return {"message_id": message_id, "message_anchor": anchor, "text": text}

    def read_latest_user_message(self, session: Any) -> dict[str, str]:
        for selector in self._selector_values("user_message"):
            locator = self._page.locator(selector)
            count = locator.count()
            if count <= 0:
                continue
            item = locator.nth(count - 1)
            text = item.inner_text().strip()
            if not text:
                continue
            message_id = item.get_attribute("id") or ""
            anchor = message_id or _indexed_message_anchor("user", count - 1, text)
            return {"message_id": message_id, "message_anchor": anchor, "text": text}
        locator = self._required_locator("user_message")
        text = locator.inner_text().strip()
        message_id = locator.get_attribute("id") or ""
        anchor = message_id or _indexed_message_anchor("user", 0, text)
        return {"message_id": message_id, "message_anchor": anchor, "text": text}

    def read_recent_user_messages(self, session: Any, limit: int = 8) -> list[dict[str, str]]:
        return self._recent_messages("user_message", max(limit, 1))

    def assistant_response_in_progress(self, session: Any) -> bool:
        payload = self._run_json_script(
            """
            (() => {
              const latestAssistant = (() => {
                const selectors = __ASSISTANT_SELECTORS__;
                for (const selector of selectors) {
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
              const buttonTexts = Array.from(document.querySelectorAll('button')).map((button) =>
                `${String(button.innerText || '').trim()} ${String(button.getAttribute('aria-label') || '').trim()} ${String(button.getAttribute('data-testid') || '').trim()}`.toLowerCase()
              );
              const stopVisible = buttonTexts.some((text) => /(^|\\s)(stop|stopp)(\\s|$)|stop-button/.test(text));
              const thinkingVisible = ['thinking…', 'thinking...', 'denke nach…', 'denke nach...'].includes(normalized);
              return JSON.stringify({ in_progress: Boolean(stopVisible || thinkingVisible || latestAssistant.streaming) });
            })()
            """.replace("__ASSISTANT_SELECTORS__", json.dumps(self._selector_values("assistant_message")))
        )
        return bool(payload.get("in_progress"))

    def cancel_assistant_response(self, session: Any) -> bool:
        payload = self._run_json_script(
            """
            (() => {
              const buttonTexts = Array.from(document.querySelectorAll('button'));
              const stopButton = buttonTexts.findLast((button) => {
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
            self._page.wait_for_timeout(max(int(getattr(self, "poll_interval_ms", 250)), 10))
            return True
        return False

    def retry_latest_assistant_response(self, session: Any) -> bool:
        if self._retry_latest_delivery_error():
            self._page.wait_for_timeout(max(int(getattr(self, "poll_interval_ms", 250)), 10))
            return True
        return False

    def latest_assistant_response_error(self, session: Any) -> str:
        return self._latest_delivery_error_text()

    def prepare_return_packet_delivery(self, session: Any) -> dict[str, str]:
        return self._prepare_composer_for_post()

    def post_user_message(self, session: Any, text: str, return_packet_id: str) -> dict[str, str]:
        composer_ready = self._prepare_composer_for_post()
        if composer_ready.get("status") != "ready":
            return {
                "status": "failed",
                "error_signature": str(composer_ready.get("error_signature", "")).strip()
                or "Return packet delivery preflight failed.",
                "return_packet_id": return_packet_id,
            }
        prepared = self._set_composer_text(text)
        if prepared.get("status") != "filled":
            error_signature = str(prepared.get("error_signature", "")).strip()
            if prepared.get("status") == "missing_composer" and not error_signature:
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
                        self._page.wait_for_timeout(poll_interval_ms)
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
                self._page.wait_for_timeout(poll_interval_ms)
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

            if time.monotonic() >= deadline:
                break
            self._page.wait_for_timeout(poll_interval_ms)

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

    def _click_send_button(self) -> dict[str, bool]:
        send_locator = self._optional_locator("send_button")
        if send_locator is None:
            return {"clicked": False}
        try:
            send_locator.click()
        except Exception:
            return {"clicked": False}
        return {"clicked": True}

    def _submit_via_enter(self) -> dict[str, bool]:
        composer = self._optional_locator("composer")
        if composer is None:
            return {"submitted": False}
        try:
            composer.press("Enter")
        except Exception:
            return {"submitted": False}
        return {"submitted": True}

    def _set_composer_text(self, text: str) -> dict[str, str]:
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
                    composer.dispatchEvent(new InputEvent('input', { bubbles: true, data: payloadChunk, inputType: replaceMode ? 'insertReplacementText' : 'insertText' }));
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
        locator = self._optional_locator("composer")
        if locator is None:
            return
        try:
            locator.fill("")
        except Exception:
            pass
        for shortcut in ("Meta+A", "Control+A"):
            try:
                locator.press(shortcut)
                break
            except Exception:
                continue
        for key in ("Backspace", "Delete"):
            try:
                locator.press(key)
                break
            except Exception:
                continue
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
                    composer.textContent = '';
                    if (String(composer.className || '').includes('ProseMirror')) {
                      composer.innerHTML = '<p><br class="ProseMirror-trailingBreak"></p>';
                    }
                    composer.dispatchEvent(new InputEvent('input', { bubbles: true, data: '', inputType: 'deleteContentBackward' }));
                    composer.dispatchEvent(new Event('change', { bubbles: true }));
                  }
                  return JSON.stringify({ cleared: true });
                })()
                """.replace("__COMPOSER_SELECTORS__", json.dumps(self._selector_values("composer")))
            )
        except Exception:
            return

    def _prepare_composer_for_post(self) -> dict[str, str]:
        locator = self._optional_locator("composer")
        if locator is None:
            access_blocker = self._access_blocker_message()
            if access_blocker:
                return {"status": "failed", "error_signature": access_blocker}
            return {
                "status": "failed",
                "error_signature": "ChatGPT DOM contract missing `composer` selector match.",
            }
        for _ in range(3):
            self._clear_composer_draft()
            if self._composer_is_empty():
                return {"status": "ready"}
            self._page.wait_for_timeout(max(int(getattr(self, "poll_interval_ms", 250)), 10))
        return {
            "status": "failed",
            "error_signature": "ChatGPT composer still contains draft text after clear verification.",
        }

    def _composer_is_empty(self) -> bool:
        text = self._composer_text_value()
        if text is None:
            return False
        normalized = text.replace("\u200b", "").replace("\xa0", " ").strip()
        return not normalized

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
        for selector in self._selector_values("user_message"):
            locator = self._page.locator(selector)
            count = locator.count()
            for index in range(count):
                text = locator.nth(index).inner_text()
                if return_packet_id in text:
                    return True
        return False

    def _latest_user_message_contains_packet(self, session: Any, return_packet_id: str) -> bool:
        try:
            latest = self.read_latest_user_message(session)
        except RuntimeError:
            return False
        return return_packet_id in str(latest.get("text", ""))

    def poll_stop_command(self, session: Any, stop_phrases: list[str]) -> dict[str, str] | None:
        recent_messages = self._recent_messages("user_message", int(getattr(self, "recent_message_scan_limit", 8)))
        for message in reversed(recent_messages):
            command = normalize_stop_command(message["text"], stop_phrases)
            if command:
                return {
                    "command": command,
                    "text": message["text"],
                    "message_id": message["message_id"],
                    "message_anchor": message["message_anchor"],
                    "message_hash": hashlib.sha1(message["text"].encode("utf-8")).hexdigest(),
                }
        return None

    def _selector_values(self, key: str) -> list[str]:
        raw_value = self.selectors.get(key, [])
        if isinstance(raw_value, str):
            values = [raw_value]
        else:
            values = [str(item) for item in raw_value]
        return [value for value in values if value.strip()]

    def _required_locator(self, key: str):
        locator = self._optional_locator(key)
        if locator is None:
            access_blocker = self._access_blocker_message()
            if access_blocker:
                raise RuntimeError(access_blocker)
            raise RuntimeError(f"ChatGPT DOM contract missing `{key}` selector match.")
        return locator

    def _optional_locator(self, key: str):
        for selector in self._selector_values(key):
            locator = self._page.locator(selector)
            count = locator.count()
            if count <= 0:
                continue
            for index in range(count - 1, -1, -1):
                candidate = locator.nth(index)
                try:
                    if candidate.is_visible():
                        return candidate
                except Exception:
                    return candidate
            return locator.last
        return None

    def _latest_delivery_error_text(self) -> str:
        locator = self._optional_locator("delivery_error")
        if locator is not None:
            text = locator.inner_text().strip()
            if text:
                return text
        try:
            payload = self._run_json_script(
                """
                (() => {
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
                """.replace("__RETRY_MARKERS__", json.dumps([marker.casefold() for marker in CHATGPT_RETRY_BUTTON_MARKERS]))
            )
        except Exception:
            return ""
        return str(payload.get("text", "")).strip()

    def _retry_latest_delivery_error(self) -> bool:
        payload = self._run_json_script(
            """
            (() => {
              const buttons = Array.from(document.querySelectorAll('button'));
              const retryButton = buttons.findLast((button) => {
                const label = `${String(button.innerText || '').trim()} ${String(button.getAttribute('aria-label') || '').trim()} ${String(button.getAttribute('data-testid') || '').trim()}`.trim();
                if (!label) return false;
                if (button.disabled) return false;
                return __MARKERS__.some((marker) => label.toLowerCase().includes(marker));
              });
              if (!retryButton) return JSON.stringify({ retried: false });
              retryButton.click();
              return JSON.stringify({ retried: true });
            })()
            """.replace("__MARKERS__", json.dumps([marker.casefold() for marker in CHATGPT_RETRY_BUTTON_MARKERS]))
        )
        return bool(payload.get("retried"))

    def _access_blocker_message(self) -> str:
        title_text = ""
        page_title = getattr(self._page, "title", None)
        if callable(page_title):
            try:
                title_text = str(page_title()).strip()
            except Exception:
                title_text = ""

        body_text = ""
        locator_method = getattr(self._page, "locator", None)
        if callable(locator_method):
            try:
                body_locator = locator_method("body")
                if body_locator.count() > 0:
                    body_text = body_locator.last.inner_text().strip()
            except Exception:
                body_text = ""

        title_lower = title_text.casefold()
        body_lower = body_text.casefold()
        if "just a moment" in title_lower or "just a moment" in body_lower:
            return (
                "ChatGPT is showing an interstitial or challenge page (`Just a moment...`) in the Bridge Browser. "
                "Open the chat in Bridge Browser, wait until the real conversation UI is visible, then start again."
            )
        if "log in" in title_lower or "sign in" in title_lower or "log in" in body_lower or "sign in" in body_lower:
            return (
                "ChatGPT is not fully authenticated in the Bridge Browser profile. "
                "Open the chat in Bridge Browser, log in there, and start again."
            )
        return ""

    def _recent_messages(self, key: str, limit: int) -> list[dict[str, str]]:
        for selector in self._selector_values(key):
            locator = self._page.locator(selector)
            count = locator.count()
            if count == 0:
                continue
            start_index = max(0, count - max(limit, 1))
            messages: list[dict[str, str]] = []
            for index in range(start_index, count):
                item = locator.nth(index)
                text = item.inner_text().strip()
                if not text:
                    continue
                message_id = item.get_attribute("id") or ""
                messages.append(
                    {
                        "text": text,
                        "message_id": message_id,
                        "message_anchor": message_id or _indexed_message_anchor(key, index, text),
                    }
                )
            return messages
        return []

    def _run_json_script(self, js_code: str) -> dict[str, Any]:
        return json.loads(self._page.evaluate(js_code))
