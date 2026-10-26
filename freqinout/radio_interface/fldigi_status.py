from __future__ import annotations

import datetime
import os
import re
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional, Tuple

from freqinout.core.perf_metrics import span as perf_span
from freqinout.core.settings_manager import SettingsManager


_RX_LINE_RE = re.compile(r"^RX\s+\d+\s+:\s+.+?\((\d{4}-\d{2}-\d{2} \d{2}:\d{2})Z\):\s*(.*)$")
_CALLSIGN_RE = re.compile(r"\bDE\s+[A-Z0-9/]{3,}\b", re.IGNORECASE)
_TOKEN_RE = re.compile(r"[A-Za-z']+")
_NON_PRINTABLE_RE = re.compile(r"[^ -~]")

_FLMSG_MARKERS = (
    "[WRAP:beg]",
    "[WRAP:fn",
    "<flmsg>",
    ":hdr_",
    ":mg:",
)
_FLAMP_PREFIXES = (
    "<PROG",
    "<FILE",
    "<ID",
    "<SIZE",
    "<DATA",
)

_COMMON_WORDS = {
    "a", "about", "after", "all", "also", "an", "and", "any", "are", "as", "at", "be", "been",
    "but", "by", "can", "do", "for", "from", "get", "had", "has", "have", "he", "her", "here",
    "his", "how", "i", "if", "in", "into", "is", "it", "just", "like", "me", "more", "my", "no",
    "not", "of", "on", "one", "or", "our", "out", "please", "radio", "report", "net", "msg",
    "schedule", "status", "station", "system", "that", "the", "their", "them", "then", "there",
    "this", "time", "to", "up", "us", "we", "were", "what", "when", "which", "who", "will",
    "with", "you", "your", "thanks", "thank", "hello", "hi", "copy", "de", "73"
}
_FLMSG_MARKERS_LOWER = tuple(marker.lower() for marker in _FLMSG_MARKERS)
_MAX_PARTIAL_LINE_REWIND_BYTES = 64 * 1024


@lru_cache(maxsize=4096)
def _token_stats_cached(token: str) -> Tuple[bool, float]:
    t = str(token or "").strip().lower()
    if len(t) < 2:
        return False, 0.0
    if t in _COMMON_WORDS:
        return True, 1.0
    if not t.isalpha():
        return False, 0.0
    vowels = sum(1 for c in t if c in "aeiouy")
    if vowels == 0:
        return False, 0.0
    vowel_ratio = vowels / len(t)
    if vowel_ratio < 0.2 or vowel_ratio > 0.75:
        return False, 0.1
    run = 0
    max_run = 0
    for c in t:
        if c in "aeiouy":
            run = 0
        else:
            run += 1
            max_run = max(max_run, run)
    if max_run >= 5:
        return False, 0.1
    return True, 0.6


@lru_cache(maxsize=2048)
def _parse_rx_timestamp_utc(ts_str: str) -> Optional[datetime.datetime]:
    try:
        ts_val = datetime.datetime.strptime(str(ts_str), "%Y-%m-%d %H:%M")
    except Exception:
        return None
    return ts_val.replace(tzinfo=datetime.timezone.utc)


@dataclass
class FldigiLogStatus:
    busy: bool
    reason: Optional[str]
    last_valid_age_s: Optional[float]


