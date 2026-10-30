from __future__ import annotations

import json
import queue
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple


JS8_TCP_API_DEFAULT_PORT = 2442
JS8_UDP_WSJT_X_DEFAULT_PORT = 2242

_FIELD_LIMIT = 256
_VALUE_LIMIT = 65536
_DEFAULT_TIMEOUT_S = 1.5
_RECONNECT_BACKOFF_STEPS_S = (1.0, 2.0, 5.0, 10.0)
_DEFAULT_EVENT_QUEUE_LIMIT = 2048


def _safe_text(value: object, *, limit: int = _VALUE_LIMIT) -> str:
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
    text = "".join(ch if ch in "\t\n\r" or ord(ch) >= 32 else " " for ch in text)
    if limit > 0 and len(text) > limit:
        return text[:limit]
    return text


def _safe_params(value: object) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    out: Dict[str, Any] = {}
    for raw_key, raw_value in list(value.items())[:256]:
        key = _safe_text(raw_key, limit=_FIELD_LIMIT).strip()
        if not key:
            continue
        out[key] = raw_value
    return out


def _message_id_from_params(params: Mapping[str, Any]) -> Optional[str]:
    if "_ID" not in params:
        return None
    try:
        return str(params.get("_ID"))
    except Exception:
        return None


@dataclass(frozen=True)
class JS8ApiEndpoint:
    host: str = "127.0.0.1"
    port: int = JS8_TCP_API_DEFAULT_PORT

    def normalized(self) -> "JS8ApiEndpoint":
        host = str(self.host or "127.0.0.1").strip() or "127.0.0.1"
        return JS8ApiEndpoint(host=host, port=int(self.port or JS8_TCP_API_DEFAULT_PORT))

    @property
    def key(self) -> Tuple[str, int]:
        n = self.normalized()
        return (n.host, int(n.port))


@dataclass
class JS8ApiMessage:
    type: str = ""
    value: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    raw_json: str = ""
    received_ts: float = 0.0
    malformed: bool = False
    quarantine_reason: str = ""

    @property
    def id(self) -> Optional[str]:
        return _message_id_from_params(self.params)

    @classmethod
    def from_raw(cls, raw: object) -> "JS8ApiMessage":
        received_ts = time.time()

        def quarantine(reason: str, text: str = "") -> "JS8ApiMessage":
            return cls(raw_json=text, received_ts=received_ts, malformed=True,
                       quarantine_reason=reason)

        if isinstance(raw, (bytes, bytearray)):
            if len(raw) > _VALUE_LIMIT * 2:
                return quarantine("frame_too_large")
            try:
                raw_text = bytes(raw).decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                return quarantine("invalid_utf8")
        elif isinstance(raw, str):
            raw_text = raw
        else:
            return quarantine("invalid_frame_type")
        if len(raw_text) > _VALUE_LIMIT:
            return quarantine("raw_text_too_large")
        if not raw_text.strip():
            return quarantine("empty_frame", raw_text)

        def unique_object(pairs: list) -> dict:
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON key")
                result[key] = value
            return result

        def reject_constant(value: str) -> None:
            raise ValueError("invalid JSON constant")

        try:
            parsed = json.loads(raw_text, object_pairs_hook=unique_object,
                                parse_constant=reject_constant)
        except (ValueError, RecursionError) as exc:
            return quarantine(f"json_parse_error:{exc.__class__.__name__}", raw_text)
        if not isinstance(parsed, Mapping):
            return quarantine("json_not_object", raw_text)
        # Refuse any input that the legacy field sanitizer would silently change.
        # Nested parameter values remain intact for consumers to validate.
        message_type = parsed.get("type")
        message_value = parsed.get("value")
        params = parsed.get("params")
        if message_type is None:
            message_type = ""
        if message_value is None:
            message_value = ""
        if not isinstance(message_type, str) or _safe_text(message_type, limit=_FIELD_LIMIT).strip() != message_type:
            return quarantine("invalid_type_field", raw_text)
        if not isinstance(message_value, str) or _safe_text(message_value) != message_value:
            return quarantine("invalid_value_field", raw_text)
        if params is None:
            params = {}
        if not isinstance(params, Mapping):
            return quarantine("invalid_params", raw_text)
        if len(params) > 256:
            return quarantine("too_many_params", raw_text)
        for key in params:
            if not key or _safe_text(key, limit=_FIELD_LIMIT).strip() != key:
                return quarantine("invalid_param_key", raw_text)
        # Lone surrogate escapes are not valid Unicode scalar values and cannot
        # be passed losslessly to downstream UTF-8 consumers.
        try:
            raw_text.encode("utf-8", errors="strict")
            json.dumps(parsed, ensure_ascii=False).encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            return quarantine("invalid_unicode", raw_text)
        return cls(type=message_type, value=message_value, params=dict(params),
                   raw_json=raw_text, received_ts=received_ts)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "value": self.value,
            "params": dict(self.params),
            "raw_json": self.raw_json,
            "received_ts": self.received_ts,
            "malformed": self.malformed,
            "quarantine_reason": self.quarantine_reason,
        }


