from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping


NCS_SESSION_SNAPSHOTS_KEY = "ncs_session_snapshots_v1"
NCS_ACTIVE_STATES = frozenset({"active", "started", "joined"})


@dataclass(frozen=True)
class NcsSessionSnapshot:
    protocol: str
    source_id: str
    source_name: str
    role: str = "NCS"
    net_name: str = ""
    timing_state: str = "idle"
    started_utc: str = ""
    ended_utc: str = ""
    detail: str = ""

    @property
    def session_key(self) -> str:
        return ncs_session_key(self.protocol, self.source_id)

    @property
    def label(self) -> str:
        parts = [self.source_name or self.source_id or "Source", self.protocol, self.role]
        if self.net_name:
            parts.append(self.net_name)
        if self.timing_state and self.timing_state != "idle":
            parts.append(self.timing_state)
        return " | ".join(part for part in parts if part)

    def to_record(self) -> dict[str, str]:
        return {key: str(value or "") for key, value in asdict(self).items()}


def ncs_session_key(protocol: object, source_id: object) -> str:
    protocol_text = str(protocol or "local").strip().lower().replace(" ", "_")
    source_text = str(source_id or "local").strip().lower().replace(" ", "_")
    return f"{protocol_text}:{source_text}"


def ncs_session_kind(protocol: object) -> str:
    text = str(protocol or "").strip().upper()
    if text.startswith("JS8"):
        return "JS8"
    if text.startswith("FLDIGI") or text.startswith("FLDIGI/") or "SSB" in text:
        return "FLDIGI"
    if text.startswith("LOCAL") or text in {"VHF", "UHF", "VHF/UHF"}:
        return "LOCAL"
    return text.replace(" ", "_") or "LOCAL"


def ncs_session_is_active(snapshot: NcsSessionSnapshot) -> bool:
    return str(snapshot.timing_state or "").strip().lower() in NCS_ACTIVE_STATES


def snapshot_from_record(record: Mapping[str, Any] | None) -> NcsSessionSnapshot | None:
    if not isinstance(record, Mapping):
        return None
    protocol = str(record.get("protocol") or "").strip()
    source_id = str(record.get("source_id") or "").strip()
    if not protocol or not source_id:
        return None
    return NcsSessionSnapshot(
        protocol=protocol,
        source_id=source_id,
        source_name=str(record.get("source_name") or source_id).strip(),
        role=str(record.get("role") or "NCS").strip() or "NCS",
        net_name=str(record.get("net_name") or "").strip(),
        timing_state=str(record.get("timing_state") or "idle").strip() or "idle",
        started_utc=str(record.get("started_utc") or "").strip(),
        ended_utc=str(record.get("ended_utc") or "").strip(),
        detail=str(record.get("detail") or "").strip(),
    )


def read_ncs_session_snapshots(settings: Any) -> dict[str, NcsSessionSnapshot]:
    try:
        raw = settings.get(NCS_SESSION_SNAPSHOTS_KEY, {})
    except Exception:
        raw = {}
    if not isinstance(raw, Mapping):
        return {}
    snapshots: dict[str, NcsSessionSnapshot] = {}
    for key, record in raw.items():
        snapshot = snapshot_from_record(record if isinstance(record, Mapping) else None)
        if snapshot is not None:
            snapshots[str(key)] = snapshot
    return snapshots


def active_ncs_session_flags(
    settings: Any,
    *,
    known_kinds: tuple[str, ...] = ("FLDIGI", "JS8", "LOCAL"),
) -> dict[str, bool]:
    flags = {str(kind).upper(): False for kind in known_kinds}
    for snapshot in read_ncs_session_snapshots(settings).values():
        kind = ncs_session_kind(snapshot.protocol)
        if kind not in flags:
            flags[kind] = False
        if ncs_session_is_active(snapshot):
            flags[kind] = True
    return flags


def active_ncs_session_snapshot_list(settings: Any) -> list[NcsSessionSnapshot]:
    snapshots = [
        snapshot
        for snapshot in read_ncs_session_snapshots(settings).values()
        if ncs_session_is_active(snapshot)
    ]
    snapshots.sort(
        key=lambda snapshot: (
            ncs_session_kind(snapshot.protocol),
            str(snapshot.source_name or snapshot.source_id).lower(),
            str(snapshot.net_name or "").lower(),
            str(snapshot.started_utc or ""),
        )
    )
    return snapshots


def active_ncs_session_summaries(settings: Any) -> list[str]:
    return [snapshot.label for snapshot in active_ncs_session_snapshot_list(settings)]


def active_ncs_session_summaries_by_kind(settings: Any) -> dict[str, list[str]]:
    summaries: dict[str, list[str]] = {}
    for snapshot in active_ncs_session_snapshot_list(settings):
        summaries.setdefault(ncs_session_kind(snapshot.protocol), []).append(snapshot.label)
    return summaries


def clear_persisted_active_ncs_sessions(settings: Any, *, timing_state: str = "interrupted") -> int:
    try:
        raw = settings.get(NCS_SESSION_SNAPSHOTS_KEY, {})
    except Exception:
        raw = {}
    if not isinstance(raw, Mapping):
        return 0
    store = dict(raw)
    changed = 0
    for key, record in list(store.items()):
        snapshot = snapshot_from_record(record if isinstance(record, Mapping) else None)
        if snapshot is None or not ncs_session_is_active(snapshot):
            continue
        updated = dict(snapshot.to_record())
        updated["timing_state"] = str(timing_state or "interrupted")
        store[str(key)] = updated
        changed += 1
    if changed:
        settings.set(NCS_SESSION_SNAPSHOTS_KEY, store)
    return changed


def write_ncs_session_snapshot(settings: Any, snapshot: NcsSessionSnapshot) -> None:
    try:
        raw = settings.get(NCS_SESSION_SNAPSHOTS_KEY, {})
    except Exception:
        raw = {}
    store = dict(raw) if isinstance(raw, Mapping) else {}
    store[snapshot.session_key] = snapshot.to_record()
    settings.set(NCS_SESSION_SNAPSHOTS_KEY, store)
