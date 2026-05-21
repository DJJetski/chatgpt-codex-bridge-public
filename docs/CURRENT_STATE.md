# Current Project State

Last refreshed: 2026-05-21.

This document is a sanitized, source-controlled snapshot of the repository's
current structure, runtime boundary, known pressure points, and cleanup posture.
It intentionally avoids private ChatGPT URLs, Codex thread IDs, raw runtime
artifacts, local transcripts, credentials, and machine-specific secrets.

## Summary

`chatgpt-codex-bridge` is a Python 3.11+ local-first supervisor package. The
current default architecture is Supervisor V2: a terminal-first kernel that
coordinates ChatGPT planning and Codex execution through explicit worker
contracts, SQLite state under `BRIDGE_HOME`, and inspectable artifacts under
`BRIDGE_HOME`.

The repository has two major code eras:

- Supervisor V2, which is the current product path.
- Legacy browser/control-panel orchestration, which remains for compatibility,
  operator workflows, and historical regression coverage.

The public release boundary is strict. Source archives and wheels must not
contain checkout-local runtime state, private maintainer docs, browser profiles,
SQLite databases, logs, thread IDs, or secret-like material.

## Inventory

Observed at refresh time:

- 109 tracked files after adding the public release checklist, code of conduct,
  and small Bridge Control Panel launcher bundle.
- 44 Python implementation files under `src/chatgpt_codex_bridge/`.
- 1 compatibility package file under `src/mastermind_bridge/`.
- 19 unittest files under `tests/`.
- 12 public Markdown docs under `docs/`.
- About 21.4k implementation lines under `src/`.
- About 23.5k test lines under `tests/`.

The checkout also contained ignored local runtime and cache paths. These are not
public source truth:

- `artifacts/`: local run artifacts and audit evidence, about 2.3 GB.
- `state/`: local runtime state, browser-profile state, prompts, and databases,
  about 30 MB.
- `.coa/codesearch/`: local code-search index cache, about 276 MB.
- `.dual-graph/`: generated graph cache, about 55 MB.
- `.dual-graph-context/`: ignored local context templates and packs.
- `dist/`: local release artifacts, about 816 KB.
- `docs/private/`: ignored local maintainer notes, not public source truth.
- `logs/`, `session_logs/`, root handoff files, and the local control-panel app
  bundle.

## Source Tree

### Root

- `README.md` is the public install, CLI, profiles, and safety entrypoint.
- `CODE_OF_CONDUCT.md` is the public collaboration conduct file.
- `pyproject.toml` owns package metadata, package discovery, optional extras,
  console scripts, and package data.
- `MANIFEST.in` prunes runtime/private material from source distributions.
- `.gitignore` and `.ignore` keep generated runtime state out of Git and
  default search surfaces.
- `.github/` contains CI, release, CodeQL, Dependabot, issue templates, and the
  pull request template.
- `docs/PUBLIC_RELEASE_CHECKLIST.md` is the public release and Git history
  readiness checklist.
- `scripts/` contains the installer, release builder, and release artifact
  scanner.
- `examples/` contains sanitized deterministic fixtures only.

### Public Package

`src/chatgpt_codex_bridge/` is the canonical implementation package.

- `cli.py` dispatches product lifecycle, legacy orchestration, cleanup, and V2
  commands.
- `app_paths.py` owns `BRIDGE_HOME`, `CODEX_HOME`, and bridge directory
  resolution.
- `profiles.py` defines explicit runtime profiles and dangerous sandbox opt-in
  gating.
- `lifecycle.py` implements install, doctor, self-test, snapshot, and uninstall.
- `launching.py` builds and applies launch plans.
- `desktop_launcher.py` starts the optional local desktop/control-panel surface.
- `runtime_cleanup.py` safely prunes reproducible local caches without deleting
  durable runtime truth.
- `codex_capabilities.py` describes Codex exec capability guidance for generated
  worker prompts.
- `policy.py`, `prompting.py`, `storage.py`, `models.py`, and `defaults.py`
  provide the public shared model, prompt, registry, and decision helpers.
- `prompts/` contains packaged prompt templates.
- `resources/` contains the bundled Codex skill and agent metadata installed by
  lifecycle commands.

### Supervisor V2

`src/chatgpt_codex_bridge/v2/` is the primary architecture.

- `types.py` defines session, turn, event, lease, ChatGPT result, and Codex
  result records.
- `store.py` is the SQLite persistence layer and schema migration owner.
- `kernel.py` owns session/turn sequencing, worker leases, idempotency, and
  recovery semantics.
- `workers.py` validates strict ChatGPT/Codex worker result payloads and runs
  worker-side integration code.
- `cli.py` wires `codex-bridge v2 session ...` commands.

The V2 invariant is that the kernel is the only sequencing authority. ChatGPT
and Codex do not directly coordinate with each other; all progress goes through
persisted state, artifacts, and validated worker outputs.

### Legacy Orchestrator

`src/chatgpt_codex_bridge/orchestrator/` is the legacy browser/control-panel
surface. It is still useful and well-covered, but it is not the default
correctness path.

- `loop.py` and `loop_support.py` run the browser-mediated loop and recovery
  heuristics.
- `contracts.py` renders ChatGPT/Codex role contracts and prompt boundaries.
- `packets.py` builds and renders return packets.
- `state.py` handles legacy JSON session, binding, and policy files.
- `supervisor.py` owns legacy session locks and supervisor lifecycle helpers.
- `control_panel.py` and `control_panel_view.py` implement the optional local
  control panel.
