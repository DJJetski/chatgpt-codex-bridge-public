# Codex New Thread Brief

Project: ${project_name}
Goal family: ${goal_family}
Work unit: ${work_unit}
Repo: ${repo_path}
Worktree: ${worktree_path}
Branch: ${branch}
Thread action: ${thread_action}

Stay in this repo/worktree only. If any prior context points at another repo, stop and report the mismatch instead of following it.

Fresh-thread startup rule:
- start by anchoring on the project context below
- refresh only the minimum durable docs and code paths that are relevant for this topic before acting, and do not front-load doc work when a direct operational step is already known
- treat existing repo dirtiness as normal WIP unless a concrete conflict appears
- continue the main implementation strand already in flight instead of inventing side cleanups or repo gardening
- preserve the newest visible worktree state; do not stash, reset, clean, or otherwise hide uncommitted work merely to make the repo look clean
- do not create or update standalone continuation or meta files such as `PLAN.md`, `HANDOFF.md`, `PROJECT_STATE.md`, `NEXT_PROMPT.md`, or `CODEX.md` unless the user explicitly asked or durable truth changed and an existing canonical file truly needs the update after the main work
- do not assume old thread memory is available unless it is explicitly restated here
- before reporting any live GUI, browser login, local auth, permission, allow-dialog, or app-state blocker, use the Computer Use Codex plugin or an equivalent real-session automation route to inspect the screen and try the live step
- prefer the user's normal Safari or Google Chrome session for login-dependent browser work, and use Passwords, Keychain, already-authenticated apps, and Messages for one-time codes when a specific approved login/auth flow needs them
- Touch ID, hardware security-key taps, and other physical-presence prompts are blockers after you navigate to the required screen and report the exact prompt
- follow action-time confirmation and sensitive-data rules before changing OS security/privacy settings, changing cloud/account permissions, deleting data, transmitting passwords or one-time codes, creating persistent access keys, submitting external forms/messages, solving CAPTCHAs, or taking financial/medical/legal account actions

${exec_capability_notes}

Read order:
${read_order}

Durable state to trust:
${durable_state}

Objective:
${objective}

Task:
${task}

Constraints:
${constraints}

Acceptance criteria:
${acceptance_criteria}

Latest verified context:
${recent_results}

Required output:
${required_output}

Response contract:
- explain what you changed
- explain how you changed it
- list commands/checks you ran
- call out blockers and remaining risks
- explain how the result maps back to the master plan
- update the relevant durable docs only after the main work and only when code changes or research change the project's durable truth
- when you finish a coherent verified forward step and can isolate your task-owned changes safely, create one local commit with a clear message by default
- if unrelated dirty changes cannot be separated safely, skip the auto-commit and say so plainly
- do not push, open a PR, or deploy unless the user explicitly asked or the repo's established workflow clearly requires it
