import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from textwrap import dedent
from unittest.mock import patch

from mastermind_bridge.orchestrator.models import OrchestratorSession


class PromptContractTests(unittest.TestCase):
    def test_build_bootstrap_prompt_enforces_deep_human_readable_contract(self):
        from mastermind_bridge.orchestrator.contracts import build_bootstrap_prompt

        session = OrchestratorSession(
            session_id="session-1",
            binding_id="binding-1",
            repo_path="/tmp/repo",
            workspace_path="/tmp/repo",
            chat_url="https://chatgpt.com/c/project/test-chat",
            time_budget_minutes=90,
            budget_remaining_minutes=90,
        )

        prompt = build_bootstrap_prompt(session)
        self.assertTrue(prompt.startswith("Session id: session-1\n"))
        self.assertIn("analyze everything Codex returned deeply", prompt)
        self.assertIn("do not blindly copy Codex's suggested next step", prompt)
        self.assertIn("reread the relevant project docs, source files, plan, and prior decisions for this repo", prompt)
        self.assertIn("do not treat Codex's suggested next step as authoritative", prompt)
        self.assertIn("do not make a known blocked, backoff, cooldown", prompt)
        self.assertIn("treat that suggestion as a hypothesis to challenge", prompt)
        self.assertIn("treat actual data population as first-class product work", prompt)
        self.assertIn("fill canonical stores, inventories, indexes, memories, and relationship graphs", prompt)
        self.assertIn("do not let repeated prompt, runner, readiness, or infrastructure hardening crowd out", prompt)
        self.assertIn("brainstorm plausible next steps", prompt)
        self.assertIn("before ever concluding that there is no next Codex work", prompt)
        self.assertIn("do not assume a quiet or blocked lane means the project is out of work", prompt)
        self.assertIn("choose another safe repo-local implementation, data backfill, inventory, indexing", prompt)
        self.assertIn("memory/brain graph linking", prompt)
        self.assertIn("media/OCR/transcription derivation", prompt)
        self.assertIn("never answer with `No Codex prompt`, `No-op`, a global pause, or an idle status", prompt)
        self.assertIn("populate missing canonical data", prompt)
        self.assertIn("index messages/reminders/notes/tasks/files/media", prompt)
        self.assertIn("link entities and relationships into memory/brain/search surfaces", prompt)
        self.assertIn("derive media text through OCR/transcription", prompt)
        self.assertIn("only declare that no runnable prompt exists as a last-resort emergency", prompt)
        self.assertIn("standing machine and repo rules for this conversation", prompt)
        self.assertIn("every cycle starts as a brand-new thread with no assumed memory", prompt)
        self.assertIn("uses injected GrapeRoot or hook-first context when available", prompt)
        self.assertIn("opens local apps itself when needed", prompt)
        self.assertIn("allows routine Little Snitch or OK dialogs on this Mac", prompt)
        self.assertIn("Keyboard Maestro, cliclick, osascript, screencapture, Codex app-server", prompt)
        self.assertIn("uses Browser Use and Computer Use when those tools are actually exposed", prompt)
        self.assertIn("restate only the specific standing rules that materially matter", prompt)
        self.assertIn("Codex can use the full local machine surface on this Mac", prompt)
        self.assertTrue(prompt.rstrip().endswith("- session_id: session-1"))

    def test_build_codex_execution_prompt_adds_repo_guardrails_and_output_contract(self):
        from mastermind_bridge.orchestrator.contracts import build_codex_execution_prompt

        prompt = build_codex_execution_prompt(
            "Inspect the current loop behavior and improve the next safe slice.",
            ["Keep the same session coherent."],
            repo_path="/tmp/repo",
            workspace_path="/tmp/repo",
            session_id="session-1",
            thread_action="same_thread",
        )

        self.assertIn("Work only in this repo:", prompt)
        self.assertIn("/tmp/repo", prompt)
        self.assertIn("If the prompt text or prior context points to another repo", prompt)
        self.assertIn("Do not assume a fixed doc list from another project.", prompt)
        self.assertIn("Source refresh guidance for this run", prompt)
        self.assertIn("normal Codex startup discipline", prompt)
        self.assertIn("start from the narrowest concrete repo/workdir", prompt)
        self.assertIn("read local repo guidance first", prompt)
        self.assertIn("load token-efficiency as the default lightweight guardrail", prompt)
        self.assertIn("inspect available skill metadata and load the strong-fit skills automatically", prompt)
        self.assertIn("consider using-superpowers", prompt)
        self.assertIn("same way a normal Codex app thread for this repo would start", prompt)
        self.assertIn("standard GrapeRoot or hook-first repo orientation first", prompt)
        self.assertIn("every Codex cycle in this conversation starts as a fresh thread with no assumed memory", prompt)
        self.assertIn("run workspace-graph only if this Codex runtime exposes a callable workspace-graph surface", prompt)
        self.assertIn("open local apps yourself when they help and are not already open", prompt)
        self.assertIn("prefer the real normal Chrome app/profile", prompt)
        self.assertIn("Browser Use Codex plugin", prompt)
        self.assertIn("local or in-app browser inspection", prompt)
        self.assertIn("Computer Use Codex plugin", prompt)
        self.assertIn("real macOS GUI work", prompt)
        self.assertIn("before declaring a live GUI, login, auth, permission, allow-dialog, or app-state blocker", prompt)
        self.assertIn("Little Snitch may block needed traffic on this Mac", prompt)
        self.assertIn("Keyboard Maestro, cliclick, and similar local helper surfaces", prompt)
        self.assertIn("Messages for one-time codes", prompt)
        self.assertIn("Touch ID, hardware security-key taps", prompt)
        self.assertIn("Codex exec capability notes:", prompt)
        self.assertIn("Skill path hints for codex exec on this machine:", prompt)
        self.assertIn("${CODEX_HOME:-$HOME/.codex}/skills/token-efficiency/SKILL.md", prompt)
        self.assertIn(
            "Do not try paths like ${CODEX_HOME:-$HOME/.codex}/skills/r0/<skill>/SKILL.md",
            prompt,
        )
        self.assertNotIn(str(Path.home()), prompt)
        self.assertIn("Do not pipe data into `python3 - <<'PY'`", prompt)
        self.assertIn("workspace-graph is unavailable, cancelled, or noisy, continue immediately", prompt)
        self.assertIn("treat the local Codex installation and current user environment as the full available operator surface", prompt)
        self.assertIn("if the user explicitly authorizes secret-backed or live operations", prompt)
        self.assertIn("use provided secrets plus existing local secure material", prompt)
        self.assertIn("if a missing dependency, SDK, toolchain, browser, app, helper, CLI, runtime, or host permission is blocking real progress", prompt)
        self.assertIn("install or enable it instead of stopping at the missing prerequisite", prompt)
        self.assertIn("env vars, Keychain, secure local credential stores, logged-in browser/app sessions", prompt)
        self.assertIn("already-open or already-authenticated browser/app session", prompt)
        self.assertIn("never write raw secrets into the repo, logs, or final answer", prompt)
        self.assertIn("actively discover and use relevant local helpers", prompt)
        self.assertIn("browser automation, screenshots, screen or app inspection, MCP servers/connectors, plugins, installed apps, and local CLIs", prompt)
        self.assertIn("browser cookies or session state, accessibility or app-automation paths", prompt)
        self.assertIn("CLI or terminal run is not text-only by default", prompt)
        self.assertIn("Apple Events or osascript app control, browser automation, screencapture, Codex app-server", prompt)
        self.assertIn("desktop app UI or TUI route is unavailable from this shell", prompt)
        self.assertIn("macOS Automation, Accessibility, Screen Recording, Full Disk Access, browser-control, or helper permission", prompt)
        self.assertIn("retry the live step after it is granted instead of quietly downgrading", prompt)
        self.assertIn("do not act artificially cautious about using already-available authenticated local state", prompt)
        self.assertIn("inspect the actual visible screen and app state early", prompt)
        self.assertIn("do not claim browser or UI progress without looking at what is actually on screen", prompt)
        self.assertIn("do not assume ChatGPT already enumerated every available capability", prompt)
        self.assertIn("authoritative source files for this specific repo", prompt)
        self.assertIn("fresh thread or a new topic", prompt)
        self.assertIn("treat the full ChatGPT message below as the authoritative task input", prompt)
        self.assertIn("do not get stuck on transport wording, wrapper text, or formatting oddities", prompt)
        self.assertIn("if the ChatGPT message contains both analysis and an explicit quoted prompt", prompt)
        self.assertIn("Response contract for this run", prompt)
        self.assertIn("what you inspected", prompt)
        self.assertIn("what you changed", prompt)
        self.assertIn("take the largest safe forward step toward the end goal", prompt)
        self.assertIn("substantial bundled work package", prompt)
        self.assertIn("specific prohibitions, exact stop conditions, and recovery-only boundaries", prompt)
        self.assertIn("be thorough, deep, and strong rather than minimal", prompt)
        self.assertIn("complete them in one run instead of stopping after the first one", prompt)
        self.assertIn("do not stop after the first local confirmation", prompt)
        self.assertIn("prefer long, deep runs with real progress", prompt)
        self.assertIn("do not spend the whole run on verification, inspection, blocker classification, or doc checking", prompt)
        self.assertIn("do not convert it into docs cleanup or adjacent implementation just to create progress", prompt)
        self.assertIn("Blocked-lane anti-churn override:", prompt)
        self.assertIn("run at most that check; if the lane is still blocked", prompt)
        self.assertIn("prefer expanding already-authorized local coverage and transfer paths", prompt)
        self.assertIn("data-population work is product work", prompt)
        self.assertIn("messages, reminders, notes, tasks, files, media metadata, transcripts", prompt)
        self.assertIn("canonical stores, inventories, indexes, memory, and relationship graphs", prompt)
        self.assertIn("do not spend repeated cycles only improving runners, prompts, policy gates", prompt)
        self.assertIn("source to inventory/index/memory/brain surfaces", prompt)
        self.assertIn("OCR, audio/video transcription", prompt)
        self.assertIn("do not avoid OCR, transcription, media derivation", prompt)
        self.assertIn("may run for hours", prompt)
        self.assertIn("overrides stale ChatGPT wording that says to keep working on the same blocked lane", prompt)
        self.assertIn("treat verification as a rung in the ladder", prompt)
        self.assertIn("if an early check passes, immediately continue to the next operational step", prompt)
        self.assertIn("if an early check fails but a repo-local workaround", prompt)
        self.assertIn("do not default to repo-local credential hunting", prompt)
        self.assertIn("use the supplied material or secure local stores first", prompt)
        self.assertIn("prefer end-to-end bundled slices", prompt)
        self.assertIn("stop only when you have actually reached the real safe frontier", prompt)
        self.assertIn("do not pad the work with unnecessary rereads", prompt)
        self.assertIn("keep durable doc refreshes and updates at the end of the run", prompt)
        self.assertIn("do not open the run with doc edits, doc audits, or doc refreshes", prompt)
        self.assertIn("incidental runtime side-effect files do not count as substantive progress", prompt)
        self.assertIn("if that is all you touched, continue to a real repo, runtime, or product frontier", prompt)
        self.assertIn("return the full answer and all important real steps you took", prompt)
        self.assertIn("push it as far as safely possible before stopping", prompt)
        self.assertIn("update the relevant docs before finishing only when durable project truth materially changed", prompt)
        self.assertIn("how the work and next step relate to the broader project plan", prompt)
        self.assertIn("Additional orchestrator instructions:", prompt)
        self.assertIn("Full ChatGPT message:", prompt)
        self.assertIn("Keep the same session coherent.", prompt)
        self.assertIn("Inspect the current loop behavior", prompt)

    def test_build_codex_execution_prompt_preserves_full_multiline_chatgpt_prompt(self):
        from mastermind_bridge.orchestrator.contracts import build_codex_execution_prompt

        full_prompt = "Header line\n" + ("A" * 6000) + "\nFooter line"
        prompt = build_codex_execution_prompt(
            full_prompt,
            [],
            repo_path="/tmp/repo",
            workspace_path="/tmp/repo",
            session_id="session-1",
            thread_action="new_thread",
        )

        self.assertIn(full_prompt, prompt)
        self.assertNotIn("[... truncated for chat stability ...]", prompt)

    def test_ensure_prompt_repo_scope_rejects_wrong_repo_references(self):
        from mastermind_bridge.orchestrator.contracts import ensure_prompt_repo_scope

        mismatch = ensure_prompt_repo_scope(
            "Work in ../personal-assistant-bridge and continue there.",
            repo_path="/tmp/test-home/chatgpt-codex-bridge",
            workspace_path="/tmp/test-home/chatgpt-codex-bridge",
        )
        allowed = ensure_prompt_repo_scope(
            "Stay in /tmp/test-home/chatgpt-codex-bridge and inspect the loop.",
            repo_path="/tmp/test-home/chatgpt-codex-bridge",
            workspace_path="/tmp/test-home/chatgpt-codex-bridge",
        )

        self.assertIn("references a different repo", mismatch)
        self.assertIn("personal-assistant-bridge", mismatch)
        self.assertEqual(allowed, "")

    def test_ensure_prompt_repo_scope_allows_negative_wrong_repo_mentions(self):
        from mastermind_bridge.orchestrator.contracts import ensure_prompt_repo_scope

        prompt = """
Stay in this repo path only:
/tmp/test-home/personal-assistant-bridge

Important correction:
- Ignore any prior bridge instruction that referenced /tmp/test-home/chatgpt-codex-bridge
- The only valid repo for this thread is /tmp/test-home/personal-assistant-bridge
"""

        allowed = ensure_prompt_repo_scope(
            prompt,
            repo_path="/tmp/test-home/personal-assistant-bridge",
            workspace_path="/tmp/test-home/personal-assistant-bridge",
        )

        self.assertEqual(allowed, "")

    def test_ensure_prompt_repo_scope_ignores_non_repo_path_fragments(self):
        from mastermind_bridge.orchestrator.contracts import ensure_prompt_repo_scope

        prompt = """
Stay in this repo path only:
/tmp/test-home/personal-assistant-bridge

Refresh only the relevant lanes:
- auth/runtime
- stable file/folder identity
- shared canonical/state/store/sync substrate
- brain/wiki compiler
"""

        allowed = ensure_prompt_repo_scope(
            prompt,
            repo_path="/tmp/test-home/personal-assistant-bridge",
            workspace_path="/tmp/test-home/personal-assistant-bridge",
        )

        self.assertEqual(allowed, "")

    def test_ensure_prompt_repo_scope_ignores_embedded_slash_fragments_inside_sentences(self):
        from mastermind_bridge.orchestrator.contracts import ensure_prompt_repo_scope

        prompt = """
Stay in the currently bound repo only: /tmp/test-home/personal-assistant-bridge.

Questions to answer explicitly:
- Does any current evidence still point to a repo-side code defect in the Google Drive auth/session/runtime path?
- Is the active blocker more likely to be repo configuration / test harness expectations versus external local toolchain environment?
"""

        allowed = ensure_prompt_repo_scope(
            prompt,
            repo_path="/tmp/test-home/personal-assistant-bridge",
            workspace_path="/tmp/test-home/personal-assistant-bridge",
        )

        self.assertEqual(allowed, "")

    def test_ensure_prompt_repo_scope_ignores_session_runtime_fragment_in_positive_repo_context(self):
        from mastermind_bridge.orchestrator.contracts import ensure_prompt_repo_scope

        prompt = """
Stay in the currently bound repo only: /tmp/test-home/personal-assistant-bridge.

Your job is to compare the latest Codex result against the bigger picture and determine the smallest correct next move for the auth/session/runtime path.
"""

        allowed = ensure_prompt_repo_scope(
            prompt,
            repo_path="/tmp/test-home/personal-assistant-bridge",
            workspace_path="/tmp/test-home/personal-assistant-bridge",
        )

        self.assertEqual(allowed, "")

    def test_ensure_prompt_repo_scope_ignores_repo_like_subdirectory_names(self):
        from mastermind_bridge.orchestrator.contracts import ensure_prompt_repo_scope

        prompt = """
Stay in this repo path only:
/tmp/test-home/personal-assistant-bridge

If the repo already has a direct bridge/import/inventory path that accepts a fixture:
- stage it only in a gitignored repo-local path, e.g. under `.local/tmp/gdrive-bridge/`
- continue in /tmp/test-home/personal-assistant-bridge for all real work
"""

        allowed = ensure_prompt_repo_scope(
            prompt,
            repo_path="/tmp/test-home/personal-assistant-bridge",
            workspace_path="/tmp/test-home/personal-assistant-bridge",
        )

        self.assertEqual(allowed, "")

    def test_ensure_prompt_repo_scope_ignores_operator_bridge_phrases_in_repo_progress_text(self):
        from mastermind_bridge.orchestrator.contracts import ensure_prompt_repo_scope

        prompt = """
Stay in this repo path only:
/tmp/test-home/personal-assistant-bridge

A valid success is live Google Drive data flowing into repo-native state and brain output via an operator-bridge path that stays consistent with the project plan.
"""

        allowed = ensure_prompt_repo_scope(
            prompt,
            repo_path="/tmp/test-home/personal-assistant-bridge",
            workspace_path="/tmp/test-home/personal-assistant-bridge",
        )

        self.assertEqual(allowed, "")

    def test_loop_runner_executor_allows_cross_repo_looking_prompt_without_repo_scope_guard(self):
        from mastermind_bridge.cli import _build_loop_runner
        from mastermind_bridge.models import RunReport
        from mastermind_bridge.orchestrator.models import ChatBinding

        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            policy_path = root / "ORCHESTRATOR_POLICY.json"
            policy_path.write_text('{"version": 1}\n', encoding="utf-8")
            runner = _build_loop_runner(
                bindings_path=root / "CHAT_BINDINGS.json",
                policy_path=policy_path,
                sessions_dir=root / "sessions",
                artifacts_root=root / "artifacts",
                registry_path=None,
                log_file=None,
                codex_bin="codex",
                model=None,
                sandbox=None,
                profile=None,
                headless=True,
            )
            session = OrchestratorSession(
                session_id="session-1",
                binding_id="binding-1",
                repo_path="/tmp/test-home/chatgpt-codex-bridge",
                workspace_path="/tmp/test-home/chatgpt-codex-bridge",
                chat_url="https://chatgpt.com/c/project/test-chat",
                current_codex_thread_id="thread-123",
                time_budget_minutes=90,
                budget_remaining_minutes=90,
            )
            binding = ChatBinding(
                binding_id="binding-1",
                project_name="bridge",
                repo_path="/tmp/test-home/chatgpt-codex-bridge",
                workspace_path="/tmp/test-home/chatgpt-codex-bridge",
                chat_url="https://chatgpt.com/c/project/test-chat",
            )

            with patch.dict(os.environ, {"BRIDGE_ENABLE_CODEX_APP_INTEGRATION": "1"}, clear=False), patch(
                "mastermind_bridge.cli.execute_codex_prompt",
                return_value=(
                    RunReport.from_dict(
                        {
                            "timestamp": "2026-04-19T16:00:00+02:00",
                            "thread_id": "session-1",
                            "summary": "Completed.",
                            "files_touched": [],
                            "checks": [],
                            "blockers": [],
                            "risks": [],
                            "next_step": "",
                            "observed_codex_thread_id": "thread-123",
                        }
                    ),
                    {"exit_code": 0},
                ),
            ) as execute_codex_prompt:
                report = runner.executor(
                    prompt="Continue in ../personal-assistant-bridge.",
                    thread_action="same_thread",
                    session=session,
                    binding=binding,
                    instructions=[],
                )

            self.assertEqual(report.summary, "Completed.")
            self.assertEqual(execute_codex_prompt.call_args.kwargs["resume_session_id"], "thread-123")
            self.assertTrue(execute_codex_prompt.call_args.kwargs["compact_after_success"])


if __name__ == "__main__":
    unittest.main()