- `browser.py`, `browser_applescript.py`, `browser_playwright.py`, and
  `browser_support.py` provide browser adapters and support functions.
- `models.py`, `control.py`, and `policy.py` hold legacy dataclasses, control
  envelopes, and loop policy helpers.

Large legacy files are expected. They should be changed carefully and with
focused regression tests rather than broad style refactors.

### Compatibility Package

`src/mastermind_bridge/__init__.py` keeps legacy imports such as
`mastermind_bridge.cli` working by pointing the compatibility package path at
the canonical `chatgpt_codex_bridge` implementation directory. This is
intentional and release-critical because older tests and callers still import
through the legacy namespace.

## Test Map

Release-critical focused suites:

- `tests/test_product_lifecycle.py`: install, doctor, self-test, resource, and
  packaging lifecycle behavior.
- `tests/test_v2.py`: V2 sessions, kernel behavior, CLI behavior, and worker
  validation.
- `tests/test_runtime_hygiene.py`: cleanup and generated-state preservation.

Legacy and compatibility suites:

- `tests/test_cli.py`: top-level CLI and legacy command behavior.
- `tests/test_executor.py` and `tests/test_executor_resume.py`: Codex exec,
  app-server, event parsing, timeout, and resume behavior.
- `tests/test_default_loop_simplified.py` and `tests/test_loop.py`: legacy loop
  behavior and recovery.
- `tests/test_browser_adapter.py`: AppleScript/Playwright browser adapters.
- `tests/test_control.py`, `tests/test_control_panel.py`,
  `tests/test_orchestrator.py`, and `tests/test_supervisor.py`: control
  envelopes, panel behavior, sessions, locks, and policies.
- `tests/test_prompt_contracts.py`, `tests/test_prompting.py`,
  `tests/test_policy.py`, `tests/test_live_monitor.py`,
  `tests/test_desktop_launcher.py`, and `tests/test_release_artifact_scan.py`:
  prompt, decision, monitoring, launcher, and release hygiene behavior.

## Runtime And Cleanup Boundary

Do not treat a clean-looking checkout as more important than preserving
inspectable runtime truth. The cleanup boundary is:

Safe to prune by default:

- `.DS_Store`.
- Python bytecode and cache directories.
- `.mypy_cache`, `.pytest_cache`, and `.ruff_cache`.
- Browser engine caches under `state/playwright-profile`.
- Root-level generated runtime logs under `artifacts/*.log`.
- Generated graph/cache indexes under `.dual-graph/` and
  `.coa/codesearch/{indexes,logs}`.

Preserve by default:

- `state/session_locks/` unless liveness checks prove a specific lock is stale.
- `state/sessions/`, V2 SQLite databases, and durable state files.
- `config/`, especially operator policy files.
- `artifacts/runs/` and run reports, because they are audit and recovery
  evidence.
- `.dual-graph-context/`, because it is the ignored local context layer rather
  than the generated graph cache.
- `dist/` unless the task is explicitly rebuilding or discarding local release
  artifacts.

Use:

```bash
codex-bridge cleanup-runtime-state --dry-run
codex-bridge cleanup-runtime-state
```

The dry run should be inspected before pruning when there is any doubt.

## Current Pressure Points

- `cli.py`, `executor.py`, `orchestrator/loop.py`,
  `orchestrator/control_panel.py`, and `orchestrator/browser_applescript.py`
  are intentionally large and behavior-dense. Prefer focused changes with
  targeted tests over structural churn.
- V2 and legacy paths coexist. New default-runtime behavior belongs in
  `src/chatgpt_codex_bridge/v2/`; legacy browser/control-panel changes belong
  under `src/chatgpt_codex_bridge/orchestrator/`.
- The compatibility namespace is deliberate. Do not remove
  `src/mastermind_bridge/__init__.py` or assume tests should stop importing
  `mastermind_bridge.*` until a migration is explicitly planned.
- The optional control panel is local-only by default and rejects malformed or
  oversized POST bodies with controlled JSON errors. It is still a rough
  compatibility UI, not the default product path.
- Release safety depends on both `.gitignore`/`MANIFEST.in` and
  `scripts/check_release_artifacts.py`. Keep all three aligned when new local
  generated paths are introduced. The scanner supports archive, plain-file, and
  directory preflight scans.
- `core-safe` must remain the default profile. Browser, control-panel,
  macOS-app integration, and dangerous sandbox bypass remain explicit opt-ins.

## Verification Baseline

Fresh focused checks run during this refresh:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_runtime_hygiene.py'
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_product_lifecycle.py'
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_v2.py'
PYTHONPATH=src python3 -m compileall -q src tests
PYTHONPATH=src python3 -m unittest discover -s tests
```

Before release, run:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
sh scripts/build_release_artifacts.sh
(cd dist && shasum -a 256 -c SHA256SUMS)
```

## Practical Rules For Future Work

- Start from `README.md`, `docs/README.md`, `docs/ARCHITECTURE.md`, this
  document, and the specific code path being changed.
- Keep runtime state under `BRIDGE_HOME`, not in the source checkout.
- Keep public docs public-safe. Private maintainer decisions stay under
  ignored local `docs/private/` and are excluded from release archives.
- Do not collapse V2 and legacy concerns just to reduce file count.
- Prefer small, verified commits over broad tidy-up sweeps.
- Update this document when a durable structure, boundary, or validation
  expectation changes.
