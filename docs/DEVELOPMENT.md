# Development

## Runtime

- Python 3.11 or newer
- packaging is handled by `pyproject.toml` with `setuptools`
- optional extras:
  - `browser` for Playwright-based work
  - `macos` for future macOS-specific integrations

## Setup

Install the package in editable mode:

```bash
python3 -m pip install -e .
```

Run local lifecycle checks:

```bash
codex-bridge doctor
codex-bridge self-test
codex-bridge install --dry-run
```

Use isolated runtime directories for clean-room testing:

```bash
export BRIDGE_HOME="$(mktemp -d)"
export CODEX_HOME="$(mktemp -d)"
```

Install the browser extra only when you are working on the browser/control-panel path:

```bash
python3 -m pip install -e '.[browser]'
```

Enable optional compatibility surfaces explicitly:

```bash
export BRIDGE_PROFILE=browser-extra
```

The default `core-safe` profile keeps browser/control-panel/macOS surfaces out of the normal runtime path. Dangerous Codex sandbox bypass is additionally gated by `BRIDGE_ALLOW_DANGEROUS_CODEX_BYPASS=1` and a deliberate `danger-full-access` sandbox request.

## Local Codex Plugins

Browser Use and Computer Use are available operator surfaces on this Mac. They are not V2 correctness dependencies for ordinary terminal-first kernel progress, but Computer Use is the required escalation path for live GUI, auth, permission, and dialog blockers.

- Browser Use is the preferred Codex plugin for local or in-app browser inspection: localhost, `127.0.0.1`, `file://` URLs, current-tab checks, DOM snapshots, screenshots, and visual verification.
- Computer Use is the required Codex plugin for live macOS GUI state when that state blocks progress: Codex.app, the local control panel browser, Safari or Chrome sessions, permission dialogs, allow/OK dialogs, screenshots, clicking, typing, and Accessibility-backed control.
- For login-dependent work, prefer the user's normal logged-in Safari or Google Chrome session through Computer Use or app-native automation. Use Passwords, Keychain, already-authenticated apps, and Messages for one-time codes when the task has a specific approved destination and the normal confirmation rules allow it.
- If a macOS privacy, Automation, Accessibility, Screen Recording, Full Disk Access, browser-control, or helper permission is missing, open the relevant settings pane with `CODEX_HOME/scripts/open_codex_privacy_settings.sh` when present, report the exact permission, and continue after it is granted.
- Touch ID, hardware security-key taps, and other physical-presence prompts are blockers after the agent has navigated to the required screen and reported the exact prompt.
- Keep Supervisor V2 terminal-first. Use these plugins when they materially advance browser-loop, control-panel, live-login, permission, or UI verification work; do not make ordinary V2 kernel progress depend on visible browser or desktop state.
- Follow action-time confirmation and sensitive-data rules before changing OS security/privacy settings, changing cloud/account permissions, deleting data, transmitting passwords or one-time codes, creating persistent access keys, submitting external forms/messages, solving CAPTCHAs, or taking financial/medical/legal account actions.
- V2 Codex worker prompts include this plugin guidance automatically so generated Codex turns can select the right local surface without ChatGPT restating it every time.

## Common Commands

Show the CLI surface:

```bash
codex-bridge --help
codex-bridge v2 --help
bridgectl --help
```

Run the product lifecycle tests:

```bash
python3 -m unittest discover -s tests -p 'test_product_lifecycle.py'
```

Run the current default-loop smoke tests:

```bash
python3 -m unittest discover -s tests -p 'test_default_loop_simplified.py'
```

Run the focused V2 suite:

```bash
python3 -m unittest discover -s tests -p 'test_v2.py'
```

Run the broader suite:

```bash
python3 -m unittest discover -s tests
```

Check the runtime cleanup helper before pruning generated state:

```bash
codex-bridge cleanup-runtime-state --dry-run
```

`state/`, `artifacts/`, `config/`, `.dual-graph/`, and `.coa/codesearch/`
in the source checkout are ignored local runtime or generated-cache paths.
Default installed runtime state is under `BRIDGE_HOME`.

The cleanup helper prunes reproducible local noise such as `.DS_Store`, Python
caches, browser engine caches, root-level generated logs, `.dual-graph/`, and
`.coa/codesearch/{indexes,logs}`. It preserves durable runtime truth such as
`state/session_locks/`, `state/sessions/`, `config/`, and `artifacts/runs/`.
See [docs/CURRENT_STATE.md](CURRENT_STATE.md) for the current cleanup boundary.

## Working Areas

- `src/chatgpt_codex_bridge/v2/` for the default runtime
- `src/chatgpt_codex_bridge/orchestrator/` for the legacy browser/control-panel loop
- `src/chatgpt_codex_bridge/` for the implementation package and public CLI namespace
- `src/mastermind_bridge/` for legacy import compatibility
- `src/chatgpt_codex_bridge/resources/` for bundled installer resources
- `examples/` for sanitized fixtures
- `tests/` for regression coverage
- `docs/` for public orientation and contributor notes
- `docs/private/` for ignored local maintainer notes only

## Validation Guidance

When changing loop behavior, prefer focused unittest runs that cover the touched path before broadening to the full suite.

When changing docs only, validate links and section references instead of running the entire test suite.

When preparing a public release or visibility change, scan tracked files and
release artifacts:

```bash
git ls-files --cached --others --exclude-standard -z | xargs -0 python3 scripts/check_release_artifacts.py
sh scripts/build_release_artifacts.sh
```

When touching generated runtime cleanup logic, use `--dry-run` first and only prune paths that are clearly local and reproducible.
