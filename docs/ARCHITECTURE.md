# Architecture

## Current Default

Supervisor V2 is the default runtime path.

For the current source tree map, local runtime/cache boundary, pressure points,
and verification baseline, see [docs/CURRENT_STATE.md](CURRENT_STATE.md).

The default shape is:

- `codex-bridge v2` as the primary CLI
- `bridgectl` as a compatibility alias
- a local kernel that sequences sessions and turns
- SQLite-backed runtime state under `BRIDGE_HOME/state`
- artifacts under `BRIDGE_HOME/artifacts`
- short-lived worker processes for ChatGPT and Codex
- Codex turns executed through the installed Codex CLI

## Main Pieces

- `src/chatgpt_codex_bridge/`
  - implementation package, public import namespace, and `codex-bridge` CLI
- `src/mastermind_bridge/`
  - compatibility import shim for legacy `mastermind_bridge.*` imports
- `src/chatgpt_codex_bridge/v2/`
  - V2 kernel, store, CLI wiring, workers, and types
- `src/chatgpt_codex_bridge/lifecycle.py`
  - install, doctor, self-test, snapshot, and uninstall implementation
- `src/chatgpt_codex_bridge/app_paths.py`
  - `BRIDGE_HOME` and `CODEX_HOME` path ownership
- `src/chatgpt_codex_bridge/resources/`
  - bundled Codex skill and package resources
- `src/chatgpt_codex_bridge/prompts/`
  - package prompt templates used after wheel/editable install
- `src/chatgpt_codex_bridge/orchestrator/`
  - legacy browser-mediated loop and control-panel code
- `examples/`
  - sanitized public fixtures

## Runtime Boundary

The source repository is not runtime storage. Runtime and machine-local data belongs under:

- `BRIDGE_HOME/state`
- `BRIDGE_HOME/config`
- `BRIDGE_HOME/artifacts`
- `BRIDGE_HOME/logs`
- `CODEX_HOME/skills/chatgpt-codex-bridge`

The repository ignores `state/`, `artifacts/`, and `config/` at the checkout root so private local state cannot drift into public release files.

## Profiles

Default profile:

- `core-safe`

Optional profiles:

- `trusted-local`
- `browser-extra`
- `macos-app`

Profiles are surfaced through lifecycle commands and must be explicit when broader capabilities are needed. Browser/control-panel/macOS paths are compatibility or operator surfaces, not core correctness dependencies.

## Testing Boundary

Focused release-critical suites:

- `tests/test_product_lifecycle.py`
- `tests/test_v2.py`
- `tests/test_runtime_hygiene.py`

Broader legacy/browser coverage remains under:

- `tests/test_default_loop_simplified.py`
- `tests/test_loop.py`
- `tests/test_control_panel.py`
- `tests/test_browser_adapter.py`

## What Not To Infer

Do not infer a hosted service, cloud sync system, or public runtime state store from this repo. The bridge is local-first and keeps runtime truth on the operator's machine.
