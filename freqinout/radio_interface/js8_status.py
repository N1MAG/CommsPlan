from __future__ import annotations

import datetime
import json
import logging
import re
import socket
import sqlite3
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

from freqinout.core.settings_manager import SettingsManager
from freqinout.core.software_status_service import SoftwareStatusService
from freqinout.core.varac_log_parser import parse_varac_event_timestamp
from freqinout.core.dependency_health import get_dependency_health_registry
from freqinout.radio_interface.js8_api_client import JS8ApiClientRegistry, JS8ApiEndpoint
from freqinout.radio_interface.js8_rx_hub import (
    JS8RxHub,
    ensure_js8net_started,
    js8net_started_endpoint,
)

log = logging.getLogger(__name__)

# Add vendored js8net to import path
JS8NET_PATH = Path(__file__).resolve().parents[2] / "third_party" / "js8net" / "js8net-main"
if JS8NET_PATH.exists():
    sys.path.insert(0, str(JS8NET_PATH))
try:
    import js8net  # type: ignore
except Exception as e:  # pragma: no cover
    js8net = None
    log.warning("js8net not available: %s", e)


class JS8StatusClient:
    """
    Very lightweight status client for JS8Call.

    We keep is_busy() as a simple TCP probe to avoid spinning up js8net
    for status checks.
    """

    def __init__(self, host: Optional[str] = None, port: Optional[int] = None, settings: Optional[object] = None):
        self.settings = settings if settings is not None else SettingsManager()
        self._software_status = SoftwareStatusService(self.settings)
        self.host = self._resolve_host(host)
        self._port_override = int(port) if port not in (None, "") else None

    def _resolve_host(self, host: Optional[str]) -> str:
        host_txt = str(host or "").strip()
        if host_txt:
            return host_txt
        try:
            host_txt = str(self.settings.get("js8_host", "") or "").strip()
        except Exception:
            host_txt = ""
        return host_txt or "127.0.0.1"

    def _get_port(self) -> int:
        """
        Prefer the UI key "js8_port" (Settings tab), fall back to the legacy
        "js8_tcp_port". Default to 2442.
        """
        if self._port_override is not None:
            return int(self._port_override)
        for key in ("js8_port", "js8_tcp_port"):
            try:
                val = self.settings.get(key, None)
                if val is not None:
                    return int(val)
            except Exception:
                continue
        return 2442

    def is_busy(self) -> bool:
        """
        Attempt a quick status query to JS8Call.

        For now, we:
          - Use JS8RxHub to observe recent RX activity and PTT state.
          - If we have recent RX activity or active PTT, return True.
          - On any failure, assume not busy (but log at debug level).
        """
        try:
            hub = JS8RxHub.instance(self.host, self._get_port())
            if not hub.start(self.host, self._get_port()):
                return False
            now_ts = time.time()
            if hub.ptt_active():
                return True
            # Treat very recent RX activity as busy to avoid QSY mid-stream.
            if now_ts - hub.last_rx_activity_ts() <= 12.0:
                return True
            return False
        except Exception as e:
            log.debug("JS8Call status query failed (assuming not busy): %s", e)
            return False


