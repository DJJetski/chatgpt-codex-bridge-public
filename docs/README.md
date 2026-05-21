# Documentation Map

Use this map to find the right document without mixing public user material
with private maintainer state.

## Public User And Contributor Docs

- `README.md` - install, CLI, profiles, safety boundary, and basic development
  commands.
- `CHANGELOG.md` - user-visible release changes.
- `CODE_OF_CONDUCT.md` - expectations for public collaboration.
- `SECURITY.md` - supported versions, reporting path, and local data boundary.
- `CONTRIBUTING.md` - setup, checks, data hygiene, and pull request guidance.
- `docs/ARCHITECTURE.md` - current public architecture summary.
- `docs/CURRENT_STATE.md` - refreshed source tree, runtime-boundary, cleanup,
  pressure-point, and verification snapshot.
- `docs/THREAD_POLICY.md` - Supervisor V2 session and thread semantics.
- `docs/WORKFLOW_SIMULATION.md` - sanitized example workflow using fixtures.
- `docs/DEVELOPMENT.md` - contributor setup and local validation guidance.
- `docs/PUBLIC_RELEASE_CHECKLIST.md` - public release, artifact, Git history,
  and GitHub readiness checklist.
- `docs/REPO_MAP.md` - source tree and release-surface map.

## Private Maintainer Docs

Private maintainer notes may exist locally under `docs/private/`. That path is
ignored by Git and excluded from public release archives. Do not add it back to
the public repository unless every file and relevant Git history has been
reviewed for public release.

## Ignored Local State

The checkout may contain ignored local files such as `AGENTS.md`, `CODEX.md`,
`EXECUTION_LOG.md`, `HANDOFF.md`, `NEXT_PROMPT.md`, `START_CYCLE.md`,
`state/`, `artifacts/`, and `config/`. Treat them as private operator state.
They are not source truth for public users and must not be committed by default.
