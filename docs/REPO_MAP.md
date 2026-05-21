# Repo Map

## Public User And Contributor Surface

- `README.md`
  - install, CLI, safety, and quick-start orientation
- `LICENSE`, `SECURITY.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`,
  `CHANGELOG.md`
  - release metadata and project policy
- `docs/README.md`
  - public/private documentation map
- `docs/CURRENT_STATE.md`
  - refreshed source tree, runtime-boundary, cleanup, pressure-point, and
    verification snapshot
- `docs/PUBLIC_RELEASE_CHECKLIST.md`
  - public release, artifact, Git history, and GitHub readiness checklist
- `pyproject.toml`
  - package metadata, optional extras, console scripts, and package data
- `src/chatgpt_codex_bridge/`
  - implementation package, public import namespace, and primary CLI
- `src/mastermind_bridge/`
  - legacy import compatibility shim
- `src/chatgpt_codex_bridge/prompts/`
  - packaged prompt templates
- `src/chatgpt_codex_bridge/resources/`
  - bundled Codex skill and installer resources
- `examples/`
  - sanitized fixtures safe for public clone/tests
- `tests/`
  - unittest-based regression coverage
- `docs/`
  - public architecture, development, workflow, thread policy, and repo map
- `scripts/install.sh`
  - thin install bootstrapper
- `scripts/build_release_artifacts.sh`
  - local/GitHub release bundle builder
- `Bridge Control Panel.app/`
  - small optional macOS launcher for the rough local control-panel UI

## Private Maintainer Surface

Private maintainer notes may exist locally under `docs/private/`. That path is
ignored by Git and excluded from public release archives. It should not be part
of the public default branch.

Ignored root files such as `AGENTS.md`, `CODEX.md`, `EXECUTION_LOG.md`,
`HANDOFF.md`, `NEXT_PROMPT.md`, and `START_CYCLE.md` are local operator state,
not repository release surface.

## Implementation Areas

- `src/chatgpt_codex_bridge/v2/`
  - V2 kernel, store, workers, CLI, and types
- `src/chatgpt_codex_bridge/lifecycle.py`
  - install, doctor, self-test, snapshot, and uninstall commands
- `src/chatgpt_codex_bridge/app_paths.py`
  - `BRIDGE_HOME`/`CODEX_HOME` path ownership
- `src/chatgpt_codex_bridge/orchestrator/`
  - legacy browser-mediated loop, control panel, browser adapters, packets, policy, and session state helpers
- `src/chatgpt_codex_bridge/executor.py`
  - Codex CLI execution helpers and native app-server thread helpers
- `src/chatgpt_codex_bridge/cli.py`
  - top-level command dispatch
- `src/chatgpt_codex_bridge/runtime_cleanup.py`
  - safe pruning helper for generated local runtime state, browser caches,
    Python caches, local graph caches, and local code-search indexes

## Tests

- `tests/test_product_lifecycle.py`
  - install/doctor/self-test/resource lifecycle coverage
- `tests/test_v2.py`
  - primary V2 session, kernel, and CLI coverage
- `tests/test_runtime_hygiene.py`
  - local runtime cleanup coverage
- `tests/test_default_loop_simplified.py`
  - current default-loop coverage for the simplified path
- `tests/test_loop.py`, `tests/test_control_panel.py`, `tests/test_browser_adapter.py`
  - legacy/browser/control-panel coverage
- `tests/test_cli.py`
  - CLI surface and orchestration helpers

## Runtime And Private Paths

These are intentionally not part of the public release surface:

- `state/`
- `artifacts/`
- `config/`
- `.dual-graph/`
- `.coa/codesearch/`
- `docs/private/`
- browser profiles
- local logs
- session locks
- SQLite runtime databases
- private ChatGPT URLs
- private Codex or ChatGPT thread IDs
- credentials, cookies, tokens, and API keys

Installed runtime defaults:

- `BRIDGE_HOME/state`
- `BRIDGE_HOME/config`
- `BRIDGE_HOME/artifacts`
- `BRIDGE_HOME/logs`
- `BRIDGE_HOME/install/manifest.json`
- `CODEX_HOME/skills/chatgpt-codex-bridge`

## Historical Files

Historical planning or handoff files may exist in this working checkout as
ignored local files. They are not default runtime truth for installed users and
should not receive new coordination state unless a task explicitly targets them.
