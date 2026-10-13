"""Verify downloaded package artifacts and stage a flat GitHub Release upload."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re
import shutil


class ReleaseAssetError(RuntimeError):
    """Raised when a release artifact set is incomplete or ambiguous."""


def expected_asset_names(
    version: str, *, windows_signed: bool = False, macos_signed: bool = False
) -> tuple[str, ...]:
    windows_suffix = "" if windows_signed else "-unsigned"
    macos_suffix = "" if macos_signed else "-unsigned"
    return (
        f"CommsPlan-{version}-linux-amd64.deb",
        f"CommsPlan-{version}-macos-arm64{macos_suffix}.dmg",
        f"CommsPlan-{version}-macos-x86_64{macos_suffix}.dmg",
        f"CommsPlan-{version}-windows-x86_64-setup{windows_suffix}.exe",
    )


def _one_match(source: Path, name: str) -> Path:
    matches = sorted(path for path in source.rglob(name) if path.is_file())
    if len(matches) != 1:
        raise ReleaseAssetError(
            f"Expected exactly one {name!r} below {source}; found {len(matches)}"
        )
    return matches[0]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _manifest_hash(path: Path, expected_name: str) -> str:
    try:
        lines = [line for line in path.read_text(encoding="ascii").splitlines() if line]
    except (UnicodeDecodeError, OSError) as exc:
        raise ReleaseAssetError(f"Unable to read checksum manifest {path}: {exc}") from exc
    if len(lines) != 1:
        raise ReleaseAssetError(f"Checksum manifest {path} must contain exactly one line")
    match = re.fullmatch(r"([0-9a-fA-F]{64})  (.+)", lines[0])
    if not match or match.group(2) != expected_name:
        raise ReleaseAssetError(
            f"Checksum manifest {path} does not name {expected_name!r} exactly"
        )
    return match.group(1).lower()


def prepare_release_assets(
    source_dir: Path,
    output_dir: Path,
    *,
    version: str,
    windows_signed: bool = False,
    macos_signed: bool = False,
) -> tuple[Path, ...]:
    """Verify exactly four packages and stage packages plus normalized manifests."""

    source = Path(source_dir).resolve()
    output = Path(output_dir).resolve()
    if not source.is_dir():
        raise ReleaseAssetError(f"Downloaded artifact directory does not exist: {source}")
    if output.exists() and any(output.iterdir()):
        raise ReleaseAssetError(f"Release staging directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)

    staged: list[Path] = []
    combined: list[str] = []
    for name in expected_asset_names(
        version,
        windows_signed=windows_signed,
        macos_signed=macos_signed,
    ):
        asset = _one_match(source, name)
        manifest = _one_match(source, f"{name}.sha256")
        expected_hash = _manifest_hash(manifest, name)
        actual_hash = _sha256(asset)
        if actual_hash != expected_hash:
            raise ReleaseAssetError(
                f"Checksum mismatch for {name}: manifest={expected_hash}, actual={actual_hash}"
            )

        staged_asset = output / name
        staged_manifest = output / f"{name}.sha256"
        shutil.copy2(asset, staged_asset)
        staged_manifest.write_text(f"{actual_hash}  {name}\n", encoding="ascii", newline="\n")
        staged.extend((staged_asset, staged_manifest))
        combined.append(f"{actual_hash}  {name}")

    combined_path = output / "SHA256SUMS.txt"
    combined_path.write_text("\n".join(combined) + "\n", encoding="ascii", newline="\n")
    staged.append(combined_path)
    return tuple(staged)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--windows-signed", action="store_true")
    parser.add_argument("--macos-signed", action="store_true")
    args = parser.parse_args()
    staged = prepare_release_assets(
        args.source_dir,
        args.output_dir,
        version=args.version,
        windows_signed=args.windows_signed,
        macos_signed=args.macos_signed,
    )
    print(f"Verified and staged {len(staged) - 1} package files and manifests.")
    for path in staged:
        print(path.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
