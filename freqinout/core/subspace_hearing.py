"""Optional Subspace receiving evidence. No RF commands or UI-owned I/O.

Native spot-map edges are band-only testimony, never verified send routes.
Immutable snapshots are shared with existing peer/map consumers.
"""
from __future__ import annotations

import datetime as dt
import math
import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, replace
from typing import Mapping

BANDS = (("160M", 1.8, 2.0), ("80M", 3.5, 4.0), ("60M", 5.0, 5.5),
         ("40M", 7.0, 7.3), ("30M", 10.1, 10.15), ("20M", 14, 14.35),
         ("17M", 18.068, 18.168), ("15M", 21, 21.45), ("12M", 24.89, 24.99),
         ("10M", 28, 29.7), ("6M", 50, 54), ("2M", 144, 148))
MAX_EDGES = 512
MAX_STATIONS = 128
MAX_AGE = 56 * 86400
_CALL = re.compile(r"^[A-Z0-9/]{3,15}$")


def hearing_configuration_key(profile):
    """Value-only identity shared by the source inventory and hearing worker."""
    radio = str(profile.get("id") or "")
    values = (radio, str(profile.get("js8_instance_id") or radio),
              str(profile.get("js8_host") or "127.0.0.1").strip(), str(profile.get("js8_port") or 2442),
              str(profile.get("js8_variant_family") or profile.get("variant_family") or "unknown"),
              str(profile.get("js8_variant_version") or profile.get("variant_version") or ""),
              str(profile.get("js8_rig_name") or profile.get("rig_name") or ""),
              str(profile.get("js8_install_path") or profile.get("path_js8call") or ""),
              str(profile.get("js8_profile_path") or profile.get("js8_profile") or ""),
              str(profile.get("application_data_root") or profile.get("js8_message_storage_root") or ""))
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def _call(value):
    text = str(value or "").strip().upper()
    if not _CALL.fullmatch(text) or not any(c.isdigit() for c in text):
        raise ValueError("invalid station identity")
    return text


def _grid(value):
    text = str(value or "").upper()
    return text if re.fullmatch(r"[A-R]{2}[0-9]{2}(?:[A-X]{2}(?:[0-9]{2})?)?", text) else ""


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        f = float(value)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def band_for_frequency(value):
    freq = _number(value)
    if freq is None:
        return ""
    return next((b for b, lo, hi in BANDS if lo * 1e6 <= freq <= hi * 1e6), "")


def _stamp(value):
    # Native putTime serializes QDateTime as epoch milliseconds, not ISO text.
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = _number(value)
        return number / 1000 if number is not None and number > 100_000_000_000 else None
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return None
        return stamp.timestamp()
    except (ValueError, OverflowError):
        return None


def _observed(row, reference, received):
    stamp = _stamp(row.get("WHEN"))
    age = _number(row.get("AGE_S"))
    # Explicit observation time wins; polling cannot freshen a retained edge.
    if row.get("AGE_S") is not None and (age is None or age < 0):
        return None
    if row.get("WHEN") is not None and stamp is None:
        return None
    if stamp is not None:
        if reference is not None and age is not None and abs((reference - stamp) - age) > 2:
            return None
        return stamp if 0 < stamp <= received and received - stamp <= MAX_AGE else None
    if reference is not None and age is not None and 0 <= age <= MAX_AGE:
        stamp = reference - age
        return stamp if 0 < stamp <= received else None
    return None


@dataclass(frozen=True)
class HearingEdge:
    origin: str
    destination: str
    band: str
    radio_id: str
    instance_id: str
    source: str
    observed_ts: float | None
    received_ts: float
    snr: float | None = None
    endpoint: tuple[str, int] = ("", 0)
    session: float = 0.0
    origin_grid: str = ""
    destination_grid: str = ""
    grid_source: str = "Subspace reported"


@dataclass(frozen=True)
class HearingSnapshot:
    instance_id: str
    radio_id: str
    label: str
    endpoint: tuple[str, int]
    band: str = ""
    local_call: str = ""
    version: str = ""
    configured_family: str = ""
    configuration_key: str = ""
    state: str = "unknown"
    detail: str = "Hearing not yet checked"
    checked_ts: float = 0.0
    session: float = 0.0
    edges: tuple[HearingEdge, ...] = ()
    history_partial: bool = False
    event_history_gap: bool = False
    polling_drop_count: int = 0


