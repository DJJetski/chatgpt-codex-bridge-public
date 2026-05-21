---
name: chatgpt-codex-bridge
description: Use when installing, operating, diagnosing, or safely running the ChatGPT Codex Bridge Supervisor V2 from a cloned or installed repository.
---

# ChatGPT Codex Bridge

Use this skill for local-first bridge operations:

- install and verify the bridge with `codex-bridge install`, `codex-bridge doctor`, and `codex-bridge self-test`
- create and operate Supervisor V2 sessions with `codex-bridge v2 session ...`
- keep bridge state under `BRIDGE_HOME` and Codex integration under `CODEX_HOME`
- keep browser/control-panel/macOS surfaces opt-in instead of default runtime requirements

## Safety Defaults

- Do not publish, paste, or commit bridge runtime state, logs, ChatGPT URLs, thread IDs, browser profiles, API keys, tokens, or local machine paths.
- Prefer `core-safe` behavior unless the operator explicitly chooses a broader profile.
- Confirm before sends, sharing, deletes, permission changes, OAuth/scope changes, payments, deployments, public links, or credential handling.
- Treat `state/`, `artifacts/`, `config/`, and logs as local runtime material, not release material.

## Useful Commands

```bash
codex-bridge doctor
codex-bridge self-test
codex-bridge install --dry-run
codex-bridge v2 session create --operator-goal "..." --repo-path "$PWD" --workspace-path "$PWD"
```

Set `BRIDGE_HOME` to isolate bridge state and `CODEX_HOME` to target a specific Codex configuration directory.
