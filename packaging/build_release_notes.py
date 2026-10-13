"""Build the public GitHub Release notes from the curated changelog section."""

from __future__ import annotations

import argparse
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


class ReleaseNotesError(RuntimeError):
    """Raised when release notes cannot be built from the checked-out source."""


def changelog_section(changelog: str, version: str) -> str:
    """Return the Markdown body for one exact ``## [version]`` section."""

    pattern = re.compile(
        rf"^## \[{re.escape(version)}\]\s*$\n(?P<body>.*?)(?=^## \[|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(changelog)
    if not match:
        raise ReleaseNotesError(f"CHANGELOG.md has no release section for {version}")
    body = match.group("body").strip()
    if not body:
        raise ReleaseNotesError(f"CHANGELOG.md release section for {version} is empty")
    return body


def build_release_notes(
    *,
    version: str,
    repository: str,
    changelog: str,
    windows_signed: bool = False,
    macos_signed: bool = False,
) -> str:
    """Create user-facing release notes with exact package and source choices."""

    windows_suffix = "" if windows_signed else "-unsigned"
    macos_suffix = "" if macos_signed else "-unsigned"
    notes = [
        f"# CommsPlan {version}",
        "",
        "Choose the package for your computer from **Assets** below. Technical users "
        "can instead use GitHub's automatically generated **Source code (zip)** or "
        "**Source code (tar.gz)** archive, or pull the tagged source manually.",
        "",
        "CommsPlan, formerly FreqInOut, retains existing station data and technical "
        "profile/package identities. Follow the guided upgrade instructions for your "
        "installation before replacing an existing app.",
        "",
        "## Downloads",
        "",
        f"- Windows 10/11 x86-64: `CommsPlan-{version}-windows-x86_64-setup{windows_suffix}.exe`",
        f"- Debian/Ubuntu-family Linux amd64: `CommsPlan-{version}-linux-amd64.deb`",
        f"- Apple Silicon macOS: `CommsPlan-{version}-macos-arm64{macos_suffix}.dmg`",
        f"- Intel macOS: `CommsPlan-{version}-macos-x86_64{macos_suffix}.dmg`",
        "- Manual/source installation: use the source archive below or check out "
        f"tag `v{version}`",
        "",
        "Verify a downloaded package with `SHA256SUMS.txt`. GitHub build-provenance "
        "attestations are also published for the native packages.",
        "",
        "## What changed",
        "",
        changelog_section(changelog, version),
        "",
        f"[View the complete changelog](https://github.com/{repository}/blob/v{version}/CHANGELOG.md).",
    ]

    if not windows_signed or not macos_signed:
        notes.extend(["", "## Package signing notice", ""])
        if not windows_signed:
            notes.append(
                "- The Windows installer is unsigned and includes `-unsigned` in its "
                "filename. Windows may display **Unknown publisher** or a Microsoft "
                "Defender SmartScreen prompt."
            )
        if not macos_signed:
            notes.append(
                "- The macOS disk images are not Developer ID signed or notarized and "
                "include `-unsigned` in their filenames. macOS may require "
                "**Open Anyway** in Privacy & Security."
            )
        notes.append(
            "- The installed application name is **CommsPlan**. Confirm the "
            "checksum before bypassing an operating-system trust prompt."
        )

    return "\n".join(notes).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--changelog", type=Path, default=ROOT / "CHANGELOG.md")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--windows-signed", action="store_true")
    parser.add_argument("--macos-signed", action="store_true")
    args = parser.parse_args()

    notes = build_release_notes(
        version=args.version,
        repository=args.repository,
        changelog=args.changelog.read_text(encoding="utf-8"),
        windows_signed=args.windows_signed,
        macos_signed=args.macos_signed,
    )
    args.output.write_text(notes, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
