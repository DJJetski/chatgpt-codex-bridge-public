from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "chatgpt-codex-bridge"
BRIDGE_HOME_ENV = "BRIDGE_HOME"
CODEX_HOME_ENV = "CODEX_HOME"


def bridge_home() -> Path:
    override = os.environ.get(BRIDGE_HOME_ENV)
    if override:
        return Path(override).expanduser()
    return _default_bridge_home()


def codex_home() -> Path:
    override = os.environ.get(CODEX_HOME_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".codex"


def bridge_state_dir() -> Path:
    return bridge_home() / "state"


def bridge_config_dir() -> Path:
    return bridge_home() / "config"


def bridge_artifacts_dir() -> Path:
    return bridge_home() / "artifacts"


def bridge_install_dir() -> Path:
    return bridge_home() / "install"


def bridge_logs_dir() -> Path:
    return bridge_home() / "logs"


def ensure_bridge_dirs() -> None:
    for path in (
        bridge_home(),
        bridge_state_dir(),
        bridge_config_dir(),
        bridge_artifacts_dir(),
        bridge_install_dir(),
        bridge_logs_dir(),
    ):
        path.mkdir(parents=True, exist_ok=True)


def redact_path(path: Path | str, *, home: Path | None = None) -> str:
    value = str(Path(path).expanduser())
    home_path = str(home or Path.home())
    if home_path and value.startswith(home_path):
        value = "<HOME>" + value[len(home_path) :]
    return value


def _default_bridge_home() -> Path:
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / APP_NAME
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / APP_NAME
        return home / "AppData" / "Roaming" / APP_NAME
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home:
        return Path(xdg_data_home).expanduser() / APP_NAME
    return home / ".local" / "share" / APP_NAME
