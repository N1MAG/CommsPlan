"""Assemble and build CommsPlan's direct-download Debian package."""

from __future__ import annotations

import argparse
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import sys
import tempfile

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from verify_release_inputs import validated_version


ROOT = Path(__file__).resolve().parents[1]
ARCHITECTURES = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
}


def deb_architecture(machine: str | None = None) -> str:
    normalized = str(machine or platform.machine()).strip().lower()
    try:
        return ARCHITECTURES[normalized]
    except KeyError as exc:
        raise ValueError(f"Unsupported Debian package architecture: {normalized or 'unknown'}") from exc


def assemble_package_root(
    package_root: Path,
    *,
    source_root: Path = ROOT,
    architecture: str | None = None,
) -> tuple[str, str]:
    source = Path(source_root).resolve()
    target = Path(package_root).resolve()
    frozen_app = source / "dist" / "CommsPlan"
    if not (frozen_app / "CommsPlan").is_file():
        raise FileNotFoundError(f"PyInstaller output is missing: {frozen_app / 'CommsPlan'}")
    version = validated_version(source)
    arch = architecture or deb_architecture()

    # Preserve the package-owned installation directory across the brand upgrade.
    app_dir = target / "opt" / "freqinout" / "FreqInOut"
    shutil.copytree(frozen_app, app_dir)

    # Both commands execute the same frozen application and use its legacy profile lock.
    for command in ("commsplan", "freqinout"):
        launcher = target / "usr" / "bin" / command
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_text(
            '#!/bin/sh\nexec /opt/freqinout/FreqInOut/CommsPlan "$@"\n',
            encoding="utf-8",
        )
        launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    desktop = target / "usr" / "share" / "applications" / "freqinout.desktop"
    desktop.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / "packaging" / "linux" / "freqinout.desktop", desktop)

    icon = target / "usr" / "share" / "icons" / "hicolor" / "256x256" / "apps" / "commsplan.png"
    icon.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / "assets" / "CommsPlan-desktop.png", icon)

    control = target / "DEBIAN" / "control"
    control.parent.mkdir(parents=True, exist_ok=True)
    # dpkg executes these hooks before replacing/removing package-owned files.
    for hook in ("preinst", "prerm"):
        destination = control.parent / hook
        shutil.copy2(source / "packaging/linux/refuse-active-station.sh", destination)
        destination.chmod(0o755)
    installed_kib = max(1, sum(path.stat().st_size for path in target.rglob("*") if path.is_file()) // 1024)
    control.write_text(
        "\n".join(
            (
                "Package: freqinout",
                f"Version: {version}",
                "Section: hamradio",
                "Priority: optional",
                f"Architecture: {arch}",
                f"Installed-Size: {installed_kib}",
                "Maintainer: N1MAG <noreply@github.com>",
                "Depends: libc6 (>= 2.35), libdbus-1-3, libegl1, libgl1, libglib2.0-0, libxkbcommon-x11-0, libxcb-cursor0",
                "Recommends: bluez, libsecret-1-0",
                "Homepage: https://github.com/N1MAG/FreqInOut",
                "Description: CommsPlan coordinated operations console for HF digital stations",
                " CommsPlan provides multi-radio scheduling, messaging, mapping,",
                " station readiness, and companion-application coordination.",
                "",
            )
        ),
        encoding="utf-8",
    )
    return version, arch


def build_deb(output_dir: Path, *, source_root: Path = ROOT) -> Path:
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="freqinout-deb-") as temporary:
        package_root = Path(temporary) / "root"
        version, arch = assemble_package_root(package_root, source_root=source_root)
        destination = output / f"CommsPlan-{version}-linux-{arch}.deb"
        if destination.exists():
            destination.unlink()
        subprocess.run(
            ["dpkg-deb", "--build", "--root-owner-group", str(package_root), str(destination)],
            check=True,
        )
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "Output")
    args = parser.parse_args()
    artifact = build_deb(args.output_dir)
    print(artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
