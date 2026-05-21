# Contributing

Thanks for considering a contribution. This project is intentionally early:
the core ChatGPT-to-Codex loop works, while the browser/control-panel UI is
still rough and operator-oriented. Focused fixes, tests, docs, and usability
improvements are welcome.

## Good Contributions

- Stability fixes for Supervisor V2, lifecycle commands, release packaging, or
  recovery behavior.
- Tests that capture real edge cases around worker results, session recovery,
  runtime cleanup, browser compatibility, or install flows.
- Documentation that makes setup, troubleshooting, or safe operation clearer.
- UI/control-panel improvements, as long as the terminal-first V2 path stays
  the default and local data stays local.
- Release, CI, security, or dependency-hygiene improvements.

## Setup

Read [docs/README.md](docs/README.md) for the public/private documentation map.

```bash
python3 -m pip install -e '.[browser]'
codex-bridge self-test
```

Use isolated runtime directories while developing:

```bash
export BRIDGE_HOME="$(mktemp -d)"
export CODEX_HOME="$(mktemp -d)"
```

## Checks

Run focused tests for the surface you changed, then broaden before release:

```bash
python3 -m unittest discover -s tests -p 'test_product_lifecycle.py'
python3 -m unittest discover -s tests -p 'test_v2.py'
python3 -m unittest discover -s tests
```

## Data Hygiene

Do not commit runtime state, artifacts, browser profiles, logs, private URLs,
thread IDs, local machine paths, or secrets. Keep fixtures under `examples/`
sanitized and deterministic.

Before opening a PR that touches release packaging or public docs, run:

```bash
git ls-files --cached --others --exclude-standard -z | xargs -0 python3 scripts/check_release_artifacts.py
```

## Pull Requests

Include:

- the behavior change
- tests run
- any skipped checks and why
- whether `BRIDGE_HOME`/`CODEX_HOME` behavior changed
- screenshots or short notes for UI/control-panel changes
- links to related issues when applicable

Keep PRs focused. A smaller change with a clear test is easier to review than a
large sweep that mixes behavior, formatting, and docs.

## Security Reports

Do not open public issues with secrets, private URLs, logs, browser profile
data, thread IDs, or runtime artifacts. Follow [SECURITY.md](SECURITY.md).
