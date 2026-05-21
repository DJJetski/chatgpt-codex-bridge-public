import tempfile
import unittest
from pathlib import Path

from mastermind_bridge.models import RunReport
from mastermind_bridge.orchestrator.control import (
    BridgeControlParseError,
    extract_bridge_control_envelope,
    infer_bridge_control_envelope,
    render_bridge_control_block,
)
from mastermind_bridge.orchestrator.packets import build_return_packet, render_return_packet
from mastermind_bridge.orchestrator.policy import (
    apply_instruction_updates,
    consume_next_run_instructions,
    resolve_instruction_texts,
)


class BridgeControlTests(unittest.TestCase):
    def test_extract_bridge_control_envelope_from_fenced_block(self):
        assistant_text = "\n".join(
            [
                "Proceed with the next Codex run.",
                "",
                "```bridge-control",
                "{",
                '  "protocol_version": "1",',
                '  "session_id": "session-1",',
                '  "decision": "run_codex",',
                '  "codex_thread_action": "same_thread",',
                '  "prompt": "Continue the implementation.",',
                '  "task_label": "loop-runner",',
                '  "instruction_updates": [',
                '    {"scope": "next_run", "mode": "append", "text": "Capture visible trace excerpts."}',
                "  ],",
                '  "time_budget_remaining_hint": "45m",',
                '  "notes_for_audit": ["Continue the same session."]',
                "}",
                "```",
            ]
        )

        envelope = extract_bridge_control_envelope(assistant_text)

        self.assertEqual(envelope.session_id, "session-1")
        self.assertEqual(envelope.codex_thread_action, "same_thread")
        self.assertEqual(envelope.instruction_updates[0].scope, "next_run")
        self.assertEqual(envelope.notes_for_audit, ["Continue the same session."])

    def test_extract_bridge_control_envelope_from_rendered_code_block_text(self):
        assistant_text = "\n".join(
            [
                "Proceed with the next Codex run.",
                "",
                "bridge-control",
                "{",
                '  "protocol_version": "1",',
                '  "session_id": "session-1",',
                '  "decision": "run_codex",',
                '  "codex_thread_action": "same_thread",',
                '  "prompt": "Continue the implementation.",',
                '  "task_label": "loop-runner"',
                "}",
            ]
        )

        envelope = extract_bridge_control_envelope(assistant_text)

        self.assertEqual(envelope.session_id, "session-1")
        self.assertEqual(envelope.decision, "run_codex")

    def test_extract_bridge_control_envelope_from_fenced_json_block(self):
        assistant_text = "\n".join(
            [
                "```json",
                "{",
                '  "protocol_version": "1",',
                '  "session_id": "session-raw-json",',
                '  "decision": "run_codex",',
                '  "codex_thread_action": "same_thread",',
                '  "prompt": "Continue the implementation.",',
                '  "task_label": "loop-runner"',
                "}",
                "```",
            ]
        )

        envelope = extract_bridge_control_envelope(assistant_text)

        self.assertEqual(envelope.session_id, "session-raw-json")
        self.assertEqual(envelope.codex_thread_action, "same_thread")

    def test_extract_bridge_control_envelope_from_raw_json_object(self):
        assistant_text = "\n".join(
            [
                "{",
                '  "protocol_version": "1",',
                '  "session_id": "session-bare-json",',
                '  "decision": "pause",',
                '  "codex_thread_action": "same_thread",',
                '  "prompt": "Wait for the next human input.",',
                '  "task_label": "pause-request"',
                "}",
            ]
        )

        envelope = extract_bridge_control_envelope(assistant_text)

        self.assertEqual(envelope.session_id, "session-bare-json")
        self.assertEqual(envelope.decision, "pause")

    def test_extract_bridge_control_envelope_from_rendered_json_label_and_object(self):
        assistant_text = "\n".join(
            [
                "JSON",
                "{",
                '  "protocol_version": "1.0",',
                '  "session_id": "session-rendered-json",',
                '  "decision": "run_codex",',
                '  "codex_thread_action": "new_thread",',
                '  "prompt": "Continue with a fresh Codex thread.",',
                '  "task_label": "continue_cycle"',
                "}",
            ]
        )

        envelope = extract_bridge_control_envelope(assistant_text)

        self.assertEqual(envelope.session_id, "session-rendered-json")
        self.assertEqual(envelope.codex_thread_action, "new_thread")
        self.assertEqual(envelope.task_label, "continue_cycle")

    def test_extract_bridge_control_envelope_prefers_last_control_block_when_multiple_are_present(self):
        assistant_text = "\n".join(
            [
                "Initial draft.",
                "",
                "```bridge-control",
                "{",
                '  "protocol_version": "1.0",',
                '  "session_id": "session-first",',
                '  "decision": "pause",',
                '  "codex_thread_action": "same_thread",',
                '  "prompt": "Stop here.",',
                '  "task_label": "draft_block"',
                "}",
                "```",
                "",
                "Final answer.",
                "",
                "bridge-control",
                'protocol_version: "1.0"',
                'session_id: "session-final"',
                'decision: "run_codex"',
                'codex_thread_action: "new_thread"',
                'task_label: "final_block"',
                "prompt: |",
                "  Continue with the stronger final prompt only.",
            ]
        )

        envelope = extract_bridge_control_envelope(assistant_text)

        self.assertEqual(envelope.session_id, "session-final")
        self.assertEqual(envelope.decision, "run_codex")
        self.assertEqual(envelope.task_label, "final_block")

    def test_extract_bridge_control_envelope_from_human_readable_key_value_block(self):
        assistant_text = "\n".join(
            [
                "Keep the same Codex thread.",
                "",
                "bridge-control",
                'protocol_version: "1.0"',
                'session_id: "session-22bf7e07"',
                'decision: "run_codex"',
                'codex_thread_action: "continue_same_thread"',
                'task_label: "gdrive_v1_on_shared_substrate"',
                "prompt: |",
                "  Stay in this repo path only.",
                "  Continue in the same Codex thread.",
            ]
        )

        envelope = extract_bridge_control_envelope(assistant_text)

        self.assertEqual(envelope.session_id, "session-22bf7e07")
        self.assertEqual(envelope.decision, "run_codex")
        self.assertEqual(envelope.codex_thread_action, "same_thread")
        self.assertEqual(
            envelope.prompt,
            "Stay in this repo path only.\nContinue in the same Codex thread.",
        )

    def test_extract_bridge_control_envelope_from_human_readable_block_with_unindented_prompt_body(self):
        assistant_text = "\n".join(
            [
                "bridge-control",
                'protocol_version: "1.0"',
                'session_id: "session-93428acd"',
                'decision: "run_codex"',
                'codex_thread_action: "same_thread"',
                'task_label: "recover_and_activate_tracked_roots_with_codex_probe"',
                "prompt: |",
                "Continue on the same Codex thread.",
                "",
                "Carried-forward truths:",
                "* This repo is a SwiftPM monorepo.",
            ]
        )

        envelope = extract_bridge_control_envelope(assistant_text)

        self.assertEqual(envelope.task_label, "recover_and_activate_tracked_roots_with_codex_probe")
        self.assertEqual(
            envelope.prompt,
            "Continue on the same Codex thread.\n\nCarried-forward truths:\n* This repo is a SwiftPM monorepo.",
        )

    def test_extract_bridge_control_envelope_from_human_readable_block_with_nbsp_indent(self):
        nbsp = "\u00a0"
        assistant_text = "\n".join(
            [
                "Fixed.",
                "",
                "bridge-control",
                'protocol_version: "1.0"',
                'session_id: "session-22bf7e07"',
                'decision: "run_codex"',
                'codex_thread_action: "same_thread"',
                'task_label: "gdrive_auth_runtime_hardening"',
                "prompt: |",
                f"{nbsp}{nbsp}Stay in this repo path only: /tmp/example-home/Codex/personal-assistant-bridge",
                f"{nbsp}{nbsp}Continue in the same Codex thread for this live bridge test.",
            ]
        )

        envelope = extract_bridge_control_envelope(assistant_text)

        self.assertEqual(envelope.task_label, "gdrive_auth_runtime_hardening")
        self.assertEqual(
            envelope.prompt,
            "Stay in this repo path only: /tmp/example-home/Codex/personal-assistant-bridge\n"
            "Continue in the same Codex thread for this live bridge test.",
        )

    def test_extract_bridge_control_rejects_missing_required_fields(self):
        assistant_text = "\n".join(
            [
                "```bridge-control",
                '{"session_id":"session-1","decision":"run_codex"}',
                "```",
            ]
        )

        with self.assertRaises(BridgeControlParseError):
            extract_bridge_control_envelope(assistant_text)

    def test_extract_bridge_control_reports_missing_payload_after_header(self):
        with self.assertRaisesRegex(BridgeControlParseError, "Missing bridge-control payload after header."):
            extract_bridge_control_envelope("bridge-control")

    def test_extract_bridge_control_rejects_empty_prompt_body(self):
        assistant_text = "\n".join(
            [
                "bridge-control",
                'protocol_version: "1.0"',
                'session_id: "session-empty-prompt"',
                'decision: "run_codex"',
                'codex_thread_action: "same_thread"',
                'task_label: "empty_prompt"',
                "prompt: |",
                "",
            ]
        )

        with self.assertRaisesRegex(BridgeControlParseError, "bridge-control prompt must not be empty."):
            extract_bridge_control_envelope(assistant_text)

    def test_infer_bridge_control_envelope_prefers_large_fenced_prompt_block(self):
        assistant_text = "\n".join(
            [
                "Die Audit-Antwort ist stark genug. Jetzt wuerde ich Codex nicht noch einen weiteren Rundum-Report schreiben lassen.",
                "",
                "Schick Codex jetzt genau das:",
                "",
                "```text",
                "Dein Audit reicht. Jetzt wechselst du von Inventur in Ausfuehrung.",
                "",
                "AUFGABE 1 - TRACKED ROOTS END-TO-END REAL MACHEN",
                "- Finde den existierenden Registrierungs-/Sync-Pfad fuer `tracked_roots`.",
                "",
                "AUFGABE 2 - EVIDENZBASIERTE CODEX-KONVERSATIONS-SUCHE",
                "- Suche nach realen Codex-Konversationsartefakten.",
                "```",
                "",
                "Wenn Codex darauf antwortet, schick mir das Ergebnis rein.",
            ]
        )

        envelope = infer_bridge_control_envelope(
            assistant_text,
            session_id="session-productive",
            default_thread_action="same_thread",
            parse_error="Missing bridge-control block.",
        )

        self.assertIsNotNone(envelope)
        assert envelope is not None
        self.assertEqual(envelope.codex_thread_action, "same_thread")
        self.assertEqual(envelope.prompt, assistant_text)
        self.assertIn("Schick Codex jetzt genau das:", envelope.prompt)
        self.assertIn("Dein Audit reicht. Jetzt wechselst du von Inventur", envelope.prompt)
        self.assertIn("Wenn Codex darauf antwortet", envelope.prompt)

    def test_infer_bridge_control_envelope_respects_explicit_thread_action_hint_in_freeform_reply(self):
        assistant_text = "\n".join(
            [
                "The last Codex run fixed the right layers first.",
                "",
                "bridge-control",
                "thread_action: new_thread",
                "prompt: |",
                "  Continue work in /tmp/example-home/Codex/personal-assistant-bridge only.",
                "  Start a fresh Codex thread for the next filesystem frontier run.",
            ]
        )

        envelope = infer_bridge_control_envelope(
            assistant_text,
            session_id="session-explicit-thread-hint",
            default_thread_action="same_thread",
            parse_error="Missing required bridge-control fields: codex_thread_action, decision, protocol_version, session_id, task_label",
        )

        self.assertIsNotNone(envelope)
        assert envelope is not None
        self.assertEqual(envelope.codex_thread_action, "new_thread")
        self.assertEqual(envelope.prompt, assistant_text)

    def test_render_bridge_control_block_round_trips(self):
        assistant_text = "\n".join(
            [
                "Planning complete.",
                "",
                "```bridge-control",
                '{"protocol_version":"1","session_id":"session-2","decision":"pause","codex_thread_action":"same_thread","prompt":"Wait for the next human input.","task_label":"pause-request"}',
                "```",
            ]
        )

        envelope = extract_bridge_control_envelope(assistant_text)
        rendered = render_bridge_control_block(envelope)

        self.assertIn("```bridge-control", rendered)
        self.assertIn('"decision": "pause"', rendered)
        self.assertEqual(extract_bridge_control_envelope(rendered).decision, "pause")


