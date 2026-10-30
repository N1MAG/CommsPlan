"""Qt-free, generation-keyed snapshots for the bounded Resources workspace.

Catalog interaction code consumes immutable snapshots only.  Loading happens in
a small shared worker pool; callers discard completed generations that are no
longer current, which keeps rapid navigation from replacing a coherent screen
with an older query result.
"""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Mapping

from freqinout.core.resource_catalog_models import (
    CatalogSource,
    FrequencyResource,
    NetDirectoryEntry,
    NetDirectorySession,
    ResourceUsage,
)
from freqinout.core.resource_catalog_store import MAX_RESULTS, ResourceCatalogStore


_RESOURCE_SNAPSHOT_WORKERS = ThreadPoolExecutor(max_workers=4, thread_name_prefix="resource-catalog-snapshot")


@dataclass(frozen=True)
class ResourceCatalogSnapshot:
    sources: Mapping[str, CatalogSource]
    frequencies: tuple[FrequencyResource, ...]
    entries: tuple[NetDirectoryEntry, ...]
    sessions_by_entry: Mapping[str, tuple[NetDirectorySession, ...]]
    frequency_usage: Mapping[str, ResourceUsage]
    entry_usage: Mapping[str, ResourceUsage]
    session_usage: Mapping[str, ResourceUsage]


@dataclass(frozen=True)
class SnapshotCompletion:
    generation: int
    snapshot: ResourceCatalogSnapshot | None
    error: Exception | None = None


def load_resource_catalog_snapshot(store: ResourceCatalogStore, *, frequency_search: str = "",
                                   frequency_service: str | None = None,
                                   frequency_active: bool | None = None,
                                   frequency_offset: int = 0) -> ResourceCatalogSnapshot:
    """Load one bounded, coherent Resources presentation snapshot off the GUI thread."""
    frequencies = tuple(store.list_frequencies(search=frequency_search, service=frequency_service,
                        active=frequency_active, offset=frequency_offset, limit=MAX_RESULTS))
    entries = tuple(store.list_net_entries(active=None, limit=MAX_RESULTS))
    sources = {source.source_key: source for source in store.list_sources(enabled=None, limit=MAX_RESULTS)}
    sessions_by_entry: dict[str, tuple[NetDirectorySession, ...]] = {}
    session_usage: dict[str, ResourceUsage] = {}
    for entry in entries:
        sessions = tuple(store.list_sessions(net_entry_key=entry.net_entry_key, active=None, limit=MAX_RESULTS))
        sessions_by_entry[entry.net_entry_key] = sessions
        session_usage.update({session.net_session_key: store.session_usage(session.net_session_key) for session in sessions})
    return ResourceCatalogSnapshot(
        sources=sources,
        frequencies=frequencies,
        entries=entries,
        sessions_by_entry=sessions_by_entry,
        frequency_usage={item.frequency_resource_key: store.frequency_usage(item.frequency_resource_key) for item in frequencies},
        entry_usage={item.net_entry_key: store.net_entry_usage(item.net_entry_key) for item in entries},
        session_usage=session_usage,
    )


class ResourceCatalogSnapshotService:
    """Keep only the newest requested snapshot generation for one view/host."""

    def __init__(self, store: ResourceCatalogStore, *, loader: Callable[[ResourceCatalogStore], ResourceCatalogSnapshot] = load_resource_catalog_snapshot) -> None:
        self._store = store
        self._loader = loader
        self._generation = 0
        self._pending: dict[int, Future[ResourceCatalogSnapshot]] = {}

    @property
    def generation(self) -> int:
        return self._generation

    def request(self, *, loader=None) -> int:
        self._generation += 1
        generation = self._generation
        # Coalesce generations that have not started.  An already-running
        # read is allowed to finish, while only the newest queued refresh is
        # retained for this view.
        for prior_generation, prior_future in tuple(self._pending.items()):
            if prior_future.cancel():
                del self._pending[prior_generation]
        self._pending[generation] = _RESOURCE_SNAPSHOT_WORKERS.submit(loader or self._loader, self._store)
        return generation

    def take_latest(self) -> SnapshotCompletion | None:
        """Return the current completed generation; stale completions are discarded."""
        for generation, future in tuple(self._pending.items()):
            if not future.done():
                continue
            del self._pending[generation]
            if generation != self._generation:
                continue
            try:
                return SnapshotCompletion(generation, future.result())
            except Exception as exc:  # keep the last coherent UI snapshot visible
                return SnapshotCompletion(generation, None, exc)
        return None

    def has_pending(self) -> bool:
        # A completed future still owns work until take_latest consumes it.
        # Completion can happen between a caller's poll and this idle check;
        # looking only for running futures would strand that result forever.
        return bool(self._pending)


def filter_frequencies(snapshot: ResourceCatalogSnapshot, *, search: str, service: str | None, active: bool | None) -> tuple[FrequencyResource, ...]:
    needle = str(search or "").strip().casefold()
    values = []
    for item in snapshot.frequencies:
        haystack = " ".join(str(getattr(item, name) or "") for name in ("label", "band", "channel", "locality", "coverage", "notes")).casefold()
        for hz in (item.receive_hz, item.transmit_hz, item.center_hz):
            if hz is not None:
                mhz = f"{hz / 1000000:.6f}".rstrip("0").rstrip(".")
                haystack += f" {hz} {mhz} mhz {hz} hz"
        if needle and needle not in haystack:
            continue
        if service and item.service != str(service).strip().upper():
            continue
        if active is not None and (item.active != active or item.retired == active):
            continue
        values.append(item)
    return tuple(values[:MAX_RESULTS])


def filter_entries(snapshot: ResourceCatalogSnapshot, *, search: str, active: bool | None) -> tuple[NetDirectoryEntry, ...]:
    needle = str(search or "").strip().casefold()
    values = []
    for item in snapshot.entries:
        haystack = " ".join(str(getattr(item, name) or "") for name in ("name", "description", "scope")).casefold()
        if needle and needle not in haystack:
            continue
        if active is not None and (item.active != active or item.retired == active):
            continue
        values.append(item)
    return tuple(values[:MAX_RESULTS])


def source_label(snapshot: ResourceCatalogSnapshot, source_key: str) -> str:
    return str(getattr(snapshot.sources.get(source_key), "label", "") or "Catalog source unavailable")


__all__ = [
    "ResourceCatalogSnapshot", "ResourceCatalogSnapshotService", "SnapshotCompletion",
    "filter_entries", "filter_frequencies", "load_resource_catalog_snapshot", "source_label",
]
