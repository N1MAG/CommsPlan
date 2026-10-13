"""Explicit native 2.0.4 inspection and cold profile backup; no installer execution.

Release digests were read from the public GitHub release API on 2026-10-11.
Matching an artifact does not attest that the selected installed payload matches
it. Native replacement, startup verification and cleanup remain separate gates.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import plistlib
import stat
import struct
import subprocess
import sys

from freqinout.core import brand_migration as backup
from freqinout.core.brand_migration import MigrationError, _plain_path, _hash

# Exact distributed packages only. No invented 1.2.8 Linux/macOS forms.
RELEASES = {
    "windows-2.0.4": {"platform": "win32", "architecture": "x86_64", "version": "2.0.4",
        "filename": "FreqInOut-2.0.4-windows-x86_64-setup-unsigned.exe", "size": 127357124,
        "sha256": "5444fbbc65ee7a96cb1be7e7013ad9069b94c02975c6d5ad1202c7d75c7ee060"},
    "linux-amd64-2.0.4": {"platform": "linux", "architecture": "amd64", "version": "2.0.4",
        "filename": "FreqInOut-2.0.4-linux-amd64.deb", "size": 289942566,
        "sha256": "96bafc48b96b8cf167c36b95342087775621b1a1ba811517d43f3b76b2ab112a"},
    "macos-arm64-2.0.4": {"platform": "darwin", "architecture": "arm64", "version": "2.0.4",
        "filename": "FreqInOut-2.0.4-macos-arm64-unsigned.dmg", "size": 221432335,
        "sha256": "db8323674ac5bc6f5dd03aa1a520d7f996656fce1991052fce850bb15dfe98ef"},
    "macos-x86_64-2.0.4": {"platform": "darwin", "architecture": "x86_64", "version": "2.0.4",
        "filename": "FreqInOut-2.0.4-macos-x86_64-unsigned.dmg", "size": 231295017,
        "sha256": "0bec5c05c58588d4ca55745fba16b9b99f4d87fd0946bdec06c862d1918ff955"},
}


def _package(path: Path, release: str) -> dict:
    if release not in RELEASES:
        raise MigrationError("Unsupported native release. The 1.2.8 installer version mismatch requires qualification; use the source procedure only with a verified source installation.")
    path = _plain_path(path)
    expected = RELEASES[release]
    if not path.is_file() or path.stat().st_size != expected["size"] or _hash(path) != expected["sha256"]:
        raise MigrationError("Rollback package does not match the pinned public release size and SHA-256.")
    return {"path": str(path), "release": release, **expected,
            "release_url": "https://github.com/N1MAG/FreqInOut/releases/tag/v2.0.4"}


def _inventory(root: Path) -> dict:
    """Allow bundle-internal links; keep the profile's stricter policy separate."""
    result = {}
    def visit(folder):
        for path in sorted(folder.iterdir()):
            name = path.relative_to(root).as_posix()
            mode = stat.S_IMODE(path.lstat().st_mode)
            if path.is_symlink():
                try:
                    resolved = path.resolve(strict=True)
                except (OSError, RuntimeError) as exc:
                    raise MigrationError("Broken or cyclic installation link needs review.") from exc
                target = os.readlink(path)
                if Path(target).is_absolute() or not resolved.is_relative_to(root):
                    raise MigrationError("Installation link escapes the selected tree or is absolute.")
                result[name] = {"kind": "link", "target": target}
            elif path.is_dir():
                result[name] = {"kind": "directory", "mode": mode}
                visit(path)
            elif path.is_file():
                result[name] = {"kind": "file", "mode": mode, "sha256": _hash(path)}
            else:
                raise MigrationError("Unsupported installation entry needs review.")
    visit(root)
    return result


