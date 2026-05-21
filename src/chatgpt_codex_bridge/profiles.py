from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .app_paths import bridge_install_dir

PROFILE_ENV = "BRIDGE_PROFILE"
ALLOW_DANGEROUS_BYPASS_ENV = "BRIDGE_ALLOW_DANGEROUS_CODEX_BYPASS"


@dataclass(frozen=True, slots=True)
class BridgeProfile:
    name: str
    description: str
    capabilities: frozenset[str]
    default_sandbox: str
    default_approval_policy: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "capabilities": sorted(self.capabilities),
            "default_sandbox": self.default_sandbox,
            "default_approval_policy": self.default_approval_policy,
        }


PROFILES: dict[str, BridgeProfile] = {
    "core-safe": BridgeProfile(
        name="core-safe",
        description="Terminal-first bridge operation with no browser, macOS app, or dangerous sandbox bypass.",
        capabilities=frozenset({"v2", "install", "doctor", "self-test", "snapshot", "manifest-uninstall"}),
        default_sandbox="workspace-write",
        default_approval_policy="on-request",
    ),
    "trusted-local": BridgeProfile(
        name="trusted-local",
        description="Local operator-trusted mode. Dangerous Codex bypass still requires an explicit env opt-in.",
        capabilities=frozenset(
            {
                "v2",
                "install",
                "doctor",
                "self-test",
                "snapshot",
                "manifest-uninstall",
                "local-codex-exec",
                "dangerous-codex-bypass",
            }
        ),
        default_sandbox="workspace-write",
        default_approval_policy="on-request",
    ),
    "browser-extra": BridgeProfile(
        name="browser-extra",
        description="Trusted-local plus browser-backed legacy orchestration surfaces.",
        capabilities=frozenset(
            {
                "v2",
                "install",
                "doctor",
                "self-test",
                "snapshot",
                "manifest-uninstall",
                "local-codex-exec",
                "browser",
                "control-panel",
                "dangerous-codex-bypass",
            }
        ),
        default_sandbox="workspace-write",
        default_approval_policy="on-request",
    ),
    "macos-app": BridgeProfile(
        name="macos-app",
        description="Browser-extra plus optional macOS Codex app integration when available.",
        capabilities=frozenset(
            {
                "v2",
                "install",
                "doctor",
                "self-test",
                "snapshot",
                "manifest-uninstall",
                "local-codex-exec",
                "browser",
                "control-panel",
                "macos-app",
                "dangerous-codex-bypass",
            }
        ),
        default_sandbox="workspace-write",
        default_approval_policy="on-request",
    ),
}

PROFILE_CHOICES = tuple(PROFILES)


def normalize_profile(profile: str | None) -> str:
    normalized = str(profile or "core-safe").strip() or "core-safe"
    if normalized not in PROFILES:
        return "core-safe"
    return normalized


def active_profile(profile: str | None = None, *, bridge_home_path: Path | None = None) -> BridgeProfile:
    if profile is not None and str(profile).strip():
        return PROFILES[normalize_profile(profile)]
    env_profile = os.environ.get(PROFILE_ENV)
    if env_profile is not None and str(env_profile).strip():
        return PROFILES[normalize_profile(env_profile)]
    return PROFILES[normalize_profile(_installed_profile_name(bridge_home_path))]


def profile_payload(profile: str | None = None, *, bridge_home_path: Path | None = None) -> dict[str, Any]:
    return active_profile(profile, bridge_home_path=bridge_home_path).as_dict()


def profile_allows(capability: str, profile: str | None = None, *, bridge_home_path: Path | None = None) -> bool:
    return capability in active_profile(profile, bridge_home_path=bridge_home_path).capabilities


def dangerous_bypass_opted_in() -> bool:
    normalized = str(os.environ.get(ALLOW_DANGEROUS_BYPASS_ENV, "")).strip().casefold()
    return normalized in {"1", "true", "yes", "on"}


def _installed_profile_name(bridge_home_path: Path | None = None) -> str:
    manifest_path = (
        Path(bridge_home_path).expanduser() / "install" / "manifest.json"
        if bridge_home_path is not None
        else bridge_install_dir() / "manifest.json"
    )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "core-safe"
    return str(manifest.get("profile") or "core-safe")