def parse_hearing(message, context: HearingSnapshot, *, now=None):
    now = float(time.time() if now is None else now)
    if message.malformed or message.type != "RX.SPOT_MAP":
        raise ValueError("incompatible hearing response")
    p = message.params
    if not isinstance(p, Mapping):
        raise ValueError("invalid hearing object")
    if p.get("ERROR"):
        return replace(context, state="unavailable", detail="Native hearing map unavailable", checked_ts=now)
    if str(p.get("BAND", "")).upper() != context.band:
        raise ValueError("hearing band mismatch")
    if _call(p.get("MY_CALL")) != context.local_call:
        raise ValueError("hearing station identity mismatch")
    rows = p.get("HEARING")
    if not isinstance(rows, list) or len(rows) > MAX_STATIONS:
        raise ValueError("unsupported hearing station count/schema")
    reference = _stamp(p.get("UTC"))
    if reference is not None and (reference > now or now - reference > 300):
        reference = None
    edges = {}
    count = 0
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("invalid hearer")
        hearer = _call(row.get("CALL"))
        heard = row.get("HEARS")
        if not isinstance(heard, list):
            raise ValueError("invalid hearing edges")
        count += len(heard)
        if count > MAX_EDGES:
            raise ValueError("hearing edge limit exceeded")
        for edge in heard:
            if not isinstance(edge, Mapping):
                raise ValueError("invalid hearing edge")
            subject = _call(edge.get("CALL"))
            if subject == hearer:
                continue
            source = edge.get("SOURCE", "unknown")
            if not isinstance(source, str) or len(source) > 32 or any(ord(c) < 32 for c in source):
                raise ValueError("invalid hearing source")
            observed = _observed(edge, reference, now)
            item = HearingEdge(subject, hearer, context.band, context.radio_id,
                               context.instance_id, source.lower(), observed, now,
                               _number(edge.get("SNR")) if observed is not None else None,
                               context.endpoint, context.session,
                               _grid(edge.get("GRID")), _grid(row.get("GRID")))
            key = (subject, hearer, source.lower(), observed)
            edges[key] = item
    return replace(context, state="supported", detail="Subspace hearing · band-only receiving evidence",
                   checked_ts=now, edges=tuple(edges.values()))


class HearingCache:
    def __init__(self):
        self._lock = threading.RLock()
        self._generation = 0
        self._items = {}

    def reset(self):
        with self._lock:
            self._generation += 1
            self._items.clear()
            return self._generation

    @property
    def generation(self):
        with self._lock:
            return self._generation

    def publish(self, generation, snapshot):
        with self._lock:
            if generation != self._generation:
                return False
            old = self._items.get((snapshot.instance_id, snapshot.radio_id, snapshot.band))
            # Failed/empty snapshots don't prove an earlier observation vanished.
            retained = { (e.origin, e.destination, e.source, e.observed_ts, e.endpoint, e.session): e
                         for e in old.edges if snapshot.checked_ts - (e.observed_ts or e.received_ts) <= MAX_AGE } if old else {}
            for e in snapshot.edges:
                key = (e.origin, e.destination, e.source, e.observed_ts, e.endpoint, e.session)
                if e.observed_ts is None and key in retained:
                    e = replace(e, received_ts=retained[key].received_ts)
                retained[key] = e
            partial = len(retained) > MAX_EDGES or (old is not None and old.history_partial)
            if partial:
                snapshot = replace(snapshot, history_partial=True,
                                   state="partial" if snapshot.state == "supported" else snapshot.state,
                                   detail=snapshot.detail + "; retained history is bounded/partial")
            snapshot = replace(snapshot, edges=tuple(sorted(retained.values(),
                               key=lambda e: e.observed_ts or 0, reverse=True)[:MAX_EDGES]))
            self._items[(snapshot.instance_id, snapshot.radio_id, snapshot.band)] = snapshot
            return snapshot

    def snapshots(self):
        with self._lock:
            return tuple(self._items.values())

    def edges(self, *, now=None, age=1800):
        now = time.time() if now is None else now
        return tuple(e for s in self.snapshots() for e in s.edges
                     if e.observed_ts is not None and 0 <= now - e.observed_ts <= age)

    def signature(self, *, age=1800, now=None):
        """Semantic map inputs: polling alone must not rebuild the map."""
        now = time.time() if now is None else now
        with self._lock:
            return (self._generation, tuple((s.instance_id, s.radio_id, s.band, s.label,
                s.state, s.detail, s.history_partial,
                tuple((e.origin, e.destination, e.source, e.observed_ts, e.snr, e.endpoint, e.session)
                      for e in s.edges if e.observed_ts is not None and 0 <= now-e.observed_ts <= age))
                for s in self._items.values()))