def _metadata(root: Path, release: dict) -> dict:
    if sys.platform != release["platform"]:
        raise MigrationError("Inspect this installation on its native operating system.")
    if sys.platform == "darwin":
        try:
            path = _plain_path(root / "Contents/Info.plist")
            with path.open("rb") as handle:
                data = plistlib.load(handle)
            fields = {"CFBundleIdentifier": "org.n1mag.freqinout", "CFBundleName": "FreqInOut",
                      "CFBundleExecutable": "FreqInOut", "CFBundleShortVersionString": "2.0.4",
                      "CFBundleVersion": "2.0.4"}
            if not isinstance(data, dict) or any(data.get(k) != v for k, v in fields.items()):
                raise MigrationError("Historical macOS bundle identity/version does not match 2.0.4.")
            binary = _plain_path(root / "Contents/MacOS/FreqInOut")
            with binary.open("rb") as handle:
                header = handle.read(8)
            endian = {b"\xcf\xfa\xed\xfe": "<", b"\xfe\xed\xfa\xcf": ">"}.get(header[:4])
            cpu = struct.unpack(endian + "I", header[4:8])[0] if endian and len(header) == 8 else None
            expected_cpu = {"arm64": 0x0100000C, "x86_64": 0x01000007}[release["architecture"]]
            if cpu != expected_cpu or not os.access(binary, os.X_OK):
                raise MigrationError("Historical macOS executable architecture/permissions do not match the selected package.")
            return {**fields, "plist_sha256": _hash(path), "executable": str(binary)}
        except (OSError, ValueError, plistlib.InvalidFileException) as exc:
            raise MigrationError("Cannot inspect historical macOS bundle metadata.") from exc
    if sys.platform == "win32":
        import winreg
        try:
            name = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\FreqInOut_is1"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, name, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                data = {k: winreg.QueryValueEx(key, k)[0] for k in ("DisplayName", "DisplayVersion", "InstallLocation")}
            if (data["DisplayName"] != "FreqInOut" or data["DisplayVersion"] != "2.0.4"
                    or _plain_path(Path(data["InstallLocation"])) != root):
                raise MigrationError("Selected installation does not match historical Windows registration.")
            binary = _plain_path(root / "FreqInOut.exe")
            with binary.open("rb") as handle:
                header = handle.read(64)
                offset = struct.unpack("<I", header[60:64])[0] if len(header) == 64 else 0
                if header[:2] != b"MZ" or offset < 64 or offset > 1024 * 1024:
                    raise MigrationError("Historical Windows executable has an invalid PE header.")
                handle.seek(offset)
                if handle.read(6) != b"PE\x00\x00\x64\x86":
                    raise MigrationError("Historical Windows executable is not an x86-64 PE payload.")
            return {**data, "registration": name, "executable": str(binary)}
        except (OSError, TypeError, ValueError) as exc:
            raise MigrationError("Cannot validate historical Windows registration; portable/per-user installations require review.") from exc
    # Do not shell out to an app/installer; query only the OS package database.
    binary = _plain_path(root / "FreqInOut")
    if root != Path("/opt/freqinout/FreqInOut"):
        raise MigrationError("Selected path does not match the distributed Debian installation.")
    try:
        data = subprocess.run(["/usr/bin/dpkg-query", "-W", "-f=${Status}\n${Version}\n${Architecture}", "freqinout"],
                              check=True, capture_output=True, text=True, timeout=15).stdout.splitlines()
        owner = subprocess.run(["/usr/bin/dpkg-query", "-S", str(binary)], check=True,
                               capture_output=True, text=True, timeout=15).stdout.strip()
        if data != ["install ok installed", "2.0.4", "amd64"] or owner != f"freqinout: {binary}":
            raise MigrationError("Debian package ownership/version does not match the selected installation.")
        with binary.open("rb") as handle:
            header = handle.read(20)
            if (len(header) != 20 or header[:7] != b"\x7fELF\x02\x01\x01"
                    or struct.unpack("<H", header[18:20])[0] != 62 or not os.access(binary, os.X_OK)):
                raise MigrationError("Historical Linux executable is invalid.")
        return {"package": "freqinout", "version": data[1], "architecture": data[2], "executable": str(binary)}
    except (OSError, subprocess.SubprocessError) as exc:
        raise MigrationError("Cannot inspect Debian package registration.") from exc


def _report(installation: Path, profile: Path, package: Path, release: str) -> dict:
    root, profile = _plain_path(installation), _plain_path(profile)
    artifact = _package(package, release)
    if not root.is_dir() or root == profile or root.is_relative_to(profile) or profile.is_relative_to(root):
        raise MigrationError("Select separate existing native installation and profile directories.")
    if Path(artifact["path"]).is_relative_to(root) or Path(artifact["path"]).is_relative_to(profile):
        raise MigrationError("Retain the rollback package outside installation and profile directories.")
    metadata = _metadata(root, RELEASES[release])
    fingerprint = hashlib.sha256(json.dumps(_inventory(root), sort_keys=True).encode()).hexdigest()
    return dict(backup._inspect_profile(profile), source_version=artifact["version"], installation=str(root),
                source_metadata_sha256=fingerprint,
                source_qualification="Release artifact digest verified; installed payload fingerprinted and local metadata checked, but not matched to artifact contents or release-qualified.",
                native_installation={"form": "native", "metadata": metadata, "fingerprint_sha256": fingerprint,
                                     "rollback_package": artifact},
                instructions="Close app and companion writers; verify backup and retained rollback package before any separately qualified native replacement.")


def inspect_native(installation: Path, profile: Path, package: Path, release: str) -> dict:
    profile = _plain_path(profile)
    if not profile.is_dir():
        raise MigrationError("The explicitly selected profile must exist.")
    with backup._locked_profile(profile):
        report = _report(installation, profile, package, release)
        if _report(installation, profile, package, release) != report:
            raise MigrationError("Installation evidence changed during inspection; close all writers and retry.")
        return report


def backup_native(installation: Path, profile: Path, package: Path, release: str, destination: Path) -> Path:
    root, destination = _plain_path(installation), _plain_path(destination)
    if destination == root or destination.is_relative_to(root):
        raise MigrationError("Native backup destination must be outside the installation.")
    return backup._create_backup(profile, destination, lambda: _report(root, profile, package, release))


