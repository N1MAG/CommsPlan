"""Independent ISED assigned-callsign snapshot. All operations are worker-only."""
from __future__ import annotations

from contextlib import contextmanager, closing
from datetime import datetime, timezone
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import threading
import time
from zipfile import ZipFile

SOURCE_URL = "https://apc-cap.ic.gc.ca/datafiles/amateur_delim.zip"
SOURCE_NOTICE = ("Amateur call sign list — Innovation, Science and Economic Development Canada. "
                 "Local copy of " + SOURCE_URL)
HEADER = ("callsign", "first_name", "surname", "address_line", "city", "prov_cd", "postal_code",
          "qual_a", "qual_b", "qual_c", "qual_d", "qual_e", "club_name", "club_name_2",
          "club_address", "club_city", "club_prov_cd", "club_postal_code")
PROVINCES = tuple("AB BC MB NB NL NS NT NU ON PE QC SK YT".split())
MIN_ROWS, MAX_ROWS = 50000, 500000
MAX_BYTES = 32 * 1024 * 1024
_CALL = re.compile(r"[A-Z0-9]{3,12}\Z", re.ASCII)


class IsedReference:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / "ised_amateur.sqlite3"

    @contextmanager
    def read(self):
        if not self.path.is_file():
            yield None
            return
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=1)) as conn:
            if conn.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise ValueError("Unsupported ISED reference version; refresh Canada")
            conn.row_factory = sqlite3.Row
            yield conn

    def download(self, cancel, progress, **kwargs):
        import requests
        from freqinout.core.call_finder import ImportCancelled
        self.root.mkdir(parents=True, exist_ok=True)
        if cancel.is_set():
            raise ImportCancelled("Canada download cancelled; previous reference retained")
        deadline = time.monotonic() + 180
        with tempfile.TemporaryDirectory(prefix="ised-download-", dir=self.root) as directory:
            path = Path(directory) / "amateur_delim.zip"
            progress("Canada: contacting ISED")
            with requests.get(SOURCE_URL, stream=True, timeout=(15, 20), allow_redirects=False,
                              headers={"User-Agent": "CommsPlan offline callsign reference"}) as response:
                if response.status_code != 200:
                    raise ValueError(f"ISED returned HTTP {response.status_code}; use a local archive or retry later")
                length = response.headers.get("Content-Length", "")
                expected = int(length) if length.isdigit() else None
                if expected and expected > MAX_BYTES:
                    raise ValueError("ISED archive exceeds 32 MiB")
                size = 0
                with path.open("wb") as output:
                    for block in response.iter_content(256 * 1024):
                        if cancel.is_set():
                            raise ImportCancelled("Canada download cancelled; previous reference retained")
                        if time.monotonic() > deadline:
                            raise ValueError("ISED download timed out")
                        size += len(block)
                        if size > MAX_BYTES:
                            raise ValueError("ISED archive exceeds 32 MiB")
                        output.write(block)
                        progress(f"Canada: downloaded {size // 1024:,} KiB")
                if not size or (expected is not None and size != expected):
                    raise ValueError("ISED archive download is incomplete")
            return self.refresh(path, cancel, progress, **kwargs)

    def refresh(self, source, cancel=None, progress=None, *, accept_count_drop=False):
        from freqinout.core import call_finder as reference
        cancel = cancel or threading.Event()
        progress = progress or (lambda _message: None)
        source = Path(source).resolve()
        if source.is_dir():
            candidates = {}
            for entry in source.iterdir():
                if entry.is_file() and entry.name.lower() in ("amateur_delim.txt","amateur_delim.zip"):
                    if entry.name.lower() in candidates:
                        raise ValueError("Duplicate ISED source filenames; choose one archive explicitly")
                    candidates[entry.name.lower()] = entry
            source = candidates.get("amateur_delim.txt") or candidates.get("amateur_delim.zip")
            if source is None:
                raise ValueError("Choose a folder containing amateur_delim.zip or amateur_delim.txt")
        if source == self.path.resolve():
            raise ValueError("Choose the ISED source, not the reference database")
        self.root.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="ised-build-", suffix=".sqlite3", dir=self.root)
        os.close(fd)
        temporary = Path(name)
        conn = None

        def check():
            if cancel.is_set():
                raise reference.ImportCancelled("Canada refresh cancelled; previous reference retained")

        @contextmanager
        def stream():
            if source.suffix.lower() == ".zip":
                if source.stat().st_size > MAX_BYTES:
                    raise ValueError("ISED archive exceeds 32 MiB")
                with ZipFile(source) as archive:
                    entries = [x for x in archive.infolist() if x.filename.lower() == "amateur_delim.txt"]
                    if len(archive.infolist()) > 32 or len(entries) != 1:
                        raise ValueError("ISED ZIP needs exactly one amateur_delim.txt table")
                    if entries[0].file_size > 64 * 1024 * 1024:
                        raise ValueError("ISED table exceeds 64 MiB")
                    with archive.open(entries[0]) as handle:
                        yield handle
            else:
                if source.name.lower() != "amateur_delim.txt":
                    raise ValueError("Expected amateur_delim.txt or amateur_delim.zip")
                with source.open("rb") as handle:
                    yield handle

        try:
            check()
            conn = sqlite3.connect(temporary)
            conn.executescript("""
                PRAGMA journal_mode=DELETE; PRAGMA synchronous=OFF;
                PRAGMA cache_size=-8192; PRAGMA temp_store=FILE;
                CREATE TABLE licensee(callsign TEXT PRIMARY KEY,name TEXT,first_name TEXT,last_name TEXT,
                  state TEXT,service TEXT,kind TEXT,license_class TEXT,frn TEXT,fcc_id TEXT,expires TEXT,
                  provider TEXT,jurisdiction TEXT,source_key TEXT,postal_code TEXT,qualifications TEXT,
                  sponsor_name TEXT,source_notice TEXT);
                CREATE TABLE metadata(payload TEXT NOT NULL);
            """)
            digest, size, rows, duplicates = hashlib.sha256(), 0, 0, 0

            def lines(handle):
                nonlocal size
                while True:
                    check()
                    line = handle.readline(16385)
                    if not line:
                        return
                    size += len(line)
                    if len(line) > 16384 or size > 64 * 1024 * 1024:
                        raise ValueError("ISED table/line exceeds supported limits")
                    digest.update(line)
                    yield line.decode("utf-8-sig" if size == len(line) else "utf-8")

            with stream() as handle:
                # ISED's delimited export is not RFC CSV: quotes in names are
                # literal (including unmatched quotes), not field delimiters.
                reader = csv.reader(lines(handle), delimiter=";", quoting=csv.QUOTE_NONE)
                if tuple(next(reader, ())) != HEADER:
                    raise ValueError("ISED table header does not match the supported 18-column format")
                for row in reader:
                    if len(row) != 18:
                        raise ValueError("Malformed ISED record; expected 18 fields")
                    row = [value.strip() for value in row]
                    call = row[0].upper()
                    if not _CALL.fullmatch(call) or any(row[i] not in ("", flag) for i, flag in enumerate("ABCDE", 7)):
                        raise ValueError("Invalid ISED callsign or qualification flags")
                    club = any(row[12:])
                    if club and not row[12]:
                        raise ValueError("ISED club fields have no club name")
                    region, postal = (row[16], row[17]) if club else (row[5], row[6])
                    region = region.upper()
                    if region and region not in PROVINCES:
                        raise ValueError("Unknown ISED province/territory code")
                    flags = "".join(row[7:12])
                    summary = " · ".join(label for flag, label in (("A", "Basic"), ("E", "Honours"), ("D", "Advanced")) if flag in flags)
                    first, last = ("", "") if club else (row[1], row[2])
                    display = " ".join(row[12:14]) if club else ", ".join(x for x in (last, first) if x)
                    values = (call, display, first, last, region, "Amateur", "Club" if club else "Individual",
                              "" if club else summary, "", "", "", "ised_amateur", "CA", call,
                              re.sub(r"\s+", "", postal.upper()), flags,
                              " ".join(row[1:3]) if club else "", SOURCE_NOTICE)
                    try:
                        conn.execute("INSERT INTO licensee VALUES (" + ",".join("?" for _ in values) + ")", values)
                    except sqlite3.IntegrityError:
                        previous = tuple(conn.execute("SELECT * FROM licensee WHERE callsign=?", (call,)).fetchone())
                        if previous != values:
                            raise ValueError("Conflicting duplicate ISED callsign; previous reference retained")
                        duplicates += 1
                    rows += 1
                    if rows > MAX_ROWS:
                        raise ValueError("ISED record count exceeds supported limit")
                    if rows % 4096 == 0:
                        conn.commit()
                        progress(f"Canada: validated {rows:,} assigned callsigns")
            unique = rows - duplicates
            if not MIN_ROWS <= unique <= MAX_ROWS:
                raise ValueError("ISED assigned callsign count is outside the supported national range")
            old_count = 0
            try:
                with reference._LOCK, self.read() as previous:
                    if previous is not None:
                        old_count = reference.CallFinderStore._status(previous).active_rows
            except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
                pass  # A validated source can repair a damaged reference.
            if old_count and unique < old_count * 0.9 and not accept_count_drop:
                raise ValueError(f"ISED count fell from {old_count:,} to {unique:,} (>10%). Review the source and use Accept smaller snapshot; previous reference retained")
            refreshed = datetime.now(timezone.utc).isoformat(timespec="seconds")
            meta = reference.SourceStatus("Canada", "", refreshed, unique, duplicates, source.name,
                                          provider="ised_amateur", jurisdiction="CA")
            conn.execute("INSERT INTO metadata VALUES (?)", (json.dumps({**meta.__dict__, "sha256": digest.hexdigest(),
                         "source_notice": SOURCE_NOTICE, "accepted_count_drop": bool(accept_count_drop)}),))
            conn.executescript("CREATE INDEX licensee_state_call ON licensee(state,callsign); PRAGMA user_version=1;")
            conn.commit()
            conn.execute("ANALYZE")
            conn.commit()
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("ISED reference integrity check failed")
            conn.close()
            conn = None
            check()
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            with reference._LOCK:
                check()
                if accept_count_drop and old_count and unique < old_count * 0.9:
                    # One-time reviewed recovery keeps the previous national
                    # reference available for rollback, without doubling normal I/O.
                    backup = self.path.with_suffix(".previous.sqlite3")
                    backup_stage = temporary.with_suffix(".previous")
                    try:
                        shutil.copyfile(self.path,backup_stage)
                        with backup_stage.open("rb") as handle:
                            os.fsync(handle.fileno())
                        os.replace(backup_stage,backup)
                    finally:
                        backup_stage.unlink(missing_ok=True)
                os.replace(temporary, self.path)
                reference._CACHE_REVISION += 1
            return meta
        finally:
            if conn is not None:
                conn.close()
            temporary.unlink(missing_ok=True)
