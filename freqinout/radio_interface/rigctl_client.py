from __future__ import annotations

import logging
import re
import socket
import threading
from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

from xmlrpc.client import ServerProxy, Transport

from freqinout.core.mode_utils import voice_sideband_for_band

log = logging.getLogger(__name__)


_FLRIG_ENDPOINT_LOCKS_GUARD = threading.Lock()
_FLRIG_ENDPOINT_LOCKS: dict[tuple[str, int], threading.RLock] = {}


def _flrig_endpoint_lock(host: str, port: int) -> threading.RLock:
    """Return the process-wide serialization lock for one FLRig endpoint."""

    key = (str(host or "").strip().casefold() or "127.0.0.1", int(port))
    with _FLRIG_ENDPOINT_LOCKS_GUARD:
        lock = _FLRIG_ENDPOINT_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _FLRIG_ENDPOINT_LOCKS[key] = lock
        return lock


def _settings_text(settings: object, key: str, default: str = "") -> str:
    try:
        getter = getattr(settings, "get", None)
        if callable(getter):
            return str(getter(key, default) or "").strip()
    except Exception:
        pass
    return str(default or "").strip()


def _settings_int(settings: object, key: str, default: int) -> int:
    try:
        getter = getattr(settings, "get", None)
        if callable(getter):
            value = getter(key, default)
            return int(value if value not in (None, "") else default)
    except Exception:
        pass
    return int(default)


@runtime_checkable
class RigControlClient(Protocol):
    def is_available(self) -> bool:
        ...

    def get_ptt(self) -> bool:
        ...

    def get_vfo_frequency(self) -> Optional[int]:
        ...

    def get_active_vfo(self) -> Optional[str]:
        ...

    def set_frequency(self, cmd: "FrequencyCommand") -> bool:
        ...


@dataclass
class FrequencyCommand:
    """
    Represents a frequency change request to the rig or JS8Call.

    Accepts both the legacy 'frequency_hz' (used by older SchedulerEngine code)
    and the newer 'rig_hz' name. Extra fields (offsets, band, js8 group) are
    ignored by FLRig but passed through by callers for JS8Call where relevant.
    """

    # Preferred field names
    rig_hz: Optional[int] = None
    fldigi_center_hz: Optional[int] = None
    js8_tune_hz: Optional[int] = None
    band: Optional[str] = None

    # Legacy / shared fields
    frequency_hz: Optional[int] = None
    mode: Optional[str] = None
    vfo: Optional[str] = None
    js8_group: Optional[str] = None

    @property
    def hz(self) -> int:
        """
        Return the chosen frequency in Hz, preferring rig_hz but falling back
        to legacy frequency_hz for compatibility.
        """
        if self.rig_hz is not None:
            return int(self.rig_hz)
        if self.frequency_hz is not None:
            return int(self.frequency_hz)
        raise ValueError("FrequencyCommand missing rig_hz/frequency_hz")


