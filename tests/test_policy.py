import unittest

from mastermind_bridge.policy import DecisionContext, decide_actions


class PolicyTests(unittest.TestCase):
    def test_same_thread_when_goal_and_work_unit_are_stable(self):
        context = DecisionContext.from_dict(
            {
                "project_name": "bridge",
                "task_label": "continue-kernel",
                "current_thread_id": "thread-1",
                "goal_family": "bridge-core",
                "work_unit": "kernel",
                "repo_path": "/tmp/repo",
                "current_worktree": "/tmp/repo",
                "target_worktree": "/tmp/repo",
                "current_branch": "main",
                "target_branch": "main",
                "same_goal_family": True,
                "same_work_unit": True,
                "same_repo": True,
                "same_worktree": True,
                "same_branch": True,
                "assumptions_stable": True,
                "rebrief_cost": "low",
                "topic_shift": "none",
                "context_overloaded": False,
                "parallel_isolation_needed": False,
                "risky_changes": False,
                "unrelated_uncommitted_changes": False,
                "needs_clean_replay": False,
            }
        )

        decision = decide_actions(context)

        self.assertEqual(decision.thread_action, "same_thread")
        self.assertEqual(decision.worktree_action, "reuse_worktree")
        self.assertEqual(decision.branch_action, "reuse_branch")
        self.assertGreaterEqual(decision.context_continuity_percent, 70)
        self.assertEqual(decision.continuity_band, "high")
        self.assertIn("Goal family is unchanged.", decision.reasons)

    def test_fork_thread_when_context_is_overloaded_but_goal_family_matches(self):
        context = DecisionContext.from_dict(
            {
                "project_name": "bridge",
                "task_label": "fork-for-implementation",
                "current_thread_id": "thread-1",
                "candidate_thread_id": "thread-2",
                "goal_family": "bridge-core",
                "work_unit": "implementation",
                "repo_path": "/tmp/repo",
                "current_worktree": "/tmp/repo",
                "target_worktree": "/tmp/repo",
                "current_branch": "main",
                "target_branch": "main",
                "same_goal_family": True,
                "same_work_unit": False,
                "same_repo": True,
                "same_worktree": True,
                "same_branch": True,
                "assumptions_stable": True,
                "rebrief_cost": "medium",
                "topic_shift": "adjacent",
                "context_overloaded": True,
                "parallel_isolation_needed": False,
                "risky_changes": False,
                "unrelated_uncommitted_changes": False,
                "needs_clean_replay": False,
            }
        )

        decision = decide_actions(context)

        self.assertEqual(decision.thread_action, "fork_thread")
        self.assertEqual(decision.selected_thread_id, "thread-2")
        self.assertGreaterEqual(decision.context_continuity_percent, 40)
        self.assertLess(decision.context_continuity_percent, 70)
        self.assertEqual(decision.continuity_band, "medium")
        self.assertIn("Current thread is context-overloaded.", decision.reasons)

    def test_new_thread_when_assumptions_changed(self):
        context = DecisionContext.from_dict(
            {
                "project_name": "bridge",
                "task_label": "restart-after-assumption-shift",
                "current_thread_id": "thread-1",
                "candidate_thread_id": "thread-3",
                "goal_family": "bridge-core",
                "work_unit": "sdk-research",
                "repo_path": "/tmp/repo",
                "current_worktree": "/tmp/repo",
                "target_worktree": "/tmp/repo",
                "current_branch": "main",
                "target_branch": "main",
                "same_goal_family": False,
                "same_work_unit": False,
                "same_repo": True,
                "same_worktree": True,
                "same_branch": True,
                "assumptions_stable": False,
                "rebrief_cost": "high",
                "topic_shift": "major",
                "context_overloaded": True,
                "parallel_isolation_needed": False,
                "risky_changes": False,
                "unrelated_uncommitted_changes": False,
                "needs_clean_replay": True,
            }
        )

        decision = decide_actions(context)

        self.assertEqual(decision.thread_action, "new_thread")
        self.assertEqual(decision.selected_thread_id, "thread-3")
        self.assertLess(decision.context_continuity_percent, 40)
        self.assertEqual(decision.continuity_band, "low")
        self.assertIn("Core assumptions changed.", decision.reasons)

    def test_new_worktree_and_branch_when_parallel_risky_changes_need_isolation(self):
        context = DecisionContext.from_dict(
            {
                "project_name": "bridge",
                "task_label": "parallel-risky-slice",
                "current_thread_id": "thread-1",
                "candidate_thread_id": "thread-4",
                "goal_family": "bridge-core",
                "work_unit": "codex-wrapper",
                "repo_path": "/tmp/repo",
                "current_worktree": "/tmp/repo",
                "target_worktree": "/tmp/repo-codex-wrapper",
                "current_branch": "main",
                "target_branch": "feature/codex-wrapper",
                "same_goal_family": True,
                "same_work_unit": False,
                "same_repo": True,
                "same_worktree": False,
                "same_branch": False,
                "assumptions_stable": True,
                "rebrief_cost": "medium",
                "topic_shift": "adjacent",
                "context_overloaded": False,
                "parallel_isolation_needed": True,
                "risky_changes": True,
                "unrelated_uncommitted_changes": True,
                "needs_clean_replay": False,
            }
        )

        decision = decide_actions(context)

        self.assertEqual(decision.worktree_action, "new_worktree")
        self.assertEqual(decision.branch_action, "new_branch")
        self.assertEqual(decision.suggested_worktree, "/tmp/repo-codex-wrapper")
        self.assertEqual(decision.suggested_branch, "feature/codex-wrapper")

    def test_new_thread_when_continuity_falls_below_40_percent(self):
        context = DecisionContext.from_dict(
            {
                "project_name": "bridge",
                "task_label": "hard-restart",
                "current_thread_id": "thread-7",
                "candidate_thread_id": "thread-8",
                "goal_family": "bridge-core",
                "work_unit": "fresh-problem",
                "repo_path": "/tmp/repo",
                "current_worktree": "/tmp/repo",
                "target_worktree": "/tmp/repo",
                "current_branch": "main",
                "target_branch": "main",
                "same_goal_family": True,
                "same_work_unit": False,
                "same_repo": True,
                "same_worktree": True,
                "same_branch": True,
                "assumptions_stable": False,
                "rebrief_cost": "high",
                "topic_shift": "major",
                "context_overloaded": True,
                "parallel_isolation_needed": False,
                "risky_changes": False,
                "unrelated_uncommitted_changes": False,
                "needs_clean_replay": True,
            }
        )

        decision = decide_actions(context)

        self.assertEqual(decision.thread_action, "new_thread")
        self.assertLess(decision.context_continuity_percent, 40)
        self.assertIn("Continuity fell below the 40 percent threshold.", decision.reasons)


if __name__ == "__main__":
    unittest.main()
