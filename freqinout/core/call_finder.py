"""Independent, rebuildable FCC license reference. No managed-operator writes.

Disk operations belong on workers. cached_exact() is a memory-only consumer API.
"""
from __future__ import annotations

from contextlib import contextmanager, closing
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time
from typing import Callable
from zipfile import ZipFile
from freqinout.core.zip_grid import normalize_zip, US_MAIL_STATES, ESTIMATED_GRID_DESCRIPTION


SERVICES = {"Amateur": ("HA", "HV"), "GMRS": ("ZA",)}
CLASSES = {"T": "Technician", "G": "General", "E": "Amateur Extra", "A": "Advanced", "N": "Novice", "P": "Technician Plus"}
APPLICANT_TYPES = {"I": "Individual", "B": "Club", "M": "Military Recreation", "R": "RACES",
                   "C": "Corporation", "G": "Government", "U": "Association", "P": "Partnership",
                   "L": "LLC", "T": "Trust", "O": "Consortium"}
FIELD_COUNTS = {"hd.dat": 59, "en.dat": 30, "am.dat": 18}
SCHEMA_VERSION = 2
_CALL = re.compile(r"[A-Z0-9]{3,12}\Z", re.ASCII)
_FRN = re.compile(r"[0-9]{10}\Z", re.ASCII)
_LOCK = threading.RLock()
_CACHE_REVISION = 0
EXACT_BATCH_LIMIT = 128
EXACT_CACHE_LIMIT = 4096
_DEFAULT_STORES: dict[str, "CallFinderStore"] = {}
_DEFAULT_STORES_LOCK = threading.Lock()


def default_call_finder_store():
    """Share bounded identity results within a profile without resolving disk paths."""
    key = os.environ.get("FREQINOUT_CONFIG_DIR", "")
    with _DEFAULT_STORES_LOCK:
        if key not in _DEFAULT_STORES:
            if len(_DEFAULT_STORES) >= 4:
                _DEFAULT_STORES.pop(next(iter(_DEFAULT_STORES)))
            root = Path(key) / "cache" / "fcc_call_finder" if key else None
            _DEFAULT_STORES[key] = CallFinderStore(root)
        return _DEFAULT_STORES[key]


@dataclass(frozen=True)
class LicenseRecord:
    callsign: str
    name: str
    first_name: str
    last_name: str
    state: str
    service: str
    kind: str
    license_class: str
    frn: str
    fcc_id: str
    expires: str
    source_date: str
    refreshed_at: str
    mail_zip: str = ""
    zip_reason: str = ""
    estimated_grid6: str = ""  # Compatibility name: estimated Grid4 or Grid6 only.
    grid_source: str = ""
    grid_reason: str = ""
    grid_vintage: str = ""
    provider: str = ""
    jurisdiction: str = ""
    source_key: str = ""
    postal_code: str = ""
    qualifications: str = ""
    sponsor_name: str = ""
    source_notice: str = ""


@dataclass(frozen=True)
class SourceStatus:
    service: str
    source_date: str
    refreshed_at: str
    active_rows: int
    skipped_rows: int
    source_name: str
    zip_ready: bool = False
    provider: str = ""
    jurisdiction: str = ""


@dataclass(frozen=True)
class SearchResult:
    rows: tuple[LicenseRecord, ...] = ()
    has_more: bool = False
    sources: tuple[SourceStatus, ...] = ()
    errors: tuple[str, ...] = ()
    kind: str = "search"
    links: tuple[tuple[LicenseRecord, LicenseRecord], ...] = ()
    grid_status: object | None = None
    anchor: LicenseRecord | None = None
    complete: bool = True
    sources_complete: bool = True
    canada_grid_status: object | None = None


def reference_key(record):
    """Identity of a source record, never an inferred person identity."""
    return (record.provider or f"fcc_{record.service.lower()}",
            record.source_key or record.fcc_id or record.callsign)


class ImportCancelled(RuntimeError):
    pass


def valid_frn(value: str) -> str:
    value = value.strip()
    return value if _FRN.fullmatch(value) and int(value) else ""


def _postal_exclusion(state: str, postal: str) -> str:
    if state in {"AA", "AE", "AP"}:
        return "military"
    if state not in US_MAIL_STATES:
        return "unsupported_state"
    return "invalid_zip" if not postal else ""