@dataclass
class JS8CapabilitySnapshot:
    endpoint: JS8ApiEndpoint
    connected: bool
    mode: str
    version: str = ""
    supported: Dict[str, bool] = field(default_factory=dict)
    errors: Dict[str, str] = field(default_factory=dict)
    checked_ts: float = 0.0

    def supports(self, command: str) -> bool:
        return bool(self.supported.get(str(command or "").strip().upper(), False))


@dataclass(frozen=True)
class JS8ApiClientStatus:
    endpoint: JS8ApiEndpoint
    name: str
    running: bool
    connected: bool
    listener_count: int = 0
    pending_request_count: int = 0
    queued_event_count: int = 0
    event_drop_count: int = 0
    last_connected_ts: float = 0.0
    last_message_ts: float = 0.0
    last_error: str = ""
    last_error_ts: float = 0.0
    last_closing_reason: str = ""

    @property
    def key(self) -> Tuple[str, int]:
        return self.endpoint.key

    def as_dict(self) -> Dict[str, Any]:
        return {
            "host": self.endpoint.host,
            "port": self.endpoint.port,
            "key": f"{self.endpoint.host}:{self.endpoint.port}",
            "name": self.name,
            "running": self.running,
            "connected": self.connected,
            "listener_count": self.listener_count,
            "pending_request_count": self.pending_request_count,
            "queued_event_count": self.queued_event_count,
            "event_drop_count": self.event_drop_count,
            "last_connected_ts": self.last_connected_ts,
            "last_message_ts": self.last_message_ts,
            "last_error": self.last_error,
            "last_error_ts": self.last_error_ts,
            "last_closing_reason": self.last_closing_reason,
        }


class JS8ApiError(RuntimeError):
    pass


class JS8ApiTimeout(JS8ApiError):
    pass


class JS8ApiConnectionError(JS8ApiError):
    pass


