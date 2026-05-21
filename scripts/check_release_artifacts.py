#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable

_PRIVATE_HOME_RE = re.compile(rb"/Users/[A-Za-z0-9._-]+")
_THREAD_ID_RE = re.compile(rb"\b019[0-9a-f]{5}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_SECRET_RE = re.compile(
    rb"(sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{20,}|-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----)"
)
_SQLITE_SIDECAR_RE = re.compile(r"\.(?:sqlite|sqlite3|db)(?:-|$)")

_RUNTIME_PARTS = {
    ".coa",
    ".dual-graph",
    ".dual-graph-context",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "artifacts",
    "config",
    "session_logs",
    "state",
}
_PRIVATE_TOP_LEVEL_FILES = {
    "AGENTS.md",
    "CODEX.md",
    "DECISIONS.md",
    "EXECUTION_LOG.md",
    "HANDOFF.md",
    "MASTER_PLAN.md",
    "NEW_PLAN.md",
    "NEXT_PROMPT.md",
    "RETURN_TO_MASTERMIND.md",
    "START_CYCLE.md",
}
_BLOCKED_SUFFIXES = (
    ".db",
    ".env",
    ".key",
    ".log",
    ".pem",
    ".pyc",
    ".pyo",
    ".secret",
    ".sqlite",
    ".sqlite3",
    ".token",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan release artifacts for private runtime material.")
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    violations: list[dict[str, str]] = []
    for path in args.paths:
        violations.extend(scan_path(path))

    if args.json:
        print(json.dumps({"ok": not violations, "violations": violations}, indent=2, sort_keys=True))
    elif violations:
        for item in violations:
            print(f"{item['path']}: {item['reason']}", file=sys.stderr)

    return 1 if violations else 0


def scan_path(path: Path) -> list[dict[str, str]]:
    if path.is_dir():
        return list(_scan_directory(path))
    if zipfile.is_zipfile(path):
        return list(_scan_zip(path))
    if tarfile.is_tarfile(path):
        return list(_scan_tar(path))
    data = path.read_bytes()
    return _entry_violations(str(path), _plain_entry_name(path), data)


def _scan_directory(path: Path) -> Iterable[dict[str, str]]:
    for child in sorted(item for item in path.rglob("*") if item.is_file()):
        entry_name = child.relative_to(path).as_posix()
        yield from _entry_violations(str(child), entry_name, child.read_bytes())


def _scan_zip(path: Path) -> Iterable[dict[str, str]]:
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            entry_name = info.filename
            data = archive.read(info)
            yield from _entry_violations(f"{path}!{entry_name}", entry_name, data)


def _scan_tar(path: Path) -> Iterable[dict[str, str]]:
    with tarfile.open(path) as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            extracted = archive.extractfile(member)
            if extracted is None:
                continue
            entry_name = member.name
            yield from _entry_violations(f"{path}!{entry_name}", entry_name, extracted.read())


def _entry_violations(display_path: str, entry_name: str, data: bytes) -> list[dict[str, str]]:
    violations: list[dict[str, str]] = []
    blocked_reason = _blocked_entry_reason(entry_name)
    if blocked_reason:
        violations.append({"path": display_path, "reason": blocked_reason})
    if _PRIVATE_HOME_RE.search(data):
        violations.append({"path": display_path, "reason": "private_home_path"})
    if _THREAD_ID_RE.search(data):
        violations.append({"path": display_path, "reason": "codex_thread_id"})
    if _SECRET_RE.search(data):
        violations.append({"path": display_path, "reason": "secret_like_material"})
    return violations


def _blocked_entry_reason(entry_name: str) -> str:
    parts = _public_parts(entry_name)
    if any(part in _RUNTIME_PARTS for part in parts):
        return "runtime_path"
    if _is_private_doc_path(parts):
        return "private_doc_path"
    basename = parts[-1] if parts else entry_name
    if len(parts) == 1 and basename in _PRIVATE_TOP_LEVEL_FILES:
        return "private_control_file"
    if basename == ".DS_Store":
        return "macos_metadata"
    if basename == ".env" or basename.startswith(".env."):
        return "env_file"
    if _SQLITE_SIDECAR_RE.search(basename):
        return "blocked_suffix"
    if basename.endswith(_BLOCKED_SUFFIXES):
        return "blocked_suffix"
    return ""


def _is_private_doc_path(parts: tuple[str, ...]) -> bool:
    return any(
        parts[index] == "docs" and parts[index + 1] == "private"
        for index in range(0, max(len(parts) - 1, 0))
    )


def _public_parts(entry_name: str) -> tuple[str, ...]:
    parts = tuple(part for part in PurePosixPath(entry_name).parts if part not in {"", ".", "/"})
    for index, part in enumerate(parts):
        if part.startswith("chatgpt-codex-bridge-"):
            return parts[index + 1 :]
    return parts


def _plain_entry_name(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


if __name__ == "__main__":
    raise SystemExit(main())
