import unittest

from mastermind_bridge.prompting import PromptRequest, render_prompt


class PromptingTests(unittest.TestCase):
    def test_render_new_thread_prompt_formats_lists_as_bullets(self):
        request = PromptRequest.from_dict(
            {
                "mode": "codex_new_thread",
                "project_name": "bridge",
                "thread_id": "thread-2",
                "goal_family": "bridge-core",
                "work_unit": "kernel",
                "repo_path": "/tmp/repo",
                "worktree_path": "/tmp/repo",
                "branch": "main",
                "thread_action": "fork_thread",
                "objective": "Build the local bridge kernel.",
                "task": "Implement decisioning and prompt generation.",
                "decision_summary": "Context is overloaded but still in the same goal family.",
                "delta_summary": "Architecture is already chosen.",
                "read_order": ["README.md", "MASTER_PLAN.md"],
                "durable_state": ["HANDOFF.md", "state/THREAD_REGISTRY.json"],
                "constraints": ["Keep state local.", "Do not depend on SDKs yet."],
                "acceptance_criteria": ["Tests pass.", "Prompt is generated."],
                "recent_results": ["Repo scaffold exists."],
                "required_output": ["Code changes", "Updated prompt"],
            }
        )

        prompt = render_prompt(request)

        self.assertIn("Project: bridge", prompt)
        self.assertIn("- README.md", prompt)
        self.assertIn("- Keep state local.", prompt)
        self.assertIn("Fresh-thread startup rule:", prompt)
        self.assertIn("refresh only the minimum durable docs", prompt)
        self.assertIn("do not front-load doc work", prompt)
        self.assertIn("before reporting any live GUI, browser login, local auth, permission, allow-dialog, or app-state blocker", prompt)
        self.assertIn("Computer Use Codex plugin", prompt)
        self.assertIn("Codex exec capability notes:", prompt)
        self.assertIn("Skill path hints for codex exec on this machine:", prompt)
        self.assertIn("update the relevant durable docs only after the main work", prompt)
        self.assertIn("Required output:", prompt)

    def test_render_continue_thread_prompt_includes_decision_summary(self):
        request = PromptRequest.from_dict(
            {
                "mode": "codex_continue_thread",
                "project_name": "bridge",
                "thread_id": "thread-1",
                "goal_family": "bridge-core",
                "work_unit": "kernel",
                "repo_path": "/tmp/repo",
                "worktree_path": "/tmp/repo",
                "branch": "main",
                "thread_action": "same_thread",
                "task": "Continue test coverage.",
                "decision_summary": "The same thread still holds the necessary context.",
                "delta_summary": "Only one narrow task remains.",
                "constraints": ["Keep the scope narrow."],
                "acceptance_criteria": ["New tests exist."],
            }
        )

        prompt = render_prompt(request)

        self.assertIn("Existing thread: thread-1", prompt)
        self.assertIn("The same thread still holds the necessary context.", prompt)
        self.assertIn("reuse the still-valid thread context", prompt)
        self.assertIn("Same-thread execution rule:", prompt)
        self.assertIn("do not reread the full project docs", prompt)
        self.assertIn("before reporting any live GUI, browser login, local auth, permission, allow-dialog, or app-state blocker", prompt)
        self.assertIn("Computer Use Codex plugin", prompt)
        self.assertIn("Codex exec capability notes:", prompt)
        self.assertIn("update the relevant durable docs only after the main work", prompt)
        self.assertIn("Continue with:", prompt)

    def test_render_rebrief_prompt_includes_fresh_thread_startup_rule(self):
        request = PromptRequest.from_dict(
            {
                "mode": "codex_rebrief",
                "project_name": "bridge",
                "thread_id": "thread-2",
                "parent_thread_id": "thread-1",
                "goal_family": "bridge-core",
                "work_unit": "new-topic",
                "repo_path": "/tmp/repo",
                "worktree_path": "/tmp/repo",
                "branch": "main",
                "thread_action": "new_thread",
                "objective": "Start a clean thread for a new topic.",
                "task": "Inspect the new topic and propose the safest first slice.",
                "decision_summary": "A clean thread is better for this new topic.",
                "rebrief_reason": "The work is now a distinct topic.",
                "read_order": ["MASTER_PLAN.md", "HANDOFF.md"],
                "durable_state": ["docs/private/DECISIONS.md"],
                "carry_forward": ["Keep the same repo binding."],
                "constraints": ["Stay in this repo."],
                "acceptance_criteria": ["Report findings clearly."],
                "recent_results": ["Previous topic is complete enough to branch away."],
                "required_output": ["Detailed analysis", "Recommended next step"],
            }
        )

        prompt = render_prompt(request)

        self.assertIn("Fresh-thread startup rule:", prompt)
        self.assertIn("refresh only the minimum durable docs", prompt)
        self.assertIn("do not front-load doc work", prompt)
        self.assertIn("do not assume old thread memory", prompt)
        self.assertIn("before reporting any live GUI, browser login, local auth, permission, allow-dialog, or app-state blocker", prompt)
        self.assertIn("Computer Use Codex plugin", prompt)
        self.assertIn("Codex exec capability notes:", prompt)
        self.assertIn("update the relevant durable docs only after the main work", prompt)


if __name__ == "__main__":
    unittest.main()