class JS8ApiClient:
    """
    Native JS8Call TCP API client.

    This client is intentionally independent from the vendored js8net module.
    It is safe to create one instance per JS8Call endpoint, which is required
    for multi-rig operation.
    """

    def __init__(
        self,
        endpoint: JS8ApiEndpoint,
        *,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        auto_reconnect: bool = True,
        name: str = "",
    ) -> None:
        self.endpoint = endpoint.normalized()
        self.timeout_s = float(timeout_s or _DEFAULT_TIMEOUT_S)
        self.auto_reconnect = bool(auto_reconnect)
        self.name = name or f"{self.endpoint.host}:{self.endpoint.port}"
        self._socket: Optional[socket.socket] = None
        self._running = threading.Event()
        self._connected = threading.Event()
        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._reader_thread: Optional[threading.Thread] = None
        self._pending: Dict[str, "queue.Queue[JS8ApiMessage]"] = {}
        self._pending_expected: Dict[str, frozenset[str]] = {}
        self._pending_require_id: set[str] = set()
        self._listeners: List[Callable[[JS8ApiMessage], None]] = []
        # Push traffic can be continuous on busy JS8 networks.  Keep the
        # optional polling backlog bounded; registered listeners still see
        # each event immediately.  Dropping the oldest queued event prevents
        # an unattended consumer from growing memory without limit.
        self._events: "queue.Queue[JS8ApiMessage]" = queue.Queue(maxsize=_DEFAULT_EVENT_QUEUE_LIMIT)
        self._event_drop_count = 0
        self._next_id = int(time.time() * 1000) % 1_000_000_000
        self.last_error: str = ""
        self.last_error_ts: float = 0.0
        self.last_connected_ts: float = 0.0
        self.last_message_ts: float = 0.0
        self.last_closing_reason: str = ""

    @property
    def is_running(self) -> bool:
        return self._running.is_set()

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    def start(self) -> bool:
        if self._running.is_set():
            return self.is_connected
        self._running.set()
        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name=f"JS8ApiClient-{self.endpoint.host}:{self.endpoint.port}",
            daemon=True,
        )
        self._reader_thread.start()
        deadline = time.time() + min(self.timeout_s, 2.0)
        while time.time() < deadline:
            if self.is_connected:
                return True
            time.sleep(0.02)
        return self.is_connected

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._running.clear()
            self._connected.clear()
            self._drain_pending("Client stopped", detail="JS8Call client stopped")
            self._close_socket()
        thread = self._reader_thread
        if thread and thread.is_alive():
            thread.join(timeout=1.0)
        self._reader_thread = None

    def add_listener(self, listener: Callable[[JS8ApiMessage], None]) -> None:
        with self._state_lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[JS8ApiMessage], None]) -> None:
        with self._state_lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def get_event_nowait(self) -> Optional[JS8ApiMessage]:
        try:
            return self._events.get_nowait()
        except queue.Empty:
            return None

    def drain_events(self, limit: int = 100) -> List[JS8ApiMessage]:
        out: List[JS8ApiMessage] = []
        for _ in range(max(0, int(limit))):
            msg = self.get_event_nowait()
            if msg is None:
                break
            out.append(msg)
        return out

    def status_snapshot(self) -> JS8ApiClientStatus:
        with self._state_lock:
            listener_count = len(self._listeners)
            pending_request_count = len(self._pending)
            event_drop_count = self._event_drop_count
        try:
            queued_event_count = int(self._events.qsize())
        except Exception:
            queued_event_count = 0
        return JS8ApiClientStatus(
            endpoint=self.endpoint,
            name=self.name,
            running=self.is_running,
            connected=self.is_connected,
            listener_count=listener_count,
            pending_request_count=pending_request_count,
            queued_event_count=queued_event_count,
            event_drop_count=event_drop_count,
            last_connected_ts=float(self.last_connected_ts or 0.0),
            last_message_ts=float(self.last_message_ts or 0.0),
            last_error=str(self.last_error or ""),
            last_error_ts=float(self.last_error_ts or 0.0),
            last_closing_reason=str(self.last_closing_reason or ""),
        )

    def request(
        self,
        command: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        value: object = "",
        expect_types: Optional[Iterable[str]] = None,
        timeout_s: Optional[float] = None,
        require_id: bool = False,
    ) -> JS8ApiMessage:
        """Request a reply, optionally requiring an echoed request identifier.

        Legacy type-only response fallback remains the default. Mutations whose
        verification cannot accept unsolicited state use ``require_id=True``.
        """
        command_text = _safe_text(command, limit=_FIELD_LIMIT).strip().upper()
        if not command_text:
            raise ValueError("JS8 API command is required")
        if not self.is_connected:
            raise JS8ApiConnectionError(f"JS8Call API is not connected at {self.endpoint.host}:{self.endpoint.port}")
        msg_id = self._allocate_id()
        request_params = dict(params or {})
        request_params["_ID"] = msg_id
        expected = frozenset(
            str(item or "").strip().upper()
            for item in (expect_types or ())
            if str(item or "").strip()
        )
        waiter: "queue.Queue[JS8ApiMessage]" = queue.Queue(maxsize=1)
        with self._state_lock:
            self._pending[str(msg_id)] = waiter
            self._pending_expected[str(msg_id)] = expected
            if require_id:
                self._pending_require_id.add(str(msg_id))
        try:
            self._send({"type": command_text, "value": _safe_text(value), "params": request_params})
            timeout = float(timeout_s if timeout_s is not None else self.timeout_s)
            response = waiter.get(timeout=max(0.05, timeout))
        except queue.Empty as exc:
            raise JS8ApiTimeout(f"Timed out waiting for JS8Call response to {command_text}") from exc
        finally:
            with self._state_lock:
                self._pending.pop(str(msg_id), None)
                self._pending_expected.pop(str(msg_id), None)
                self._pending_require_id.discard(str(msg_id))
        if response.type == "API.ERROR":
            self._record_error(response.value or "JS8Call API returned API.ERROR")
            if _safe_text(response.params.get("ERROR_CLASS"), limit=64) == "connection":
                raise JS8ApiConnectionError(response.value or "JS8Call API connection closed")
            raise JS8ApiError(response.value or "JS8Call API returned API.ERROR")
        if expect_types:
            expected = {str(item or "").strip().upper() for item in expect_types if str(item or "").strip()}
            if expected and response.type.upper() not in expected:
                raise JS8ApiError(
                    f"Unexpected JS8Call response to {command_text}: {response.type or '<empty>'}"
                )
        return response

    def send(
        self,
        command: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        value: object = "",
    ) -> None:
        """Send a JS8Call API command without waiting for a response."""
        command_text = _safe_text(command, limit=_FIELD_LIMIT).strip().upper()
        if not command_text:
            raise ValueError("JS8 API command is required")
        if not self.is_connected:
            raise JS8ApiConnectionError(f"JS8Call API is not connected at {self.endpoint.host}:{self.endpoint.port}")
        self._send({"type": command_text, "value": _safe_text(value), "params": dict(params or {})})

    def probe_capabilities(self, *, timeout_s: float = 0.4) -> JS8CapabilitySnapshot:
        """
        Probe common JS8Call TCP API commands and classify endpoint capability.

        This method calls ``start()`` automatically if the client is not
        already running. Phase 1 probes are intentionally sequential so the
        probe remains simple and easy to reason about; keep the per-command
        timeout short because a slow or partially-started JS8Call can otherwise
        turn nine probe commands into several seconds of waiting.
        """
        if not self.is_running:
            self.start()
        supported: Dict[str, bool] = {}
        errors: Dict[str, str] = {}
        version = ""

        probes: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
            ("RIG.GET_FREQ", ("RIG.FREQ", "STATION.STATUS")),
            ("RIG.GET_PTT", ("RIG.PTT_STATUS",)),
            ("TX.GET_QUEUE_DEPTH", ("TX.QUEUE_DEPTH",)),
            ("STATION.VERSION", ("STATION.VERSION",)),
            ("STATION.GET_CONFIG", ("STATION.CONFIG",)),
            ("RX.GET_CALL_ACTIVITY", ("RX.CALL_ACTIVITY",)),
            ("RX.GET_BAND_ACTIVITY", ("RX.BAND_ACTIVITY",)),
            ("MODE.GET_SPEED", ("MODE.SPEED",)),
            ("RX.GET_FREE_OFFSETS", ("RX.FREE_OFFSETS",)),
        )

        for command, expect in probes:
            try:
                response = self.request(command, expect_types=expect, timeout_s=timeout_s)
                supported[command] = True
                if command == "STATION.VERSION":
                    version = _safe_text(response.params.get("VERSION") or response.value, limit=128).strip()
            except Exception as exc:
                supported[command] = False
                errors[command] = _safe_text(exc, limit=512)

        mode = classify_js8_capability_mode(supported, connected=self.is_connected)
        return JS8CapabilitySnapshot(
            endpoint=self.endpoint,
            connected=self.is_connected,
            mode=mode,
            version=version,
            supported=supported,
            errors=errors,
            checked_ts=time.time(),
        )

    def _allocate_id(self) -> int:
        with self._state_lock:
            self._next_id += 1
            if self._next_id > 9_000_000_000:
                self._next_id = int(time.time() * 1000) % 1_000_000_000
            return self._next_id

    def _send(self, payload: Mapping[str, Any]) -> None:
        data = json.dumps(dict(payload), separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
        with self._send_lock:
            sock = self._socket
            if sock is None:
                raise JS8ApiConnectionError("JS8Call API socket is not connected")
            try:
                sock.sendall(data)
            except Exception as exc:
                self._connected.clear()
                self._close_socket()
                raise JS8ApiConnectionError(f"Failed to write JS8Call API request: {exc}") from exc

    def _reader_loop(self) -> None:
        backoff_index = 0
        while self._running.is_set():
            if not self._connect_socket():
                delay = _RECONNECT_BACKOFF_STEPS_S[min(backoff_index, len(_RECONNECT_BACKOFF_STEPS_S) - 1)]
                backoff_index += 1
                time.sleep(delay if self.auto_reconnect else min(delay, 0.25))
                if not self.auto_reconnect:
                    break
                continue
            backoff_index = 0
            self._read_socket_until_closed()
            if not self.auto_reconnect:
                break
        self._connected.clear()

    def _connect_socket(self) -> bool:
        self._close_socket()
        try:
            sock = socket.create_connection(self.endpoint.key, timeout=self.timeout_s)
            sock.settimeout(0.5)
        except Exception as exc:
            self._record_error(f"connect_failed:{exc}")
            return False
        self._socket = sock
        self._connected.set()
        self.last_connected_ts = time.time()
        self.last_error = ""
        return True

    def _read_socket_until_closed(self) -> None:
        buffer = b""
        discarding_oversize = False
        limit = _VALUE_LIMIT * 2
        while self._running.is_set() and self._socket is not None:
            try:
                chunk = self._socket.recv(65536)
            except socket.timeout:
                continue
            except Exception as exc:
                self._record_error(f"read_failed:{exc}")
                break
            if not chunk:
                self._record_error("connection_closed")
                break
            # An oversized frame is discarded through its delimiter, including
            # any tail that happens to look like a complete JSON object.
            while chunk:
                if discarding_oversize:
                    delimiter = chunk.find(b"\n")
                    if delimiter < 0:
                        break
                    chunk = chunk[delimiter + 1:]
                    discarding_oversize = False
                    continue
                delimiter = chunk.find(b"\n")
                part = chunk if delimiter < 0 else chunk[:delimiter]
                if len(buffer) + len(part) > limit:
                    self._handle_message(JS8ApiMessage(
                        received_ts=time.time(), malformed=True,
                        quarantine_reason="frame_too_large"))
                    buffer = b""
                    if delimiter < 0:
                        discarding_oversize = True
                        break
                else:
                    buffer += part
                    if delimiter >= 0:
                        self._handle_raw_frame(buffer)
                        buffer = b""
                if delimiter < 0:
                    break
                chunk = chunk[delimiter + 1:]
        if buffer:
            self._handle_message(JS8ApiMessage(
                received_ts=time.time(), malformed=True,
                quarantine_reason="incomplete_frame"))
        self._connected.clear()
        self._close_socket()
        with self._lifecycle_lock:
            if self._running.is_set():
                self._drain_pending("Connection closed", detail="JS8Call API connection closed")

    def _handle_raw_frame(self, raw: bytes) -> None:
        message = JS8ApiMessage.from_raw(raw)
        self._handle_message(message)

    def _handle_message(self, message: JS8ApiMessage) -> None:
        self.last_message_ts = message.received_ts or time.time()
        if message.type == "STATION.CLOSING":
            self.last_closing_reason = _safe_text(message.params.get("REASON") or message.value, limit=512)
            self._connected.clear()
            self._drain_pending(
                "JS8Call closed",
                detail=f"STATION.CLOSING: {self.last_closing_reason or 'JS8Call closed'}",
            )
        if message.type == "API.ERROR":
            self._record_error(message.value or "API.ERROR")
        if message.type == "TX.SEND_MESSAGE" and bool(message.params.get("REFUSED")):
            reason = _safe_text(message.params.get("REASON") or message.value, limit=512).strip()
            self._record_error(f"tx_refused:{reason or 'JS8Call refused the send request'}")
        msg_id = None if message.malformed else message.id
        if msg_id:
            with self._state_lock:
                waiter = self._pending.get(str(msg_id))
            if waiter is not None:
                try:
                    waiter.put_nowait(message)
                except queue.Full:
                    pass
                return
        elif message.type and not message.malformed:
            # Released JS8Call builds commonly omit the caller-provided _ID
            # from responses. Correlate the oldest pending request expecting
            # this response type. The client send lock preserves request order,
            # while the type gate prevents unrelated RX traffic from satisfying
            # a request.
            response_type = message.type.strip().upper()
            with self._state_lock:
                fallback_waiter = next(
                    (
                        self._pending[key]
                        for key in self._pending
                        if key not in self._pending_require_id
                        and response_type in self._pending_expected.get(key, frozenset())
                    ),
                    None,
                )
            if fallback_waiter is not None:
                try:
                    fallback_waiter.put_nowait(message)
                except queue.Full:
                    pass
                return
        try:
            self._events.put_nowait(message)
        except queue.Full:
            with self._state_lock:
                self._event_drop_count += 1
            try:
                self._events.get_nowait()
            except queue.Empty:
                pass
            try:
                self._events.put_nowait(message)
            except queue.Full:
                pass
        with self._state_lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(message)
            except Exception:
                continue

    def _record_error(self, error: object) -> None:
        self.last_error = _safe_text(error, limit=1024)
        self.last_error_ts = time.time()

    def _drain_pending(self, value: str, *, detail: str = "") -> None:
        with self._state_lock:
            pending = list(self._pending.values())
            self._pending.clear()
            self._pending_expected.clear()
            self._pending_require_id.clear()
        for waiter in pending:
            try:
                waiter.put_nowait(
                    JS8ApiMessage(
                        type="API.ERROR",
                        value=_safe_text(detail or value, limit=1024),
                        params={"ERROR_CLASS": "connection"},
                        received_ts=time.time(),
                    )
                )
            except queue.Full:
                pass

    def _close_socket(self) -> None:
        sock = self._socket
        self._socket = None
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass


class JS8ApiClientRegistry:
    """Process-local registry that prevents duplicate FIO sockets per endpoint."""

    _lock = threading.Lock()
    _clients: Dict[Tuple[str, int], JS8ApiClient] = {}

    @classmethod
    def get(
        cls,
        endpoint: JS8ApiEndpoint,
        *,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        auto_reconnect: bool = True,
    ) -> JS8ApiClient:
        normalized = endpoint.normalized()
        with cls._lock:
            client = cls._clients.get(normalized.key)
            if client is None:
                client = JS8ApiClient(
                    normalized,
                    timeout_s=timeout_s,
                    auto_reconnect=auto_reconnect,
                )
                cls._clients[normalized.key] = client
            return client

    @classmethod
    def remove(cls, endpoint: JS8ApiEndpoint) -> None:
        normalized = endpoint.normalized()
        with cls._lock:
            client = cls._clients.pop(normalized.key, None)
        if client is not None:
            client.stop()

    @classmethod
    def status_snapshot(cls) -> List[JS8ApiClientStatus]:
        with cls._lock:
            clients = list(cls._clients.values())
        return [client.status_snapshot() for client in clients]

    @classmethod
    def status_dicts(cls) -> List[Dict[str, Any]]:
        return [snapshot.as_dict() for snapshot in cls.status_snapshot()]

    @classmethod
    def shutdown_all(cls) -> None:
        with cls._lock:
            clients = list(cls._clients.values())
            cls._clients.clear()
        for client in clients:
            client.stop()


def classify_js8_capability_mode(supported: Mapping[str, bool], *, connected: bool) -> str:
    if not connected:
        return "offline"
    normalized = {str(key or "").strip().upper(): bool(value) for key, value in supported.items()}
    api_full_required = {"RIG.GET_FREQ", "RIG.GET_PTT", "TX.GET_QUEUE_DEPTH", "STATION.VERSION"}
    api_basic_required = {"RIG.GET_FREQ", "RX.GET_CALL_ACTIVITY", "MODE.GET_SPEED"}
    if all(normalized.get(cmd, False) for cmd in api_full_required):
        return "api_full"
    if all(normalized.get(cmd, False) for cmd in api_basic_required):
        return "api_basic"
    return "file_fallback"