hearing_cache = HearingCache()


class HearingRuntime:
    """One bounded daemon read lane per unique existing connected endpoint.

    Never owns/stops the shared API client. Reconfiguration fences publications;
    retiring lanes cannot publish into the new generation.
    """
    def __init__(self, cache=hearing_cache, client_lookup=None):
        self.cache = cache
        self._lookup = client_lookup
        self._lock = threading.RLock()
        self._signature = None
        self._stop = threading.Event()
        self._threads = []
        self._endpoint_locks = {}
        self._health_contexts = {}

    def configure(self, profiles, local_call):
        subscriptions = []
        for p in profiles:
            if not any(str(p.get(key) or "").lower() in {"true", "1", "yes", "on"}
                       for key in ("use_js8call", "use_js8spotter")):
                continue
            radio = str(p.get("id", "") or "")
            instance = str(p.get("js8_instance_id", "") or radio)
            if not radio or not instance:
                continue
            host = str(p.get("js8_host") or "127.0.0.1").strip()
            try:
                port = int(p.get("js8_port") or 2442)
            except (ValueError, TypeError):
                continue
            if not 1 <= port <= 65535:
                continue
            subscriptions.append((instance, radio, str(p.get("name") or radio), host, port,
                                  str(p.get("js8_variant_family") or p.get("variant_family") or "unknown"), hearing_configuration_key(p)))
        counts = {}
        for s in subscriptions:
            counts[s[3:5]] = counts.get(s[3:5], 0) + 1
        all_subscriptions = tuple(sorted(subscriptions))
        subscriptions = tuple(s for s in all_subscriptions if counts[s[3:5]] == 1)[:8]
        excluded = tuple(s for s in all_subscriptions if s not in subscriptions)
        signature = (all_subscriptions, str(local_call or "").strip().upper())
        with self._lock:
            if signature == self._signature:
                return
            self._stop.set()
            self._retire_health()
            self._stop = threading.Event()
            self._signature = signature
            generation = self.cache.reset()
            self._threads = [t for t in self._threads if t.is_alive()]
            for s in excluded:
                reason = "Shared API endpoint; hearing attribution unavailable" if counts[s[3:5]] > 1 else "Hearing endpoint limit reached"
                snap = HearingSnapshot(s[0], s[1], s[2], s[3:5], configured_family=s[5], configuration_key=s[6],
                                       state="unavailable", detail=reason, checked_ts=time.time())
                self.cache.publish(generation, snap)
                self._publish_health(snap, generation)
            for subscription in subscriptions:
                thread = threading.Thread(target=self._run, args=(subscription, signature[1], generation, self._stop),
                                          name="subspace-hearing", daemon=True)
                self._threads.append(thread)
                thread.start()

    def stop(self):
        with self._lock:
            self._stop.set()
            self._retire_health()
            self._signature = None
            self.cache.reset()

    def is_stopped(self):
        with self._lock:
            return not any(t.is_alive() for t in self._threads)

    def _retire_health(self):
        from freqinout.core.dependency_health import get_dependency_health_registry
        for key in self._health_contexts:
            get_dependency_health_registry().discard(key)
        self._health_contexts.clear()

    @staticmethod
    def _existing_client(endpoint):
        from freqinout.radio_interface.js8_api_client import JS8ApiClientRegistry, JS8ApiEndpoint
        statuses = JS8ApiClientRegistry.status_snapshot()
        if not any(s.endpoint.key == endpoint and s.connected for s in statuses):
            return None
        return JS8ApiClientRegistry.get(JS8ApiEndpoint(*endpoint))

    def read_once(self, context, expected_call, client):
        before = client.status_snapshot()
        if not before.connected:
            raise RuntimeError("JS8 API disconnected")
        request = lambda cmd, typ, params=None: client.request(cmd, params=params, expect_types=(typ,),
                                                             timeout_s=1.0, require_id=True)
        version_msg = request("STATION.VERSION", "STATION.VERSION")
        if version_msg.malformed:
            raise ValueError("invalid application identity")
        version = version_msg.params.get("VERSION") or version_msg.value or ""
        if not isinstance(version, str) or len(version) > 128:
            raise ValueError("incompatible application version schema")
        call_msg = request("STATION.GET_CALLSIGN", "STATION.CALLSIGN")
        local = _call(call_msg.value or call_msg.params.get("CALLSIGN"))
        if expected_call and local != expected_call:
            raise ValueError("configured station identity mismatch")
        freq_msg = request("RIG.GET_FREQ", "RIG.FREQ")
        band = band_for_frequency(freq_msg.params.get("DIAL"))
        if not band:
            raise ValueError("current radio band unavailable")
        same_session = context.session == before.last_connected_ts
        previous_gap = context.event_history_gap if same_session else False
        previous_drops = context.polling_drop_count if same_session else before.event_drop_count
        context = replace(context, version=version, local_call=local, band=band,
                          session=before.last_connected_ts)
        # Native Bands::find emits case-sensitive ADIF keys ("40m").
        # CP's normalized display/filter band remains "40M".
        message = request("RX.GET_SPOT_MAP", "RX.SPOT_MAP", {"BAND": band.lower()})
        after = client.status_snapshot()
        if not after.connected or before.last_connected_ts != after.last_connected_ts:
            raise ValueError("API connection changed during hearing read")
        # The polling queue is independent of require_id RPC dispatch. Its
        # overflow is a history diagnostic, not damage to this complete frame.
        return replace(parse_hearing(message, context),
                       event_history_gap=previous_gap or previous_drops != after.event_drop_count,
                       polling_drop_count=after.event_drop_count)

    def _publish_health(self, context, generation):
        with self._lock:
            if generation != self.cache.generation:
                return
            from freqinout.core.dependency_health import get_dependency_health_registry
            key = f"js8call:{context.endpoint[0]}:{context.endpoint[1]}:hearing:{context.instance_id}:{context.radio_id}"
            self._health_contexts[key] = context
            get_dependency_health_registry().record_success(
                key,
                owner="Subspace hearing", metadata={
                    "version": context.version, "configured_family": context.configured_family,
                    "hearing_configuration_key": context.configuration_key,
                    "hearing_state": context.state, "hearing_detail": context.detail,
                    "event_history_gap": context.event_history_gap, "history_partial": context.history_partial,
                    "source_radio_id": context.radio_id, "radio_id": context.radio_id,
                    "instance_id": context.instance_id, "js8_instance_id": context.instance_id,
                    "generation": generation, "profile": context.label, "label": context.label})

    def _run(self, subscription, expected_call, generation, stop):
        instance, radio, label, host, port, family, configuration_key = subscription
        context = HearingSnapshot(instance, radio, label, (host, port), configured_family=family,
                                  configuration_key=configuration_key)
        failures = 0
        while not stop.is_set():
            try:
                client = (self._lookup or self._existing_client)(context.endpoint)
                if client is None:
                    raise RuntimeError("JS8 API not connected")
                with self._lock:
                    lane = self._endpoint_locks.setdefault(context.endpoint, threading.Lock())
                if not lane.acquire(blocking=False):
                    if stop.wait(1):
                        return
                    continue
                try:
                    if stop.is_set():
                        return
                    context = self.read_once(context, expected_call, client)
                finally:
                    lane.release()
                failures = 0 if context.state == "supported" else failures + 1
            except Exception as exc:
                failures += 1
                context = replace(context, state="incompatible" if isinstance(exc, ValueError) else "inaccessible", checked_ts=time.time(),
                                  detail=str(exc)[:160], edges=(), version="")
            if stop.is_set():
                return
            published = self.cache.publish(generation, context)
            if not published:
                return
            self._publish_health(published, generation)
            # Old edges are retained only by the cache, not republished as new input.
            context = replace(context, edges=())
            stop.wait(min(300, 30 * 2 ** min(max(0, failures - 1), 4)))


