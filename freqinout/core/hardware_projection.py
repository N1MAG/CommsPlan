"""Qualified launch/native projections for reviewed radio hardware bindings."""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from freqinout.core.hardware_reconciliation import RadioHardwareSnapshot


QUALIFIED_PROJECTION_KEYS = frozenset(
    {
        "fldigi:pulse-env:v1",
        "js8call:pulse-env:v1",
        "wsjtx:pulse-env:v1",
        "flrig:fltk-prefs:v1",
    }
)
_AUDIO_KEYS_BY_APP = {
    "fldigi": "fldigi:pulse-env:v1",
    "js8call": "js8call:pulse-env:v1",
    "wsjt-x": "wsjtx:pulse-env:v1",
    "wsjtx": "wsjtx:pulse-env:v1",
}


@dataclass(frozen=True)
class HardwareLaunchProjection:
    environment: tuple[tuple[str, str], ...] = ()
    blocked: bool = False
    reason: str = ""
    projection_key: str = ""
    verified_roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class NativeProjectionResult:
    target: Path
    backup: Path | None
    previous_bytes: bytes
    desired_locator: str
    changed: bool
    readback_verified: bool


def _role(snapshot: RadioHardwareSnapshot, name: str):
    return next((item for item in snapshot.roles if item.role == name), None)


def _exact_locator(snapshot: RadioHardwareSnapshot, role: str, key: str) -> tuple[str, str]:
    item = _role(snapshot, role)
    if item is None:
        return "", f"No reviewed {role.replace('_', ' ')} assignment exists for this radio."
    if item.state not in {"exact", "port_bound", "unstable_confirmed"}:
        return "", item.reason or f"The {role.replace('_', ' ')} assignment is not available."
    value = str(item.observed_locator.get(key, "") or "").strip()
    if not value:
        return "", f"The matched {role.replace('_', ' ')} assignment has no exact {key}."
    if value.casefold() in {"default", "@default_source@", "@default_sink@"}:
        return "", f"The {role.replace('_', ' ')} assignment resolved to a forbidden default endpoint."
    return value, ""


def _exact_locator_preferred(
    snapshot: RadioHardwareSnapshot,
    role: str,
    keys: tuple[str, ...],
) -> tuple[str, str]:
    item = _role(snapshot, role)
    if item is None:
        return "", f"No reviewed {role.replace('_', ' ')} assignment exists for this radio."
    if item.state not in {"exact", "port_bound", "unstable_confirmed"}:
        return "", item.reason or f"The {role.replace('_', ' ')} assignment is not available."
    for key in keys:
        value = str(item.observed_locator.get(key, "") or "").strip()
        if value and value.casefold() not in {"default", "@default_source@", "@default_sink@"}:
            return value, ""
    return "", f"The matched {role.replace('_', ' ')} assignment has no exact {' or '.join(keys)}."


def build_hardware_launch_projection(
    app_name: str,
    snapshot: RadioHardwareSnapshot,
    *,
    projection_key: str,
    receive_only: bool = False,
) -> HardwareLaunchProjection:
    """Build a process-only projection; never guesses a consumer capability."""

    app = str(app_name or "").strip().casefold()
    key = str(projection_key or "").strip().casefold()
    if key not in QUALIFIED_PROJECTION_KEYS:
        return HardwareLaunchProjection(
            reason="Application-managed hardware: this app/version has no qualified CommsPlan projection.",
        )
    expected = _AUDIO_KEYS_BY_APP.get(app)
    if expected:
        if key != expected:
            return HardwareLaunchProjection(blocked=True, reason="The reviewed audio projection does not match this application.")
        rx_role = _role(snapshot, "audio_rx")
        tx_role = _role(snapshot, "audio_tx")
        if rx_role is not None and (rx_role.platform != "linux" or rx_role.provider != "pulse"):
            return HardwareLaunchProjection(
                blocked=True,
                reason="This audio assignment is not a qualified Linux Pulse-compatible endpoint.",
                projection_key=key,
            )
        if tx_role is not None and (tx_role.platform != "linux" or tx_role.provider != "pulse"):
            return HardwareLaunchProjection(
                blocked=True,
                reason="This transmit-audio assignment is not a qualified Linux Pulse-compatible endpoint.",
                projection_key=key,
            )
        source, source_error = _exact_locator(snapshot, "audio_rx", "node_name")
        sink, sink_error = _exact_locator(snapshot, "audio_tx", "node_name")
        # RX-only configurations may omit TX only when the canonical binding
        # and launch scope are explicitly receive-only. A transmit-capable
        # application must never inherit its desktop/default sink.
        has_tx_role = _role(snapshot, "audio_tx") is not None
        missing_transmit_assignment = not has_tx_role and not receive_only
        if source_error or (has_tx_role and sink_error) or missing_transmit_assignment:
            return HardwareLaunchProjection(
                blocked=True,
                reason=(
                    source_error
                    or (
                        "No reviewed transmit-audio assignment exists for this transmit-capable radio."
                        if missing_transmit_assignment
                        else sink_error
                    )
                ),
                projection_key=key,
            )
        environment = [("PULSE_SOURCE", source), ("FREQINOUT_AUDIO_BINDING", key)]
        roles = ["audio_rx"]
        if sink:
            environment.append(("PULSE_SINK", sink))
            roles.append("audio_tx")
        return HardwareLaunchProjection(
            environment=tuple(environment),
            projection_key=key,
            verified_roles=tuple(roles),
        )
    if app == "flrig" and key == "flrig:fltk-prefs:v1":
        cat_role = _role(snapshot, "cat")
        if cat_role is not None and (cat_role.platform != "linux" or cat_role.provider != "udev"):
            return HardwareLaunchProjection(
                blocked=True,
                reason="This FLRig writer is qualified only for Linux udev serial identities.",
                projection_key=key,
            )
        locator, error = _exact_locator_preferred(snapshot, "cat", ("by_id", "path"))
        return HardwareLaunchProjection(
            environment=(("FLRIG_SERIAL_PORT", locator),) if locator else (),
            blocked=bool(error),
            reason=error,
            projection_key=key,
            verified_roles=("cat",) if locator else (),
        )
    return HardwareLaunchProjection(blocked=True, reason="The hardware projection capability is not valid for this application.")


