"""Compatibility import path for the legacy ``mastermind_bridge`` package."""

from __future__ import annotations

from pathlib import Path

import chatgpt_codex_bridge as _canonical

__all__ = getattr(_canonical, "__all__", ["__version__"])
__version__ = _canonical.__version__

# Keep legacy imports such as ``mastermind_bridge.cli`` working while the
# canonical implementation lives under ``src/chatgpt_codex_bridge``.
__path__ = [str(Path(_canonical.__file__).resolve().parent)]