class ReturnPacketTests(unittest.TestCase):
    def test_build_return_packet_includes_trace_delivery_and_packet_id(self):
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T13:00:00+02:00",
                "thread_id": "thread-2",
                "summary": "Loop runner executed successfully.",
                "files_touched": ["README.md", "mastermind_bridge/orchestrator/loop.py"],
                "checks": ["python3 -m unittest discover -s tests"],
                "blockers": [],
                "risks": ["Browser adapter still depends on a local ChatGPT login session."],
                "next_step": "Wait for the next ChatGPT turn.",
                "workspace_path": "/tmp/repo",
                "thread_action": "same_thread",
                "observed_codex_thread_id": "codex-thread-123",
                "visible_assistant_trace": [
                    "Inspected the existing orchestration state.",
                    "Implemented the loop runner transitions.",
                    "Verified the changed control flow.",
                ],
                "commands_observed": [
                    {
                        "command": "/bin/zsh -lc 'python3 -m unittest discover -s tests'",
                        "status": "completed",
                        "exit_code": 0,
                        "aggregated_output": "..........................................",
                    }
                ],
                "delivery_status": "delivered",
                "delivery_attempt_count": 1,
                "session_id": "session-1",
                "binding_id": "binding-1",
                "usage": {"input_tokens": 120000, "cached_input_tokens": 40000, "output_tokens": 1500},
                "context_window_tokens": 200000,
                "context_used_tokens": 121500,
                "estimated_context_remaining_percent": 39,
                "context_signal_source": "default",
                "context_continuity_percent": 62,
                "continuity_band": "medium",
                "budget_snapshot": {
                    "time_budget_minutes": 90,
                    "budget_remaining_minutes": 74,
                },
                "policy_outcome": "allow",
                "return_packet_id": "packet-123",
            }
        )

        packet = build_return_packet(report)
        rendered = render_return_packet(packet)

        self.assertEqual(packet.return_packet_id, "packet-123")
        self.assertTrue(rendered.startswith("Session id: session-1\nreturn_packet_id: packet-123\n"))
        self.assertIn("return_packet_id: packet-123", rendered)
        self.assertIn("refresh your understanding of the current project sources and plan", rendered)
        self.assertIn("analyze everything Codex returned deeply", rendered)
        self.assertIn("before ever concluding that there is no next Codex work", rendered)
        self.assertIn("never answer with `No Codex prompt`, `No-op`, a global pause", rendered)
        self.assertIn("populate missing canonical data", rendered)
        self.assertIn("index messages/reminders/notes/tasks/files/media", rendered)
        self.assertIn("link entities and relationships into memory/brain/search surfaces", rendered)
        self.assertIn("write your whole actionable reply as the next plain-language prompt for Codex", rendered)
        self.assertIn("do not emit bridge-control, JSON, YAML, or any transport wrapper", rendered)
        self.assertIn("allows routine Little Snitch or OK dialogs on this Mac", rendered)
        self.assertIn("real browser sessions, Apple Events, screenshots, screen or app inspection", rendered)
        self.assertIn("Terminal, Accessibility, Keyboard Maestro, cliclick", rendered)
        self.assertIn("all important steps it took, including real commands, checks, decisions, blockers, and risks", rendered)
        self.assertIn("default to operational progress on the active user goal", rendered)
        self.assertIn("optimize for depth, specificity, and execution quality rather than brevity", rendered)
        self.assertIn("Here is what Codex wrote:", rendered)
        self.assertIn("Loop runner executed successfully.", rendered)
        self.assertIn("Visible Codex trace:", rendered)
        self.assertIn("Inspected the existing orchestration state.", rendered)
        self.assertIn("Verified the changed control flow.", rendered)
        self.assertIn("/bin/zsh -lc 'python3 -m unittest discover -s tests' | status: completed | exit: 0", rendered)
        self.assertIn("Files touched:", rendered)
        self.assertIn("Checks:", rendered)
        self.assertIn("Risks:", rendered)
        self.assertIn("Recommended next step: Wait for the next ChatGPT turn.", rendered)

    def test_render_return_packet_includes_final_output_before_compact_session_log_trace(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            prompt_path = Path(tmp_dir) / "prompt.md"
            prompt_path.write_text(
                "Supervisor says: continue the same session and implement the next slice.\n",
                encoding="utf-8",
            )
            session_log = Path(tmp_dir) / "session.log"
            session_log.write_text(
                "\n".join(
                    [
                        "=== run started 2026-04-19T05:19:23+02:00 ===",
                        "session_id=session-1",
                        'STDOUT | {"type":"thread.started","thread_id":"thread-codex-1"}',
                        "STDERR | 2026-04-19T01:02:49.508047Z  WARN codex_core_plugins::manifest: ignoring interface.defaultPrompt: maximum of 3 prompts is supported",
                        'STDOUT | {"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"Inspecting the live ChatGPT surfaces before changing code."}}',
                        'STDOUT | {"type":"item.started","item":{"id":"item_1","type":"command_execution","command":"python3 - <<\'PY\' import sqlite3; print(\'inspect real schema before editing\') PY","aggregated_output":"","exit_code":null,"status":"in_progress"}}',
                        'STDOUT | {"type":"item.completed","item":{"id":"item_1","type":"command_execution","command":"python3 - <<\'PY\' import sqlite3; print(\'inspect real schema before editing\') PY","aggregated_output":"line-01\\nline-02\\nline-03\\n","exit_code":0,"status":"completed"}}',
                        'STDOUT | {"type":"item.completed","item":{"id":"item_2","type":"file_change","status":"completed","changes":[{"kind":"update","path":"mastermind_bridge/orchestrator/packets.py"},{"kind":"update","path":"mastermind_bridge/live_monitor.py"},{"kind":"update","path":"tests/test_control.py"}]}}',
                        'STDOUT | {"type":"item.completed","item":{"id":"item_3","type":"collab_tool_call","tool":"close_agent","status":"completed","agents_states":{"agent-1":{"status":"completed","message":"Reviewed files only (read-only), plus related tests.\\n\\nExtra noisy detail."}}}}',
                        "STDERR | 2026-04-19T01:02:50.000000Z ERROR codex_core::tools::router: error=agent with id stale-agent not found",
                        "STDERR | 2026-04-19T01:02:51.000000Z  WARN codex_mcp::rmcp_client: failed to initialize MCP client during shutdown: MCP startup failed",
                        "STDERR | 2026-04-19T01:02:52.000000Z  WARN codex_rmcp_client::stdio_server_launcher: Failed to kill MCP process group 123: No such process (os error 3)",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            report = RunReport.from_dict(
                {
                    "timestamp": "2026-04-15T13:00:00+02:00",
                    "thread_id": "thread-2",
                    "summary": "Legacy packet summary.",
                    "final_agent_message": "Complete final Codex output that must be rendered.",
                    "visible_assistant_trace": ["legacy trace item"],
                    "files_touched": ["legacy.py"],
                    "checks": ["legacy-check"],
                    "blockers": ["legacy-blocker"],
                    "risks": ["legacy-risk"],
                    "next_step": "legacy-next-step",
                    "prompt_path": str(prompt_path),
                    "session_live_log_path": str(session_log),
                    "session_id": "session-1",
                    "binding_id": "binding-1",
                    "return_packet_id": "packet-expanded",
                }
            )

            packet = build_return_packet(report)
            rendered = render_return_packet(packet)

        self.assertIn("Here is what Codex wrote:", rendered)
        self.assertIn("Supervisor prompt sent to Codex:", rendered)
        self.assertIn("Supervisor says: continue the same session and implement the next slice.", rendered)
        self.assertLess(
            rendered.index("Supervisor prompt sent to Codex:"),
            rendered.index("Here is what Codex wrote:"),
        )
        self.assertIn("Complete final Codex output that must be rendered.", rendered)
        self.assertLess(
            rendered.index("Complete final Codex output that must be rendered."),
            rendered.index("Execution trace excerpt for ChatGPT continuity"),
        )
        self.assertIn(
            "Execution trace excerpt for ChatGPT continuity (use this to understand the real steps, commands, checks, and decisions):",
            rendered,
        )
        self.assertIn("Run started: 2026-04-19T05:19:23+02:00", rendered)
        self.assertIn("Thread started: thread-codex-1", rendered)
        self.assertNotIn("ignoring interface.defaultPrompt", rendered)
        self.assertIn("Agent update [item_0]", rendered)
        self.assertNotIn("Started command [item_1]", rendered)
        self.assertIn("Ran command [item_1]", rendered)
        self.assertIn("inspect real schema before editing", rendered)
        self.assertIn("  Result:", rendered)
        self.assertIn("    line-02", rendered)
        self.assertIn("Edited 3 files [item_2]", rendered)
        self.assertIn("  update: mastermind_bridge/orchestrator/packets.py", rendered)
        self.assertIn("  update: mastermind_bridge/live_monitor.py", rendered)
        self.assertIn("  … 1 more changes", rendered)
        self.assertIn("Subagent closed [item_3] (completed)", rendered)
        self.assertIn("agent-1: completed - Reviewed files only (read-only), plus related tests.", rendered)
        self.assertNotIn('"agents_states"', rendered)
        self.assertNotIn('"message"', rendered)
        self.assertNotIn("stale-agent not found", rendered)
        self.assertNotIn("failed to initialize MCP client during shutdown", rendered)
        self.assertNotIn("Failed to kill MCP process group", rendered)
        self.assertIn("Files touched:", rendered)
        self.assertIn("- legacy.py", rendered)
        self.assertIn("Checks:", rendered)
        self.assertIn("- legacy-check", rendered)
        self.assertIn("Blockers:", rendered)
        self.assertIn("- legacy-blocker", rendered)
        self.assertIn("Risks:", rendered)
        self.assertIn("- legacy-risk", rendered)
        self.assertIn("Recommended next step: legacy-next-step", rendered)
        self.assertNotIn("Visible Codex trace:", rendered)
        self.assertNotIn("Technical loop context:", rendered)
        self.assertNotIn("Usage:", rendered)
        self.assertNotIn("Artifacts:", rendered)

    def test_render_return_packet_prefers_run_local_live_log_over_cumulative_session_log(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            run_dir = root / "run"
            run_dir.mkdir()
            run_log = run_dir / "live_output.log"
            run_log.write_text(
                "\n".join(
                    [
                        "=== run started 2026-04-24T15:06:24+02:00 ===",
                        "session_id=session-1",
                        'STDOUT | {"type":"item.completed","item":{"id":"fresh","type":"agent_message","text":"Fresh execution marker."}}',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            session_log = root / "session.log"
            session_log.write_text(
                "\n".join(
                    [
                        "=== run started 2026-04-24T14:49:57+02:00 ===",
                        "session_id=session-1",
                        'STDOUT | {"type":"item.completed","item":{"id":"old","type":"agent_message","text":"Old run history."}}',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            report = RunReport.from_dict(
                {
                    "timestamp": "2026-04-24T15:06:30+02:00",
                    "thread_id": "session-1",
                    "summary": "Fresh execution marker.",
                    "files_touched": [],
                    "checks": [],
                    "blockers": [],
                    "risks": [],
                    "next_step": "Wait for ChatGPT.",
                    "artifacts_dir": str(run_dir),
                    "session_live_log_path": str(session_log),
                    "session_id": "session-1",
                    "binding_id": "binding-1",
                }
            )

            packet = build_return_packet(report)
            rendered = render_return_packet(packet)

        self.assertIn("Fresh execution marker.", rendered)
        self.assertNotIn("Old run history.", rendered)
        self.assertNotIn("2026-04-24T14:49:57", rendered)

    def test_render_return_packet_falls_back_to_size_capped_summary_for_huge_payload(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            report = RunReport.from_dict(
                {
                    "timestamp": "2026-04-15T13:00:00+02:00",
                    "thread_id": "thread-2",
                    "summary": "Large session log should be condensed automatically.",
                    "final_agent_message": "Summary line.\n" + ("A" * 250000),
                    "files_touched": ["a.py", "b.py"],
                    "checks": ["pytest"],
                    "blockers": [],
                    "risks": ["keep an eye on delivery size"],
                    "next_step": "Continue from the condensed status.",
                    "session_id": "session-1",
                    "binding_id": "binding-1",
                    "return_packet_id": "packet-large",
                }
            )

            packet = build_return_packet(report)
            rendered = render_return_packet(packet)

        self.assertLessEqual(len(rendered), 180000)
        self.assertIn("Condensed Codex status:", rendered)
        self.assertIn("full Codex trace was larger than this chat can reliably accept", rendered)
        self.assertIn("Recommended next step: Continue from the condensed status.", rendered)
        self.assertNotIn("live Codex trace truncated for chat delivery.", rendered)

    def test_build_return_packet_omits_duplicate_final_output_without_truncating_sections(self):
        final_output = "Detailed final answer."
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T13:00:00+02:00",
                "thread_id": "thread-2",
                "summary": "Loop runner executed successfully.",
                "next_step": "Wait for the next ChatGPT turn.",
                "final_agent_message": final_output,
                "visible_assistant_trace": [
                    "First trace item.",
                    "Second trace item.",
                    final_output,
                    "Third trace item.",
                    "Fourth trace item.",
                    "Fifth trace item.",
                    "Sixth trace item.",
                    "Seventh trace item.",
                ],
                "commands_observed": [
                    {
                        "command": f"/bin/zsh -lc 'echo command-{index} {'x' * 80}'",
                        "status": "completed",
                        "exit_code": 0,
                    }
                    for index in range(20)
                ],
                "artifacts_dir": "/tmp/repo/artifacts/run-1",
                "prompt_path": "/tmp/repo/artifacts/run-1/prompt.md",
                "raw_output_path": "/tmp/repo/artifacts/run-1/stdout.jsonl",
                "last_message_path": "/tmp/repo/artifacts/run-1/last_message.md",
                "stderr_path": "/tmp/repo/artifacts/run-1/stderr.txt",
                "session_id": "session-1",
                "binding_id": "binding-1",
            }
        )

        packet = build_return_packet(report)
        rendered = render_return_packet(packet)

        self.assertEqual(packet.final_output, final_output)
        self.assertEqual(
            packet.visible_trace,
            [
                "First trace item.",
                "Second trace item.",
                "Third trace item.",
                "Fourth trace item.",
                "Fifth trace item.",
                "Sixth trace item.",
                "Seventh trace item.",
            ],
        )
        self.assertEqual(len(packet.commands_observed), 20)
        self.assertTrue(all("status: completed | exit: 0" in item for item in packet.commands_observed))
        self.assertTrue(any("xxxxxxxx" in item for item in packet.commands_observed))
        self.assertEqual(
            packet.artifacts,
            [
                "/tmp/repo/artifacts/run-1",
                "/tmp/repo/artifacts/run-1/prompt.md",
                "/tmp/repo/artifacts/run-1/stdout.jsonl",
                "/tmp/repo/artifacts/run-1/last_message.md",
                "/tmp/repo/artifacts/run-1/stderr.txt",
            ],
        )
        self.assertEqual(rendered.count(final_output), 1)

    def test_build_return_packet_keeps_very_large_final_output_intact(self):
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T13:00:00+02:00",
                "thread_id": "thread-2",
                "summary": "Fallback summary.",
                "final_agent_message": "A" * 5000,
                "session_id": "session-1",
                "binding_id": "binding-1",
            }
        )

        packet = build_return_packet(report)
        rendered = render_return_packet(packet)

        self.assertEqual(packet.final_output, "A" * 5000)
        self.assertNotIn("[... truncated for chat stability ...]", packet.final_output)
        self.assertIn("A" * 5000, rendered)

    def test_render_return_packet_truncates_huge_footer_sections(self):
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T13:00:00+02:00",
                "thread_id": "thread-2",
                "summary": "Compact summary.",
                "final_agent_message": "Short final output.",
                "files_touched": [f"file-{index}.md" for index in range(60)],
                "checks": [f"check-{index}" for index in range(45)],
                "session_id": "session-1",
                "binding_id": "binding-1",
            }
        )

        packet = build_return_packet(report)
        rendered = render_return_packet(packet)

        self.assertIn("- file-0.md", rendered)
        self.assertIn("- file-39.md", rendered)
        self.assertNotIn("- file-40.md", rendered)
        self.assertIn("- … 20 more", rendered)
        self.assertIn("- check-39", rendered)
        self.assertNotIn("- check-40", rendered)
        self.assertIn("- … 5 more", rendered)

    def test_build_return_packet_omits_generated_assistant_memory_files(self):
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T13:00:00+02:00",
                "thread_id": "thread-2",
                "summary": "Fresh marker completed.",
                "final_agent_message": "Fresh marker completed.",
                "files_touched": [
                    "assistant-memory/compiled/cards/codex-workspaces/codex/files/old-run.md",
                    "Sources/App.swift",
                ],
                "checks": [],
                "session_id": "session-1",
                "binding_id": "binding-1",
            }
        )

        packet = build_return_packet(report)
        rendered = render_return_packet(packet)

        self.assertEqual(packet.files_touched, ["Sources/App.swift"])
        self.assertIn("- Sources/App.swift", rendered)
        self.assertNotIn("assistant-memory/compiled", rendered)

    def test_build_return_packet_omits_generated_noise_from_blockers_and_risks(self):
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T13:00:00+02:00",
                "thread_id": "thread-2",
                "summary": "Done.",
                "final_agent_message": "Done.",
                "blockers": [
                    "2026-05-19T00:05:13.285568Z  WARN codex_otel::events::session_telemetry: metrics counter [codex.skill.injected] failed: tag value contains invalid characters: superpowers:executing-plans",
                    "2026-05-20T14:22:38.558893Z  WARN codex_core::session::turn: stream disconnected - retrying sampling request (1/5 in 191ms)...",
                    "real blocker",
                ],
                "risks": [
                    "Paste the full raw Codex output back into the mastermind chat for deep analysis.",
                    "Structured fields like files touched still need human review against the raw agent reply.",
                    "real risk",
                ],
                "next_step": "Review the raw Codex artifacts, then paste the return packet and raw output into the mastermind chat.",
                "session_id": "session-1",
                "binding_id": "binding-1",
            }
        )

        packet = build_return_packet(report)
        rendered = render_return_packet(packet)

        self.assertEqual(packet.blockers, ["real blocker"])
        self.assertEqual(packet.risks, ["real risk"])
        self.assertEqual(
            packet.next_step,
            "Continue from the final Codex output and clean execution trace in the same ChatGPT chat.",
        )
        self.assertNotIn("codex_otel::events::session_telemetry", rendered)
        self.assertNotIn("WARN codex_", rendered)
        self.assertNotIn("Paste the full raw Codex output", rendered)

    def test_render_return_packet_compact_mode_still_returns_full_output(self):
        report = RunReport.from_dict(
            {
                "timestamp": "2026-04-15T13:00:00+02:00",
                "thread_id": "thread-2",
                "summary": "Compact summary.",
                "final_agent_message": "B" * 5000,
                "visible_assistant_trace": ["trace-1", "trace-2", "trace-3", "trace-4"],
                "commands_observed": [
                    {"command": "/bin/zsh -lc 'echo one'", "status": "completed", "exit_code": 0}
                ],
                "files_touched": ["a.py", "b.py", "c.py", "d.py", "e.py"],
                "checks": ["pytest", "mypy", "ruff", "coverage"],
                "session_id": "session-1",
                "binding_id": "binding-1",
                "return_packet_id": "packet-compact",
                "estimated_context_remaining_percent": 35,
                "context_continuity_percent": 60,
                "thread_action": "new_thread",
                "observed_codex_thread_id": "codex-thread-1",
                "next_step": "Continue safely.",
            }
        )

        packet = build_return_packet(report)
        rendered = render_return_packet(packet, compact=True)
        full = render_return_packet(packet)

        self.assertEqual(rendered, full)
        self.assertIn("Here is what Codex wrote:", rendered)
        self.assertIn("plain-language prompt for Codex", rendered)
        self.assertIn("return_packet_id: packet-compact", rendered)
        self.assertIn("Commands observed:", rendered)
        self.assertIn("Files touched:", rendered)
        self.assertIn("Checks:", rendered)
        self.assertNotIn("Technical loop context:", rendered)
        self.assertNotIn("Usage:", rendered)
        self.assertNotIn("Artifacts:", rendered)


class InstructionScopePolicyTests(unittest.TestCase):
    def test_apply_instruction_updates_and_resolve_in_priority_order(self):
        session_payload = {
            "instruction_updates": [
                {"scope": "session", "mode": "append", "text": "Keep existing session context."}
            ]
        }
        policy_state = {"project_instruction_updates": ["Honor project-level safety policy."]}
        apply_instruction_updates(
            session_payload,
            policy_state,
            [
                {"scope": "next_run", "mode": "append", "text": "Use the narrowest possible diff."},
                {"scope": "project", "mode": "append", "text": "Always return to the same orchestrator chat."},
            ],
        )

        resolved = resolve_instruction_texts(session_payload, policy_state)

        self.assertEqual(
            resolved,
            [
                "Honor project-level safety policy.",
                "Always return to the same orchestrator chat.",
                "Keep existing session context.",
                "Use the narrowest possible diff.",
            ],
        )

    def test_consume_next_run_instructions_removes_ephemeral_entries(self):
        session_payload = {
            "instruction_updates": [
                {"scope": "next_run", "mode": "append", "text": "Use the narrowest possible diff."},
                {"scope": "session", "mode": "append", "text": "Keep existing session context."},
            ]
        }

        consume_next_run_instructions(session_payload)

        self.assertEqual(
            session_payload["instruction_updates"],
            [{"scope": "session", "mode": "append", "text": "Keep existing session context."}],
        )


if __name__ == "__main__":
    unittest.main()
