"""Bounded background inventory and immutable per-radio reconciliation cache."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Callable, Mapping

from freqinout.core.hardware_inventory import HardwareInventorySnapshot, discover_hardware_inventory
from freqinout.core.hardware_reconciliation import RadioHardwareSnapshot, reconcile_radio_hardware


@dataclass(frozen=True)
class HardwareLifecycleSnapshot:
    generation: int
    captured_monotonic: float
    inventory: HardwareInventorySnapshot
    radios: Mapping[int, RadioHardwareSnapshot]


def _linux_hardware_change_token() -> tuple[tuple[str, int, tuple[str, ...]], ...]:
    """Return a cheap hotplug token without querying Pulse or opening devices."""

    if not sys.platform.startswith("linux"):
        return ()
    values: list[tuple[str, int, tuple[str, ...]]] = []
    for path in (
        Path("/dev/serial/by-id"),
        Path("/dev/snd/by-id"),
        Path("/dev/snd/by-path"),
    ):
        try:
            stat = path.stat()
            names = tuple(sorted(entry.name for entry in path.iterdir()))
            values.append((str(path), int(stat.st_mtime_ns), names))
        except OSError:
            values.append((str(path), 0, ()))
    return tuple(values)


class HardwareLifecycleService:
    """One coalesced station inventory worker with generation-fenced results."""

    def __init__(
        self,
        bundle_loader: Callable[[], list[dict]],
        *,
        inventory_provider: Callable[..., HardwareInventorySnapshot] = discover_hardware_inventory,
        minimum_interval_sec: float = 2.0,
        refresh_interval_sec: float = 300.0,
        change_probe: Callable[[], object] | None = _linux_hardware_change_token,
        probe_interval_sec: float = 1.0,
        settle_quiet_sec: float = 0.35,
        settle_max_sec: float = 2.0,
    ) -> None:
        self._bundle_loader = bundle_loader
        self._inventory_provider = inventory_provider
        self._minimum_interval_sec = max(0.25, float(minimum_interval_sec))
        self._refresh_interval_sec = max(self._minimum_interval_sec, float(refresh_interval_sec))
        self._change_probe = change_probe
        self._probe_interval_sec = max(0.1, float(probe_interval_sec))
        self._settle_quiet_sec = max(0.0, float(settle_quiet_sec))
        self._settle_max_sec = max(self._settle_quiet_sec, float(settle_max_sec))
        self._condition = threading.Condition()
        self._requested_generation = 0
        self._published_generation = 0
        self._shutdown = False
        self._snapshot: HardwareLifecycleSnapshot | None = None
        self._listeners: list[Callable[[HardwareLifecycleSnapshot], None]] = []
        self._thread = threading.Thread(target=self._run, name="FIO hardware inventory", daemon=True)
        self._thread.start()
        self.request_refresh()

    def add_listener(self, listener: Callable[[HardwareLifecycleSnapshot], None]) -> None:
        with self._condition:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def latest(self) -> HardwareLifecycleSnapshot | None:
        with self._condition:
            return self._snapshot

    def request_refresh(self) -> int:
        with self._condition:
            if self._shutdown:
                return self._requested_generation
            self._requested_generation += 1
            generation = self._requested_generation
            self._condition.notify_all()
            return generation

    def stop(self, timeout_sec: float = 1.0) -> bool:
        with self._condition:
            self._shutdown = True
            self._requested_generation += 1
            self._condition.notify_all()
        self._thread.join(timeout=max(0.0, float(timeout_sec)))
        return not self._thread.is_alive()

    def _run(self) -> None:
        last_scan = 0.0
        next_periodic = time.monotonic() + self._refresh_interval_sec
        change_token: object | None = None
        while True:
            with self._condition:
                while not self._shutdown and self._requested_generation <= self._published_generation:
                    now = time.monotonic()
                    self._condition.wait(
                        timeout=min(
                            self._probe_interval_sec,
                            max(0.0, next_periodic - now),
                        )
                    )
                    if self._shutdown or self._requested_generation > self._published_generation:
                        continue
                    now = time.monotonic()
                    detected_change = False
                    if self._change_probe is not None:
                        try:
                            probed = self._change_probe()
                        except Exception:
                            probed = change_token
                        if change_token is None:
                            change_token = probed
                        elif probed != change_token:
                            change_token = probed
                            detected_change = True
                    if detected_change or now >= next_periodic:
                        self._requested_generation += 1
                if self._shutdown:
                    return
                generation = self._requested_generation
                settle_started = time.monotonic()
                quiet_deadline = settle_started + self._settle_quiet_sec
                hard_deadline = settle_started + self._settle_max_sec
                while not self._shutdown:
                    now = time.monotonic()
                    remaining = min(quiet_deadline - now, hard_deadline - now)
                    if remaining <= 0:
                        break
                    prior_generation = self._requested_generation
                    self._condition.wait(timeout=remaining)
                    if self._requested_generation != prior_generation:
                        generation = self._requested_generation
                        quiet_deadline = time.monotonic() + self._settle_quiet_sec
                if self._shutdown:
                    return
            remaining = self._minimum_interval_sec - (time.monotonic() - last_scan)
            if remaining > 0:
                with self._condition:
                    self._condition.wait(timeout=remaining)
                    if self._shutdown:
                        return
                    generation = self._requested_generation
            try:
                bundles = tuple(self._bundle_loader())
                inventory = (
                    self._inventory_provider(generation=generation)
                    if bundles
                    else HardwareInventorySnapshot(generation, sys.platform, (), ())
                )
                radios = {
                    int(bundle.get("radio_profile_id", 0) or 0): reconcile_radio_hardware(bundle, inventory)
                    for bundle in bundles
                    if int(bundle.get("radio_profile_id", 0) or 0) > 0
                }
                snapshot = HardwareLifecycleSnapshot(
                    generation=generation,
                    captured_monotonic=time.monotonic(),
                    inventory=inventory,
                    radios=radios,
                )
            except Exception:
                # Publish unavailable evidence for this generation. Retaining
                # a prior matched snapshot after a provider failure could
                # authorize a launch with hardware that is no longer present.
                snapshot = HardwareLifecycleSnapshot(
                    generation=generation,
                    captured_monotonic=time.monotonic(),
                    inventory=HardwareInventorySnapshot(
                        generation,
                        sys.platform,
                        (),
                        ("Hardware inventory failed; automatic hardware use is unavailable.",),
                    ),
                    radios={},
                )
            with self._condition:
                if generation != self._requested_generation or self._shutdown:
                    continue
                self._snapshot = snapshot
                self._published_generation = generation
                listeners = tuple(self._listeners)
            last_scan = time.monotonic()
            next_periodic = last_scan + self._refresh_interval_sec
            if self._change_probe is not None:
                try:
                    change_token = self._change_probe()
                except Exception:
                    pass
            for listener in listeners:
                try:
                    listener(snapshot)
                except Exception:
                    continue


__all__ = ["HardwareLifecycleService", "HardwareLifecycleSnapshot"]


_SHARED_SERVICES: dict[str, HardwareLifecycleService] = {}
_SHARED_LOCK = threading.Lock()


def shared_hardware_lifecycle_service(store: object) -> HardwareLifecycleService:
    db_path = str(Path(getattr(store, "db_path")).resolve())
    with _SHARED_LOCK:
        service = _SHARED_SERVICES.get(db_path)
        if service is None:
            loader = getattr(store, "list_radio_hardware_bundles")
            service = HardwareLifecycleService(loader)
            _SHARED_SERVICES[db_path] = service
        return service


def existing_hardware_lifecycle_service(store: object) -> HardwareLifecycleService | None:
    db_path = str(Path(getattr(store, "db_path")).resolve())
    with _SHARED_LOCK:
        return _SHARED_SERVICES.get(db_path)


def shutdown_hardware_lifecycle_services(timeout_sec: float = 1.0) -> None:
    with _SHARED_LOCK:
        services = tuple(_SHARED_SERVICES.values())
        _SHARED_SERVICES.clear()
    deadline = time.monotonic() + max(0.0, float(timeout_sec))
    for service in services:
        service.stop(max(0.0, deadline - time.monotonic()))


__all__ += [
    "existing_hardware_lifecycle_service",
    "shared_hardware_lifecycle_service",
    "shutdown_hardware_lifecycle_services",
]
