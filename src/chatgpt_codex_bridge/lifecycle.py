from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any

from . import __version__
from .app_paths import (
    APP_NAME,
    bridge_artifacts_dir,
    bridge_config_dir,
    bridge_home,
    bridge_install_dir,
    bridge_state_dir,
    codex_home,
    ensure_bridge_dirs,
    redact_path,
)
from .profiles import profile_payload
from .prompting import available_prompt_templates
from .v2.store import V2Store

SKILL_NAME = "chatgpt-codex-bridge"
MANIFEST_VERSION = 1
SENTINEL_NAME = ".chatgpt-codex-bridge-home"
_PRIVATE_HOME_PATH_RE = re.compile(r"/Users/[^\s\"'<>:]+")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)([\"']?\b(?:api[_-]?key|token|secret|password)[\"']?\s*[:=]\s*)[\"']?[^\s,\"']+"
)
_BEARER_TOKEN_RE = re.compile(r"(?i)\b(Authorization\s*:\s*Bearer\s+)[\"']?[^\s,\"']+")


@dataclass(slots=True)
class LifecycleResult:
    status: str
    payload: dict[str, Any]
    exit_code: int = 0


def install_bridge(
    *,
    bridge_home_path: Path | None = None,
    codex_home_path: Path | None = None,
    prefix: Path | None = None,
    profile: str = "core-safe",
    dry_run: bool = False,
    force: bool = False,
) -> LifecycleResult:
    resolved_bridge_home = _resolved_bridge_home(bridge_home_path)
    resolved_codex_home = _resolved_codex_home(codex_home_path)
    skill_target = resolved_codex_home / "skills" / SKILL_NAME
    manifest_path = _manifest_path(resolved_bridge_home)
    actions: list[dict[str, Any]] = []

    if skill_target.exists() and not force and not _manifest_owns_path(manifest_path, skill_target):
        return LifecycleResult(
            "blocked",
            {
                "error": "skill_target_exists",
                "skill_target": _safe_path(skill_target),
                "hint": "Pass --force only if this bridge installation should replace that skill.",
            },
            2,
        )

    actions.append({"action": "ensure_dir", "path": _safe_path(resolved_bridge_home)})
    actions.append({"action": "install_skill", "target": _safe_path(skill_target)})
    actions.append({"action": "write_manifest", "path": _safe_path(manifest_path)})
    if prefix is not None:
        actions.append({"action": "record_prefix", "path": _safe_path(prefix)})

    if dry_run:
        return LifecycleResult(
            "dry_run",
            _base_payload(resolved_bridge_home, resolved_codex_home)
            | {"profile": profile, "profile_policy": profile_payload(profile), "actions": actions},
        )

    install_tmp: Path | None = None
    backup_target: Path | None = None
    old_manifest_text = manifest_path.read_text(encoding="utf-8") if manifest_path.exists() else None
    sentinel_path = resolved_bridge_home / SENTINEL_NAME
    old_sentinel_existed = sentinel_path.exists()
    old_sentinel_text = sentinel_path.read_text(encoding="utf-8") if old_sentinel_existed else ""
    target_installed = False
    sentinel_written = False

    try:
        for path in (
            resolved_bridge_home,
            resolved_bridge_home / "state",
            resolved_bridge_home / "config",
            resolved_bridge_home / "artifacts",
            resolved_bridge_home / "install",
            resolved_bridge_home / "logs",
            skill_target.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)

        install_tmp = Path(tempfile.mkdtemp(prefix="install-", dir=resolved_bridge_home / "install"))
        staged_skill = install_tmp / SKILL_NAME
        _copy_traversable_tree(_bundled_skill_root(), staged_skill)

        if skill_target.exists():
            backup_root = resolved_bridge_home / "install" / "backups"
            backup_root.mkdir(parents=True, exist_ok=True)
            backup_target = backup_root / f"{SKILL_NAME}-{datetime.now().astimezone().strftime('%Y%m%d%H%M%S')}"
            shutil.move(str(skill_target), str(backup_target))

        shutil.move(str(staged_skill), str(skill_target))
        target_installed = True
        sentinel_path.write_text(f"{APP_NAME}\n", encoding="utf-8")
        sentinel_written = True

        files = _manifest_file_entries(skill_target)
        files.append({"path": str(sentinel_path), "sha256": _sha256_file(sentinel_path), "kind": "home_sentinel"})
        manifest = {
            "manifest_version": MANIFEST_VERSION,
            "app_name": APP_NAME,
            "version": __version__,
            "installed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "profile": profile,
            "bridge_home": str(resolved_bridge_home),
            "codex_home": str(resolved_codex_home),
            "prefix": str(prefix) if prefix is not None else "",
            "files": files,
            "backups": (
                [{"path": str(backup_target), "kind": "previous_skill"}]
                if backup_target is not None
                else []
            ),
        }
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(manifest_path, manifest)
    except Exception:
        if target_installed and skill_target.exists():
            shutil.rmtree(skill_target)
        if backup_target is not None and backup_target.exists():
            skill_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(backup_target), str(skill_target))
        if sentinel_written:
            if old_sentinel_existed:
                sentinel_path.write_text(old_sentinel_text, encoding="utf-8")
            else:
                sentinel_path.unlink(missing_ok=True)
        if old_manifest_text is None:
            manifest_path.unlink(missing_ok=True)
        else:
            manifest_path.write_text(old_manifest_text, encoding="utf-8")
        raise
    finally:
        if install_tmp is not None and install_tmp.exists():
            shutil.rmtree(install_tmp, ignore_errors=True)
    return LifecycleResult(
        "installed",
        _base_payload(resolved_bridge_home, resolved_codex_home)
        | {
            "profile": profile,
            "profile_policy": profile_payload(profile),
            "manifest": _safe_path(manifest_path),
            "skill_target": _safe_path(skill_target),
            "installed_files": len(files),
        },
    )