def _peer_observation(e):
    from freqinout.core.peer_evidence import PeerEvidence, CLASS_REPORTED_HEARING, CLASS_UNKNOWN
    return PeerEvidence(
        source_family="js8call", source_reference=f"Subspace hearing · {e.source} · band only · {e.endpoint} · session {e.session}",
        source_radio_id=e.radio_id, source_app=e.instance_id,
        source_session=f"{e.endpoint}:{e.session}", evidence_scope="band_only",
        observed_ts=e.observed_ts, received_ts=e.received_ts,
        origin=e.origin, destination=e.destination, reporter=e.destination, subject=e.origin,
        band=e.band, snr=e.snr,
        classification=CLASS_REPORTED_HEARING if e.source in {"radio", "hearing"} else CLASS_UNKNOWN,
        direction_known=e.source in {"radio", "hearing"})


def peer_observations(*, now=None):
    """Adapt to the canonical peer model; unknown passband excludes route proof."""
    now = time.time() if now is None else now
    return tuple(_peer_observation(e) for s in hearing_cache.snapshots() for e in s.edges
                 if 0 <= now - (e.observed_ts or e.received_ts) <= MAX_AGE)


def receiving_paths(edges, target, local_call, *, now=None):
    """Reuse canonical bounded receiving search, explicitly in band-only scope."""
    from freqinout.core.peer_evidence import _relay_candidates
    now = time.time() if now is None else now
    best = {}
    for e in edges:
        if e.source not in {"radio", "hearing"} or e.observed_ts is None or not 0 <= now - e.observed_ts <= MAX_AGE:
            continue
        key = (e.instance_id, e.radio_id, e.endpoint, e.session, e.band, e.origin, e.destination)
        if key not in best or e.observed_ts > best[key].observed_ts:
            best[key] = e
    edges = tuple(best.values())
    candidates = _relay_candidates(tuple(_peer_observation(e) for e in edges), target, local_call,
                                   now,
                                   allow_band_only=True, max_age_seconds=MAX_AGE)
    paths = []
    for candidate in candidates:
        route = []
        for origin, destination in zip(candidate.path, candidate.path[1:]):
            matches = [e for e in edges if e.origin == origin and e.destination == destination
                       and e.radio_id == candidate.source_radio_id and e.instance_id == candidate.source_app
                       and f"{e.endpoint}:{e.session}" == candidate.source_session and e.band == candidate.band]
            if not matches:
                break
            route.append(max(matches, key=lambda e: e.observed_ts or 0))
        if len(route) == candidate.edges:
            paths.append(tuple(route))
    paths.sort(key=lambda route: (len(route), -min(e.observed_ts for e in route), -max(e.session for e in route)))
    unique = {}
    for route in paths:
        key = (route[0].radio_id, route[0].instance_id, route[0].endpoint, route[0].band,
               (route[0].origin,) + tuple(e.destination for e in route))
        unique.setdefault(key, route)
    return tuple(unique.values())