def _resolve_source(service: str, source: Path, required: tuple[str, ...]) -> Path:
    """A chosen folder can hold extracted tables or the official service ZIP."""
    if not source.is_dir():
        return source
    entries = tuple(source.iterdir())
    names = {p.name.lower() for p in entries if p.is_file()}
    if all(name in names for name in required):
        return source
    archive_name = "l_amat.zip" if service == "Amateur" else "l_gmrs.zip"
    archives = [p for p in entries if p.name.lower() == archive_name and p.is_file()]
    if len(archives) > 1:
        raise ValueError(f"FCC folder contains duplicate {archive_name} archives; choose one archive explicitly")
    if archives:
        return archives[0]
    table_names = ", ".join(name.upper() for name in required)
    raise ValueError(f"Choose a folder containing extracted {service} tables ({table_names}) or {archive_name}; alternatively use Choose archive .zip")


@contextmanager
def _source_files(source: Path, required: tuple[str, ...]):
    """Open exact table names without extracting untrusted ZIP members."""
    if source.is_dir():
        files = {}
        for p in source.iterdir():
            if p.name.lower() in (*required, "counts"):
                if p.name.lower() in files:
                    raise ValueError("Duplicate FCC source table")
                files[p.name.lower()] = p
        if not all(n in files for n in required):
            raise ValueError("FCC folder is missing required license tables")
        @contextmanager
        def open_member(name):
            with files[name].open("rb") as stream:
                yield stream
        yield files, open_member
    else:
        with ZipFile(source) as archive:
            files = {}
            for item in archive.infolist():
                name = item.filename.lower()
                if name in (*required, "counts"):
                    if name in files:
                        raise ValueError("Duplicate FCC archive table")
                    files[name] = item.filename
            if not all(n in files for n in required):
                raise ValueError("FCC ZIP is missing required license tables")
            @contextmanager
            def open_member(name):
                with archive.open(files[name]) as stream:
                    yield stream
            yield files, open_member


