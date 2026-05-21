# Changelog

## Unreleased

- Prepare the repository for public GitHub collaboration with expanded README
  status notes, clearer contribution guidance, and a project code of conduct.
- Add a public release checklist covering artifact scanning, Git history
  hygiene, GitHub readiness, and WIP UI release-note expectations.
- Stop tracking private maintainer docs in the public source tree; keep
  `docs/private/` ignored and excluded from release archives.
- Include the small Bridge Control Panel app launcher in the public source tree
  so clone-based tests and the optional local UI entrypoint work out of the box.
- Harden release artifact scanning so plain file and directory scans preserve
  path context instead of checking only basenames.
- Harden the optional control-panel HTTP handler so malformed or oversized POST
  bodies return controlled JSON errors instead of aborting the request handler.
- Add Python 3.14 to package classifiers and CI/release validation matrices,
  with job timeouts for safer GitHub Actions runs.
- Add `docs/CURRENT_STATE.md` as a refreshed source tree, runtime-boundary,
  cleanup, pressure-point, and verification snapshot.
- Extend runtime cleanup to prune generated `.dual-graph/` cache data and local
  `.coa/codesearch` indexes/logs while preserving durable runtime truth.
- Align `.coa/` ignores, source distribution pruning, and release artifact
  scanning so local code-search caches cannot leak into release bundles.
- Restore hard timeout behavior for the standalone `execute-codex` CLI path
  while leaving loop-managed stop/pause polling on the supervised path.

## 0.1.0 - 2026-05-18

- Add the public `codex-bridge` CLI alongside the legacy `bridgectl` alias.
- Add `install`, `doctor`, `self-test`, `snapshot`, and `uninstall` lifecycle commands.
- Move default runtime state and artifacts out of the source checkout and under `BRIDGE_HOME`.
- Bundle prompt templates and the Codex skill as package resources.
- Remove tracked runtime `state/` and `artifacts/` material from the public repo surface.
- Add sanitized examples under `examples/`.