def uninstall_bridge(
    *,
    bridge_home_path: Path | None = None,
    dry_run: bool = False,
    purge: bool = False,
) -> LifecycleResult:
    resolved_bridge_home = _resolved_bridge_home(bridge_home_path)
    manifest_path = _manifest_path(resolved_bridge_home)
    if not manifest_path.exists():
        return LifecycleResult(
            "blocked",
            {"error": "manifest_missing", "manifest": _safe_path(manifest_path)},
            2,
        )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if purge:
        purge_block = _purge_block_reason(resolved_bridge_home, manifest)
        if purge_block:
            return LifecycleResult(
                "blocked",
                {
                    "error": "unsafe_purge_target",
                    "reason": purge_block,
                    "bridge_home": _safe_path(resolved_bridge_home),
                    "manifest": _safe_path(manifest_path),
                },
                2,
            )
    removals: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for entry in manifest.get("files", []):
        path = Path(str(entry.get("path", ""))).expanduser()
        expected_hash = str(entry.get("sha256", ""))
        if not path.exists():
            continue
        if path.is_symlink():
            skipped.append({"path": _safe_path(path), "reason": "symlink"})
            continue
        if path.is_file() and _sha256_file(path) != expected_hash:
            skipped.append({"path": _safe_path(path), "reason": "hash_mismatch"})
            continue
        removals.append({"path": _safe_path(path)})
        if not dry_run:
            path.unlink()

    if not dry_run:
        _remove_empty_parents(manifest.get("files", []), stop_at=Path(str(manifest.get("codex_home", ""))))
        manifest_path.unlink(missing_ok=True)
        _remove_empty_tree(manifest_path.parent, stop_at=resolved_bridge_home)
        if purge and resolved_bridge_home.exists():
            shutil.rmtree(resolved_bridge_home)

    return LifecycleResult(
        "dry_run" if dry_run else "uninstalled",
        {
            "bridge_home": _safe_path(resolved_bridge_home),
            "manifest": _safe_path(manifest_path),
            "purge": purge,
            "removed": removals,
            "skipped": skipped,
        },
        1 if skipped else 0,
    )


