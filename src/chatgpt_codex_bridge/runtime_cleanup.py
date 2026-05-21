from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

_EXPLICIT_RUNTIME_CACHE_PATHS = (
    Path(".dual-graph"),
    Path(".coa/codesearch/indexes"),
    Path(".coa/codesearch/logs"),
    Path("state/playwright-profile/BrowserMetrics"),
    Path("state/playwright-profile/Default/Cache"),
    Path("state/playwright-profile/Default/Code Cache"),
    Path("state/playwright-profile/Default/DawnGraphiteCache"),
    Path("state/playwright-profile/Default/GPUCache"),
    Path("state/playwright-profile/GraphiteDawnCache"),
    Path("state/playwright-profile/GrShaderCache"),
)
_RUNTIME_CACHE_GLOBS = (
    "**/__pycache__",
    "**/.mypy_cache",
    "**/.pytest_cache",
    "**/.ruff_cache",
    "**/.DS_Store",
    "artifacts/*.log",
    "**/*.pyc",
    "**/*.pyo",
)


@dataclass(slots=True)
class RuntimeCleanupResult:
    repo_root: str
    dry_run: bool
    matched_paths: list[str]
    removed_paths: list[str]
    bytes_reclaimed: int

    def as_dict(self) -> dict[str, object]:
        return {
            "repo_root": self.repo_root,
            "dry_run": self.dry_run,
            "matched_paths": list(self.matched_paths),
            "removed_paths": list(self.removed_paths),
            "bytes_reclaimed": self.bytes_reclaimed,
        }


def cleanup_runtime_state(repo_root: Path, *, dry_run: bool = False) -> RuntimeCleanupResult:
    root = repo_root.resolve()
    targets = _cleanup_targets(root)
    matched_paths: list[str] = []
    removed_paths: list[str] = []
    bytes_reclaimed = 0

    for path in targets:
        relative = path.relative_to(root).as_posix()
        matched_paths.append(relative)
        bytes_reclaimed += _path_size(path)
        if dry_run:
            continue
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                path.unlink()
            except FileNotFoundError:
                continue
        removed_paths.append(relative)

    return RuntimeCleanupResult(
        repo_root=str(root),
        dry_run=dry_run,
        matched_paths=matched_paths,
        removed_paths=removed_paths,
        bytes_reclaimed=bytes_reclaimed,
    )


def _cleanup_targets(repo_root: Path) -> list[Path]:
    candidates: list[Path] = []
    for relative_path in _EXPLICIT_RUNTIME_CACHE_PATHS:
        candidate = repo_root / relative_path
        if candidate.exists():
            candidates.append(candidate)
    for pattern in _RUNTIME_CACHE_GLOBS:
        candidates.extend(path for path in repo_root.glob(pattern) if path.exists())

    unique_candidates = sorted(
        {path.resolve() for path in candidates if path.exists()},
        key=lambda item: (len(item.parts), str(item)),
    )
    selected: list[Path] = []
    for candidate in unique_candidates:
        if any(parent == candidate or parent in candidate.parents for parent in selected):
            continue
        selected.append(candidate)
    return selected


def _path_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for child in path.rglob("*"):
        if not child.is_file():
            continue
        try:
            total += child.stat().st_size
        except OSError:
            continue
    return total