class JS8ControlClient(JS8StatusClient):
    """
    js8net-backed JS8Call controller.
    Call set_frequency() from your rig-control path when control_via == 'JS8Call'.
    """

    def __init__(self, host: Optional[str] = None, port: Optional[int] = None, settings: Optional[object] = None):
        super().__init__(host=host, port=port, settings=settings)
        self._net_started = False

    def _get_port(self) -> int:
        # Prefer settings_tab key, fall back to legacy key
        if self._port_override is not None:
            return int(self._port_override)
        for key in ("js8_port", "js8_tcp_port"):
            try:
                val = self.settings.get(key, None)
                if val:
                    return int(val)
            except Exception:
                continue
        return 2442

    def _js8call_running(self) -> bool:
        """
        Lightweight process check to avoid spawning JS8Call
        if it is not already running.
        """
        try:
            return bool(self._software_status.program_is_running("JS8Call"))
        except Exception:
            return False

    def _ensure_net(self) -> bool:
        if js8net is None:
            log.error("JS8ControlClient: js8net not available")
            return False
        if not self._js8call_running():
            log.info("JS8ControlClient: JS8Call not running; skipping js8net start.")
            return False
        if not self._net_started:
            try:
                requested_endpoint = (self.host, self._get_port())
                fallback_endpoint = js8net_started_endpoint()
                if fallback_endpoint is not None and fallback_endpoint != requested_endpoint:
                    # js8net is process-global and cannot represent two JS8
                    # endpoints. The endpoint-scoped native client above is
                    # authoritative; never credit or warn about another
                    # radio's legacy fallback connection.
                    log.debug(
                        "JS8ControlClient: native endpoint %s:%s is unavailable; "
                        "legacy js8net fallback belongs to %s:%s",
                        requested_endpoint[0],
                        requested_endpoint[1],
                        fallback_endpoint[0],
                        fallback_endpoint[1],
                    )
                    return False
                self._net_started = ensure_js8net_started(self.host, self._get_port())
                if not self._net_started:
                    log.debug(
                        "JS8ControlClient: legacy js8net fallback could not start for %s:%s",
                        self.host,
                        self._get_port(),
                    )
                    return False
                log.info("JS8ControlClient: js8net started on %s:%s", self.host, self._get_port())
            except Exception as e:
                log.warning("JS8ControlClient: failed to start js8net: %s", e)
                self._net_started = False
                return False
        return True

    def _native_client(self):
        endpoint = JS8ApiEndpoint(self.host, self._get_port())
        return JS8ApiClientRegistry.get(endpoint, timeout_s=1.0, auto_reconnect=True)

    @staticmethod
    def _freq_hz_from_response(response: object) -> Optional[int]:
        params = getattr(response, "params", {}) or {}
        value = getattr(response, "value", "") or ""
        for key in ("DIAL", "FREQ", "FREQUENCY"):
            if isinstance(params, dict) and key in params:
                try:
                    return int(float(params.get(key) or 0))
                except Exception:
                    continue
        try:
            return int(float(value))
        except Exception:
            return None

    @staticmethod
    def _offset_hz_from_response(response: object) -> Optional[int]:
        params = getattr(response, "params", {}) or {}
        if isinstance(params, dict) and "OFFSET" in params:
            try:
                return int(float(params.get("OFFSET") or 0))
            except Exception:
                return None
        return None

    def set_frequency(self, dial_hz: int, offset_hz: Optional[int] = None) -> bool:
        """
        Set JS8Call dial (and optional audio offset) using the endpoint-scoped native API.
        """
        dial_hz = int(dial_hz)
        off = int(offset_hz) if offset_hz is not None else 0
        try:
            client = self._native_client()
            if client.start():
                client.send("RIG.SET_FREQ", params={"DIAL": dial_hz, "OFFSET": off})
                log.info("JS8ControlClient set dial=%d Hz%s via native API", dial_hz, "" if offset_hz is None else f" offset={off} Hz")
                return True
        except Exception as exc:
            log.debug("JS8ControlClient native set_frequency failed; trying js8net fallback: %s", exc)
        try:
            if not self._ensure_net():
                return False
            js8net.set_freq(dial_hz, off)
            log.info("JS8ControlClient set dial=%d Hz%s", dial_hz, "" if offset_hz is None else f" offset={off} Hz")
            return True
        except Exception as e:
            log.error("JS8ControlClient failed to set frequency: %s", e)
            return False

    def get_frequency(self) -> Optional[int]:
        """
        Return current JS8Call dial frequency in Hz, or None on failure.
        """
        try:
            client = self._native_client()
            if client.start():
                response = client.request("RIG.GET_FREQ", expect_types=("RIG.FREQ", "STATION.STATUS"), timeout_s=0.8)
                freq = self._freq_hz_from_response(response)
                if freq is not None:
                    return freq
        except Exception as exc:
            log.debug("JS8ControlClient native get_frequency failed; trying js8net fallback: %s", exc)
        try:
            if not self._ensure_net():
                return None
            resp = js8net.get_freq()
            if not resp:
                return None
            hz = resp.get("dial") or resp.get("freq")
            return int(hz) if hz else None
        except Exception as e:
            log.debug("JS8ControlClient get_frequency failed: %s", e)
            return None

    def get_offset(self) -> Optional[int]:
        """
        Return current JS8Call audio offset in Hz, or None on failure.
        """
        try:
            client = self._native_client()
            if client.start():
                response = client.request("RIG.GET_FREQ", expect_types=("RIG.FREQ", "STATION.STATUS"), timeout_s=0.8)
                offset = self._offset_hz_from_response(response)
                if offset is not None:
                    return offset
        except Exception as exc:
            log.debug("JS8ControlClient native get_offset failed; trying js8net fallback: %s", exc)
        try:
            if not self._ensure_net():
                return None
            resp = js8net.get_freq()
            if not resp:
                return None
            off = resp.get("offset")
            return int(off) if off is not None else None
        except Exception as e:
            log.debug("JS8ControlClient get_offset failed: %s", e)
            return None

    def set_offset(self, offset_hz: int) -> bool:
        """
        Explicitly set JS8Call audio offset by reusing current dial.
        """
        try:
            cur = self.get_frequency()
            if cur is None:
                return False
            return self.set_frequency(cur, offset_hz)
        except Exception as e:
            log.error("JS8ControlClient failed to set offset: %s", e)
            return False

    def stop(self):
        # js8net is process-global and shared by the receive hub, scheduler,
        # and JS8 Net Control. Individual clients must not close it.
        self._net_started = False