def doctor_bridge(
    *,
    bridge_home_path: Path | None = None,
    codex_home_path: Path | None = None,
    profile: str | None = None,
    redact: bool = True,
) -> LifecycleResult:
    resolved_bridge_home = _resolved_bridge_home(bridge_home_path)
    resolved_codex_home = _resolved_codex_home(codex_home_path)
    profile_policy = profile_payload(profile, bridge_home_path=resolved_bridge_home)
    checks = [
        _check_python_version(),
        _check_path_writable(resolved_bridge_home),
        _check_prompt_resources(),
        _check_codex_cli(),
        _check_codex_home(resolved_codex_home),
        _check_skill_installation(resolved_codex_home),
    ]
    ok = all(check["status"] in {"ok", "warning"} for check in checks)
    payload = _base_payload(resolved_bridge_home, resolved_codex_home, redact=redact) | {
        "profile": profile_policy["name"],
        "profile_policy": profile_policy,
        "checks": checks if not redact else [_redact_check(check) for check in checks],
    }
    return LifecycleResult("ok" if ok else "failed", payload, 0 if ok else 1)


def self_test_bridge(
    *,
    bridge_home_path: Path | None = None,
    codex_home_path: Path | None = None,
) -> LifecycleResult:
    resolved_bridge_home = _resolved_bridge_home(bridge_home_path)
    resolved_codex_home = _resolved_codex_home(codex_home_path)
    checks: list[dict[str, Any]] = []
    try:
        templates = available_prompt_templates()
        checks.append({"name": "prompt_resources", "status": "ok", "count": len(templates)})
    except Exception as exc:  # pragma: no cover - defensive report path
        checks.append({"name": "prompt_resources", "status": "failed", "error": str(exc)})

    try:
        skill_root = _bundled_skill_root()
        skill_file = skill_root.joinpath("SKILL.md")
        if not skill_file.is_file():
            raise FileNotFoundError("bundled SKILL.md missing")
        checks.append({"name": "bundled_skill", "status": "ok"})
    except Exception as exc:  # pragma: no cover - defensive report path
        checks.append({"name": "bundled_skill", "status": "failed", "error": str(exc)})

    try:
        with tempfile.TemporaryDirectory(prefix="bridge-self-test-") as tmp_dir:
            db_path = Path(tmp_dir) / "state" / "supervisor_v2.sqlite3"
            V2Store(db_path)
            checks.append({"name": "v2_store_init", "status": "ok"})
    except Exception as exc:  # pragma: no cover - defensive report path
        checks.append({"name": "v2_store_init", "status": "failed", "error": str(exc)})

    manifest_check = _check_manifest_integrity(_manifest_path(resolved_bridge_home))
    if manifest_check is not None:
        checks.append(manifest_check)

    ok = all(check["status"] == "ok" for check in checks)
    return LifecycleResult(
        "ok" if ok else "failed",
        _base_payload(resolved_bridge_home, resolved_codex_home) | {"checks": checks},
        0 if ok else 1,
    )


def snapshot_bridge(
    *,
    bridge_home_path: Path | None = None,
    codex_home_path: Path | None = None,
    redact: bool = True,
) -> LifecycleResult:
    resolved_bridge_home = _resolved_bridge_home(bridge_home_path)
    resolved_codex_home = _resolved_codex_home(codex_home_path)
    payload = _base_payload(resolved_bridge_home, resolved_codex_home, redact=redact) | {
        "version": __version__,
        "python": sys.version.split()[0],
        "git": _git_snapshot(redact=redact),
        "environment": {
            "BRIDGE_HOME": _safe_path(os.environ.get("BRIDGE_HOME", ""), redact=redact),
            "CODEX_HOME": _safe_path(os.environ.get("CODEX_HOME", ""), redact=redact),
        },
    }
    return LifecycleResult("ok", payload)


