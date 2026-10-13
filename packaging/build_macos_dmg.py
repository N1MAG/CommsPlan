"""Assemble a native CommsPlan macOS disk image from the PyInstaller app."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import tempfile

from verify_release_inputs import validated_version


ROOT = Path(__file__).resolve().parents[1]
ARCHITECTURES = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "arm64": "arm64",
    "aarch64": "arm64",
}


def macos_architecture(machine: str | None = None) -> str:
    normalized = str(machine or platform.machine()).strip().lower()
    try:
        return ARCHITECTURES[normalized]
    except KeyError as exc:
        raise ValueError(f"Unsupported macOS package architecture: {normalized or 'unknown'}") from exc


def build_dmg(
    output_dir: Path,
    *,
    source_root: Path = ROOT,
    architecture: str | None = None,
) -> Path:
    source = Path(source_root).resolve()
    app = source / "dist" / "CommsPlan.app"
    executable = app / "Contents" / "MacOS" / "CommsPlan"
    if not executable.is_file():
        raise FileNotFoundError(f"PyInstaller macOS app is missing: {executable}")
    if not os.access(executable, os.X_OK):
        raise ValueError("macOS app main binary is not executable")

    version = validated_version(source)
    try:
        with (app / "Contents" / "Info.plist").open("rb") as handle:
            metadata = plistlib.load(handle)
    except (OSError, ValueError, plistlib.InvalidFileException) as exc:
        raise ValueError("macOS app has no readable bundle identity") from exc
    expected = {
        "CFBundleIdentifier": "org.n1mag.freqinout",
        "CFBundleExecutable": "CommsPlan",
        "CFBundleName": "CommsPlan",
        "CFBundleDisplayName": "CommsPlan",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
    }
    if not isinstance(metadata, dict) or any(metadata.get(key) != value for key, value in expected.items()):
        raise ValueError("macOS app identity/version does not match the CommsPlan release")
    instructions = source / "docs" / "CommsPlan-macos-upgrade.txt"
    if not instructions.is_file() or not instructions.read_text(encoding="utf-8").strip():
        raise ValueError("macOS replacement instructions are missing or empty")
    arch = architecture or macos_architecture()
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    destination = output / f"CommsPlan-{version}-macos-{arch}.dmg"
    if destination.exists():
        destination.unlink()

    with tempfile.TemporaryDirectory(prefix="freqinout-dmg-") as temporary:
        staging = Path(temporary) / "CommsPlan"
        staging.mkdir()
        shutil.copytree(app, staging / app.name, symlinks=True)
        shutil.copy2(instructions, staging / "READ ME - Upgrading from FreqInOut.txt")
        shutil.copy2(source / "docs/CommsPlan-migration.md", staging / "CommsPlan-migration.md")
        (staging / "Applications").symlink_to("/Applications", target_is_directory=True)
        subprocess.run(
            [
                "hdiutil",
                "create",
                "-quiet",
                "-ov",
                "-format",
                "UDZO",
                "-volname",
                "CommsPlan",
                "-srcfolder",
                str(staging),
                str(destination),
            ],
            check=True,
        )
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "Output")
    args = parser.parse_args()
    artifact = build_dmg(args.output_dir)
    print(artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
