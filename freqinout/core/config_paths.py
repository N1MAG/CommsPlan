"""
Shared helpers for locating user-writable config/data directories.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from freqinout.core.product_identity import STORAGE_NAME


APP_NAME = STORAGE_NAME


def _check_profile_copy(root: Path) -> None:
    if (root / ".commsplan-copy-incomplete").exists():
        raise RuntimeError("CommsPlan profile copy is incomplete. Use the original profile; do not activate this copy.")


def get_config_dir() -> Path:
    """
    Return a user-writable config directory.
    Priority:
      1) FREQINOUT_CONFIG_DIR env var (user override)
      2) Windows: LOCALAPPDATA/APPDATA/FreqInOut
         Others: ~/.freqinout
      3) CWD/freqinout_config (fallback if creation fails)
    """
    env_cfg = os.environ.get("FREQINOUT_CONFIG_DIR")
    if env_cfg:
        selected = Path(env_cfg)
        _check_profile_copy(selected)
        return selected

    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or Path.home()) / APP_NAME
    else:
        base = Path.home() / f".{APP_NAME.lower()}"

    _check_profile_copy(base)
    try:
        base.mkdir(parents=True, exist_ok=True)
        return base
    except Exception:
        fallback = Path.cwd() / "freqinout_config"
        _check_profile_copy(fallback)
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def get_fldigi_checkin_dir() -> Path:
    """
    Return the default FLDigi check-in directory under the OS temp folder.
    """
    return Path(tempfile.gettempdir()) / APP_NAME / "fldigi"
