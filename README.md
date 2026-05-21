# ChatGPT Codex Bridge

[![CI](https://github.com/DJJetski/chatgpt-codex-bridge-public/actions/workflows/ci.yml/badge.svg)](https://github.com/DJJetski/chatgpt-codex-bridge-public/actions/workflows/ci.yml)
[![CodeQL](https://github.com/DJJetski/chatgpt-codex-bridge-public/actions/workflows/codeql.yml/badge.svg)](https://github.com/DJJetski/chatgpt-codex-bridge-public/actions/workflows/codeql.yml)

Local-first supervisor tooling for coordinating ChatGPT planning with Codex execution.

## Project Status

This is alpha software. The important loop works: ChatGPT can plan, Codex can
execute, and Supervisor V2 keeps the handoff recoverable through local state,
strict worker contracts, and audit artifacts.

The operator UI and legacy browser/control-panel surfaces are still rough. The
Safari or browser-facing experience is not polished, not especially
user-friendly, and should be treated as a work-in-progress compatibility layer.
The default path is the terminal-first Supervisor V2 runtime.

Contributions are welcome. Useful pull requests include focused stability
fixes, clearer setup docs, tests for edge cases, safer defaults, and UI/control
panel improvements that do not weaken the local data boundary.

## What Works Now

- Terminal-first Supervisor V2 sessions with one active turn per session.
- Strict ChatGPT and Codex worker result validation.
- Local SQLite state and inspectable artifacts under `BRIDGE_HOME`.
- Install, doctor, self-test, snapshot, and uninstall lifecycle commands.
- Release artifact scanning for runtime state, local paths, private docs, thread
  IDs, and secret-like material.
- Optional legacy browser/control-panel flows for operator compatibility.

The default runtime is **Supervisor V2**:

- terminal-first and kernel-first
- one active turn per session
- strict JSON worker contracts
- local SQLite state under `BRIDGE_HOME`
- inspectable artifacts under `BRIDGE_HOME`
- Codex execution through the installed Codex CLI

Browser, control-panel, and macOS app-server paths remain optional compatibility surfaces. They are not required for the core safe runtime.

Runtime capability profiles are explicit:

- `core-safe` is the default lifecycle and runtime posture.
- `trusted-local` allows local Codex execution conveniences but still keeps dangerous sandbox bypass off unless explicitly opted in.
- `browser-extra` enables optional browser/control-panel surfaces.
- `macos-app` enables browser/control-panel surfaces plus optional macOS Codex app integration.

Use `BRIDGE_PROFILE=browser-extra` or `BRIDGE_PROFILE=macos-app` before starting the legacy control panel. Dangerous Codex sandbox bypass additionally requires `BRIDGE_ALLOW_DANGEROUS_CODEX_BYPASS=1` and `sandbox=danger-full-access`; otherwise the bridge passes normal Codex `-a/-s` sandbox flags.

## Install From A Clone

```bash
git clone https://github.com/DJJetski/chatgpt-codex-bridge-public.git chatgpt-codex-bridge
cd chatgpt-codex-bridge
python3 -m pip install -e .
codex-bridge doctor
codex-bridge self-test
codex-bridge install --dry-run
```

## Install From A Release

Release tags build a wheel, sdist, source archive, `install.sh`, and
`SHA256SUMS`.

```bash
shasum -a 256 -c SHA256SUMS
python3 -m pip install chatgpt_codex_bridge-0.1.0-py3-none-any.whl
codex-bridge self-test
```

The release `install.sh` is a bootstrapper. Run it from the source archive or
from a directory that also contains the release wheel or sdist.

The source checkout and release archive must remain free of runtime state,
browser profiles, logs, thread IDs, local ChatGPT URLs, and secrets.

The installer places bridge-owned runtime material outside the source checkout:

- `BRIDGE_HOME`: bridge config, state, artifacts, logs, and install manifest
- `CODEX_HOME`: Codex configuration directory, defaulting to `~/.codex`

Override both for clean-room testing:

```bash
BRIDGE_HOME="$(mktemp -d)" CODEX_HOME="$(mktemp -d)" codex-bridge self-test
```

## Documentation

Start with [docs/README.md](docs/README.md) when you need the documentation
map. Public user and contributor docs live in the root and public `docs/`
files. Private maintainer notes may exist locally under `docs/private/`; that
path is ignored and excluded from public release archives.

For a refreshed map of the current source tree, runtime boundary, cleanup
posture, and verification baseline, see
[docs/CURRENT_STATE.md](docs/CURRENT_STATE.md).
Before making the repository public or cutting a GitHub release, use
[docs/PUBLIC_RELEASE_CHECKLIST.md](docs/PUBLIC_RELEASE_CHECKLIST.md).

## CLI

Primary command:

```bash
codex-bridge --help
```

Compatibility alias:

```bash
bridgectl --help
```

Product lifecycle commands:

```bash
codex-bridge install
codex-bridge doctor
codex-bridge self-test
codex-bridge snapshot
codex-bridge uninstall
```

Supervisor V2 commands:

```bash
codex-bridge v2 session create \
  --operator-goal "Implement the next stable slice" \
  --repo-path "$PWD" \
  --workspace-path "$PWD"

codex-bridge v2 session bootstrap chatgpt --session-id <session-id>
codex-bridge v2 session arm --session-id <session-id>
codex-bridge v2 session start --session-id <session-id>
codex-bridge v2 session status --session-id <session-id>
codex-bridge v2 session pause --session-id <session-id>
codex-bridge v2 session resume --session-id <session-id>
codex-bridge v2 session stop --session-id <session-id>
codex-bridge v2 session abort-turn --session-id <session-id>
```

## Safety Boundary

This repository is intended to be cloneable without private runtime state.

Do not commit:

- `state/`
- `artifacts/`
- `config/`
- browser profiles
- logs
- ChatGPT URLs
- thread IDs from private sessions
- API keys, tokens, cookies, or local auth material

The checked-in `examples/` directory contains sanitized fixtures only.

## Development

After installing the checkout in editable mode, run focused checks:

```bash
python3 -m unittest discover -s tests -p 'test_product_lifecycle.py'
python3 -m unittest discover -s tests -p 'test_v2.py'
python3 -m unittest discover -s tests -p 'test_runtime_hygiene.py'
```

Run the broader suite before release:

```bash
python3 -m unittest discover -s tests
```

Build a local release bundle:

```bash
sh scripts/build_release_artifacts.sh
(cd dist && shasum -a 256 -c SHA256SUMS)
```

## Contributing

Open issues for reproducible bugs or focused feature proposals, and open pull
requests for contained improvements. Good PRs explain the behavior change, list
the checks run, and keep runtime/private data out of the diff.

See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), and the
pull request template before submitting changes.

## License

MIT. See [LICENSE](LICENSE).
