# Security Policy

## Supported Versions

Security fixes target the latest released `0.x` version.

## Reporting

Do not open public issues containing secrets, tokens, ChatGPT URLs, private thread IDs, browser profile contents, local logs, or runtime artifacts.

Report privately to the repository maintainer or through GitHub private vulnerability reporting when it is enabled.

If you accidentally include private data in an issue or pull request, delete or
redact it immediately and notify the maintainer privately. Do not quote the
secret or private URL again in follow-up comments.

## Local Data Boundary

Bridge runtime data belongs under `BRIDGE_HOME`; Codex integration data belongs under `CODEX_HOME`.

The public repository must not contain:

- `state/`
- `artifacts/`
- `config/`
- browser profiles
- local logs
- credentials or tokens
- private ChatGPT URLs
- private Codex or ChatGPT thread IDs

Run `codex-bridge snapshot --json` for a redacted support snapshot.

## Public Release Boundary

Release archives are scanned before publication. The scanner rejects private
docs, runtime paths, local home paths, SQLite databases and sidecars, logs,
thread IDs, and common secret patterns.

For local checks, run:

```bash
git ls-files --cached --others --exclude-standard -z | xargs -0 python3 scripts/check_release_artifacts.py
```
