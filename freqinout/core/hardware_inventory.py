"""Bounded, read-only hardware inventory for radio connection setup."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from freqinout.core.radio_hardware_binding import HardwareBindingError, normalize_hardware_binding


CommandRunner = Callable[[Sequence[str], float], str]


@dataclass(frozen=True)
class HardwareInventorySnapshot:
    generation: int
    platform: str
    candidates: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...] = ()


def _default_command_runner(command: Sequence[str], timeout_seconds: float) -> str:
    completed = subprocess.run(
        list(command),
        check=False,
        capture_output=True,
        text=True,
        timeout=max(0.1, float(timeout_seconds)),
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"exit {completed.returncode}"
        raise RuntimeError(detail)
    return completed.stdout


_SERIAL_INTERFACE_PATTERN = re.compile(r"-if(?P<number>[0-9a-fA-F]{2})-")


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except (OSError, RuntimeError):
        return ""


def _sysfs_parent_value(start: Path, name: str, *, stop: Path | None = None) -> str:
    """Read one sysfs attribute without escaping the bounded parent chain."""

    try:
        current = start.resolve(strict=True)
    except (OSError, RuntimeError):
        return ""
    boundary = stop.resolve() if stop is not None else Path("/sys")
    for candidate in (current, *current.parents):
        try:
            candidate.relative_to(boundary)
        except ValueError:
            break
        value = _read_text(candidate / name)
        if value:
            return value
        if candidate == boundary:
            break
    return ""


def _linux_serial_candidates(
    serial_by_id_dir: Path,
    *,
    sysfs_tty_root: Path = Path("/sys/class/tty"),
) -> list[dict[str, Any]]:
    if not serial_by_id_dir.is_dir():
        return []
    candidates: list[dict[str, Any]] = []
    for entry in sorted(serial_by_id_dir.iterdir(), key=lambda path: path.name.casefold()):
        try:
            resolved = str(entry.resolve(strict=True))
        except (OSError, RuntimeError):
            continue
        tty_name = Path(resolved).name
        sysfs_device = sysfs_tty_root / tty_name / "device"
        match = _SERIAL_INTERFACE_PATTERN.search(entry.name)
        interface_number = _sysfs_parent_value(
            sysfs_device,
            "bInterfaceNumber",
            stop=sysfs_tty_root.parents[1],
        )
        if not interface_number and match is not None:
            interface_number = match.group("number")
        interface_number = interface_number.lower()
        interface_text = _sysfs_parent_value(
            sysfs_device,
            "interface",
            stop=sysfs_tty_root.parents[1],
        )
        function = interface_text.strip()
        selector: dict[str, Any] = {"by_id": str(entry), "stability_class": "stable"}
        if interface_number:
            selector["interface_number"] = interface_number
        if function:
            selector["function"] = function
        vendor_id = _sysfs_parent_value(
            sysfs_device,
            "idVendor",
            stop=sysfs_tty_root.parents[1],
        )
        product_id = _sysfs_parent_value(
            sysfs_device,
            "idProduct",
            stop=sysfs_tty_root.parents[1],
        )
        hardware_serial = _sysfs_parent_value(
            sysfs_device,
            "serial",
            stop=sysfs_tty_root.parents[1],
        )
        if vendor_id:
            selector["vendor_id"] = vendor_id
        if product_id:
            selector["product_id"] = product_id
        if hardware_serial:
            selector["hardware_serial"] = hardware_serial
        human_function = function or (
            f"interface {interface_number}" if interface_number else "unconfirmed interface"
        )
        binding = normalize_hardware_binding(
            {
                "role": "cat",
                "platform": "linux",
                "provider": "udev",
                "transport": "usb-serial",
                "label": f"{entry.name.replace('_', ' ')} · {human_function}",
                "selector": selector,
                # The durable by-id path is the path consumers should open.
                # The current tty path remains observational evidence only.
                "observed_locator": {
                    "path": str(entry),
                    "by_id": str(entry),
                    "resolved_path": resolved,
                    "interface": interface_text,
                },
            }
        )
        binding["candidate_key"] = f"serial:{binding['selector_fingerprint']}"
        folded_function = function.casefold()
        if "enhanced" in folded_function:
            binding["endpoint_kind"] = "primary_cat"
        elif "standard" in folded_function:
            binding["endpoint_kind"] = "secondary_serial"
        else:
            binding["endpoint_kind"] = "unconfirmed_serial"
        binding["capabilities"] = ("cat", "ptt")
        candidates.append(binding)
    return candidates


def _pulse_properties(row: Mapping[str, Any]) -> dict[str, Any]:
    properties = row.get("properties", {})
    return dict(properties) if isinstance(properties, Mapping) else {}


def _is_monitor_source(row: Mapping[str, Any], properties: Mapping[str, Any], node_name: str) -> bool:
    monitor = row.get("monitor_of_sink")
    if monitor not in (None, "", -1, 4294967295, "4294967295"):
        return True
    return bool(
        node_name.endswith(".monitor")
        or str(properties.get("media.class", "") or "").casefold() == "audiomonitor"
        or str(properties.get("device.class", "") or "").casefold() == "monitor"
    )


def _audio_sysfs_path(raw_path: str, *, sysfs_root: Path) -> Path | None:
    value = str(raw_path or "").strip()
    if not value:
        return None
    candidate = Path(value)
    if candidate.is_absolute() and str(candidate).startswith(str(sysfs_root)):
        return candidate
    if candidate.is_absolute() and str(candidate).startswith("/devices/"):
        return sysfs_root / "devices" / str(candidate).removeprefix("/devices/")
    return None


def _usb_serial_from_sysfs(properties: Mapping[str, Any], *, sysfs_root: Path) -> str:
    sysfs_path = _audio_sysfs_path(
        str(properties.get("sysfs.path", "") or ""),
        sysfs_root=sysfs_root,
    )
    if sysfs_path is None:
        return ""
    # sysfs is the authority. Pulse descriptors are often decorated or
    # composite and remain display/corroborating evidence only.
    return _sysfs_parent_value(sysfs_path, "serial", stop=sysfs_root)


def _linux_audio_candidates(
    rows: Any,
    *,
    role: str,
    sysfs_root: Path = Path("/sys"),
) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(rows, list):
        return [], 0
    candidates: list[dict[str, Any]] = []
    skipped = 0
    direction = "input" if role == "audio_rx" else "output"
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        row = dict(raw)
        properties = _pulse_properties(row)
        node_name = str(row.get("name", "") or "").strip()
        serial = _usb_serial_from_sysfs(properties, sysfs_root=sysfs_root)
        bus_path = str(
            properties.get("device.bus_path", "")
            or properties.get("sysfs.path", "")
            or ""
        ).strip()
        profile = str(
            properties.get("device.profile.name", "")
            or properties.get("device.profile.description", "")
            or ""
        ).strip()
        monitor = role == "audio_rx" and _is_monitor_source(row, properties, node_name)
        selector: dict[str, Any] = {"direction": direction}
        if serial:
            selector.update(
                {
                    "hardware_serial": serial,
                    # A Pulse/PipeWire node name may acquire a new suffix after
                    # reconnect. Keep it as observed evidence, not identity.
                    "endpoint_uid": "|".join(
                        part for part in (serial, profile, direction) if part
                    ),
                    "stability_class": "stable",
                }
            )
            if bus_path:
                selector["physical_path"] = bus_path
        elif bus_path:
            selector.update(
                {
                    "physical_path": bus_path,
                    "endpoint_uid": "|".join(part for part in (bus_path, profile, direction) if part),
                    "stability_class": "port_bound",
                }
            )
        else:
            skipped += 1
            continue
        vendor_id = str(properties.get("device.vendor.id", "") or "").strip()
        product_id = str(properties.get("device.product.id", "") or "").strip()
        if vendor_id:
            selector["vendor_id"] = vendor_id
        if product_id:
            selector["product_id"] = product_id
        label = str(
            properties.get("device.description", "")
            or properties.get("node.description", "")
            or properties.get("alsa.card_name", "")
            or node_name
            or "USB audio"
        ).strip()
        bus = str(properties.get("device.bus", "") or "").strip().casefold()
        endpoint_kind = (
            "monitor"
            if monitor
            else "physical"
            if bus == "usb" or "usb" in bus_path.casefold()
            else "desktop"
        )
        transport = (
            "pulse-monitor"
            if monitor
            else "usb-audio"
            if endpoint_kind == "physical"
            else "desktop-audio"
        )
        try:
            binding = normalize_hardware_binding(
                {
                    "role": role,
                    "platform": "linux",
                    "provider": "pulse",
                    "transport": transport,
                    "label": f"{label} · {direction}",
                    "selector": selector,
                    "observed_locator": {
                        "node_name": node_name,
                        "endpoint_kind": endpoint_kind,
                        "sample_specification": row.get("sample_specification", {}),
                        "channel_map": row.get("channel_map", ""),
                    },
                }
            )
        except HardwareBindingError:
            skipped += 1
            continue
        binding["candidate_key"] = f"audio:{binding['selector_fingerprint']}"
        binding["capabilities"] = (role,)
        binding["endpoint_kind"] = endpoint_kind
        candidates.append(binding)
    return candidates, skipped


def discover_hardware_inventory(
    *,
    platform_name: str | None = None,
    serial_by_id_dir: Path = Path("/dev/serial/by-id"),
    sysfs_root: Path = Path("/sys"),
    command_runner: CommandRunner = _default_command_runner,
    generation: int | None = None,
) -> HardwareInventorySnapshot:
    """Take one bounded snapshot. Call from a worker, never a UI callback."""

    platform_key = str(platform_name or sys.platform).strip().lower()
    if platform_key.startswith("linux"):
        platform_key = "linux"
    elif platform_key.startswith("win"):
        platform_key = "windows"
    elif platform_key.startswith("darwin") or platform_key.startswith("mac"):
        platform_key = "macos"
    snapshot_generation = int(generation if generation is not None else time.monotonic_ns())
    candidates: list[dict[str, Any]] = []
    warnings: list[str] = []

    if platform_key == "linux":
        candidates.extend(
            _linux_serial_candidates(
                serial_by_id_dir,
                sysfs_tty_root=sysfs_root / "class" / "tty",
            )
        )
        for command_name, role in (("sources", "audio_rx"), ("sinks", "audio_tx")):
            try:
                raw_json = command_runner(("pactl", "-f", "json", "list", command_name), 3.0)
                rows = json.loads(raw_json or "[]")
                audio_candidates, skipped = _linux_audio_candidates(rows, role=role, sysfs_root=sysfs_root)
                candidates.extend(audio_candidates)
                if skipped:
                    warnings.append(
                        f"{skipped} {command_name} lacked stable identity and were not offered for automatic binding."
                    )
            except FileNotFoundError:
                warnings.append("Pulse-compatible audio discovery is unavailable because pactl was not found.")
                break
            except (json.JSONDecodeError, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                warnings.append(f"Unable to read {command_name} safely: {exc}")
    else:
        warnings.append(
            "Automatic hardware discovery is not qualified on this platform yet. Existing application-managed connections are unchanged."
        )

    return HardwareInventorySnapshot(
        generation=snapshot_generation,
        platform=platform_key,
        candidates=tuple(candidates),
        warnings=tuple(dict.fromkeys(warnings)),
    )
