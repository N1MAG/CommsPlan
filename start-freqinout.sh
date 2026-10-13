#!/usr/bin/env bash
set -euo pipefail

SCRIPT_WORKTREE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKTREE="${FREQINOUT_INSTALL_DIR:-$SCRIPT_WORKTREE}"

if [[ -d "$WORKTREE/.venv" ]]; then
  if [[ ! -x "$WORKTREE/.venv/bin/python" ]]; then
    echo "CommsPlan installation is incomplete: .venv has no usable Python." >&2
    echo "Run: python3.11 install_freqinout.py" >&2
    exit 1
  fi
  PYTHON="$WORKTREE/.venv/bin/python"
elif [[ -x "$WORKTREE/venv/bin/python" ]]; then
  PYTHON="$WORKTREE/venv/bin/python"
else
  echo "CommsPlan is not installed in $WORKTREE." >&2
  echo "Run: python3.11 install_freqinout.py" >&2
  exit 1
fi

# FIO owns the platform-specific default profile location. Do not silently
# create another profile when an operator upgrades an existing station.
if [[ -n "${FREQINOUT_RUNTIME_ROOT:-}" && -z "${FREQINOUT_CONFIG_DIR:-}" ]]; then
  export FREQINOUT_CONFIG_DIR="$FREQINOUT_RUNTIME_ROOT"
  echo "Warning: FREQINOUT_RUNTIME_ROOT is deprecated; use FREQINOUT_CONFIG_DIR." >&2
fi

cd "$WORKTREE"
if [[ -z "${FREQINOUT_CONFIG_DIR:-}" ]]; then
  FREQINOUT_CONFIG_DIR="$("$PYTHON" - <<'PROFILE'
import json
from pathlib import Path
receipt = json.loads(Path('.freqinout-install-verified.json').read_text())
root = receipt.get('profile_root')
if not isinstance(root, str) or not Path(root).is_absolute():
    raise SystemExit('CommsPlan profile selection is invalid; rerun setup with --config-root.')
print(root)
PROFILE
)" || { echo "CommsPlan profile selection failed. Run install-commsplan.sh setup." >&2; exit 1; }
  export FREQINOUT_CONFIG_DIR
fi
export FIO_LAUNCH_WORKTREE="$WORKTREE"
if ! "$PYTHON" -c "import hashlib, json, os, pathlib, sys; from freqinout.version import __version__; import freqinout.main, PySide6; root=pathlib.Path(os.environ['FIO_LAUNCH_WORKTREE']); r=json.loads((root/'.freqinout-install-verified.json').read_text()); assert r.get('version') == __version__; assert pathlib.Path(r.get('python','')).resolve() == pathlib.Path(sys.executable).resolve(); assert r.get('requirements_sha256') == hashlib.sha256((root/'requirements.txt').read_bytes()).hexdigest()" >/dev/null 2>&1; then
  echo "CommsPlan installation validation failed." >&2
  echo "Run: python3.11 install_freqinout.py" >&2
  exit 1
fi
exec "$PYTHON" -m freqinout.main "$@"