def render_lifecycle_result(result: LifecycleResult, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps({"status": result.status, **result.payload}, indent=2, sort_keys=True))
        return
    print(f"status: {result.status}")
    for key, value in result.payload.items():
        if isinstance(value, (dict, list)):
            print(f"{key}: {json.dumps(value, sort_keys=True)}")
        else:
            print(f"{key}: {value}")


def _resolved_bridge_home(path: Path | None) -> Path:
    if path is not None:
        return Path(path).expanduser()
    return bridge_home()


def _resolved_codex_home(path: Path | None) -> Path:
    if path is not None:
        return Path(path).expanduser()
    return codex_home()


def _base_payload(
    resolved_bridge_home: Path,
    resolved_codex_home: Path,
    *,
    redact: bool = True,
) -> dict[str, Any]:
    return {
        "bridge_home": _safe_path(resolved_bridge_home, redact=redact),
        "codex_home": _safe_path(resolved_codex_home, redact=redact),
    }


def _safe_path(path: Path | str, *, redact: bool = True) -> str:
    if path == "":
        return ""
    return redact_path(path) if redact else str(path)


def _manifest_path(resolved_bridge_home: Path) -> Path:
    return resolved_bridge_home / "install" / "manifest.json"


def _bundled_skill_root() -> Traversable:
    return resources.files("chatgpt_codex_bridge.resources.skills").joinpath(SKILL_NAME)


def _copy_traversable_tree(source: Traversable, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        child_target = target / child.name
        if child.is_dir():
            _copy_traversable_tree(child, child_target)
        elif child.is_file():
            child_target.parent.mkdir(parents=True, exist_ok=True)
            child_target.write_bytes(child.read_bytes())


def _manifest_file_entries(root: Path) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        entries.append(
            {
                "path": str(path),
                "sha256": _sha256_file(path),
                "kind": "skill",
            }
        )
    return entries


def _manifest_owns_path(manifest_path: Path, target: Path) -> bool:
    if not manifest_path.exists():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    target = target.resolve()
    for entry in manifest.get("files", []):
        try:
            path = Path(str(entry.get("path", ""))).resolve()
        except OSError:
            continue
        if path == target or target in path.parents:
            return True
    return False


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def _check_manifest_integrity(manifest_path: Path) -> dict[str, Any] | None:
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"name": "install_manifest_integrity", "status": "failed", "error": str(exc)}

    missing: list[str] = []
    mismatched: list[str] = []
    for entry in manifest.get("files", []):
        path = Path(str(entry.get("path", ""))).expanduser()
        expected_hash = str(entry.get("sha256", ""))
        if not path.exists():
            missing.append(_safe_path(path))
        elif path.is_file() and expected_hash and _sha256_file(path) != expected_hash:
            mismatched.append(_safe_path(path))

    return {
        "name": "install_manifest_integrity",
        "status": "ok" if not missing and not mismatched else "failed",
        "manifest": _safe_path(manifest_path),
        "missing": missing[:10],
        "mismatched": mismatched[:10],
    }


def _purge_block_reason(resolved_bridge_home: Path, manifest: dict[str, Any]) -> str:
    try:
        bridge_home_resolved = resolved_bridge_home.expanduser().resolve()
    except OSError:
        return "bridge_home_not_resolvable"
    home_resolved = Path.home().resolve()
    if bridge_home_resolved in {Path(bridge_home_resolved.anchor), home_resolved}:
        return "bridge_home_is_too_broad"
    if manifest.get("app_name") != APP_NAME:
        return "manifest_app_mismatch"
    try:
        manifest_bridge_home = Path(str(manifest.get("bridge_home", ""))).expanduser().resolve()
    except OSError:
        return "manifest_bridge_home_not_resolvable"
    if manifest_bridge_home != bridge_home_resolved:
        return "manifest_bridge_home_mismatch"
    sentinel_path = bridge_home_resolved / SENTINEL_NAME
    if not sentinel_path.is_file():
        return "sentinel_missing"
    if sentinel_path.read_text(encoding="utf-8").strip() != APP_NAME:
        return "sentinel_mismatch"
    return ""