class CallFinderStore:
    def __init__(self, root: Path | None = None):
        self._root = Path(root) if root is not None else None
        self._exact_cache = OrderedDict()
        self._exact_lock = threading.Lock()
        self._exact_revision = _CACHE_REVISION
        self._exact_source_errors = {}
        self._installed_sources = set()

    def cached_exact(self, callsign: str) -> tuple[LicenseRecord, ...] | None:
        """O(1), memory-only. None means not cached; () is a known recent miss.

        This lock never guards file/SQLite/network work and cannot wait on an
        importer. Positive entries live 10 minutes; misses live 30 seconds.
        """
        call = callsign.strip().upper()
        with self._exact_lock:
            if self._exact_revision != _CACHE_REVISION:
                self._exact_cache.clear()
                self._exact_source_errors.clear()
                self._exact_revision = _CACHE_REVISION
            entry = self._exact_cache.get(call)
            if entry is None:
                return None
            expires, records, participants = entry
            if participants != frozenset(self._installed_sources):
                del self._exact_cache[call]
                return None
            if expires <= time.monotonic():
                del self._exact_cache[call]
                return None
            self._exact_cache.move_to_end(call)
            return records

    def exact_many(self, callsigns) -> SearchResult:
        """Bounded indexed batch for NCS/future heard-station enrichment workers.

        Deduplicates input; never scans a national table or runs FRN joins.
        Callers drain larger inventories in separate batches with backpressure.
        """
        revision = _CACHE_REVISION
        unique = {}
        for index, call in enumerate(callsigns):
            if index >= EXACT_BATCH_LIMIT:
                raise ValueError(f"FCC exact lookup batch exceeds {EXACT_BATCH_LIMIT} callsigns")
            normalized = str(call).strip().upper()
            if _CALL.fullmatch(normalized):
                unique[normalized] = None
        calls = tuple(unique)
        rows, missing, errors, sources = {}, [], [], []
        for call in calls:
            cached = self.cached_exact(call)
            if cached is None:
                missing.append(call)
            else:
                rows[call] = cached
        if not missing:
            if revision != _CACHE_REVISION:
                return SearchResult(kind="exact_retry")
            return SearchResult(tuple(r for call in calls for r in rows[call]), kind="exact")
        found = {call: [] for call in missing}
        for service in (*SERVICES, "Canada"):
            with self._exact_lock:
                failure = self._exact_source_errors.get(service)
            if failure is not None and failure[0] > time.monotonic():
                errors.append(failure[1])
                continue
            try:
                with self._read(service) as conn:
                    if conn is None:
                        continue
                    meta = self._status(conn)
                    sources.append(meta)
                    marks = ",".join("?" for _ in missing)
                    for row in conn.execute(f"SELECT * FROM licensee WHERE callsign IN ({marks})", missing):
                        found[row["callsign"]].append(self._record(row, meta))
            except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, IndexError) as exc:
                message = f"{service} reference unavailable: {exc}"
                errors.append(message)
                with self._exact_lock:
                    if revision == _CACHE_REVISION:
                        self._exact_cache.clear()
                        self._exact_source_errors[service] = (time.monotonic() + 30, message)
        now = time.monotonic()
        with self._exact_lock:
            if revision == _CACHE_REVISION:
                if self._exact_revision != revision:
                    self._exact_cache.clear()
                    self._exact_revision = revision
                for call, records in found.items():
                    # A failed source must not turn an unknown identity into a
                    # cached miss. Retry on later activity instead.
                    if not errors:
                        self._exact_cache[call] = (now + (600 if records else 30), tuple(records),
                                                   frozenset(self._installed_sources))
                        self._exact_cache.move_to_end(call)
                while len(self._exact_cache) > EXACT_CACHE_LIMIT:
                    self._exact_cache.popitem(last=False)
        rows.update({call: tuple(records) for call, records in found.items()})
        if revision != _CACHE_REVISION:
            return SearchResult(kind="exact_retry")
        return SearchResult(tuple(r for call in calls for r in rows[call]),
                            sources=tuple(sources), errors=tuple(errors), kind="exact", complete=not errors)

    @property
    def root(self) -> Path:
        if self._root is None:
            from freqinout.core.config_paths import get_config_dir
            self._root = get_config_dir() / "cache" / "fcc_call_finder"
        return self._root

    def _path(self, service: str) -> Path:
        if service == "Canada":
            return self.root / "ised_amateur.sqlite3"
        if service not in SERVICES:
            raise ValueError("Unknown FCC service")
        return self.root / (service.lower() + ".sqlite3")

    @contextmanager
    def _read(self, service):
        # Open+query+close serialized against publication, not against the build.
        with _LOCK:
            path = self._path(service)
            if not path.is_file():
                if service in self._installed_sources:
                    raise ValueError(f"Installed {service} reference is missing; refresh this source")
                yield None
                return
            with self._exact_lock:
                self._installed_sources.add(service)
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as conn:
                if conn.execute("PRAGMA user_version").fetchone()[0] not in ((1,) if service == "Canada" else (1, SCHEMA_VERSION)):
                    raise ValueError("Unsupported Call Finder cache version; refresh this source")
                conn.row_factory = sqlite3.Row
                yield conn

    @staticmethod
    def _status(conn) -> SourceStatus:
        info = json.loads(conn.execute("SELECT payload FROM metadata").fetchone()[0])
        return SourceStatus(**{k: info[k] for k in SourceStatus.__dataclass_fields__ if k in info})

    @staticmethod
    def _record(row, meta):
        values = dict(row)
        if meta.provider == "ised_amateur":
            return LicenseRecord(**values, source_date=meta.source_date, refreshed_at=meta.refreshed_at,
                                 grid_reason="Canadian postal-grid estimates are not loaded.")
        if not meta.zip_ready:
            values["zip_reason"] = "legacy"
        elif values.get("zip_reason") == "po_box":
            # Old v2 caches retain ZIP/state, so reinterpret the former flag
            # without modifying disk or requiring a national-source rebuild.
            values["zip_reason"] = _postal_exclusion(values.get("state", ""),
                                                     normalize_zip(values.get("mail_zip", "")))
        return LicenseRecord(**values, source_date=meta.source_date, refreshed_at=meta.refreshed_at)

    def _query(self, prefix="", service="", state="", *, record=None, limit=50, kind="search", country=""):
        limit = max(1, min(int(limit), 50))
        rows, sources, errors = [], [], []
        providers = ("Canada",) if country == "CA" else tuple(SERVICES) if country == "US" else (*SERVICES, "Canada")
        if record is not None:
            providers = () if record.provider == "ised_amateur" else tuple(SERVICES)
        for svc in providers:
            try:
                with self._read(svc) as conn:
                    if conn is None:
                        if country == "CA":
                            errors.append("Canada reference is not loaded. Open Data sources to download or import ISED data.")
                        continue
                    meta = self._status(conn)
                    sources.append(meta)
                    if service and ("Amateur" if svc == "Canada" else svc) != service:
                        continue
                    if kind == "status" or (kind == "search" and not _CALL.fullmatch(prefix)):
                        continue
                    if kind == "related":
                        if not valid_frn(record.frn):
                            continue
                        sql = "SELECT * FROM licensee WHERE frn=? ORDER BY callsign LIMIT ?"
                        args = [record.frn, limit + 2]
                    else:
                        sql = "SELECT * FROM licensee WHERE callsign>=? AND callsign<?"
                        args = [prefix, prefix + "["]
                        if state:
                            sql += " AND state=?"
                            args.append(state)
                        sql += " ORDER BY callsign LIMIT ?"
                        args.append(limit + 1)
                    for row in conn.execute(sql, args):
                        if record and row["fcc_id"] == record.fcc_id and row["service"] == record.service:
                            continue
                        rows.append(self._record(row, meta))
            except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, IndexError) as exc:
                errors.append(f"{svc} reference unavailable: {exc}")
        rows.sort(key=lambda r: (r.callsign != prefix, r.callsign, r.jurisdiction or "US", reference_key(r)))
        return SearchResult(tuple(rows[:limit]), len(rows) > limit, tuple(sources), tuple(errors), kind,
                            sources_complete=not country)

    def search(self, prefix: str, service: str = "", state: str = "", limit: int = 50, *, country: str = ""):
        if service and service not in SERVICES:
            raise ValueError("Unknown FCC service")
        if country not in ("", "US", "CA"):
            raise ValueError("Unknown licensing country")
        revision = _CACHE_REVISION
        result = self._query(prefix.strip().upper(), service, state.strip().upper(), limit=limit, country=country)
        return self._with_grids(self._display_links(result, revision))

    def _display_links(self, result: SearchResult, revision: int) -> SearchResult:
        """Worker-only bounded display enrichment, never the NCS exact path.

        Two indexed rows per FRN/service are enough to reject any ambiguous
        registration. A link is deliberately stricter than FRN relatedness.
        """
        frns = tuple(dict.fromkeys(row.frn for row in result.rows
                                  if row.kind == "Individual" and valid_frn(row.frn)))
        if not frns or result.errors:
            return result
        found = {frn: {} for frn in frns}
        try:
            for service in SERVICES:
                with self._read(service) as conn:
                    if conn is None:
                        return result
                    meta = self._status(conn)
                    for frn in frns:
                        records = tuple(self._record(row, meta) for row in conn.execute(
                            "SELECT * FROM licensee WHERE frn=? LIMIT 2", (frn,)))
                        found[frn][service] = records
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, IndexError) as exc:
            return replace(result, errors=result.errors + (f"Related display unavailable: {exc}",))
        if revision != _CACHE_REVISION:
            return result
        links = []
        visible = set(result.rows)
        for services in found.values():
            amateur, gmrs = services.get("Amateur", ()), services.get("GMRS", ())
            if len(amateur) != 1 or len(gmrs) != 1:
                continue
            left, right = amateur[0], gmrs[0]
            name = " ".join(left.name.casefold().split())
            if (left.kind == right.kind == "Individual" and name
                    and name == " ".join(right.name.casefold().split())
                    and left.state == right.state and (left in visible or right in visible)):
                links.append((left, right))
        return replace(result, links=tuple(links))

    def status(self):
        return self._with_grids(self._query(kind="status"))

    def related(self, record: LicenseRecord, limit: int = 50):
        revision = _CACHE_REVISION
        result = replace(self._query(record=record, limit=limit, kind="related"), anchor=record)
        return self._with_grids(self._display_links(result, revision))

    def _with_grids(self, result):
        from freqinout.core.zip_grid import ZipGridStore

        records = tuple(dict.fromkeys(result.rows + tuple(row for pair in result.links for row in pair)
                                      + ((result.anchor,) if result.anchor is not None else ())))
        reasons = {
            "legacy": "Refresh this FCC source to import mailing ZIPs.",
            "military": "Military mailing address; no geographic ZIP estimate.",
            "invalid_zip": "Mailing ZIP is missing or invalid.",
            "unsupported_state": "Mailing state is not a supported U.S. state or territory.",
        }
        from freqinout.core.canada_postal_grid import (
            CanadaPostalGridStore, normalize_postal,
            ESTIMATED_GRID_DESCRIPTION as CANADA_GRID_DESCRIPTION,
        )
        has_us = any(row.provider != "ised_amateur" for row in records)
        has_ca = any(row.provider == "ised_amateur" for row in records)
        # Metadata requests may read both, while a scoped populated search opens
        # only the relevant country's grid store. Never called by exact_many.
        status, points, failure = None, {}, ""
        canada_status, canada_points, canada_failure = None, {}, ""
        if has_us or (not records and (result.sources_complete or any(m.jurisdiction != "CA" for m in result.sources))):
            try:
                status, points = ZipGridStore(self.root).lookup_many(
                    row.mail_zip for row in records if row.provider != "ised_amateur" and row.mail_zip and not row.zip_reason)
            except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, IndexError) as exc:
                failure = f"ZIP grid reference unavailable: {exc}"
        if has_ca or (not records and (result.sources_complete or any(m.jurisdiction == "CA" for m in result.sources))):
            try:
                canada_status, canada_points = CanadaPostalGridStore(self.root).lookup_many(
                    row.postal_code for row in records if row.provider == "ised_amateur")
            except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, IndexError) as exc:
                canada_failure = f"Canada postal-grid reference unavailable: {exc}"
        enriched = {}
        for row in records:
            if row.provider == "ised_amateur":
                postal = normalize_postal(row.postal_code)
                point = canada_points.get(postal)
                reason = canada_failure or ("Load Canada postal grids in Data sources." if canada_status is None
                                            else "Mailing postal code has no full-code GeoNames match.")
                if not postal:
                    reason = "Mailing postal code is missing or invalid."
                elif point and row.state and point.province != row.state:
                    point = None
                    reason = "Mailing province and postal reference disagree; no grid estimate."
                if point:
                    reason = CANADA_GRID_DESCRIPTION
                    if not point.grid6:
                        reason = "Postal reference points span multiple Grid4 cells; no single grid estimate."
                    elif len(point.grid6) == 4:
                        reason += " Multiple postal points share Grid4; Grid6 is ambiguous."
                enriched[row] = replace(row, estimated_grid6=point.grid6 if point else "",
                    grid_source="GeoNames full Canadian postal representative points (CC BY 4.0)" if point else "",
                    grid_vintage=canada_status.vintage if point else "", grid_reason=reason)
                continue
            reason = reasons.get(row.zip_reason, row.zip_reason)
            if not reason and not row.mail_zip:
                reason = reasons["invalid_zip"]
            if not reason:
                reason = failure or ("Load ZIP grids in Data sources." if status is None
                                     else "Mailing ZIP has no matching Census ZCTA.")
            point = points.get(row.mail_zip) if not row.zip_reason else None
            enriched[row] = replace(row, estimated_grid6=point.grid6 if point else "",
                                    grid_source=f"U.S. Census Bureau {status.vintage} ZCTA representative point" if point else "",
                                    grid_vintage=status.vintage if point else "",
                                    grid_reason=ESTIMATED_GRID_DESCRIPTION if point else reason)
        return replace(result, rows=tuple(enriched[row] for row in result.rows),
                       links=tuple(tuple(enriched[row] for row in pair) for pair in result.links),
                       anchor=enriched[result.anchor] if result.anchor is not None else None,
                       grid_status=status, canada_grid_status=canada_status,
                       errors=result.errors + tuple(message for message in (failure, canada_failure) if message))

    def refresh_grid(self, source, cancel=None, progress=None):
        from freqinout.core.zip_grid import ZipGridStore
        return ZipGridStore(self.root).refresh(source, cancel, progress)

    def download_refresh_grid(self, cancel=None, progress=None):
        from freqinout.core.zip_grid import ZipGridStore
        return ZipGridStore(self.root).download_refresh(cancel, progress)

    def refresh_canada_grid(self, source, cancel=None, progress=None):
        from freqinout.core.canada_postal_grid import CanadaPostalGridStore
        return CanadaPostalGridStore(self.root).refresh(source, cancel, progress)

    def download_refresh_canada_grid(self, cancel=None, progress=None):
        from freqinout.core.canada_postal_grid import CanadaPostalGridStore
        return CanadaPostalGridStore(self.root).download_refresh(cancel, progress)

    def download_refresh(self, service: str, cancel: threading.Event | None = None,
                         progress: Callable[[str], None] | None = None, *, accept_count_drop=False) -> SourceStatus:
        """Download only the fixed official complete-license URL, then validate.

        Download staging is discarded on every exit. No credentials, arbitrary
        URL entry, extraction, or automatic scheduling is involved.
        """
        import requests
        if service == "Canada":
            from freqinout.core.ised_reference import IsedReference
            return IsedReference(self.root).download(cancel or threading.Event(), progress or (lambda _text: None),
                                                     accept_count_drop=accept_count_drop)
        if service not in SERVICES:
            raise ValueError("Unknown FCC service")
        cancel = cancel or threading.Event()
        progress = progress or (lambda _text: None)
        name = "l_amat.zip" if service == "Amateur" else "l_gmrs.zip"
        url = "https://data.fcc.gov/download/pub/uls/complete/" + name
        self.root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="fcc-download-", dir=self.root) as directory:
            path = Path(directory) / name
            if cancel.is_set():
                raise ImportCancelled("FCC download cancelled; previous reference retained")
            progress(f"{service}: contacting FCC")
            # Do not follow host changes or redirects to an HTML login/error page.
            with requests.get(url, stream=True, timeout=(15, 20), allow_redirects=False,
                              headers={"User-Agent": "CommsPlan FCC offline reference"}) as response:
                if response.status_code != 200:
                    raise ValueError(f"FCC download returned HTTP {response.status_code}; retry later or import a local ZIP")
                size = 0
                last_report = -1
                total = response.headers.get("Content-Length", "")
                expected = int(total) if total.isdigit() else None
                maximum = 1024 * 1024 * 1024
                if expected and expected > maximum:
                    raise ValueError("FCC archive exceeds the supported 1 GiB download limit")
                with path.open("wb") as output:
                    for block in response.iter_content(chunk_size=256 * 1024):
                        if cancel.is_set():
                            raise ImportCancelled("FCC download cancelled; previous reference retained")
                        size += len(block)
                        if size > maximum:
                            raise ValueError("FCC archive exceeds the supported 1 GiB download limit")
                        output.write(block)
                        mb = size // (1024 * 1024)
                        if mb // 5 != last_report:
                            last_report = mb // 5
                            progress(f"{service}: downloaded {mb:,} MiB" + (f" of {expected // (1024 * 1024):,} MiB" if expected else ""))
                if not size or (expected is not None and size != expected):
                    raise ValueError("FCC archive download is incomplete")
            return self.refresh(service, path, cancel, progress)

    def refresh(self, service: str, source: Path, cancel: threading.Event | None = None,
                progress: Callable[[str], None] | None = None, *, accept_count_drop=False) -> SourceStatus:
        global _CACHE_REVISION
        if service == "Canada":
            from freqinout.core.ised_reference import IsedReference
            return IsedReference(self.root).refresh(source, cancel, progress, accept_count_drop=accept_count_drop)
        destination = self._path(service)
        source = Path(source).resolve()
        if source == destination.resolve():
            raise ValueError("Choose FCC source files, not the active cache")
        cancel = cancel or threading.Event()
        progress = progress or (lambda _text: None)
        required = ("hd.dat", "en.dat", "am.dat") if service == "Amateur" else ("hd.dat", "en.dat")
        source = _resolve_source(service, source, required)
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=service.lower() + "-build-", suffix=".sqlite3", dir=destination.parent)
        os.close(fd)
        temporary = Path(temp_name)
        conn = None
        def check():
            if cancel.is_set():
                raise ImportCancelled("FCC refresh cancelled; previous reference retained")
        try:
            check()
            conn = sqlite3.connect(temporary)
            conn.set_progress_handler(lambda: int(cancel.is_set()), 10000)
            conn.executescript("""
                PRAGMA journal_mode=DELETE;
                PRAGMA synchronous=OFF;
                PRAGMA cache_size=-16384;
                PRAGMA temp_store=FILE;
                CREATE TABLE hd (fcc_id TEXT PRIMARY KEY, callsign TEXT UNIQUE, expires TEXT);
                CREATE TABLE en (fcc_id TEXT PRIMARY KEY, name TEXT, first_name TEXT, last_name TEXT, state TEXT, kind TEXT, frn TEXT, mail_zip TEXT, zip_reason TEXT);
                CREATE TABLE am (fcc_id TEXT PRIMARY KEY, license_class TEXT);
            """)
            skipped = 0
            counts, hashes = {}, {}
            bad_ids = {"en.dat": set(), "am.dat": set()}
            with _source_files(source, required) as (members, opener):
                created = ""
                if "counts" in members:
                    with opener("counts") as stream:
                        raw = stream.read(65537)
                    if len(raw) > 65536:
                        raise ValueError("FCC counts metadata is too large")
                    text = raw.decode("ascii", errors="replace")
                    match = re.search(r"File Creation Date:\s*(.+)", text)
                    if match:
                        # FCC supplies weekday/month/day/time/timezone/year. Preserve
                        # the labeled source timestamp verbatim, not local mtime.
                        created = match.group(1).strip()
                    for n in required:
                        match = re.search(r"(?m)^\s*(\d+)\s+[^\r\n]*[/\\]" + re.escape(n) + r"\s*$", text, re.I)
                        if match:
                            counts[n] = int(match.group(1))
                if not created:
                    created = "File date: " + datetime.fromtimestamp(source.stat().st_mtime, timezone.utc).isoformat()
                for name in required:
                    progress(f"{service}: reading {name.upper()}")
                    seen, malformed = 0, 0
                    digest = hashlib.sha256()
                    batch = []
                    with opener(name) as stream:
                        while True:
                            raw = stream.readline(65537)
                            if not raw:
                                break
                            check()
                            if len(raw) > 65536:
                                raise ValueError("FCC source row exceeds the supported length")
                            digest.update(raw)
                            seen += 1
                            try:
                                fields = raw.decode("utf-8").rstrip("\r\n").split("|")
                            except UnicodeDecodeError:
                                # FCC public data includes legacy Windows punctuation.
                                fields = raw.decode("cp1252", errors="replace").rstrip("\r\n").split("|")
                            if (len(fields) != FIELD_COUNTS[name] or fields[0] != name[:2].upper()
                                    or not fields[1].isascii() or not fields[1].isdigit()):
                                malformed += 1
                                if name in bad_ids and len(fields) > 1 and fields[1].isascii() and fields[1].isdigit():
                                    bad_ids[name].add(fields[1])
                                if malformed > 1000:
                                    raise ValueError("Unsupported or excessively malformed FCC table layout")
                                continue
                            cs = fields[4].strip().upper()
                            if name == "hd.dat":
                                if fields[5] != "A":
                                    continue
                                if fields[6] not in SERVICES[service]:
                                    raise ValueError("FCC source contains the wrong active radio service")
                                if not _CALL.fullmatch(cs):
                                    malformed += 1
                                    continue
                                batch.append((fields[1], cs, fields[8].strip()))
                                sql = "INSERT INTO hd VALUES (?,?,?)"
                            elif name == "en.dat":
                                if fields[5] != "L":
                                    continue
                                # Keep only entities matching an active HD and its call.
                                header = conn.execute("SELECT callsign FROM hd WHERE fcc_id=?", (fields[1],)).fetchone()
                                if not header or header[0] != cs:
                                    continue
                                first, last = fields[8].strip(), fields[10].strip()
                                entity = fields[7].strip()
                                applicant = fields[23].strip()
                                kind = APPLICANT_TYPES.get(applicant, "Other/Unknown")
                                display = entity or " ".join(p.strip() for p in (first, fields[9], last, fields[11]) if p.strip())
                                state = fields[17].strip().upper()
                                if not display or (state and not re.fullmatch(r"[A-Z]{2}", state, flags=re.ASCII)):
                                    malformed += 1
                                    bad_ids[name].add(fields[1])
                                    continue
                                postal = normalize_zip(fields[18])
                                reason = _postal_exclusion(state, postal)
                                batch.append((fields[1], display, first, last, state, kind, valid_frn(fields[22]), postal, reason))
                                sql = "INSERT INTO en VALUES (?,?,?,?,?,?,?,?,?)"
                            else:
                                header = conn.execute("SELECT callsign FROM hd WHERE fcc_id=?", (fields[1],)).fetchone()
                                if not header or header[0] != cs:
                                    continue
                                batch.append((fields[1], CLASSES.get(fields[5].strip(), "Unknown")))
                                sql = "INSERT INTO am VALUES (?,?)"
                            if len(batch) >= 5000:
                                conn.executemany(sql, batch)
                                batch.clear()
                                conn.commit()
                            if seen % 100000 == 0:
                                progress(f"{service}: {name.upper()} · {seen:,} rows read")
                        if batch:
                            conn.executemany(sql, batch)
                            conn.commit()
                    if not seen or (name in counts and counts[name] != seen):
                        raise ValueError("FCC source row count does not match counts metadata")
                    if malformed > max(5, seen // 10000):
                        raise ValueError("Too many malformed FCC source rows; previous reference retained")
                    skipped += malformed
                    hashes[name] = digest.hexdigest()
            check()
            progress(f"{service}: validating and indexing")
            active = conn.execute("SELECT count(*) FROM hd").fetchone()[0]
            missing_limit = max(5, active // 10000)
            missing_en = {row[0] for row in conn.execute("SELECT hd.fcc_id FROM hd LEFT JOIN en USING(fcc_id) WHERE en.fcc_id IS NULL LIMIT ?", (missing_limit + 1,))}
            if not active or not missing_en.issubset(bad_ids["en.dat"]):
                raise ValueError("Active FCC licenses are missing licensee records")
            if len(missing_en) > missing_limit:
                raise ValueError("Too many active FCC licenses are missing licensee records")
            if service == "Amateur":
                missing_am = {row[0] for row in conn.execute("SELECT hd.fcc_id FROM hd LEFT JOIN am USING(fcc_id) WHERE am.fcc_id IS NULL LIMIT ?", (missing_limit + 1,))}
                if not missing_am.issubset(bad_ids["am.dat"]) or len(missing_am) > missing_limit:
                    raise ValueError("Active amateur licenses are missing amateur records")
                skipped += len(missing_am - missing_en)
            skipped += len(missing_en)
            conn.executescript("""
                CREATE TABLE licensee (
                  callsign TEXT PRIMARY KEY, name TEXT NOT NULL, first_name TEXT,
                  last_name TEXT, state TEXT, service TEXT, kind TEXT,
                  license_class TEXT, frn TEXT, fcc_id TEXT, expires TEXT,
                  mail_zip TEXT, zip_reason TEXT);
            """)
            join_am = "JOIN" if service == "Amateur" else "LEFT JOIN"
            conn.execute("""INSERT INTO licensee
              SELECT hd.callsign,en.name,en.first_name,en.last_name,en.state,?,en.kind,
              CASE WHEN en.kind != 'Individual' THEN '' ELSE coalesce(am.license_class,'') END,
              en.frn,hd.fcc_id,hd.expires,en.mail_zip,en.zip_reason FROM hd JOIN en USING(fcc_id) """ + join_am + " am USING(fcc_id)", (service,))
            active = conn.execute("SELECT count(*) FROM licensee").fetchone()[0]
            if not active:
                raise ValueError("No complete active FCC licenses were found")
            # A complete source should not unexpectedly erase a substantial part
            # of a working national reference, even when counts was omitted.
            try:
                with self._read(service) as previous:
                    old_count = self._status(previous).active_rows if previous is not None else 0
            except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, IndexError):
                old_count = 0  # A valid refresh must be able to repair a bad cache.
            if old_count >= 10000 and active < old_count * 0.8:
                raise ValueError("FCC active license count fell by more than 20%; previous reference retained")
            conn.executescript("""
                CREATE INDEX licensee_state_call ON licensee(state,callsign);
                CREATE INDEX licensee_frn ON licensee(frn);
                DROP TABLE hd; DROP TABLE en; DROP TABLE am;
                CREATE TABLE metadata(payload TEXT NOT NULL);
            """)
            refreshed = datetime.now(timezone.utc).isoformat(timespec="seconds")
            meta = SourceStatus(service, created, refreshed, active, skipped, source.name, zip_ready=True)
            info = {**meta.__dict__, "hashes": hashes, "schema_version": SCHEMA_VERSION}
            conn.execute("INSERT INTO metadata VALUES (?)", (json.dumps(info),))
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.commit()
            conn.execute("VACUUM")
            conn.execute("ANALYZE")
            conn.commit()
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("FCC cache integrity check failed")
            conn.close()
            conn = None
            check()
            # Ensure the file content reaches storage before publishing its name.
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            with _LOCK:
                check()
                os.replace(temporary, destination)
                _CACHE_REVISION += 1
            return meta
        except sqlite3.IntegrityError as exc:
            raise ValueError("Duplicate FCC license/entity records; previous reference retained") from exc
        finally:
            if conn is not None:
                conn.close()
            temporary.unlink(missing_ok=True)