def _replace_fltk_value(text: str, key: str, value: str) -> str:
    line_pattern = re.compile(rf"(?m)^(?P<prefix>\s*{re.escape(key)}\s*:)\s*.*$")
    if line_pattern.search(text):
        return line_pattern.sub(lambda match: f"{match.group('prefix')}{value}", text, count=1)
    suffix = "" if not text or text.endswith("\n") else "\n"
    return f"{text}{suffix}{key}:{value}\n"


def apply_flrig_fltk_serial_projection(
    target: Path,
    desired_locator: str,
    *,
    projection_key: str,
) -> NativeProjectionResult:
    """Atomically change only FLRig's serial locator with exact readback.

    The caller must have selected the explicit ``flrig:fltk-prefs:v1``
    capability after version/platform qualification. Unknown versions never
    reach this writer.
    """

    if str(projection_key or "").strip().casefold() != "flrig:fltk-prefs:v1":
        raise ValueError("FLRig native configuration is not qualified for this app/version.")
    locator = str(desired_locator or "").strip()
    if not locator or locator.casefold() == "default":
        raise ValueError("FLRig requires an exact reviewed serial locator.")
    target = Path(target)
    if not target.is_file():
        raise FileNotFoundError(f"FLRig preference target does not exist: {target}")
    previous = target.read_bytes()
    try:
        text = previous.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("FLRig preference target is not qualified UTF-8 FLTK preferences.") from exc
    # Requiring other known FLTK keys prevents a same-named arbitrary file
    # from being patched.
    if "xcvr_name:" not in text or "xcvr_serial_port:" not in text:
        raise ValueError("FLRig preference target does not match the qualified FLTK layout.")
    rendered = _replace_fltk_value(text, "xcvr_serial_port", locator).encode("utf-8")
    if rendered == previous:
        return NativeProjectionResult(target, None, previous, locator, False, True)
    backup = target.with_name(f"{target.name}.fio-backup")
    backup.write_bytes(previous)
    temporary_name = ""
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, target)
        readback = target.read_text(encoding="utf-8")
        expected = re.compile(rf"(?m)^\s*xcvr_serial_port\s*:\s*{re.escape(locator)}\s*$")
        if not expected.search(readback):
            raise RuntimeError("FLRig serial projection readback did not match the reviewed locator.")
        return NativeProjectionResult(target, backup, previous, locator, True, True)
    except Exception:
        try:
            target.write_bytes(previous)
        finally:
            if temporary_name:
                try:
                    Path(temporary_name).unlink(missing_ok=True)
                except Exception:
                    pass
        raise


def rollback_native_projection(result: NativeProjectionResult) -> None:
    if result.changed:
        result.target.write_bytes(result.previous_bytes)


__all__ = [
    "HardwareLaunchProjection",
    "NativeProjectionResult",
    "QUALIFIED_PROJECTION_KEYS",
    "apply_flrig_fltk_serial_projection",
    "build_hardware_launch_projection",
    "rollback_native_projection",
]
