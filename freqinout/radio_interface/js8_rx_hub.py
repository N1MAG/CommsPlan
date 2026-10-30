from __future__ import annotations

import time
import queue
import threading
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import QObject, QTimer, QCoreApplication

try:  # PySide leaves Python wrappers behind after Qt deletes the C++ object.
    from shiboken6 import isValid as _qt_is_valid
except Exception:  # pragma: no cover - shiboken is present with PySide in normal app runs.
    _qt_is_valid = None

from freqinout.core.settings_manager import SettingsManager
from freqinout.core.software_status_service import SoftwareStatusService
from freqinout.radio_interface.js8_api_client import (
    JS8ApiClientRegistry,
    JS8ApiEndpoint,
    JS8ApiMessage,
)

JS8NET_PATH = Path(__file__).resolve().parents[2] / "third_party" / "js8net" / "js8net-main"
if JS8NET_PATH.exists():
    import sys

    sys.path.insert(0, str(JS8NET_PATH))
try:
    import js8net  # type: ignore
except Exception:  # pragma: no cover
    js8net = None


_JS8_HUB_TEXT_LIMIT = 8192
_JS8_HUB_FIELD_LIMIT = 256
_JS8_HUB_QUEUE_LIMIT = 2048
_JS8_HUB_DISPOSABLE_TYPES = frozenset({"TX.FRAME"})
_JS8NET_START_LOCK = threading.Lock()
_JS8NET_STARTED_ENDPOINT: Optional[Tuple[str, int]] = None


def ensure_js8net_started(host: str, port: int) -> bool:
    """Start the process-global js8net connection at most once."""
    global _JS8NET_STARTED_ENDPOINT
    if js8net is None:
        return False
    endpoint = (str(host or "127.0.0.1").strip() or "127.0.0.1", int(port))
    with _JS8NET_START_LOCK:
        if _JS8NET_STARTED_ENDPOINT == endpoint:
            return True
        if _JS8NET_STARTED_ENDPOINT is not None:
            return False
        try:
            js8net.start_net(*endpoint)
        except Exception:
            return False
        _JS8NET_STARTED_ENDPOINT = endpoint
        return True


def js8net_started_endpoint() -> Optional[Tuple[str, int]]:
    """Return the endpoint owned by the legacy process-global fallback."""

    with _JS8NET_START_LOCK:
        return _JS8NET_STARTED_ENDPOINT


def _safe_js8_hub_text(value: object, *, limit: int = _JS8_HUB_TEXT_LIMIT) -> str:
    try:
        if value is None:
            text = ""
        elif isinstance(value, bytes):
            text = value.decode("utf-8", errors="replace")
        elif isinstance(value, bytearray):
            text = bytes(value).decode("utf-8", errors="replace")
        else:
            text = str(value)
    except Exception:
        text = ""
    if "\x00" in text:
        text = text.replace("\x00", "")
    text = "".join(ch if ch in "\t\n\r" or ord(ch) >= 32 else " " for ch in text).strip()
    if limit > 0 and len(text) > limit:
        return text[:limit]
    return text


def _safe_js8_hub_params(params: object) -> Dict[str, str]:
    if not isinstance(params, dict):
        return {}
    out: Dict[str, str] = {}
    for key, value in list(params.items())[:64]:
        key_txt = _safe_js8_hub_text(key, limit=_JS8_HUB_FIELD_LIMIT)
        if not key_txt:
            continue
        out[key_txt] = _safe_js8_hub_text(value)
    return out


def _safe_js8_hub_message(msg: object) -> Optional[dict]:
    if not isinstance(msg, dict):
        return None
    out = {
        "type": _safe_js8_hub_text(msg.get("type"), limit=_JS8_HUB_FIELD_LIMIT),
        "value": _safe_js8_hub_text(msg.get("value")),
        "params": _safe_js8_hub_params(msg.get("params")),
    }
    if "time" in msg:
        out["time"] = _safe_js8_hub_text(msg.get("time"), limit=_JS8_HUB_FIELD_LIMIT)
    if not out["type"] and not out["params"] and not out["value"]:
        return None
    return out


def _js8_hub_truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "on", "ptt", "tx"}


def _js8_hub_qt_object_valid(obj: object) -> bool:
    if obj is None:
        return False
    if _qt_is_valid is None:
        return True
    try:
        return bool(_qt_is_valid(obj))
    except Exception:
        return False