class VarACStatusClient:
    """VarAC busy check using VarAC traffic/main logs in the install folder."""

    _DB_TRANSFER_POLL_INTERVAL_S = 1.0
    _DB_TRANSFER_COOLDOWN_S = 15.0
    _DB_TRANSFER_ACTIVE_STALE_S = 300.0
    _DB_TRANSFER_SCAN_LIMIT = 64
    _TRANSIENT_EVENT_STALE_S = 300.0

    def __init__(self, settings: Optional[object] = None) -> None:
        self.settings = settings if settings is not None else SettingsManager()
        self._software_status = SoftwareStatusService(self.settings)
        self._last_status: Dict[str, object] = {}
        self._last_db_transfer_status: Dict[str, object] = {
            "busy": False,
            "reason": None,
            "transfer_active": False,
            "cooldown_active": False,
        }
        self._last_db_transfer_poll_monotonic: float = 0.0
        self._last_db_transfer_path: str = ""
        self._cached_log_paths: List[Path] = []
        self._cached_log_paths_ts: float = 0.0
        self._cached_log_paths_sig: tuple[str, str] = ("", "")

    def _operator_callsign(self) -> str:
        return (self.settings.get("operator_callsign", "") or "").strip().upper()

    def _split_events(self, text: str) -> List[str]:
        pattern = re.compile(r"\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2} - ")
        matches = list(pattern.finditer(text))
        if not matches:
            return [ln.strip() for ln in text.splitlines() if ln.strip()]
        events: List[str] = []
        prefix = text[: matches[0].start()].strip()
        if prefix:
            events.append(" ".join(prefix.splitlines()).strip())
        for idx, match in enumerate(matches):
            start = match.start()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
            chunk = text[start:end].strip()
            if chunk:
                cleaned = " ".join(chunk.splitlines()).strip()
                if cleaned:
                    events.append(cleaned)
        return events

    def _evaluate_status(self, text: str) -> Dict[str, object]:
        now_local = datetime.datetime.now(datetime.timezone.utc).astimezone()
        events = self._split_events(text)
        callsign = self._operator_callsign()
        last_connecting: Optional[datetime.datetime] = None
        last_connected: Optional[datetime.datetime] = None
        last_disconnected: Optional[datetime.datetime] = None
        last_session_terminal: Optional[datetime.datetime] = None
        last_incoming: Optional[datetime.datetime] = None
        last_no_luck: Optional[datetime.datetime] = None
        last_broadcast: Optional[datetime.datetime] = None
        last_broadcast_complete: Optional[bool] = None
        last_wait_freq: Optional[datetime.datetime] = None
        last_file_wait: Optional[datetime.datetime] = None
        last_transfer: Optional[datetime.datetime] = None
        last_transfer_done: Optional[datetime.datetime] = None

        def _is_newer(a: Optional[datetime.datetime], b: Optional[datetime.datetime]) -> bool:
            return a is not None and (b is None or a > b)

        def _is_recent(ts_val: Optional[datetime.datetime], window_s: float) -> bool:
            if ts_val is None:
                return False
            try:
                return abs((now_local - ts_val).total_seconds()) <= float(window_s)
            except Exception:
                return False

        for raw in events:
            upper = raw.upper()
            ts_val: Optional[datetime.datetime] = (
                parse_varac_event_timestamp(raw[:19]) if len(raw) >= 19 else None
            )

            if "STARTING VARAC" in upper:
                # Reset QSO state for a new app session so stale events in the
                # tail from previous sessions do not pin busy=True.
                last_connecting = None
                last_connected = None
                last_disconnected = None
                last_incoming = None
                last_no_luck = None
                last_broadcast = None
                last_broadcast_complete = None
                last_wait_freq = None
                last_file_wait = None
                last_transfer = None
                last_transfer_done = None
                continue

            if (
                "CONNECTING VARA MAIN MODEM" in upper
                or "CONNECTING VARA MONITOR MODEM" in upper
                or "CONNECTED TO VARA MODEM" in upper
            ):
                # Modem transport setup should not be treated as an on-air QSO.
                continue

            if "WAITING FOR FREQUENCY TO CLEAR" in upper:
                last_wait_freq = ts_val or now_local
                continue
            if "INCOMING CONNECTION REQUEST" in upper:
                last_incoming = ts_val or now_local
                continue
            if "NO LUCK." in upper:
                last_no_luck = ts_val or now_local
                continue
            if "QSO SUMMARY:" in upper:
                last_session_terminal = ts_val or now_local
                continue
            if "DISCONNECTING " in upper:
                last_session_terminal = ts_val or now_local
                continue
            if "LOGGING QSO TO DB:" in upper:
                last_session_terminal = ts_val or now_local
                continue
            if "CONNECTING " in upper:
                last_connecting = ts_val or now_local
                continue
            if "CONNECTED TO" in upper:
                last_connected = ts_val or now_local
                continue
            if "DISCONNECTED FROM" in upper:
                last_disconnected = ts_val or now_local
                last_session_terminal = ts_val or now_local
                continue
            if "FILE SENT. WAITING FOR CONFIRMATION OF RECEIPT" in upper:
                last_file_wait = ts_val or now_local
                last_transfer = ts_val or now_local
                continue
            if "RECEIVING FILE TRANSFER DATA" in upper:
                last_transfer = ts_val or now_local
                continue
            if (
                "SENDFILE HEADER RECEIVED" in upper
                or "INCOMING FILE PACKET" in upper
                or "CONVERTING FILE" in upper
                or "WRITING FILE TO DISK" in upper
            ):
                last_transfer = ts_val or now_local
                continue
            if "FILE SUCCESSFULLY RECEIVED" in upper or "FILE SUCCESSFULLY SENT" in upper:
                last_transfer_done = ts_val or now_local
                continue
            if " - BROADCAST - " in upper:
                last_broadcast = ts_val or now_local
                last_broadcast_complete = raw.rstrip().endswith("-")
                continue
            if "<SENDING ASYNC MESSAGE>" in upper and "VMAIL RELAY NOTIFICATION" in upper:
                continue
            if "SENDING" in upper and "BEACON" in upper:
                if callsign and upper.rstrip().endswith(f"DE {callsign}"):
                    continue

        waiting_for_frequency = (
            _is_newer(last_wait_freq, last_disconnected)
            and _is_newer(last_wait_freq, last_connected)
            and _is_newer(last_wait_freq, last_session_terminal)
            and _is_recent(last_wait_freq, self._TRANSIENT_EVENT_STALE_S)
        )
        connected_active = _is_newer(last_connected, last_disconnected) and _is_newer(
            last_connected, last_session_terminal
        )
        connecting_active = (
            _is_newer(last_connecting, last_disconnected)
            and _is_newer(last_connecting, last_connected)
            and _is_newer(last_connecting, last_session_terminal)
            and _is_recent(last_connecting, self._TRANSIENT_EVENT_STALE_S)
        )
        incoming_active = (
            _is_newer(last_incoming, last_no_luck)
            and _is_newer(last_incoming, last_connected)
            and _is_newer(last_incoming, last_disconnected)
            and _is_newer(last_incoming, last_session_terminal)
            and _is_recent(last_incoming, self._TRANSIENT_EVENT_STALE_S)
        )
        file_wait_active = _is_newer(last_file_wait, last_disconnected) and _is_newer(
            last_file_wait, last_session_terminal
        ) and _is_recent(last_file_wait, self._TRANSIENT_EVENT_STALE_S)
        transfer_active = _is_newer(last_transfer, last_disconnected) and _is_newer(
            last_transfer, last_session_terminal
        ) and _is_recent(last_transfer, self._TRANSIENT_EVENT_STALE_S)
        broadcast_active = False
        if last_broadcast and last_broadcast_complete is False:
            delta = abs((now_local - last_broadcast).total_seconds())
            broadcast_active = delta <= 12.0

        busy = bool(
            waiting_for_frequency
            or connecting_active
            or connected_active
            or incoming_active
            or file_wait_active
            or transfer_active
            or broadcast_active
        )
        reason = None
        if waiting_for_frequency:
            reason = "waiting_for_frequency"
        elif connecting_active:
            reason = "connecting"
        elif connected_active:
            reason = "connected"
        elif incoming_active:
            reason = "incoming"
        elif file_wait_active:
            reason = "file_wait"
        elif transfer_active:
            reason = "transfer"
        elif broadcast_active:
            reason = "broadcast_incomplete"

        return {
            "busy": busy,
            "waiting_for_frequency": waiting_for_frequency,
            "reason": reason,
        }

    def _resolve_log_paths(self) -> List[Path]:
        raw_install = (self.settings.get("varac_path", "") or "").strip()
        raw_db = (self.settings.get("varac_db_path", "") or "").strip()
        sig = (raw_install, raw_db)
        now = time.monotonic()
        if (
            self._cached_log_paths
            and sig == self._cached_log_paths_sig
            and (now - float(self._cached_log_paths_ts or 0.0)) < 5.0
        ):
            return list(self._cached_log_paths)
        bases: List[Path] = []
        if raw_install:
            install = Path(raw_install)
            bases.append(install.parent if install.suffix.lower() == ".exe" else install)
        elif raw_db:
            p = Path(raw_db)
            bases.append(p.parent if p.is_file() or p.suffix.lower() == ".db" else p)
        # Radio-scoped readers never discover another instance’s sources.
        # De-duplicate while preserving order.
        uniq_bases: List[Path] = []
        seen: set[str] = set()
        for base in bases:
            key = str(base).lower()
            if key in seen:
                continue
            seen.add(key)
            uniq_bases.append(base)

        out: List[Path] = []
        for base in uniq_bases:
            for name in ("VarAC_traffic.log", "VarAC.log", "varalog.log"):
                p = base / name
                if p.exists():
                    out.append(p)
        self._cached_log_paths = list(out)
        self._cached_log_paths_sig = sig
        self._cached_log_paths_ts = now
        return out

    def _resolve_db_path(self) -> Optional[Path]:
        raw_db = (self.settings.get("varac_db_path", "") or "").strip()
        raw_install = (self.settings.get("varac_path", "") or "").strip()
        if raw_db:
            p = Path(raw_db)
            if p.is_file():
                return p
            candidate = p / "VarAC.db"
            if candidate.exists():
                return candidate
        if raw_install:
            p = Path(raw_install)
            if p.is_file() and p.name.lower() == "varac.db":
                return p
            candidate = (p.parent if p.suffix.lower() == ".exe" else p) / "VarAC.db"
            if candidate.exists():
                return candidate
        return None

    @staticmethod
    def _parse_db_event_ts(value: object) -> Optional[datetime.datetime]:
        text = str(value or "").strip()
        if not text:
            return None
        normalized = text.replace("Z", "+00:00")
        try:
            dt_val = datetime.datetime.fromisoformat(normalized)
        except Exception:
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
                try:
                    dt_val = datetime.datetime.strptime(text, fmt)
                    break
                except Exception:
                    dt_val = None
            if dt_val is None:
                return None
        if dt_val.tzinfo is not None:
            try:
                return dt_val.astimezone().replace(tzinfo=None)
            except Exception:
                return dt_val.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return dt_val

    @staticmethod
    def _db_event_matches_transfer(entry_txt: str) -> bool:
        upper = entry_txt.upper()
        return any(
            token in upper
            for token in (
                "RECEIVING FILE TRANSFER DATA",
                "FILE SENT. WAITING FOR CONFIRMATION OF RECEIPT",
                "SENDFILE HEADER RECEIVED",
                "INCOMING FILE PACKET",
                "CONVERTING FILE",
                "WRITING FILE TO DISK",
                "FILE SUCCESSFULLY RECEIVED",
                "FILE SUCCESSFULLY SENT",
                "FILE TRANSFER ABORT",
            )
        )

    def _db_transfer_status(self) -> Dict[str, object]:
        health = get_dependency_health_registry()
        health_key = "varac:db_transfer"
        started = time.monotonic()
        db_path = self._resolve_db_path()
        if db_path is None or not db_path.exists():
            self._last_db_transfer_status = {
                "busy": False,
                "reason": None,
                "transfer_active": False,
                "cooldown_active": False,
                "source_available": False,
                "source_checked_monotonic": 0.0,
            }
            self._last_db_transfer_path = ""
            self._last_db_transfer_poll_monotonic = time.monotonic()
            return dict(self._last_db_transfer_status)

        now_mono = time.monotonic()
        path_key = str(db_path)
        if (
            self._last_db_transfer_path == path_key
            and (now_mono - self._last_db_transfer_poll_monotonic) < self._DB_TRANSFER_POLL_INTERVAL_S
        ):
            return dict(self._last_db_transfer_status)

        self._last_db_transfer_poll_monotonic = now_mono
        self._last_db_transfer_path = path_key
        try:
            uri = f"file:{db_path.as_posix()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=1.0)
        except Exception as exc:
            log.debug("VarACStatusClient: failed to open VarAC.db %s: %s", db_path, exc)
            try:
                health.record_failure(
                    health_key,
                    owner="VarACStatusClient",
                    error=f"VarAC DB unavailable: {exc}",
                    duration_ms=(time.monotonic() - started) * 1000.0,
                    cooldown_sec=30.0,
                    metadata={"path": str(db_path)},
                )
            except Exception:
                pass
            self._last_db_transfer_status["source_available"] = False
            return dict(self._last_db_transfer_status)

        last_transfer: Optional[datetime.datetime] = None
        last_transfer_done: Optional[datetime.datetime] = None
        last_transfer_abort: Optional[datetime.datetime] = None
        last_qso_end: Optional[datetime.datetime] = None

        def _is_newer(a: Optional[datetime.datetime], b: Optional[datetime.datetime]) -> bool:
            return a is not None and (b is None or a > b)
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT d.creation_time, d.entry, COALESCE(q.endtime, '')
                FROM datastream d
                LEFT JOIN qso q ON q.guid = d.qso_guid
                WHERE COALESCE(d.is_deleted, 0) = 0
                ORDER BY d.id DESC
                LIMIT ?
                """,
                (int(self._DB_TRANSFER_SCAN_LIMIT),),
            )
            rows = cur.fetchall()
        except Exception as exc:
            log.debug("VarACStatusClient: failed to query VarAC.db transfer rows: %s", exc)
            conn.close()
            try:
                health.record_failure(
                    health_key,
                    owner="VarACStatusClient",
                    error=f"VarAC DB query failed: {exc}",
                    duration_ms=(time.monotonic() - started) * 1000.0,
                    cooldown_sec=30.0,
                    metadata={"path": str(db_path)},
                )
            except Exception:
                pass
            self._last_db_transfer_status["source_available"] = False
            return dict(self._last_db_transfer_status)
        finally:
            try:
                conn.close()
            except Exception:
                pass

        for ts_raw, entry_raw, qso_end_raw in reversed(rows):
            entry_txt = str(entry_raw or "")
            if not self._db_event_matches_transfer(entry_txt):
                continue
            ts_val = self._parse_db_event_ts(ts_raw) or datetime.datetime.now()
            qso_end_val = self._parse_db_event_ts(qso_end_raw)
            upper = entry_txt.upper()
            if qso_end_val is not None and _is_newer(qso_end_val, last_qso_end):
                last_qso_end = qso_end_val
            if "FILE TRANSFER ABORT" in upper:
                last_transfer_abort = ts_val
                continue
            if "FILE SUCCESSFULLY RECEIVED" in upper or "FILE SUCCESSFULLY SENT" in upper:
                last_transfer_done = ts_val
                continue
            last_transfer = ts_val

        last_terminal = last_transfer_done
        if _is_newer(last_transfer_abort, last_terminal):
            last_terminal = last_transfer_abort
        if _is_newer(last_qso_end, last_terminal):
            last_terminal = last_qso_end
        transfer_active = _is_newer(last_transfer, last_terminal)
        if transfer_active and last_transfer is not None:
            transfer_active = (
                abs((datetime.datetime.now() - last_transfer).total_seconds())
                <= self._DB_TRANSFER_ACTIVE_STALE_S
            )
        cooldown_active = False
        if not transfer_active and last_transfer_done is not None:
            cooldown_active = (
                abs((datetime.datetime.now() - last_transfer_done).total_seconds())
                <= self._DB_TRANSFER_COOLDOWN_S
            )
        reason = None
        if transfer_active:
            reason = "transfer"
        elif cooldown_active:
            reason = "transfer_cooldown"
        self._last_db_transfer_status = {
            "busy": bool(transfer_active or cooldown_active),
            "reason": reason,
            "transfer_active": bool(transfer_active),
            "cooldown_active": bool(cooldown_active),
            "source_available": True,
            "source_checked_monotonic": time.monotonic(),
        }
        try:
            health.record_success(
                health_key,
                owner="VarACStatusClient",
                duration_ms=(time.monotonic() - started) * 1000.0,
                slow_ms=500.0,
                metadata={"path": str(db_path)},
            )
        except Exception:
            pass
        return dict(self._last_db_transfer_status)

    @staticmethod
    def _read_tail_checked(path: Path, max_bytes: int = 16384) -> tuple[str, bool]:
        try:
            with path.open("rb") as fh:
                try:
                    fh.seek(-max_bytes, 2)
                except OSError:
                    fh.seek(0)
                return fh.read(max_bytes).decode("utf-8", errors="replace"), True
        except Exception:
            return "", False

    @staticmethod
    def _read_tail(path: Path, max_bytes: int = 16384) -> str:
        return VarACStatusClient._read_tail_checked(path, max_bytes)[0]

    def _reader_process_state(self) -> Dict[str, object]:
        """Worker-only, configured instance attribution from existing inventory."""
        install = str(self.settings.get("varac_path", "") or "").strip()
        db = str(self.settings.get("varac_db_path", "") or "").strip()
        if not install and not db:
            return {"running": None, "checked_monotonic": 0.0}
        base = Path(install or db).expanduser()
        target = base if base.suffix.lower() == ".exe" else (
            base.parent / "VarAC.exe" if base.suffix.lower() == ".db" else base / "VarAC.exe")
        # A relative name cannot prove a radio's process identity.
        if not target.is_absolute():
            return {"running": None, "checked_monotonic": 0.0}
        arguments = ()
        default_ini = ""
        ini = str(self.settings.get("varac_ini_path", "") or "").strip()
        from freqinout.core.varac_launch_recipe import _wine_identity
        default_path = str(target.parent / "VarAC.ini")
        wine_default = _wine_identity(default_path)
        default_ini = wine_default[1] if wine_default else default_path
        if ini and Path(ini).resolve() != (target.parent / "VarAC.ini").resolve():
            wine_identity = _wine_identity(ini)
            arguments = (wine_identity[1] if wine_identity else ini,)
            default_ini = ""
        return self._software_status.cached_instance_process_state("VarAC", str(target), arguments, default_ini=default_ini)

    def get_status(self, *, include_db_transfer: bool = True) -> Dict[str, object]:
        log_paths = self._resolve_log_paths()
        status = {"busy": False, "waiting_for_frequency": False, "reason": None}
        readable = False
        read_stamp = 0.0
        try:
            if log_paths:
                # Read tails from available VarAC logs. Main and traffic logs carry
                # different event types depending on VarAC version/settings.
                reads = [self._read_tail_checked(p) for p in log_paths]
                readable = any(ok for _, ok in reads)
                if readable:
                    read_stamp = time.monotonic()
                text = "\n".join(text for text, ok in reads if ok and text)
                status = self._evaluate_status(text)
        except Exception as e:
            log.debug("VarACStatusClient: failed to read log: %s", e)
        db_status = self._db_transfer_status() if include_db_transfer else {}
        if bool(db_status.get("busy")):
            status["busy"] = True
            if not bool(status.get("waiting_for_frequency")):
                status["reason"] = db_status.get("reason") or status.get("reason")
        status["db_transfer_busy"] = bool(db_status.get("busy"))
        status["db_transfer_active"] = bool(db_status.get("transfer_active"))
        status["db_transfer_cooldown"] = bool(db_status.get("cooldown_active"))
        if db_status.get("source_available"):
            readable = True
            read_stamp = max(read_stamp, float(db_status.get("source_checked_monotonic") or 0.0))
        process = self._reader_process_state()
        # Availability is tied to explicitly configured sources only.
        configured = bool(self.settings.get("varac_path", "") or self.settings.get("varac_db_path", ""))
        status.update(process_running=process["running"],
                      process_checked_monotonic=process["checked_monotonic"],
                      reader_available=bool(configured and readable),
                      reader_checked_monotonic=read_stamp if configured else 0.0)
        self._last_status = status
        return status

    def is_busy(self) -> bool:
        status = self.get_status()
        return bool(status.get("busy"))
