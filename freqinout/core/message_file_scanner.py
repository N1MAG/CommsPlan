from __future__ import annotations

import base64
import os
import re
import stat as stat_module
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from freqinout.core.perf_metrics import emit_span
from freqinout.core.worker_lifecycle import OperationCancelled


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".raw"}
AUTH_FILE_EXTS = {".b2s", ".k2s", ".sig", ".asc", ".gpg"}
SUPPORTED_EXT = {
    ".b2s",
    ".k2s",
    ".sig",
    ".asc",
    ".gpg",
    ".txt",
    ".rtf",
    ".ff",
    ".xml",
    ".json",
    ".html",
    ".htm",
    *IMAGE_EXTS,
}
ORIGIN_EXTS = {
    "flmsg": {".b2s", ".k2s", ".txt", ".rtf", *IMAGE_EXTS},
    "flamp": {".txt", ".rtf", *AUTH_FILE_EXTS},
    "varac": {".txt", ".html", ".htm", *AUTH_FILE_EXTS, *IMAGE_EXTS},
    "bbs": set(SUPPORTED_EXT),
}
FLAMP_AUTH_EXTS = set(AUTH_FILE_EXTS)
BBS_HELPER_FILE_PREFIXES = (
    "BBS MSG - ",
    "00 HOW TO USE -",
    "00 READ FIRST -",
    "00 NOTICE -",
    "01 COMMANDS -",
    "BBS_QUEUE_LIST",
    "BBS_BLOCK_LIST",
)


@dataclass
class FileRecord:
    path: Path
    origin: str
    size: int = 0
    mtime: float = 0.0
    source_id: str = ""
    source_label: str = ""

    def display_name(self) -> str:
        # Keep scanner/UI presentation safe even when a filesystem name holds
        # bytes which are not valid UTF-8.
        return file_path_display(self.path.name)

    def info_line(self) -> str:
        return f"{self.display_name()} - {self.size} bytes"


def file_path_key(path: str | os.PathLike[str]) -> str:
    """Return a stable SQLite-safe identity for an arbitrary filesystem path.

    POSIX filenames are byte sequences.  ``Path`` may therefore contain
    surrogate escapes which SQLite's text binder rejects.  The opaque key is
    deliberately based on ``fsencode`` rather than a lossy unicode conversion,
    and can be reversed with :func:`file_path_from_key` when FIO needs to act on
    the real file.
    """

    raw = os.fsencode(os.fspath(path))
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def file_path_from_key(key: str) -> Path:
    """Reverse :func:`file_path_key` without decoding surrogate paths as text."""

    text = str(key or "")
    padding = "=" * (-len(text) % 4)
    return Path(os.fsdecode(base64.urlsafe_b64decode((text + padding).encode("ascii"))))


def file_path_display(path: str | os.PathLike[str]) -> str:
    """Produce printable, database-safe path text without surrogate codepoints."""

    raw = os.fsencode(os.fspath(path))
    decoded = raw.decode("utf-8", "backslashreplace")
    # Keep normal unicode readable, while making controls (including embedded
    # newlines) explicit so a filename can never corrupt a table cell/log line.
    return "".join(char if char.isprintable() else f"\\x{ord(char):02x}" for char in decoded)