class FLRigClient:
    """
    Minimal XML-RPC client for FLRig.

    Default FLRig server address is 127.0.0.1:12345.

    Uses documented XML-RPC methods, for example: :contentReference[oaicite:1]{index=1}
      - main.get_version
      - rig.get_ptt
      - rig.get_vfo
      - rig.set_AB
      - rig.set_verify_frequency
      - rig.set_mode
      - rig.tune
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 12345,
        fldigi_port: int = 7362,
        fldigi_host: Optional[str] = None,
        timeout: float = 0.8,
    ):
        self.host = host
        self.port = port
        self._proxy: Optional[ServerProxy] = None
        self._lock = threading.Lock()
        # Multiple FIO services create short-lived clients for status, launch,
        # and scheduler work.  FLRig serves these through one XML-RPC endpoint,
        # so serialize only callers sharing that exact host/port.  Other radios
        # (and therefore other ports) remain independent.
        self._endpoint_lock = _flrig_endpoint_lock(host, port)
        self.fldigi_port = fldigi_port
        self.fldigi_host = fldigi_host or host
        self._fldigi_proxy: Optional[ServerProxy] = None
        self.timeout = float(timeout)

    def _transport(self) -> Transport:
        timeout = self.timeout

        class TimeoutTransport(Transport):
            def __init__(self, timeout_val: float):
                super().__init__()
                self._timeout_val = timeout_val

            def make_connection(self, host):
                conn = super().make_connection(host)
                try:
                    conn.timeout = self._timeout_val
                except Exception:
                    pass
                return conn

        return TimeoutTransport(timeout)

    # ------------- INTERNAL -------------

    def _connect(self) -> ServerProxy:
        if self._proxy is None:
            url = f"http://{self.host}:{self.port}"
            log.info("Connecting to FLRig XML-RPC at %s", url)
            self._proxy = ServerProxy(url, allow_none=True, transport=self._transport())
        return self._proxy

    def _with_proxy(self, func, *, label: str) -> object:
        """
        Serialize access to the XML-RPC proxy and retry once on transport glitches.
        """
        with self._endpoint_lock:
            with self._lock:
                try:
                    proxy = self._connect()
                    return func(proxy)
                except Exception as e:
                    msg = str(e)
                    if "Request-sent" in msg or "Idle" in msg:
                        log.warning("FLRig XML-RPC transient error during %s; reconnecting.", label)
                        self._proxy = None
                        proxy = self._connect()
                        return func(proxy)
                    raise

    def _connect_fldigi(self) -> Optional[ServerProxy]:
        if self._fldigi_proxy is None:
            url = f"http://{self.fldigi_host}:{self.fldigi_port}"
            try:
                log.info("Connecting to FLDigi XML-RPC at %s", url)
                self._fldigi_proxy = ServerProxy(url, allow_none=True, transport=self._transport())
            except Exception as e:
                log.warning("FLDigi XML-RPC connect failed: %s", e)
                self._fldigi_proxy = None
        return self._fldigi_proxy

    def is_fldigi_available(self) -> bool:
        proxy = self._connect_fldigi()
        if proxy is None:
            return False
        try:
            _ = proxy.modem.get_name()
            return True
        except Exception as e:
            log.debug("FLDigi XML-RPC probe failed: %s", e)
            self._fldigi_proxy = None
            return False

    def set_fldigi_mode_offset(self, mode: Optional[str], offset_hz: Optional[int]) -> bool:
        proxy = self._connect_fldigi()
        if proxy is None:
            return False
        try:
            if mode:
                log.info("Setting FLDigi mode to %s", mode)
                proxy.modem.set_by_name(mode)
            if offset_hz is not None:
                log.info("Setting FLDigi carrier offset to %s Hz", offset_hz)
                proxy.modem.set_carrier(int(offset_hz))
            return True
        except Exception as e:
            log.warning("Failed to set FLDigi mode/offset: %s", e)
            self._fldigi_proxy = None
            return False

    def get_fldigi_offset(self) -> Optional[int]:
        proxy = self._connect_fldigi()
        if proxy is None:
            return None
        try:
            val = proxy.modem.get_carrier()
            return int(val) if val is not None else None
        except Exception as e:
            log.debug("Failed to read FLDigi carrier offset: %s", e)
            self._fldigi_proxy = None
            return None

    def get_fldigi_mode(self) -> Optional[str]:
        proxy = self._connect_fldigi()
        if proxy is None:
            return None
        try:
            mode = proxy.modem.get_name()
            return str(mode).strip() if mode else None
        except Exception as e:
            log.debug("Failed to read FLDigi mode: %s", e)
            self._fldigi_proxy = None
            return None

    def _set_fldigi_wfhz(self, offset_hz: Optional[int]) -> None:
        """
        Best-effort FLDigi waterfall offset via XML-RPC using the documented
        script syntax: FLDIGI.WFHZ:<offset>. Non-fatal if FLDigi is unavailable.
        Tries both "fldigi.main.shell" and fallback "main.shell".
        """
        if offset_hz is None:
            return
        proxy = self._connect_fldigi()
        if proxy is None:
            return
        cmd = f"FLDIGI.WFHZ:{int(offset_hz)}"
        for path in ("fldigi.main.shell", "main.shell"):
            try:
                fn = proxy
                for part in path.split("."):
                    fn = getattr(fn, part)
                fn(cmd)
                log.info("Set FLDigi WFHZ via %s to %s Hz", path, offset_hz)
                return
            except Exception as e:
                log.debug("FLDigi WFHZ via %s failed: %s", path, e)
        log.warning("Failed to set FLDigi WFHZ to %s Hz (all paths tried).", offset_hz)

    # ------------- STATUS METHODS -------------

    def is_available(self) -> bool:
        """
        Quick health check: ask FLRig for its version.
        """
        try:
            self._with_proxy(lambda p: p.main.get_version(), label="get_version")
            return True
        except Exception as e:
            log.debug("FLRig not available: %s", e)
            return False

    def get_ptt(self) -> bool:
        """
        Returns True if FLRig reports PTT active (transmitting).
        """
        try:
            return self.get_ptt_checked()
        except Exception as e:
            log.warning("Failed to get PTT from FLRig: %s", e)
            return False

    def get_ptt_checked(self) -> bool:
        """Return PTT or raise so safety callers can distinguish read failure."""

        state = self._with_proxy(lambda p: p.rig.get_ptt(), label="get_ptt")
        return bool(state)

    def get_vfo_frequency(self) -> Optional[int]:
        """
        Returns the current VFO frequency in Hz, or None on failure.
        """
        try:
            freq_str = self._with_proxy(
                lambda p: p.rig.get_vfo(),
                label="get_vfo",
            )  # documented as "return current VFO in Hz" :contentReference[oaicite:2]{index=2}
            return int(float(freq_str))
        except Exception as e:
            log.warning("Failed to get VFO frequency from FLRig: %s", e)
            return None

    def get_transceiver_name(self) -> Optional[str]:
        """Return FLRig's online transceiver name, or None when it is offline.

        FLRig's ``rig.get_vfo`` method returns a fallback frequency even when
        its transceiver is offline.  ``rig.get_xcvr`` is the documented
        online-state-aware method: FLRig returns an empty string while its
        internal transceiver connection is not online.
        """

        try:
            raw = self._with_proxy(lambda p: p.rig.get_xcvr(), label="get_xcvr")
            name = str(raw or "").strip()
            return name or None
        except Exception as e:
            log.warning("Failed to get transceiver identity from FLRig: %s", e)
            return None

    def get_launch_readback(self) -> tuple[Optional[str], Optional[int]]:
        """Return the standard FLRig identity/frequency launch proof.

        ``rig.get_xcvr`` and ``rig.get_vfo`` are established XML-RPC methods
        present in FLRig 2.0.8 and later.  Read both in one endpoint-serialized
        transaction so FIO's scheduler and launch/status workers cannot overlap
        requests to the same FLRig instance.  Deliberately do not send a raw
        CAT command here: launch readiness must not compete with FLRig's radio
        polling queue or depend on a model-specific CAT response deadline.
        """

        try:
            def _read(proxy: ServerProxy) -> tuple[object, object]:
                raw_name = proxy.rig.get_xcvr()
                if not str(raw_name or "").strip():
                    return raw_name, None
                return raw_name, proxy.rig.get_vfo()

            raw_name, raw_frequency = self._with_proxy(
                _read,
                label="launch_readback",
            )
            name = str(raw_name or "").strip() or None
            if not name:
                return None, None
        except Exception as e:
            log.warning("Failed FLRig launch readback: %s", e)
            return None, None
        try:
            frequency = int(float(raw_frequency))
        except (TypeError, ValueError):
            return name, None
        return name, frequency if frequency > 0 else None

    def get_live_transceiver_name(self) -> Optional[str]:
        """Return the transceiver name after any supported fresh CAT proof.

        FLRig retains its internal online flag after some already-initialized
        radios are subsequently powered off.  Its FTdx10 and FT710 drivers use
        the read-only Yaesu ``ID;`` command for their own initialization check,
        and ``rig.cat_string`` exposes the actual response rather than a cached
        display value.  Require that fresh response for those two drivers.
        Other drivers retain the nonempty online identity check until an
        equally safe model-specific proof is defined.
        """

        name = self.get_transceiver_name()
        if not name:
            return None
        normalized_name = re.sub(r"[^A-Z0-9]", "", name.upper())
        if normalized_name not in {"FTDX10", "FT710"}:
            return name
        try:
            raw = self._with_proxy(
                lambda p: p.rig.cat_string("ID;"),
                label="cat_identity",
            )
            response = str(raw or "").strip().upper()
            if response and "NO RESPONSE" not in response and "ID" in response:
                return name
        except Exception as e:
            log.warning("Failed live transceiver identity check through FLRig: %s", e)
        return None

    def get_active_vfo(self) -> Optional[str]:
        """
        Returns the active FLRig VFO ("A" or "B"), or None on failure.
        """
        try:
            raw = self._with_proxy(lambda p: p.rig.get_AB(), label="get_AB")
            vfo = str(raw or "").strip().upper()[:1]
            return vfo if vfo in {"A", "B"} else None
        except Exception as e:
            log.debug("Failed to get active VFO from FLRig: %s", e)
            return None

    # ------------- CONTROL METHODS -------------

    @staticmethod
    def _normalize_rig_mode(mode: Optional[str], band: Optional[str]) -> Optional[str]:
        txt = str(mode or "").strip()
        if not txt:
            return None
        up = txt.upper()
        if up in {"USB", "LSB"}:
            return up
        if up in {"SSB", "VOICE"}:
            return voice_sideband_for_band(band)
        if up in {"DIGI", "DIGITAL", "DATA"}:
            # FLRig expects explicit data mode labels, not "DIGI".
            return "DATA-U"
        return txt

    def set_frequency(self, cmd: FrequencyCommand) -> bool:
        """
        Set rig frequency (and optionally mode/VFO) via FLRig.
        """
        try:
            def _do_set(p):
                freq_hz = cmd.hz
                freq = float(freq_hz)
                target_vfo = cmd.vfo if cmd.vfo in ("A", "B") else None

                def _set_vfo(vfo: str) -> None:
                    log.info("Setting FLRig VFO to %s", vfo)
                    p.rig.set_AB(vfo)

                def _set_mode(mode_cmd: str, vfo: Optional[str]) -> None:
                    if vfo == "A":
                        try:
                            p.rig.set_verify_modeA(mode_cmd)
                            return
                        except Exception:
                            try:
                                p.rig.set_modeA(mode_cmd)
                                return
                            except Exception:
                                pass
                    elif vfo == "B":
                        try:
                            p.rig.set_verify_modeB(mode_cmd)
                            return
                        except Exception:
                            try:
                                p.rig.set_modeB(mode_cmd)
                                return
                            except Exception:
                                pass
                    try:
                        p.rig.set_verify_mode(mode_cmd)
                    except Exception:
                        p.rig.set_mode(mode_cmd)

                def _set_frequency(freq_val: float, vfo: Optional[str]) -> None:
                    if vfo == "A":
                        try:
                            p.rig.set_verify_vfoA(freq_val)
                            return
                        except Exception:
                            p.rig.set_vfoA(freq_val)
                            return
                    if vfo == "B":
                        try:
                            p.rig.set_verify_vfoB(freq_val)
                            return
                        except Exception:
                            p.rig.set_vfoB(freq_val)
                            return
                    try:
                        p.rig.set_verify_frequency(freq_val)
                    except Exception:
                        p.rig.set_frequency(freq_val)

                def _readback_frequency(vfo: Optional[str]) -> int:
                    if vfo == "A":
                        try:
                            return int(float(p.rig.get_vfoA()))
                        except Exception:
                            pass
                    elif vfo == "B":
                        try:
                            return int(float(p.rig.get_vfoB()))
                        except Exception:
                            pass
                    return int(float(p.rig.get_vfo()))

                # Select target VFO first so subsequent mode/frequency calls apply
                # to the intended side.
                if target_vfo:
                    _set_vfo(target_vfo)

                mode_cmd = self._normalize_rig_mode(cmd.mode, cmd.band)
                if mode_cmd:
                    log.info("Setting FLRig mode to %s", mode_cmd)
                    _set_mode(mode_cmd, target_vfo)

                # Apply frequency after mode so rigs with band-stack-per-mode do
                # not drift off the requested target frequency.
                log.info("Setting FLRig frequency to %d Hz", freq_hz)
                _set_frequency(freq, target_vfo)

                # Re-assert selected VFO; some rigs can flip active side during
                # mode/frequency writes.
                if target_vfo:
                    try:
                        _set_vfo(target_vfo)
                    except Exception:
                        pass
                    active_mismatch = False
                    try:
                        active_raw = str(p.rig.get_AB() or "").strip().upper()
                        active_vfo = active_raw[:1] if active_raw else ""
                        if active_vfo in {"A", "B"} and active_vfo != target_vfo:
                            active_mismatch = True
                    except Exception:
                        active_mismatch = False

                # Explicit readback check so scheduler can back off when rig does
                # not converge to requested frequency.
                try:
                    verify_hz = _readback_frequency(target_vfo)
                except Exception as e:
                    log.warning("Failed to verify FLRig frequency readback: %s", e)
                    return False
                if abs(verify_hz - freq_hz) > 20:
                    log.warning(
                        "FLRig frequency verify mismatch: target=%dHz readback=%dHz",
                        freq_hz,
                        verify_hz,
                    )
                    return False
                if target_vfo and active_mismatch:
                    log.warning(
                        "FLRig VFO verify mismatch: target=%s readback=%s (frequency verified on target VFO)",
                        target_vfo,
                        active_vfo,
                    )
                return True

            return bool(self._with_proxy(_do_set, label="set_frequency"))
        except Exception as e:
            log.error("Failed to set frequency via FLRig: %s", e)
            return False

    def tune(self) -> bool:
        """
        Ask FLRig to run the rig's tune function (if supported).
        """
        try:
            log.info("Invoking FLRig tune()")
            self._with_proxy(lambda p: p.rig.tune(), label="tune")
            return True
        except Exception as e:
            log.error("Failed to start tune via FLRig: %s", e)
            return False


class RigctldClient:
    """
    Minimal Hamlib rigctld TCP client.

    Uses single-command request/response exchanges with short timeouts so an
    unavailable endpoint fails safely without blocking the UI for long periods.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 4532,
        timeout: float = 0.8,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)
        self._lock = threading.RLock()

    @staticmethod
    def _normalize_rig_mode(mode: Optional[str], band: Optional[str]) -> Optional[str]:
        txt = str(mode or "").strip()
        if not txt:
            return None
        up = txt.upper()
        if up in {"USB", "LSB", "CW", "FM", "AM", "RTTY"}:
            return up
        if up in {"SSB", "VOICE"}:
            return voice_sideband_for_band(band)
        if up in {"DIGI", "DIGITAL", "DATA", "DATA-U", "DATAU", "PKTUSB"}:
            return "PKTUSB"
        if up in {"DATA-L", "DATAL", "PKTLSB"}:
            return "PKTLSB"
        return up

    def _request_lines_locked(self, command: str) -> list[str]:
        cmd_txt = str(command or "").strip()
        if not cmd_txt:
            raise ValueError("rigctld command cannot be empty")
        payload = (cmd_txt + "\n").encode("utf-8", errors="ignore")
        chunks: list[bytes] = []
        with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
            sock.settimeout(self.timeout)
            sock.sendall(payload)
            try:
                sock.shutdown(socket.SHUT_WR)
            except Exception:
                pass
            while True:
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    break
                if not chunk:
                    break
                chunks.append(chunk)
        raw = b"".join(chunks).decode("utf-8", errors="ignore")
        return [line.strip() for line in raw.splitlines() if line.strip()]

    @staticmethod
    def _rprt_code(lines: list[str]) -> Optional[int]:
        for line in reversed(lines):
            txt = str(line or "").strip()
            if not txt.upper().startswith("RPRT"):
                continue
            parts = txt.split()
            if len(parts) < 2:
                return None
            try:
                return int(parts[1])
            except Exception:
                return None
        return None

    def _command_ok_locked(self, command: str, *, label: str) -> bool:
        lines = self._request_lines_locked(command)
        rprt = self._rprt_code(lines)
        if rprt is not None:
            if rprt == 0:
                return True
            log.debug("rigctld %s failed with RPRT %s", label, rprt)
            return False
        return bool(lines)

    def _scalar_response_locked(self, command: str, *, label: str) -> Optional[str]:
        lines = self._request_lines_locked(command)
        rprt = self._rprt_code(lines)
        if rprt not in (None, 0):
            log.debug("rigctld %s failed with RPRT %s", label, rprt)
            return None
        for line in lines:
            txt = str(line or "").strip()
            if txt and not txt.upper().startswith("RPRT"):
                return txt
        return None

    @staticmethod
    def _parse_first_int(text: Optional[str]) -> Optional[int]:
        txt = str(text or "").strip()
        if not txt:
            return None
        match = re.search(r"(-?\d+(?:\.\d+)?)", txt)
        if not match:
            return None
        try:
            return int(round(float(match.group(1))))
        except Exception:
            return None

    def is_available(self) -> bool:
        try:
            return self.get_vfo_frequency() is not None
        except Exception:
            return False

    def get_ptt(self) -> bool:
        with self._lock:
            try:
                return self.get_ptt_checked()
            except Exception as e:
                log.debug("Failed to get PTT from rigctld: %s", e)
                return False

    def get_ptt_checked(self) -> bool:
        """Return PTT or raise so safety callers can distinguish read failure."""

        with self._lock:
            raw = self._scalar_response_locked("t", label="get_ptt")
            return bool(int(str(raw or "0").strip() or "0"))

    def get_vfo_frequency(self) -> Optional[int]:
        with self._lock:
            try:
                raw = self._scalar_response_locked("f", label="get_frequency")
                return self._parse_first_int(raw)
            except Exception as e:
                log.debug("Failed to get VFO frequency from rigctld: %s", e)
                return None

    def get_active_vfo(self) -> Optional[str]:
        with self._lock:
            try:
                raw = self._scalar_response_locked("v", label="get_vfo")
                txt = str(raw or "").strip().upper()
                if "VFOA" in txt or txt == "A":
                    return "A"
                if "VFOB" in txt or txt == "B":
                    return "B"
            except Exception as e:
                log.debug("Failed to get active VFO from rigctld: %s", e)
            return None

    def _set_vfo_locked(self, vfo: Optional[str]) -> bool:
        target = str(vfo or "").strip().upper()
        if target not in {"A", "B"}:
            return True
        hamlib_vfo = "VFOA" if target == "A" else "VFOB"
        return self._command_ok_locked(f"V {hamlib_vfo}", label="set_vfo")

    def _set_mode_locked(self, mode: Optional[str], band: Optional[str]) -> bool:
        mode_cmd = self._normalize_rig_mode(mode, band)
        if not mode_cmd:
            return True
        return self._command_ok_locked(f"M {mode_cmd} 0", label="set_mode")

    def set_frequency(self, cmd: FrequencyCommand) -> bool:
        freq_hz = cmd.hz
        with self._lock:
            try:
                if not self._set_vfo_locked(cmd.vfo):
                    log.warning("rigctld VFO select failed for target=%s", cmd.vfo)
                    return False
                if not self._set_mode_locked(cmd.mode, cmd.band):
                    log.warning("rigctld mode apply failed for mode=%s band=%s", cmd.mode, cmd.band)
                if not self._command_ok_locked(f"F {int(freq_hz)}", label="set_frequency"):
                    return False
                verify_hz = self._parse_first_int(self._scalar_response_locked("f", label="verify_frequency"))
                if verify_hz is None:
                    log.warning("rigctld frequency verify readback failed after set.")
                    return False
                if abs(int(verify_hz) - int(freq_hz)) > 20:
                    log.warning(
                        "rigctld frequency verify mismatch: target=%dHz readback=%dHz",
                        freq_hz,
                        verify_hz,
                    )
                    return False
                return True
            except Exception as e:
                log.error("Failed to set frequency via rigctld: %s", e)
                return False


def flrig_client_from_settings(settings: object) -> FLRigClient:
    """
    Build an FLRig client from persisted settings.
    """
    host = _settings_text(settings, "flrig_host", "127.0.0.1") or "127.0.0.1"
    port = _settings_int(settings, "flrig_port", 12345)
    fldigi_port = _settings_int(settings, "fldigi_port", 7362)
    fldigi_host = _settings_text(settings, "fldigi_host", "") or None
    return FLRigClient(host=host, port=port, fldigi_port=fldigi_port, fldigi_host=fldigi_host)


def rigctld_client_from_settings(settings: object) -> RigctldClient:
    host = _settings_text(settings, "rig_host", "127.0.0.1") or "127.0.0.1"
    port = _settings_int(settings, "rig_port", 4532)
    return RigctldClient(host=host, port=port)


def rig_control_client_from_settings(settings: object) -> Optional[RigControlClient]:
    backend = _settings_text(settings, "control_via", "FLRig").upper()
    if backend == "FLRIG":
        return flrig_client_from_settings(settings)
    if backend == "RIGCTLD":
        return rigctld_client_from_settings(settings)
    return None
