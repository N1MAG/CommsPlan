"""Validate version agreement and the public package-build inputs."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_INPUTS = (
    "FreqInOut.spec",
    "installer.iss",
    "requirements.txt",
    "packaging/build-requirements.txt",
    "packaging/pyinstaller_runtime_qt.py",
    "packaging/linux/freqinout.desktop",
    "packaging/build_macos_dmg.py",
    "docs/CommsPlan-macos-upgrade.txt",
    "docs/CommsPlan-migration.md",
    "packaging/build_release_notes.py",
    "packaging/prepare_release_assets.py",
    "packaging/macos/entitlements.plist",
    "assets/CommsPlan.ico",
    "assets/CommsPlan.icns",
    "assets/CommsPlan-desktop.png",
    "assets/CommsPlan_logo.png",
    "start-commsplan.sh",
    "start-commsplan.cmd",
)


class ReleaseInputError(RuntimeError):
    """Raised when a release cannot be reproduced from the checked-out tree."""


def _match(path: Path, pattern: str, label: str) -> str:
    match = re.search(pattern, path.read_text(encoding="utf-8"), re.MULTILINE)
    if not match:
        raise ReleaseInputError(f"Unable to read {label} from {path}")
    return match.group(1)


def validated_version(root: Path = ROOT, *, expected_tag: str = "") -> str:
    root = Path(root).resolve()
    missing = [relative for relative in REQUIRED_INPUTS if not (root / relative).is_file()]
    if missing:
        raise ReleaseInputError("Missing release inputs: " + ", ".join(missing))

    versions = {
        "runtime": _match(
            root / "freqinout/version.py",
            r'^__version__\s*=\s*["\']([^"\']+)["\']',
            "runtime version",
        ),
        "project": str(
            tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
        ),
        "windows_installer": _match(
            root / "installer.iss",
            r'^#define\s+MyAppVersion\s+"([^"]+)"',
            "Windows installer version",
        ),
        "guide": _match(
            root / "docs/guide.html",
            r"(?:Current version|Latest public release)</strong>:\s*(\d+\.\d+\.\d+(?:\.\d+)?(?:[-+][0-9A-Za-z][0-9A-Za-z.-]*)?)(?!\d|\.\d)",
            "guide version",
        ),
    }
    distinct = set(versions.values())
    if len(distinct) != 1:
        detail = ", ".join(f"{name}={value}" for name, value in sorted(versions.items()))
        raise ReleaseInputError(f"Release versions disagree: {detail}")
    version = distinct.pop()
    if f"## [{version}]" not in (root / "CHANGELOG.md").read_text(encoding="utf-8"):
        raise ReleaseInputError(f"CHANGELOG.md has no release section for {version}")
    if expected_tag:
        normalized = expected_tag.removeprefix("refs/tags/").removeprefix("v")
        if normalized != version:
            raise ReleaseInputError(
                f"Tag/version mismatch: tag={expected_tag!r}, application={version!r}"
            )
    return version


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--expected-tag", default="")
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--print-version", action="store_true")
    args = parser.parse_args()
    version = validated_version(args.root, expected_tag=args.expected_tag)
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as handle:
            handle.write(f"version={version}\n")
    if args.print_version or not args.github_output:
        print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
