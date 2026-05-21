# Public Release Checklist

Use this checklist before making a GitHub repository public, cutting a release
tag, or sharing release artifacts.

## Required Local Checks

```bash
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
git ls-files --cached --others --exclude-standard -z | xargs -0 python3 scripts/check_release_artifacts.py
sh scripts/build_release_artifacts.sh
(cd dist && shasum -a 256 -c SHA256SUMS)
```

`scripts/build_release_artifacts.sh` must run from a clean Git checkout unless
`BRIDGE_RELEASE_ALLOW_DIRTY=1` is deliberately set for local diagnostics. Do
not publish artifacts produced from a dirty checkout.

## Public Source Boundary

The public source tree must not include:

- `state/`, `artifacts/`, `config/`, `logs/`, or `session_logs/`
- browser profiles, SQLite databases, sidecars, or generated caches
- private ChatGPT URLs or Codex/ChatGPT thread IDs
- local home paths or machine-specific file paths
- API keys, tokens, cookies, private keys, or credential material
- private maintainer notes under `docs/private/`

The release scanner checks archives, plain files, and directories for these
classes of material. Keep `.gitignore`, `.gitattributes`, `MANIFEST.in`, and
`scripts/check_release_artifacts.py` aligned when new generated paths are added.

## Git History Boundary

A clean current tree is not enough for a public GitHub repository. Git history
must also be safe.

Before changing repository visibility to public, verify history with targeted
searches for private paths, runtime directories, thread IDs, and secret-like
patterns. If older commits contain private runtime artifacts or local paths,
use one of these release paths:

- Create a fresh public repository or orphan public branch from the sanitized
  current tree.
- Rewrite private history in the existing repository, then force-push only
  after maintainers explicitly accept the coordination cost.

Do not make a repository public while unsafe historical commits remain
reachable from the default branch.

## GitHub Readiness

The repository should have:

- `README.md` with install, status, safety, and contribution guidance
- `CONTRIBUTING.md`
- `CODE_OF_CONDUCT.md`
- `SECURITY.md`
- issue templates and a pull request template
- CI, CodeQL, release artifact scanning, and Dependabot
- branch protection or a ruleset for the default branch before accepting
  external contributions

Suggested default branch protection for a public repo:

- require pull requests before merging
- require CI to pass
- require conversation resolution
- block force pushes and branch deletion

## Release Notes

Use `CHANGELOG.md` as the source for release notes. The first public release
should be explicit that:

- Supervisor V2 is the default supported path.
- The core ChatGPT-to-Codex loop works.
- The browser/control-panel UI is rough, operator-oriented, and not yet
  polished.
- Contributions are welcome through issues and pull requests.