class FldigiLogStatusClient:
    """
    Determine FLDIGI activity by parsing fldigi*.log.
    This is intentionally log-only (no XML-RPC).
    """

    def __init__(
        self,
        *,
        hold_seconds: int = 90,
        flmsg_hold_seconds: int = 120,
        flamp_hold_seconds: int = 120,
        gibberish_grace_seconds: int = 90,
        gibberish_max_seconds: int = 120,
        gibberish_threshold: float = 0.55,
        min_words: int = 3,
        stale_mtime_seconds: int = 90,
        status_cache_ttl_seconds: float = 1.0,
        path_cache_ttl_seconds: float = 3.0,
        max_read_bytes: int = 1_048_576,
        settings: Optional[object] = None,
    ) -> None:
        self.settings = settings if settings is not None else SettingsManager()
        self.hold_seconds = int(hold_seconds)
        self.flmsg_hold_seconds = int(flmsg_hold_seconds)
        self.flamp_hold_seconds = int(flamp_hold_seconds)
        self.gibberish_grace_seconds = int(gibberish_grace_seconds)
        self.gibberish_max_seconds = int(gibberish_max_seconds)
        self.gibberish_threshold = float(gibberish_threshold)
        self.min_words = int(min_words)
        self.stale_mtime_seconds = int(stale_mtime_seconds)
        self.status_cache_ttl_seconds = max(0.0, float(status_cache_ttl_seconds))
        self.path_cache_ttl_seconds = max(0.0, float(path_cache_ttl_seconds))
        self.max_read_bytes = max(4096, int(max_read_bytes))
        self._last_path: Optional[Path] = None
        self._last_offset: int = 0
        self._last_file_identity: Optional[tuple[int, int]] = None
        self._last_observed_size: int = 0
        self._last_opened_mtime_ns: Optional[int] = None
        self._last_read_end: int = 0
        self._last_valid_ts: Optional[datetime.datetime] = None
        self._last_valid_reason: Optional[str] = None
        self._last_gibberish_ts: Optional[datetime.datetime] = None
        self._gibberish_run_start: Optional[datetime.datetime] = None
        self._status_cache: Optional[FldigiLogStatus] = None
        self._status_cache_ts: float = 0.0
        self._status_cache_key: str = ""
        self._resolved_path_cache: Optional[Path] = None
        self._resolved_path_cache_key: str = ""
        self._resolved_path_cache_ts: float = 0.0
        self._latest_dir_cache: Optional[Path] = None
        self._latest_dir_cache_key: str = ""
        self._latest_dir_cache_ts: float = 0.0
        self._status_cache_hits: int = 0
        self._status_cache_misses: int = 0
        self._path_cache_hits: int = 0
        self._path_cache_misses: int = 0
        self._dir_cache_hits: int = 0
        self._dir_cache_misses: int = 0

    def _current_log_setting_value(self) -> str:
        return str(self.settings.get("fldigi_log_path", "") or "").strip()

    def _resolve_log_path(self, raw: Optional[str] = None) -> Optional[Path]:
        raw = str(raw if raw is not None else self._current_log_setting_value()).strip()
        if not raw:
            self._resolved_path_cache_key = ""
            self._resolved_path_cache = None
            self._resolved_path_cache_ts = time.monotonic()
            return None
        now_mono = time.monotonic()
        if (
            raw == self._resolved_path_cache_key
            and self._resolved_path_cache_ts > 0.0
            and (now_mono - self._resolved_path_cache_ts) <= self.path_cache_ttl_seconds
        ):
            self._path_cache_hits += 1
            return self._resolved_path_cache
        self._path_cache_misses += 1
        if raw != self._resolved_path_cache_key:
            self._latest_dir_cache = None
            self._latest_dir_cache_key = ""
            self._latest_dir_cache_ts = 0.0
        path = Path(raw)
        resolved: Optional[Path]
        if path.is_dir():
            resolved = self._latest_log_in_dir(path, now_mono=now_mono)
        elif path.is_file():
            latest = self._latest_log_in_dir(path.parent, now_mono=now_mono)
            if latest and latest.stat().st_mtime >= path.stat().st_mtime:
                resolved = latest
            else:
                resolved = path
        # If path points to a non-existent file, try its parent dir
        elif path.parent.exists() and path.parent.is_dir():
            resolved = self._latest_log_in_dir(path.parent, now_mono=now_mono)
        else:
            resolved = None
        self._resolved_path_cache_key = raw
        self._resolved_path_cache = resolved
        self._resolved_path_cache_ts = now_mono
        return resolved

    def _latest_log_in_dir(self, base: Path, *, now_mono: Optional[float] = None) -> Optional[Path]:
        now_mono = time.monotonic() if now_mono is None else now_mono
        base_key = str(base)
        if (
            base_key == self._latest_dir_cache_key
            and self._latest_dir_cache_ts > 0.0
            and (now_mono - self._latest_dir_cache_ts) <= self.path_cache_ttl_seconds
        ):
            self._dir_cache_hits += 1
            return self._latest_dir_cache
        meta = {
            "cache": "miss",
            "dir": base.name or base_key,
            "candidates": 0,
            "cache_hits": self._dir_cache_hits,
            "cache_misses": self._dir_cache_misses + 1,
        }
        self._dir_cache_misses += 1
        result: Optional[Path] = None
        with perf_span("fldigi_status.scan_log_dir", settings=self.settings, meta=meta, min_ms=2.0):
            try:
                candidates = list(base.glob("fldigi*.log"))
                meta["candidates"] = len(candidates)
                if candidates:
                    result = max(candidates, key=lambda p: p.stat().st_mtime)
            except Exception:
                result = None
        self._latest_dir_cache_key = base_key
        self._latest_dir_cache = result
        self._latest_dir_cache_ts = now_mono
        return result

    def _token_stats(self, token: str) -> Tuple[bool, float]:
        return _token_stats_cached(str(token or "").strip().lower())

    def _gibberish_score(self, text: str) -> Tuple[float, int]:
        tokens = _TOKEN_RE.findall(text)
        if not tokens:
            return 1.0, 0
        valid = 0
        score = 0.0
        for tok in tokens:
            ok, s = self._token_stats(tok)
            if ok:
                valid += 1
            score += (1.0 - s)
        avg_noise = score / max(1, len(tokens))
        valid_ratio = valid / max(1, len(tokens))
        non_printable = len(_NON_PRINTABLE_RE.findall(text))
        non_printable_ratio = non_printable / max(1, len(text))
        gib = 0.6 * avg_noise + 0.3 * (1.0 - valid_ratio) + 0.1 * non_printable_ratio
        return min(max(gib, 0.0), 1.0), len(tokens)

    def _classify_payload(self, payload: str) -> Tuple[bool, Optional[str]]:
        valid, reason, _gibberish = self._classify_payload_with_gibberish(payload)
        return valid, reason

    def _classify_payload_with_gibberish(
        self, payload: str
    ) -> Tuple[bool, Optional[str], bool]:
        raw = payload.strip()
        if not raw:
            return False, None, False
        upper = raw.upper()
        raw_lower = raw.lower()
        for marker in _FLMSG_MARKERS_LOWER:
            if marker in raw_lower:
                return True, "flmsg", False
        if _CALLSIGN_RE.search(raw):
            return True, "callsign", False
        if "MODE CHANGE TO" in upper:
            return True, "mode_change", False
        for prefix in _FLAMP_PREFIXES:
            if raw.lstrip().startswith(prefix):
                return True, "flamp", False
        gib_score, token_count = self._gibberish_score(raw)
        valid = token_count >= self.min_words and gib_score < self.gibberish_threshold
        gibberish = token_count > 0 and gib_score >= self.gibberish_threshold
        return bool(valid), "text" if valid else None, bool(gibberish)

    def _is_gibberish(self, payload: str) -> bool:
        raw = payload.strip()
        if not raw:
            return False
        gib_score, token_count = self._gibberish_score(raw)
        if token_count == 0:
            return False
        return gib_score >= self.gibberish_threshold

    def _update_from_log(self, path: Path) -> Dict[str, int]:
        stats = {"bytes": 0, "lines": 0, "rx_lines": 0, "valid": 0, "gibberish": 0}
        meta = {
            "log": path.name,
            "bytes": 0,
            "lines": 0,
            "rx_lines": 0,
            "valid": 0,
            "gibberish": 0,
        }
        with perf_span("fldigi_status.update_from_log", settings=self.settings, meta=meta, min_ms=5.0):
            data = b""
            read_start = 0
            opened_size = 0
            opened_identity: tuple[int, int] | None = None
            opened_mtime_ns: int | None = None
            discard_partial_line = False
            try:
                read_meta = {"max_bytes": self.max_read_bytes}
                with perf_span("fldigi_status.read_log", settings=self.settings, meta=read_meta, min_ms=2.0):
                    with path.open("rb") as fh:
                        opened_stat = os.fstat(fh.fileno())
                        opened_size = int(opened_stat.st_size)
                        opened_identity = (int(opened_stat.st_dev), int(opened_stat.st_ino))
                        opened_mtime_ns = int(opened_stat.st_mtime_ns)
                        identity_changed = (
                            self._last_path != path
                            or self._last_file_identity != opened_identity
                        )
                        truncated = (
                            opened_size < self._last_observed_size
                            or opened_size < self._last_offset
                        )
                        discard_partial_line = False
                        offset = self._last_offset
                        if identity_changed or truncated:
                            offset = max(0, opened_size - self.max_read_bytes)
                            discard_partial_line = offset > 0
                        elif opened_size - offset > self.max_read_bytes:
                            offset = max(0, opened_size - self.max_read_bytes)
                            discard_partial_line = offset > 0
                        unchanged = (
                            not identity_changed
                            and not truncated
                            and opened_size == self._last_observed_size
                            and opened_mtime_ns == self._last_opened_mtime_ns
                            and self._last_read_end >= opened_size
                        )
                        if unchanged:
                            return stats
                        read_start = offset
                        fh.seek(read_start)
                        data = fh.read(self.max_read_bytes)
                        read_end = fh.tell()
            except Exception:
                return stats
            # Publish cursor and identity only after seek/read/tell all succeed.
            self._last_offset = read_end
            self._last_read_end = read_end
            self._last_path = path
            self._last_file_identity = opened_identity
            self._last_observed_size = opened_size
            self._last_opened_mtime_ns = opened_mtime_ns
            stats["bytes"] = len(data)
            meta["bytes"] = stats["bytes"]
            if not data:
                return stats
            if discard_partial_line and data:
                # A bounded tail may begin midway through a line.
                newline = data.find(b"\n")
                data = data[newline + 1 :] if newline >= 0 else b""
            with perf_span(
                "fldigi_status.decode_log",
                settings=self.settings,
                meta={"bytes": len(data)},
                min_ms=2.0,
            ):
                text = data.decode("utf-8", errors="replace")
            lines = text.splitlines()
            stats["lines"] = len(lines)
            meta["lines"] = stats["lines"]
            classification_meta = {"rx_lines": 0, "valid": 0, "gibberish": 0}
            with perf_span(
                "fldigi_status.classify_log",
                settings=self.settings,
                meta=classification_meta,
                min_ms=2.0,
            ):
                for line in lines:
                    m = _RX_LINE_RE.match(line.strip())
                    if not m:
                        continue
                    stats["rx_lines"] += 1
                    classification_meta["rx_lines"] = stats["rx_lines"]
                    meta["rx_lines"] = stats["rx_lines"]
                    ts_str, payload = m.group(1), m.group(2)
                    ts_val = _parse_rx_timestamp_utc(ts_str)
                    if ts_val is None:
                        continue
                    valid, reason, gibberish = self._classify_payload_with_gibberish(payload)
                    if valid:
                        self._last_valid_ts = ts_val
                        self._last_valid_reason = reason
                        self._last_gibberish_ts = None
                        self._gibberish_run_start = None
                        stats["valid"] += 1
                        classification_meta["valid"] = stats["valid"]
                        meta["valid"] = stats["valid"]
                    elif gibberish:
                        self._last_gibberish_ts = ts_val
                        if self._gibberish_run_start is None:
                            self._gibberish_run_start = ts_val
                        stats["gibberish"] += 1
                        classification_meta["gibberish"] = stats["gibberish"]
                        meta["gibberish"] = stats["gibberish"]

            # The old reader classified an unterminated final line immediately.
            # Keep that behavior, then rewind small trailing lines so a later
            # append reparses their continuation. Very large lines use a
            # strictly bounded rewind that advances the cursor. The initial
            # truncated tail fragment remains discarded and is never rewound.
            if data and not data.endswith(b"\n"):
                last_newline = data.rfind(b"\n")
                partial_bytes = len(data) - last_newline - 1
                if partial_bytes >= self.max_read_bytes:
                    rewind = min(
                        _MAX_PARTIAL_LINE_REWIND_BYTES,
                        self.max_read_bytes - 1,
                    )
                    self._last_offset = max(read_start + 1, self._last_offset - rewind)
                elif partial_bytes <= _MAX_PARTIAL_LINE_REWIND_BYTES:
                    self._last_offset = max(read_start, self._last_offset - partial_bytes)
        return stats

    def get_status(self) -> FldigiLogStatus:
        now_mono = time.monotonic()
        raw = self._current_log_setting_value()
        if raw != self._status_cache_key:
            self._status_cache = None
            self._status_cache_key = raw
        if (
            self._status_cache is not None
            and self._status_cache_ts > 0.0
            and (now_mono - self._status_cache_ts) <= self.status_cache_ttl_seconds
        ):
            self._status_cache_hits += 1
            return self._status_cache
        self._status_cache_misses += 1
        meta = {
            "cache": "miss",
            "has_path": 0,
            "stale": 0,
            "bytes": 0,
            "lines": 0,
            "rx_lines": 0,
            "valid": 0,
            "gibberish": 0,
            "status_cache_hits": self._status_cache_hits,
            "status_cache_misses": self._status_cache_misses,
            "path_cache_hits": self._path_cache_hits,
            "path_cache_misses": self._path_cache_misses,
            "dir_cache_hits": self._dir_cache_hits,
            "dir_cache_misses": self._dir_cache_misses,
        }
        with perf_span("fldigi_status.get_status", settings=self.settings, meta=meta, min_ms=2.0):
            path = self._resolve_log_path(raw=raw)
            meta["has_path"] = int(bool(path))
            if not path or not path.exists():
                status = FldigiLogStatus(busy=False, reason=None, last_valid_age_s=None)
                self._status_cache = status
                self._status_cache_ts = now_mono
                return status
            try:
                mtime = datetime.datetime.fromtimestamp(path.stat().st_mtime, tz=datetime.timezone.utc)
            except Exception:
                mtime = None
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            if mtime and (now_utc - mtime).total_seconds() > self.stale_mtime_seconds:
                meta["stale"] = 1
                status = FldigiLogStatus(busy=False, reason=None, last_valid_age_s=None)
                self._status_cache = status
                self._status_cache_ts = now_mono
                return status
            update_stats = self._update_from_log(path)
            for key in ("bytes", "lines", "rx_lines", "valid", "gibberish"):
                meta[key] = int(update_stats.get(key, 0))
            if not self._last_valid_ts:
                status = FldigiLogStatus(busy=False, reason=None, last_valid_age_s=None)
                self._status_cache = status
                self._status_cache_ts = now_mono
                return status
            age = (now_utc - self._last_valid_ts).total_seconds()
            hold = self.hold_seconds
            if self._last_valid_reason == "flmsg":
                hold = self.flmsg_hold_seconds
            elif self._last_valid_reason == "flamp":
                hold = self.flamp_hold_seconds
            gibberish_run_age = None
            if self._gibberish_run_start:
                gibberish_run_age = (now_utc - self._gibberish_run_start).total_seconds()
            gibberish_stale = (
                gibberish_run_age is not None
                and gibberish_run_age > self.gibberish_max_seconds
            )
            busy = age <= hold and not gibberish_stale
            reason = self._last_valid_reason
            if busy and self._last_gibberish_ts:
                gib_age = (now_utc - self._last_gibberish_ts).total_seconds()
                if gib_age <= self.gibberish_grace_seconds:
                    reason = "gibberish"
            status = FldigiLogStatus(
                busy=bool(busy),
                reason=reason,
                last_valid_age_s=age,
            )
            self._status_cache = status
            self._status_cache_ts = now_mono
            return status

    def is_busy(self) -> bool:
        return bool(self.get_status().busy)