def _remove_empty_parents(entries: list[dict[str, Any]], *, stop_at: Path) -> None:
    paths = [Path(str(entry.get("path", ""))).parent for entry in entries]
    for path in sorted(paths, key=lambda item: len(item.parts), reverse=True):
        _remove_empty_tree(path, stop_at=stop_at)


def _remove_empty_tree(path: Path, *, stop_at: Path) -> None:
    current = path
    stop_at = stop_at.expanduser().resolve()
    while current.exists():
        try:
            resolved = current.resolve()
        except OSError:
            break
        if resolved == stop_at or stop_at not in resolved.parents:
            break
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _check_python_version() -> dict[str, Any]:
    status = "ok" if sys.version_info >= (3, 11) else "failed"
    return {"name": "python", "status": status, "version": sys.version.split()[0], "requires": ">=3.11"}


def _check_path_writable(path: Path) -> dict[str, Any]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        test_path = path / ".write-test"
        test_path.write_text("ok", encoding="utf-8")
        test_path.unlink()
    except OSError as exc:
        return {"name": "bridge_home_writable", "status": "failed", "path": _safe_path(path), "error": str(exc)}
    return {"name": "bridge_home_writable", "status": "ok", "path": _safe_path(path)}


def _check_prompt_resources() -> dict[str, Any]:
    try:
        templates = available_prompt_templates()
    except Exception as exc:  # pragma: no cover - defensive report path
        return {"name": "prompt_resources", "status": "failed", "error": str(exc)}
    return {"name": "prompt_resources", "status": "ok", "count": len(templates)}


def _check_codex_cli() -> dict[str, Any]:
    codex_bin = shutil.which("codex")
    if not codex_bin:
        return {"name": "codex_cli", "status": "warning", "error": "codex_not_on_path"}
    return {"name": "codex_cli", "status": "ok", "path": _safe_path(codex_bin)}


def _check_codex_home(path: Path) -> dict[str, Any]:
    return {
        "name": "codex_home",
        "status": "ok" if path.exists() else "warning",
        "path": _safe_path(path),
        "exists": path.exists(),
    }


def _check_skill_installation(resolved_codex_home: Path) -> dict[str, Any]:
    skill_path = resolved_codex_home / "skills" / SKILL_NAME / "SKILL.md"
    return {
        "name": "codex_skill",
        "status": "ok" if skill_path.exists() else "warning",
        "path": _safe_path(skill_path),
        "installed": skill_path.exists(),
    }


def _redact_check(check: dict[str, Any]) -> dict[str, Any]:
    return {key: _redact_value(value) for key, value in check.items()}


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _redact_value(item) for key, item in value.items()}
    return value


def _redact_text(value: str) -> str:
    home = str(Path.home())
    redacted = value.replace(home, "<HOME>") if home else value
    redacted = _PRIVATE_HOME_PATH_RE.sub("<HOME>/...", redacted)
    redacted = _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}<REDACTED>", redacted)
    return _BEARER_TOKEN_RE.sub(lambda match: f"{match.group(1)}<REDACTED>", redacted)


def _git_snapshot(*, redact: bool) -> dict[str, Any]:
    try:
        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        status_text = subprocess.run(
            ["git", "status", "--short", "--branch", "--untracked-files=normal"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return {"available": False}
    return {
        "available": True,
        "root": _safe_path(root, redact=redact),
        "head": head,
        "dirty": _git_status_dirty(status_text),
        "status_line_count": len([line for line in status_text.splitlines() if line]),
    }


def _git_status_dirty(status_text: str) -> bool:
    return any(line and not line.startswith("## ") for line in status_text.splitlines())