@dataclass(frozen=True)
class FileScanDelta:
    """Immutable comparison of two scanner snapshots.

    ``replaced`` retains the previous record for a changed path so downstream
    projection can tombstone the old file-version ref without touching the
    physical file.  The required public categories remain simple tuples.
    """

    added_or_changed: tuple[FileRecord, ...] = ()
    removed: tuple[FileRecord, ...] = ()
    unchanged: tuple[FileRecord, ...] = ()
    replaced: tuple[tuple[FileRecord, FileRecord], ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.added_or_changed and not self.removed

    @classmethod
    def compare(
        cls,
        base_records: Dict[str, List[FileRecord]] | None,
        current_records: Dict[str, List[FileRecord]] | None,
    ) -> "FileScanDelta":
        def index(records: Dict[str, List[FileRecord]] | None) -> dict[tuple[str, str], FileRecord]:
            indexed: dict[tuple[str, str], FileRecord] = {}
            for origin, values in (records or {}).items():
                origin_key = str(origin or "").strip().lower()
                for record in values or ():
                    if not isinstance(record, FileRecord):
                        continue
                    record_origin = str(record.origin or origin_key).strip().lower()
                    key = (record_origin, MessageFileScanner._norm_path(record.path))
                    indexed[key] = record
            return indexed

        before, after = index(base_records), index(current_records)

        def source_identity(record: FileRecord, origin: str) -> str:
            explicit = str(record.source_id or "")
            if explicit:
                return explicit
            resolved_origin = str(record.origin or origin or "file").strip().lower() or "file"
            return f"{resolved_origin}:{file_path_key(record.path.parent)}"

        added: list[FileRecord] = []
        removed: list[FileRecord] = []
        unchanged: list[FileRecord] = []
        replaced: list[tuple[FileRecord, FileRecord]] = []
        for key in sorted(after):
            current = after[key]
            prior = before.get(key)
            if prior is None:
                added.append(current)
            else:
                version_changed = (
                    int(prior.size or 0) != int(current.size or 0)
                    or float(prior.mtime or 0.0) != float(current.mtime or 0.0)
                )
                source_changed = source_identity(prior, key[0]) != source_identity(current, key[0])
                label_changed = str(prior.source_label or "") != str(current.source_label or "")
                if version_changed or source_changed or label_changed:
                    added.append(current)
                if version_changed or source_changed:
                    # Tombstone only when the old external reference is a
                    # different version or belongs to a different source.
                    # A label-only update reuses the same source/ref identity.
                    replaced.append((prior, current))
                if not (version_changed or source_changed or label_changed):
                    unchanged.append(current)
        for key in sorted(before):
            if key not in after:
                removed.append(before[key])
        return cls(tuple(added), tuple(removed), tuple(unchanged), tuple(replaced))


class DirectoryMtimeSnapshot(dict[str, float]):
    """Legacy-compatible directory mtimes with process-local identity hints.

    This remains an ordinary dict for callers which persist, compare, or
    serialize the historical path-to-float mapping.  Filesystem identities
    are an additive in-memory hint and are intentionally not part of that
    mapping's serialized shape.
    """

    def __init__(self, *args, identities: Optional[Dict[str, tuple[int, int, int]]] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.identities: Dict[str, tuple[int, int, int]] = dict(identities or {})

    def record(self, path: str | Path, stat_result: os.stat_result) -> None:
        path_key = os.path.normcase(os.path.normpath(str(path)))
        self[path_key] = float(stat_result.st_mtime)
        self.identities[path_key] = (
            int(stat_result.st_dev),
            int(stat_result.st_ino),
            int(stat_result.st_mtime_ns),
        )


def is_fio_bbs_helper_file_name(name: object) -> bool:
    clean = Path(str(name or "").strip()).name.upper()
    return any(clean.startswith(prefix.upper()) for prefix in BBS_HELPER_FILE_PREFIXES) or bool(
        re.match(r"^\d{2} TYPE .+(?:\.TXT)?$", clean)
    )


class MessageFileScanner:
    def __init__(
        self,
        watch_dirs: List[Dict],
        *,
        force: bool,
        base_records: Optional[Dict[str, List[FileRecord]]] = None,
        base_dir_mtimes: Optional[Dict[str, float]] = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self._cancel_event = cancel_event
        self._watch_dirs = list(watch_dirs)
        self._force = bool(force)
        self._base_records = base_records or {}
        self._base_dir_mtimes: Dict[str, float] = {}
        for key, value in (base_dir_mtimes or {}).items():
            try:
                self._base_dir_mtimes[self._norm_path(key)] = float(value)
            except Exception:
                continue
        self._base_dir_identities: Dict[str, tuple[int, int, int]] = {}
        for key, value in getattr(base_dir_mtimes, "identities", {}).items():
            try:
                identity = tuple(int(part) for part in value)
                if len(identity) == 3:
                    self._base_dir_identities[self._norm_path(key)] = identity  # type: ignore[assignment]
            except Exception:
                continue
        self._roots_by_origin: Dict[str, List[str]] = {"varac": [], "flmsg": [], "flamp": [], "bbs": []}
        for entry in self._watch_dirs:
            self._check_cancelled()
            origin = str(entry.get("origin", "") or "").strip().lower()
            path = str(entry.get("path", "") or "").strip()
            if origin in self._roots_by_origin and path:
                norm = self._norm_path(path)
                if norm not in self._roots_by_origin[origin]:
                    self._roots_by_origin[origin].append(norm)

        # Build one-pass indexes for unchanged-directory reconciliation.  The
        # walk can then stat known entries and recurse through cached children
        # without enumerating every unchanged directory on every refresh.
        self._cached_children_by_parent: Dict[str, set[str]] = {}
        for child_norm in self._base_dir_mtimes:
            parent_norm = self._norm_path(Path(child_norm).parent)
            self._cached_children_by_parent.setdefault(parent_norm, set()).add(child_norm)
        self._cached_files_by_parent_origin: Dict[tuple[str, str], List[FileRecord]] = {}
        for origin, rows in (self._base_records or {}).items():
            origin_norm = str(origin or "").strip().lower()
            if origin_norm not in self._roots_by_origin:
                continue
            for rec in rows or ():
                if not isinstance(rec, FileRecord):
                    continue
                path = Path(rec.path)
                parent_norm = self._norm_path(path.parent)
                self._cached_files_by_parent_origin.setdefault(
                    (origin_norm, parent_norm), []
                ).append(
                    FileRecord(
                        path=path,
                        origin=origin_norm,
                        size=int(rec.size or 0),
                        mtime=float(rec.mtime or 0.0),
                        source_id=str(rec.source_id or ""),
                        source_label=str(rec.source_label or ""),
                    )
                )

    def _check_cancelled(self) -> None:
        if self._cancel_event is not None and self._cancel_event.is_set():
            raise OperationCancelled("message file scan cancelled")

    @staticmethod
    def _norm_path(path: str | Path) -> str:
        return os.path.normcase(os.path.normpath(str(path)))

    @staticmethod
    def _is_under(path_norm: str, root_norm: str) -> bool:
        return path_norm == root_norm or path_norm.startswith(root_norm + os.sep)

    def _is_under_any(self, path_norm: str, roots: set[str] | List[str]) -> bool:
        return any(self._is_under(path_norm, root_norm) for root_norm in roots)

    @staticmethod
    def _empty_result() -> Dict[str, Dict[str, FileRecord]]:
        return {"varac": {}, "flmsg": {}, "flamp": {}, "bbs": {}}

    def _full_scan_recursive(
        self,
        base: Path,
        origin: str,
        allowed_exts: Optional[set[str]],
        out_map: Dict[str, Dict[str, FileRecord]],
        dir_mtimes: DirectoryMtimeSnapshot,
        *,
        source_id: str = "",
        source_label: str = "",
    ) -> None:
        try:
            directory_stat = base.stat()
            dir_mtimes.record(self._norm_path(base), directory_stat)
        except OSError:
            return
        try:
            with os.scandir(base) as entries:
                for dent in entries:
                    self._check_cancelled()
                    try:
                        if dent.is_dir(follow_symlinks=False):
                            self._full_scan_recursive(
                                Path(dent.path),
                                origin,
                                allowed_exts,
                                out_map,
                                dir_mtimes,
                                source_id=source_id,
                                source_label=source_label,
                            )
                            continue
                        if not dent.is_file(follow_symlinks=False):
                            continue
                        if is_fio_bbs_helper_file_name(dent.name):
                            continue
                        suffix = Path(dent.name).suffix.lower()
                        if suffix not in SUPPORTED_EXT:
                            continue
                        if allowed_exts and suffix not in allowed_exts:
                            continue
                        stat = dent.stat()
                        rec = FileRecord(
                            path=Path(dent.path),
                            origin=origin,
                            size=stat.st_size,
                            mtime=stat.st_mtime,
                            source_id=source_id,
                            source_label=source_label,
                        )
                        out_map[origin][self._norm_path(rec.path)] = rec
                    except OSError:
                        continue
        except OSError:
            return

    def _full_scan_bbs(
        self,
        base: Path,
        out_map: Dict[str, Dict[str, FileRecord]],
        dir_mtimes: DirectoryMtimeSnapshot,
        *,
        source_id: str = "",
        source_label: str = "",
    ) -> None:
        try:
            directory_stat = base.stat()
            dir_mtimes.record(self._norm_path(base), directory_stat)
        except OSError:
            return
        try:
            with os.scandir(base) as entries:
                for dent in entries:
                    self._check_cancelled()
                    try:
                        if not dent.is_file(follow_symlinks=False):
                            continue
                        if is_fio_bbs_helper_file_name(dent.name):
                            continue
                        suffix = Path(dent.name).suffix.lower()
                        if suffix not in SUPPORTED_EXT:
                            continue
                        stat = dent.stat()
                        rec = FileRecord(
                            path=Path(dent.path),
                            origin="bbs",
                            size=stat.st_size,
                            mtime=stat.st_mtime,
                            source_id=source_id,
                            source_label=source_label,
                        )
                        out_map["bbs"][self._norm_path(rec.path)] = rec
                    except OSError:
                        continue
        except OSError:
            return

    def _scan_changed_recursive(
        self,
        base: Path,
        origin: str,
        allowed_exts: Optional[set[str]],
        out_map: Dict[str, Dict[str, FileRecord]],
        dir_mtimes: DirectoryMtimeSnapshot,
        seen_files: Dict[str, set[str]],
        changed_dirs: Dict[str, set[str]],
        reused_dirs: Dict[str, set[str]],
        *,
        source_id: str = "",
        source_label: str = "",
        is_watch_root: bool = False,
    ) -> None:
        base_norm = self._norm_path(base)
        try:
            # Explicit watched roots retain the legacy behavior of following a
            # root symlink.  Descendant links are never followed, including a
            # cached directory replaced by a symlink between scans.
            directory_stat = base.stat() if is_watch_root else base.stat(follow_symlinks=False)
            if not is_watch_root and not stat_module.S_ISDIR(directory_stat.st_mode):
                return
            current_mtime = float(directory_stat.st_mtime)
        except OSError:
            return
        dir_mtimes.record(base_norm, directory_stat)
        previous_mtime = self._base_dir_mtimes.get(base_norm)
        current_identity = (
            int(directory_stat.st_dev),
            int(directory_stat.st_ino),
            int(directory_stat.st_mtime_ns),
        )
        previous_identity = self._base_dir_identities.get(base_norm)
        if (
            previous_mtime is not None
            and previous_identity is not None
            and previous_identity == current_identity
            and previous_mtime == current_mtime
        ):
            reused_dirs[origin].add(base_norm)
            # A cached file can be modified in place without changing its
            # parent directory mtime. Reconcile known regular files by stat;
            # this detects size/mtime changes without opening or hashing them.
            changed_dirs[origin].add(base_norm)
            for cached in self._cached_files_by_parent_origin.get((origin, base_norm), ()):
                self._check_cancelled()
                suffix = cached.path.suffix.lower()
                if suffix not in SUPPORTED_EXT or (allowed_exts and suffix not in allowed_exts):
                    continue
                key = self._norm_path(cached.path)
                try:
                    file_stat = cached.path.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                except OSError:
                    # A transient permission/readability failure must not turn
                    # a retained source into a false deletion. Keep the prior
                    # metadata and current source attribution until a later
                    # readable reconciliation can establish a change.
                    out_map[origin][key] = FileRecord(
                        path=cached.path,
                        origin=origin,
                        size=cached.size,
                        mtime=cached.mtime,
                        source_id=source_id,
                        source_label=source_label,
                    )
                    seen_files[origin].add(key)
                    continue
                if not stat_module.S_ISREG(file_stat.st_mode):
                    continue
                record = FileRecord(
                    path=cached.path,
                    origin=origin,
                    size=file_stat.st_size,
                    mtime=file_stat.st_mtime,
                    source_id=source_id,
                    source_label=source_label,
                )
                out_map[origin][key] = record
                seen_files[origin].add(key)

            # The unchanged tree is represented by the cached directory index.
            # stat each child without following links so disappearance or a
            # directory-to-symlink replacement prunes its old subtree.
            for child_norm in self._cached_children_by_parent.get(base_norm, ()):
                self._check_cancelled()
                child = Path(child_norm)
                self._scan_changed_recursive(
                    child,
                    origin,
                    allowed_exts,
                    out_map,
                    dir_mtimes,
                    seen_files,
                    changed_dirs,
                    reused_dirs,
                    source_id=source_id,
                    source_label=source_label,
                )
            return
        changed_dirs[origin].add(base_norm)
        try:
            with os.scandir(base) as entries:
                for dent in entries:
                    self._check_cancelled()
                    try:
                        if dent.is_dir(follow_symlinks=False):
                            self._scan_changed_recursive(
                                Path(dent.path),
                                origin,
                                allowed_exts,
                                out_map,
                                dir_mtimes,
                                seen_files,
                                changed_dirs,
                                reused_dirs,
                                source_id=source_id,
                                source_label=source_label,
                            )
                            continue
                        if not dent.is_file(follow_symlinks=False):
                            continue
                        if is_fio_bbs_helper_file_name(dent.name):
                            continue
                        suffix = Path(dent.name).suffix.lower()
                        if suffix not in SUPPORTED_EXT:
                            continue
                        if allowed_exts and suffix not in allowed_exts:
                            continue
                        stat = dent.stat()
                        rec = FileRecord(
                            path=Path(dent.path),
                            origin=origin,
                            size=stat.st_size,
                            mtime=stat.st_mtime,
                            source_id=source_id,
                            source_label=source_label,
                        )
                        key = self._norm_path(rec.path)
                        out_map[origin][key] = rec
                        seen_files[origin].add(key)
                    except OSError:
                        continue
        except OSError:
            return

    def _scan_changed_bbs(
        self,
        base: Path,
        out_map: Dict[str, Dict[str, FileRecord]],
        dir_mtimes: DirectoryMtimeSnapshot,
        seen_files: Dict[str, set[str]],
        changed_dirs: Dict[str, set[str]],
        reused_dirs: Dict[str, set[str]],
        *,
        source_id: str = "",
        source_label: str = "",
    ) -> None:
        base_norm = self._norm_path(base)
        try:
            directory_stat = base.stat()
            current_mtime = float(directory_stat.st_mtime)
        except OSError:
            return
        dir_mtimes.record(base_norm, directory_stat)
        previous_mtime = self._base_dir_mtimes.get(base_norm)
        current_identity = (
            int(directory_stat.st_dev),
            int(directory_stat.st_ino),
            int(directory_stat.st_mtime_ns),
        )
        if (
            previous_mtime is not None
            and self._base_dir_identities.get(base_norm) == current_identity
            and previous_mtime == current_mtime
        ):
            reused_dirs["bbs"].add(base_norm)
            return
        changed_dirs["bbs"].add(base_norm)
        try:
            with os.scandir(base) as entries:
                for dent in entries:
                    self._check_cancelled()
                    try:
                        if not dent.is_file(follow_symlinks=False):
                            continue
                        if is_fio_bbs_helper_file_name(dent.name):
                            continue
                        suffix = Path(dent.name).suffix.lower()
                        if suffix not in SUPPORTED_EXT:
                            continue
                        stat = dent.stat()
                        rec = FileRecord(
                            path=Path(dent.path),
                            origin="bbs",
                            size=stat.st_size,
                            mtime=stat.st_mtime,
                            source_id=source_id,
                            source_label=source_label,
                        )
                        key = self._norm_path(rec.path)
                        out_map["bbs"][key] = rec
                        seen_files["bbs"].add(key)
                    except OSError:
                        continue
        except OSError:
            return

    @staticmethod
    def _finalize_maps(records_map: Dict[str, Dict[str, FileRecord]]) -> Dict[str, List[FileRecord]]:
        out: Dict[str, List[FileRecord]] = {"varac": [], "flmsg": [], "flamp": [], "bbs": []}
        for origin in out:
            out[origin] = sorted(records_map.get(origin, {}).values(), key=lambda item: item.mtime, reverse=True)
        return out

    def _run_full(self) -> tuple[Dict[str, List[FileRecord]], Dict[str, float]]:
        records_map = self._empty_result()
        dir_mtimes: DirectoryMtimeSnapshot = DirectoryMtimeSnapshot()
        for entry in self._watch_dirs:
            self._check_cancelled()
            origin = str(entry.get("origin", "") or "").strip().lower()
            if origin not in records_map:
                continue
            path = str(entry.get("path", "") or "").strip()
            if not path:
                continue
            source_id = str(entry.get("source_id", "") or "").strip()
            source_label = str(entry.get("source_label", "") or "").strip()
            base = Path(path)
            if not base.exists():
                continue
            self._full_scan_recursive(
                base,
                origin,
                ORIGIN_EXTS.get(origin),
                records_map,
                dir_mtimes,
                source_id=source_id,
                source_label=source_label,
            )
        return self._finalize_maps(records_map), dir_mtimes

    def _run_incremental(self) -> tuple[Dict[str, List[FileRecord]], Dict[str, float]]:
        records_map = self._empty_result()
        # The per-scanner indexes own defensive FileRecord copies, so the
        # initial result map can reuse those immutable-for-this-scan values
        # without copying the inventory a second time or borrowing caller rows.
        for (origin_norm, _parent_norm), rows in self._cached_files_by_parent_origin.items():
            for rec in rows or []:
                self._check_cancelled()
                key = self._norm_path(rec.path)
                records_map[origin_norm][key] = rec

        dir_mtimes: DirectoryMtimeSnapshot = DirectoryMtimeSnapshot()
        seen_files: Dict[str, set[str]] = {"varac": set(), "flmsg": set(), "flamp": set(), "bbs": set()}
        changed_dirs: Dict[str, set[str]] = {"varac": set(), "flmsg": set(), "flamp": set(), "bbs": set()}
        reused_dirs: Dict[str, set[str]] = {"varac": set(), "flmsg": set(), "flamp": set(), "bbs": set()}
        missing_roots: Dict[str, set[str]] = {"varac": set(), "flmsg": set(), "flamp": set(), "bbs": set()}

        for entry in self._watch_dirs:
            self._check_cancelled()
            origin = str(entry.get("origin", "") or "").strip().lower()
            if origin not in records_map:
                continue
            path = str(entry.get("path", "") or "").strip()
            if not path:
                continue
            source_id = str(entry.get("source_id", "") or "").strip()
            source_label = str(entry.get("source_label", "") or "").strip()
            base = Path(path)
            base_norm = self._norm_path(base)
            if not base.exists():
                missing_roots[origin].add(base_norm)
                continue
            self._scan_changed_recursive(
                base,
                origin,
                ORIGIN_EXTS.get(origin),
                records_map,
                dir_mtimes,
                seen_files,
                changed_dirs,
                reused_dirs,
                source_id=source_id,
                source_label=source_label,
                is_watch_root=True,
            )

        for origin, path_map in records_map.items():
            roots = set(self._roots_by_origin.get(origin, []))
            changed = changed_dirs.get(origin, set())
            seen = seen_files.get(origin, set())
            missing = missing_roots.get(origin, set())
            missing_dirs = {
                self._norm_path(previous_dir)
                for previous_dir in self._base_dir_mtimes
                if self._is_under_any(self._norm_path(previous_dir), roots)
                and self._norm_path(previous_dir) not in dir_mtimes
            }
            if not roots:
                path_map.clear()
                continue
            for key in list(path_map.keys()):
                if not self._is_under_any(key, roots):
                    path_map.pop(key, None)
                    continue
                if missing and self._is_under_any(key, missing):
                    path_map.pop(key, None)
                    continue
                if missing_dirs and self._is_under_any(key, missing_dirs):
                    path_map.pop(key, None)
                    continue
                parent = self._norm_path(Path(key).parent)
                if parent in changed:
                    if key in seen:
                        continue
                    path_map.pop(key, None)

        return self._finalize_maps(records_map), dir_mtimes

    def scan_with_delta(self) -> tuple[Dict[str, List[FileRecord]], Dict[str, float], str, FileScanDelta]:
        """Scan once and return the legacy result plus a snapshot delta.

        Unchanged directories use cached entry indexes and stat only known
        supported files, without rescanning directory entries or reading file
        contents. Delta comparison is against retained snapshots, not a second
        filesystem walk, so this remains safe on the UI scan completion boundary.
        """
        self._check_cancelled()
        have_base = bool(self._base_dir_mtimes) or any(
            bool(value) for value in (self._base_records or {}).values()
        )
        mode = "full" if self._force or not have_base else "incremental"
        walk_started = time.perf_counter()
        try:
            if mode == "full":
                records, dir_mtimes = self._run_full()
            else:
                records, dir_mtimes = self._run_incremental()
        except OperationCancelled:
            raise
        except Exception:
            mode = "fallback"
            records, dir_mtimes = self._run_full()
        finally:
            # Metadata only: paths, message contents, and station data are not
            # emitted in diagnostics.
            emit_span(
                "messages.file_walk",
                (time.perf_counter() - walk_started) * 1000.0,
                meta={"force": self._force, "mode": mode},
                min_ms=5.0,
            )

        delta_started = time.perf_counter()
        try:
            self._check_cancelled()
            delta = FileScanDelta.compare(self._base_records, records)
            self._check_cancelled()
        except OperationCancelled:
            raise
        except Exception:
            if mode == "fallback":
                raise
            mode = "fallback"
            walk_started = time.perf_counter()
            records, dir_mtimes = self._run_full()
            emit_span(
                "messages.file_walk",
                (time.perf_counter() - walk_started) * 1000.0,
                meta={"force": self._force, "mode": mode},
                min_ms=5.0,
            )
            delta_started = time.perf_counter()
            delta = FileScanDelta.compare(self._base_records, records)
        finally:
            emit_span(
                "messages.file_delta",
                (time.perf_counter() - delta_started) * 1000.0,
                meta={"mode": mode, "records": sum(len(values) for values in records.values())},
                min_ms=5.0,
            )
        return records, dir_mtimes, mode, delta

    def scan(self) -> tuple[Dict[str, List[FileRecord]], Dict[str, float], str]:
        """Legacy scan API retained for all existing callers."""

        records, dir_mtimes, mode, _delta = self.scan_with_delta()
        return records, dir_mtimes, mode
