from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SPEC_FILE = ROOT / "FreqInOut.spec"


def run(cmd: list[str]) -> int:
    print(f"[build_executable] Running: {' '.join(cmd)}")
    return subprocess.run(cmd, cwd=ROOT, check=False).returncode


def main() -> int:
    if not sys.platform.startswith("win"):
        print("[build_executable] ERROR: the delivered executable target is Windows only.")
        print("[build_executable] Run this helper from a Windows build environment.")
        return 2
    if not SPEC_FILE.exists():
        print(f"[build_executable] ERROR: Spec file not found: {SPEC_FILE}")
        return 1

    pyinstaller_cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        str(SPEC_FILE),
    ]
    rc = run(pyinstaller_cmd)
    if rc != 0:
        print("[build_executable] ERROR: PyInstaller build failed.")
        print("[build_executable] Tip: install PyInstaller in your active environment.")
        return rc

    dist_exe = ROOT / "dist" / "CommsPlan" / "CommsPlan.exe"
    if dist_exe.exists():
        print(f"[build_executable] SUCCESS: Built {dist_exe}")
    else:
        print("[build_executable] WARNING: Build finished but expected EXE was not found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