class JS8RxHub(QObject):
    """
    Single-consumer hub for js8net.rx_queue with listener fan-out.

    This avoids multiple tabs draining the same queue.
    """

    _instances: Dict[Tuple[str, int], "JS8RxHub"] = {}

    def __init__(self, host: str = "127.0.0.1", port: int = 2442, *, settings=None) -> None:
        app = QCoreApplication.instance()
        super().__init__(app)
        self._listeners: List[Callable[[List[dict]], None]] = []
        self._timer: QTimer | None = None
        self._ensure_timer()
        self._max_msgs = 200
        self._net_started = False
        self._using_native_api = False
        self._api_client = None
        self._api_listener_registered = False
        self._api_queue: "queue.Queue[dict]" = queue.Queue(maxsize=_JS8_HUB_QUEUE_LIMIT)
        self._api_queue_dropped = 0
        self._api_disposable_dropped = 0
        self._host = str(host or "127.0.0.1").strip() or "127.0.0.1"
        self._port = int(port or 2442)
        self._last_rx_activity_ts: float = 0.0
        self._last_ptt_ts: float = 0.0
        self._ptt_active: bool = False
        self._software_status = SoftwareStatusService(settings if settings is not None else SettingsManager())

    @classmethod
    def instance(cls, host: Optional[str] = None, port: Optional[int] = None, *, settings=None) -> "JS8RxHub":
        host_txt = str(host or "127.0.0.1").strip() or "127.0.0.1"
        port_num = int(port or 2442)
        key = (host_txt, port_num)
        existing = cls._instances.get(key)
        if existing is not None and not existing.is_valid():
            cls._instances.pop(key, None)
            existing = None
        if existing is None:
            cls._instances[key] = cls(host=host_txt, port=port_num, settings=settings)
        return cls._instances[key]

    def is_valid(self) -> bool:
        return _js8_hub_qt_object_valid(self)

    def _ensure_timer(self) -> QTimer:
        timer = getattr(self, "_timer", None)
        if timer is not None and _js8_hub_qt_object_valid(timer):
            try:
                timer.isActive()
                return timer
            except RuntimeError:
                timer = None
            except Exception:
                timer = None
        elif timer is not None:
            timer = None
        if not self.is_valid():
            raise RuntimeError("JS8RxHub Qt object has been deleted")
        timer = QTimer(self)
        timer.setInterval(1000)
        timer.timeout.connect(self._poll_queue)
        self._timer = timer
        return timer

    def is_active(self) -> bool:
        try:
            return self._ensure_timer().isActive()
        except Exception:
            return False

    def endpoint(self) -> Tuple[str, int]:
        return (self._host, int(self._port))

    def register_listener(self, cb: Callable[[List[dict]], None]) -> None:
        if cb not in self._listeners:
            self._listeners.append(cb)

    def unregister_listener(self, cb: Callable[[List[dict]], None]) -> None:
        if cb in self._listeners:
            self._listeners.remove(cb)
        try:
            timer = self._ensure_timer()
            if not self._listeners and timer.isActive():
                timer.stop()
                self._detach_api_listener()
        except Exception:
            self._detach_api_listener()

    def start(self, host: Optional[str] = None, port: Optional[int] = None, *, native_only=False) -> bool:
        if not self.is_valid():
            return False
        if host is not None:
            self._host = str(host or "").strip() or self._host
        if port is not None:
            self._port = int(port)
        timer = self._ensure_timer()
        if self._start_native_api():
            if not timer.isActive():
                timer.start()
            return True
        if native_only or js8net is None:
            return False
        if not self._net_started:
            if not self._js8call_running():
                return False
            self._net_started = ensure_js8net_started(self._host, self._port)
            if not self._net_started:
                return False
        if not timer.isActive():
            timer.start()
        return True

    def shutdown(self) -> None:
        try:
            timer = getattr(self, "_timer", None)
            if timer is not None:
                if timer.isActive():
                    timer.stop()
                timer.deleteLater()
                self._timer = None
        except Exception:
            pass
        self._detach_api_listener()
        self._listeners.clear()
        try:
            self.deleteLater()
        except Exception:
            pass
        try:
            type(self)._instances.pop((self._host, int(self._port)), None)
        except Exception:
            pass

    @classmethod
    def shutdown_all(cls) -> None:
        for hub in list(cls._instances.values()):
            try:
                hub.shutdown()
            except Exception:
                continue
        cls._instances.clear()

    def _js8call_running(self) -> bool:
        try:
            return bool(self._software_status.program_is_running("JS8Call"))
        except Exception:
            return True

    def _start_native_api(self) -> bool:
        try:
            endpoint = JS8ApiEndpoint(self._host, int(self._port))
            client = JS8ApiClientRegistry.get(endpoint, timeout_s=0.8, auto_reconnect=True)
            if self._api_client is not client:
                self._detach_api_listener()
                self._api_client = client
            if not self._api_listener_registered:
                client.add_listener(self._on_api_message)
                self._api_listener_registered = True
            if not client.start():
                return False
            self._using_native_api = True
            self._net_started = True
            return True
        except Exception:
            self._using_native_api = False
            return False

    def _detach_api_listener(self) -> None:
        client = self._api_client
        if client is not None and self._api_listener_registered:
            try:
                client.remove_listener(self._on_api_message)
            except Exception:
                pass
        self._api_listener_registered = False

    def _on_api_message(self, message: JS8ApiMessage) -> None:
        safe = _safe_js8_hub_message(message.to_dict() if hasattr(message, "to_dict") else message)
        if safe is None:
            return
        # Improved 3.x emits a TX.FRAME event for every transmitted frame,
        # including full tone arrays. No hub consumer uses those waveform
        # diagnostics, so do not let them delay directed traffic or grow
        # memory while a long transmission is in progress.
        if str(safe.get("type") or "").upper() in _JS8_HUB_DISPOSABLE_TYPES:
            self._api_disposable_dropped += 1
            return
        try:
            self._api_queue.put_nowait(safe)
        except queue.Full:
            # Preserve a bounded, recent view. TX.FRAME traffic was already
            # discarded above, so the oldest normal event is the least useful
            # item when the UI has fallen more than one queue window behind.
            try:
                self._api_queue.get_nowait()
                self._api_queue_dropped += 1
            except queue.Empty:
                pass
            try:
                self._api_queue.put_nowait(safe)
            except queue.Full:
                self._api_queue_dropped += 1

    def _poll_queue(self) -> None:
        messages: List[dict] = []
        while len(messages) < self._max_msgs:
            try:
                messages.append(self._api_queue.get_nowait())
            except queue.Empty:
                break
            except Exception:
                break
        if not messages and not self._using_native_api and js8net is not None and hasattr(js8net, "rx_queue"):
            lock = getattr(js8net, "rx_lock", None)
            acquired = False
            try:
                if lock:
                    lock.acquire()
                    acquired = True
                while True:
                    try:
                        msg = js8net.rx_queue.get_nowait()  # type: ignore[attr-defined]
                    except queue.Empty:
                        break
                    except Exception:
                        break
                    safe_msg = _safe_js8_hub_message(msg)
                    if safe_msg is not None:
                        messages.append(safe_msg)
                    if len(messages) >= self._max_msgs:
                        break
            finally:
                if lock and acquired:
                    try:
                        lock.release()
                    except Exception:
                        pass
        if not messages:
            return
        now_ts = time.time()
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            mtype = str(msg.get("type") or "").upper()
            if mtype == "RX.ACTIVITY":
                self._last_rx_activity_ts = now_ts
            elif mtype == "RIG.PTT":
                self._last_ptt_ts = now_ts
                params = msg.get("params") or {}
                if isinstance(params, dict) and "PTT" in params:
                    self._ptt_active = _js8_hub_truthy(params.get("PTT"))
                else:
                    self._ptt_active = _js8_hub_truthy(msg.get("value"))
        for cb in list(self._listeners):
            try:
                cb(messages)
            except Exception:
                continue

    def last_rx_activity_ts(self) -> float:
        return self._last_rx_activity_ts

    def last_ptt_ts(self) -> float:
        return self._last_ptt_ts

    def ptt_active(self) -> bool:
        return self._ptt_active

    def queue_stats(self) -> Dict[str, int]:
        """Return lightweight receive backpressure diagnostics."""
        return {
            "queued": int(self._api_queue.qsize()),
            "capacity": int(_JS8_HUB_QUEUE_LIMIT),
            "overflow_dropped": int(self._api_queue_dropped),
            "disposable_dropped": int(self._api_disposable_dropped),
        }