def hearing_summary(target, local_call, *, band_filter=None, pair_allowed=None, age=1800, now=None):
    now = time.time() if now is None else now
    snapshots = hearing_cache.snapshots()
    bf = band_filter or {"type": "all"}
    if bf.get("type") == "freq":
        return ""  # Band-only evidence is outside this exact-frequency view.
    edges = tuple(e for e in hearing_cache.edges(now=now, age=age or 1800)
                  if (bf.get("type") != "band" or e.band == str(bf.get("value", "")).upper())
                  and (pair_allowed is None or pair_allowed(e.origin, e.destination)))
    paths = receiving_paths(edges, target, local_call, now=now)
    labels = {s.instance_id: s.label for s in snapshots}
    details = []
    for path in paths[:3]:
        oldest_age = int(max(now - e.observed_ts for e in path) // 60)
        route = " → ".join((path[0].origin,) + tuple(e.destination for e in path))
        details.append(f"{route} · {labels.get(path[0].instance_id, path[0].radio_id)} · "
                       f"{path[0].band} · oldest {oldest_age}m · band only, unverified receiving path")
    outgoing = [e for e in edges if e.origin == local_call and e.destination == target
                and e.source in {"radio", "hearing"}]
    if outgoing:
        e = max(outgoing, key=lambda e: e.observed_ts)
        details.append(f"Reports hearing you · {labels.get(e.instance_id, e.radio_id)} · {e.band} · "
                       f"{int((now - e.observed_ts) // 60)}m ago; return path unconfirmed")
    affected = [s for s in snapshots if (bf.get("type") != "band" or s.band == str(bf.get("value", "")).upper())
                and any(e.origin == target or e.destination == target for e in s.edges)]
    if affected and not details:
        details.append("No recent RF hearing path; retained or Internet reports do not establish a route")
    if any(e.observed_ts is None for s in affected for e in s.edges if target in {e.origin, e.destination}):
        details.append("Reports with unknown observation time are excluded from paths")
    if any(s.state != "supported" for s in affected):
        details.append("Hearing refresh unavailable or partial; showing aged retained evidence")
    return "\n".join(details)


def map_hearing_links(positions, *, local_call, target="", mode="all", station="",
                      band_filter=None, age=1800, allowed=None, pair_allowed=None, now=None):
    """Native receiving overlays only. Existing map owns positions and filters."""
    if mode == "off" and not target:
        return []
    now = time.time() if now is None else now
    bf = band_filter or {"type": "all"}
    if bf.get("type") == "freq":
        return []  # Exact frequency is never supplied by the spot-map contract.
    edges = [e for e in hearing_cache.edges(now=now, age=age or 1800)
             if e.source in {"radio", "hearing"}
             and (bf.get("type") != "band" or str(bf.get("value", "")).upper() == e.band)
             and (pair_allowed is None or pair_allowed(e.origin, e.destination))]
    if target:
        edges = [e for path in receiving_paths(edges, target, local_call, now=now) for e in path]
    labels = {s.instance_id: s.label for s in hearing_cache.snapshots()}
    links = []
    seen = set()
    for e in edges:
        if mode == "my_station" and not target and local_call not in {e.origin, e.destination}:
            continue
        if mode == "station" and not target and station not in {e.origin, e.destination}:
            continue
        if allowed is not None and not (allowed(e.origin) or allowed(e.destination)):
            continue
        if e.origin not in positions or e.destination not in positions:
            continue
        key = (e.instance_id, e.band, e.origin, e.destination)
        if key in seen:
            continue
        seen.add(key)
        a, b = positions[e.origin], positions[e.destination]
        links.append(dict(origin=e.origin, destination=e.destination, lat1=a[0], lon1=a[1], lat2=b[0], lon2=b[1],
                          ts=e.observed_ts, snr=e.snr, band=e.band, direction_known=True,
                          source="Subspace hearing", source_radio_id=e.radio_id, source_app=e.instance_id,
                          scope="band_only", verified=False, is_relay=False, relay_via="",
                          tooltip=f"{e.origin} → {e.destination} · {labels.get(e.instance_id, e.radio_id)} · "
                                  f"{e.band} · {int((now-e.observed_ts)//60)}m ago · band only; unverified"))
    return links


def map_scope_allows(index, origin, destination, *, local_call="", mode="all", station="",
                     target=False, group="", region="", reachable=None):
    """Match existing map selection semantics without widening group scope."""
    pair = {origin, destination}
    def matches(call, kind, value):
        info = index.get(call, {})
        return value in info.get("groups", set()) if kind == "group" else info.get("region") == value
    def pair_matches(kind, value, station_exception=False):
        if local_call and local_call in pair:
            other = destination if origin == local_call else origin
            return matches(other, kind, value)
        if station_exception and mode == "station" and station in pair:
            return any(matches(c, kind, value) for c in pair)
        return all(matches(c, kind, value) for c in pair)
    if target:
        include = bool(local_call)
    elif mode == "all":
        include = True
    elif mode == "my_station":
        include = bool(local_call) and local_call in pair
    elif mode == "station":
        include = bool(station) and station in pair
    elif mode in {"group", "region"}:
        include = bool(station) and pair_matches(mode, station)
    else:
        include = False
    if include and group:
        include = pair_matches("group", group, station_exception=True)
    if include and region:
        include = pair_matches("region", region, station_exception=True)
    if include and reachable:
        include = bool(pair.intersection(reachable)) and (not local_call or local_call in pair)
    return include


def compose_hearing_context(target, *, age=1800, band_filter=None, pair_allowed=None, now=None):
    """Capture advisory evidence; never choose a transmitting radio or route."""
    now = time.time() if now is None else now
    bf = band_filter or {"type": "all"}
    if bf.get("type") == "freq":
        return {}
    with hearing_cache._lock:
        generation = hearing_cache._generation
        snapshots = tuple(hearing_cache._items.values())
    selected = [(s, e) for s in snapshots for e in s.edges
                if target in {e.origin, e.destination} and e.source in {"radio", "hearing"}
                and e.observed_ts is not None and 0 <= now-e.observed_ts <= age
                and (bf.get("type") != "band" or e.band == str(bf.get("value", "")).upper())
                and (pair_allowed is None or pair_allowed(e.origin, e.destination))]
    current = [(s, e) for s, e in selected if s.state in {"supported", "partial"} and s.session == e.session]
    if current:
        selected = current
    selected.sort(key=lambda item: item[1].observed_ts, reverse=True)
    if not selected:
        return {}
    labels = sorted({s.label for s, _ in selected})
    source_label = labels[0] if len(labels) == 1 else f"{len(labels)} radio sources"
    return {"source_label": f"Subspace hearing · {source_label} · receiving evidence only",
            "subspace_hearing_evidence": {
                "advisory_only": True, "target": target, "generation": generation, "captured_ts": now, "stale": not bool(current),
                "expires_ts": min(e.observed_ts + age for _, e in selected[:20]),
                "partial": len(selected) > 20 or any(s.history_partial for s, _ in selected),
                "observations": tuple(dict(origin=e.origin, destination=e.destination,
                    radio_id=e.radio_id, instance_id=e.instance_id, endpoint=e.endpoint,
                    session=e.session, band=e.band, scope="band_only", observed_ts=e.observed_ts,
                    source=e.source, configuration_key=s.configuration_key) for s,e in selected[:20])}}


def validate_compose_hearing_context(evidence, target, radio_profile=None, *, now=None, statuses=None):
    """Cache-only receiver check. Advisory state never authorizes a command."""
    def result(state, detail):
        return {"state": state, "detail": detail}
    now = time.time() if now is None else now
    if not isinstance(evidence, Mapping):
        return result("changed", "Map hearing context is unavailable; review the map. Draft retained.")
    if str(target or "").strip().upper() != evidence.get("target"):
        return result("different_target", "The recipient differs from the map selection. Review hearing for this recipient.")
    try:
        if now >= float(evidence.get("expires_ts", 0)):
            return result("expired", "Map hearing evidence has aged out. Review current paths; the draft is retained.")
        if evidence.get("generation") != hearing_cache.generation:
            return result("changed", "Map hearing configuration changed. Review the source; the draft is retained.")
        observations = evidence.get("observations")
        if not isinstance(observations, (tuple, list)) or not 0 < len(observations) <= 20:
            return result("changed", "Map hearing context is incomplete; review the source.")
        radio = str((radio_profile or {}).get("id") or "")
        if radio and radio not in {str(o.get("radio_id") or "") for o in observations}:
            return result("different_radio", "The selected send radio differs from the map hearing source. Review paths for that radio.")
        if statuses is None:
            from freqinout.radio_interface.js8_api_client import JS8ApiClientRegistry
            statuses = JS8ApiClientRegistry.status_dicts()
        endpoints = {(str(s.get("host") or "").strip(), int(s.get("port") or 0)): s for s in statuses}
        snapshots = hearing_cache.snapshots()
        for obs in observations:
            if not obs.get("configuration_key"):
                return result("changed", "The original hearing configuration is unverified. Review the source.")
            endpoint = tuple(obs.get("endpoint") or ())
            status = endpoints.get(endpoint)
            if not status or not status.get("connected"):
                return result("unavailable", "The hearing source is disconnected or its status is unknown. Review the map; draft retained.")
            if status.get("last_connected_ts") != obs.get("session"):
                return result("changed", "The hearing source reconnected. Review its current evidence; draft retained.")
            candidates = [s for s in snapshots if s.instance_id == obs.get("instance_id")
                          and s.radio_id == str(obs.get("radio_id")) and s.band == obs.get("band")]
            if len(candidates) != 1:
                return result("changed", "The original map hearing source is no longer active. Review the map.")
            source = candidates[0]
            if (source.configuration_key != obs.get("configuration_key") or source.endpoint != endpoint
                    or source.session != obs.get("session")):
                return result("changed", "The hearing endpoint or configuration changed. Review its current evidence.")
            if source.state not in {"supported", "partial"}:
                return result("unavailable", "The hearing source could not refresh. Review the retained evidence; draft retained.")
            if radio and radio == source.radio_id and hearing_configuration_key(radio_profile) != source.configuration_key:
                return result("changed", "The selected radio's configuration changed. Review its current source.")
    except (ValueError, TypeError, AttributeError):
        return result("changed", "Map hearing context could not be verified. Review the source; draft retained.")
    return result("current", "Receiving evidence only; exact frequency/passband and a return path are unverified. Review map for direction and source age.")