def verify_native_backup(path: Path) -> dict:
    report = backup.verify_backup(path)
    try:
        identity = report["native_installation"]
        if (not isinstance(identity, dict) or set(identity) != {"form", "metadata", "fingerprint_sha256", "rollback_package"}
                or identity["form"] != "native" or not isinstance(identity["metadata"], dict)
                or not isinstance(identity["fingerprint_sha256"], str)
                or len(identity["fingerprint_sha256"]) != 64
                or any(c not in "0123456789abcdef" for c in identity["fingerprint_sha256"])):
            raise MigrationError("Native backup has malformed installation evidence.")
        reference = identity["rollback_package"]
        if not isinstance(reference, dict) or not isinstance(reference.get("path"), str) or not isinstance(reference.get("release"), str):
            raise MigrationError("Native backup has malformed rollback package evidence.")
        if (_package(Path(reference["path"]), reference["release"]) != reference
                or report.get("source_version") != reference["version"]
                or report.get("source_metadata_sha256") != identity["fingerprint_sha256"]):
            raise MigrationError("Native rollback reference changed.")
        _validate_metadata_record(report, identity["metadata"], reference)
    except (KeyError, TypeError, ValueError) as exc:
        raise MigrationError("Native backup has missing or malformed rollback package evidence.") from exc
    return report


def _validate_metadata_record(report: dict, metadata: dict, release: dict) -> None:
    root = report.get("installation")
    if not isinstance(root, str):
        raise MigrationError("Native installation path metadata is malformed.")
    if release["platform"] == "win32":
        path = PureWindowsPath(root)
        fields = {"DisplayName": "FreqInOut", "DisplayVersion": "2.0.4",
                  "registration": r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\FreqInOut_is1"}
        required = set(fields) | {"InstallLocation", "executable"}
        location = metadata.get("InstallLocation")
        valid_location = isinstance(location, str) and PureWindowsPath(location) == path
        executable = path / "FreqInOut.exe"
    elif release["platform"] == "linux":
        path = PurePosixPath(root)
        fields = {"package": "freqinout", "version": "2.0.4", "architecture": "amd64"}
        required = set(fields) | {"executable"}
        valid_location = path == PurePosixPath("/opt/freqinout/FreqInOut")
        executable = path / "FreqInOut"
    else:
        path = PurePosixPath(root)
        fields = {"CFBundleIdentifier": "org.n1mag.freqinout", "CFBundleName": "FreqInOut",
                  "CFBundleExecutable": "FreqInOut", "CFBundleShortVersionString": "2.0.4", "CFBundleVersion": "2.0.4"}
        required = set(fields) | {"executable", "plist_sha256"}
        digest = metadata.get("plist_sha256")
        valid_location = isinstance(digest, str) and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)
        executable = path / "Contents/MacOS/FreqInOut"
    if (not path.is_absolute() or set(metadata) != required or not valid_location
            or any(metadata.get(k) != v for k, v in fields.items())
            or metadata.get("executable") != str(executable)):
        raise MigrationError("Native installation metadata is malformed or does not match the retained release.")


def main(argv=None, *, require_report=False) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-file", type=Path, help="NEW output JSON outside selected app/profile/backup; parent must exist")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("inspect-native", "backup-native"):
        sub = commands.add_parser(command)
        sub.add_argument("--installation", type=Path, required=True)
        sub.add_argument("--profile", type=Path, required=True)
        sub.add_argument("--package", type=Path, required=True)
        sub.add_argument("--release", choices=RELEASES, required=True)
        if command == "backup-native":
            sub.add_argument("--destination", type=Path, required=True)
    commands.add_parser("verify-native-backup").add_argument("backup", type=Path)
    args = parser.parse_args(argv)
    if require_report and args.report_file is None:
        parser.error("Packaged preparation requires --report-file before the command.")
    handle = None
    try:
        if args.report_file is not None:
            output = _plain_path(args.report_file)
            for name in ("installation", "profile", "backup", "destination"):
                selected = getattr(args, name, None)
                if selected is not None:
                    root = _plain_path(selected)
                    if output == root or output.is_relative_to(root):
                        raise MigrationError("Report file must be outside the selected app, profile and backup.")
            descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            handle = os.fdopen(descriptor, "w", encoding="utf-8")
        if args.command == "inspect-native":
            result = inspect_native(args.installation, args.profile, args.package, args.release)
        elif args.command == "backup-native":
            result = {"stage": "backed-up", "backup": str(backup_native(args.installation, args.profile, args.package, args.release, args.destination)),
                      "next": "Verify native backup. No installation, station conversion or cleanup has occurred."}
        else:
            report = verify_native_backup(args.backup)
            result = {"stage": "backed-up", "profile": report["profile"], "source_version": report["source_version"],
                      "profile_verified": True, "rollback_package_verified": True,
                      "installed_payload_matches_package": False, "native_upgrade_qualified": False}
        code = 0
    except (MigrationError, OSError) as exc:
        result, code = {"error": str(exc)}, 1
    if handle is not None:
        with handle:
            json.dump({"exit_code": code, "result": result}, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    elif not require_report:
        if code:
            print(f"Native migration preparation stopped: {result['error']}", file=sys.stderr)
        else:
            print(json.dumps(result, indent=2))
    return code
