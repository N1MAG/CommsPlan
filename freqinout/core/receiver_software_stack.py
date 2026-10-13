"""Receiver-safe software launch selections.

Observer / SDR profiles use the regular per-radio launch bundle, but their
launch entries are deliberately capability-scoped.  This module is Qt-free so
the guided setup, Settings, and launch planner all apply the same contract.

It does not attempt to configure an SDR driver or grant transmit authority to
an application. SDR++ supplies the receiver endpoint; an isolated JS8Call
instance may additionally be launched for receive-only ingest.
"""

from __future__ import annotations

import shlex
from typing import Any, Dict, Iterable, List, Mapping


RECEIVE_ONLY_EXECUTION_SCOPE = "receive_only"
STANDARD_EXECUTION_SCOPE = "standard"

# Values are intentionally operator-facing application names.  Adding another
# entry here requires a separate capability review; accepting arbitrary launch
# commands would make the "receive-only" label an unenforceable promise.
RECEIVER_STACK_APPLICATIONS: Mapping[str, Mapping[str, Any]] = {
    "SDR++": {
        "instance_key": "receiver:sdrpp",
        "display_name": "SDR++",
        "supports_startup": True,
        "default_readiness": {"readiness": "process"},
    },
}

# JS8Call is not a receiver application selection. It is created through the
# reviewed software-instance adoption flow, which owns its endpoint, profile,
# storage, and launch identity. The planner still recognizes that resulting
# launch row when it is explicitly receive-only scoped.
OBSERVER_LAUNCH_APPLICATIONS = frozenset(
    {
        *RECEIVER_STACK_APPLICATIONS,
        "JS8Call",
        "FLDigi",
        "FLMsg",
        "FLAmp",
    }
)

_OBSERVER_APPLICATION_EXECUTABLES: Mapping[str, frozenset[str]] = {
    "SDR++": frozenset({"sdrpp", "sdrpp.exe", "sdr++", "sdr++.exe", "sdr++.app"}),
    "JS8Call": frozenset(
        {
            "js8call",
            "js8call.exe",
            "js8call-improved",
            "js8call-improved.exe",
            "js8call-subspace",
            "js8call-subspace.exe",
        }
    ),
    "FLDigi": frozenset({"fldigi", "fldigi.exe", "fldigi.app"}),
    "FLMsg": frozenset({"flmsg", "flmsg.exe", "flmsg.app"}),
    "FLAmp": frozenset({"flamp", "flamp.exe", "flamp.app"}),
}


def is_observer_profile(profile: Mapping[str, Any]) -> bool:
    return str(profile.get("device_class", "") or "").strip().lower() == "observer"


def receiver_stack_application_names() -> tuple[str, ...]:
    """Return the approved receiver-only applications in stable UI order."""

    return tuple(RECEIVER_STACK_APPLICATIONS)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _command_basename(value: str) -> str:
    return str(value or "").strip().replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].casefold()


def _validate_receiver_launch_target(name: str, launch_path: str, launch_command: str) -> None:
    """Prevent an approved app label from becoming an arbitrary executable.

    LaunchOrchestrator deliberately supports free-form commands for ordinary
    radio tools. Observer profiles make a stronger promise, so the executable
    itself must also match a reviewed observer-stack application.
    """

    target = launch_command or launch_path
    if not target:
        return
    allowed = _OBSERVER_APPLICATION_EXECUTABLES.get(name, frozenset())
    if not allowed:
        raise ValueError(f"{name} is not approved for a receive-only SDR launch stack.")
    if not launch_command and _command_basename(launch_path) in allowed:
        return
    try:
        parts = shlex.split(target, posix=True)
    except ValueError as exc:
        raise ValueError(f"{name} launch target is not a valid command or path.") from exc
    if not parts:
        raise ValueError(f"{name} launch target is blank.")
    first = _command_basename(parts[0])
    if first in allowed:
        return
    if first == "open":
        for index, token in enumerate(parts[:-1]):
            if token == "-a" and _command_basename(parts[index + 1]) in allowed:
                return
    raise ValueError(
        f"{name} launch target must start the reviewed {name} application, not another executable."
    )


def execution_scope(item: Mapping[str, Any]) -> str:
    """Return the bounded execution scope stored with a launch item."""

    readiness = item.get("readiness_policy", {})
    nested = readiness.get("execution_scope") if isinstance(readiness, Mapping) else ""
    scope = _clean(item.get("execution_scope") or nested).lower().replace("-", "_")
    return scope or STANDARD_EXECUTION_SCOPE


def is_receive_only_launch_item(item: Mapping[str, Any]) -> bool:
    """Whether an item is explicitly declared and approved for observer use."""

    name = _clean(item.get("name"))
    return execution_scope(item) == RECEIVE_ONLY_EXECUTION_SCOPE and name in OBSERVER_LAUNCH_APPLICATIONS


def build_receiver_launch_items(
    profile: Mapping[str, Any],
    selections: Iterable[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Build durable, observer-safe bundle rows from guided-setup selections.

    A selected application must provide either an executable/app path or a
    fully quoted launch command.  The bundle intentionally owns only startup
    and process-readiness behavior: it never adds PTT, transmit, scheduler,
    or radio-control settings.
    """

    if not is_observer_profile(profile):
        raise ValueError("Receive-only software stacks can only be configured for observer / SDR profiles.")

    items: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for selection in selections:
        if not isinstance(selection, Mapping):
            raise ValueError("Each receiver software selection must be an object.")
        name = _clean(selection.get("name") or selection.get("application"))
        meta = RECEIVER_STACK_APPLICATIONS.get(name)
        if meta is None:
            allowed = ", ".join(receiver_stack_application_names())
            raise ValueError(f"Unsupported receive-only application: {name or 'blank'}. Supported: {allowed}.")
        key = name.casefold()
        if key in seen:
            raise ValueError(f"Duplicate receive-only application: {name}.")
        seen.add(key)

        launch_path = _clean(selection.get("launch_path_override") or selection.get("launch_path"))
        launch_command = _clean(selection.get("launch_command_override") or selection.get("launch_command"))
        startup = _truthy(selection.get("startup", selection.get("launch_at_startup", True)))
        if startup and not (launch_path or launch_command):
            raise ValueError(f"{name} needs an application path or launch command before CommsPlan can launch it.")
        _validate_receiver_launch_target(name, launch_path, launch_command)

        readiness = dict(meta.get("default_readiness", {}))
        supplied_readiness = selection.get("readiness_policy", {})
        if isinstance(supplied_readiness, Mapping):
            # Preserve only generic launch readiness metadata.  Scope is always
            # set below and no radio/transmit settings are carried in this row.
            readiness.update({str(key): value for key, value in supplied_readiness.items() if str(key)})
        readiness["execution_scope"] = RECEIVE_ONLY_EXECUTION_SCOPE
        items.append(
            {
                "name": name,
                "instance_key": _clean(selection.get("instance_key")) or str(meta["instance_key"]),
                "enabled": _truthy(selection.get("enabled", True)),
                "startup": startup,
                "monitor_health": _truthy(selection.get("monitor_health", True)),
                "launch_path_override": launch_path,
                "launch_command_override": launch_command,
                "dependencies": [],
                "readiness_policy": readiness,
                "execution_scope": RECEIVE_ONLY_EXECUTION_SCOPE,
            }
        )
    return items


def validate_observer_launch_items(items: Iterable[Mapping[str, Any]]) -> None:
    """Reject ordinary launch entries before an observer launch plan is made."""

    for raw in items:
        if not isinstance(raw, Mapping):
            continue
        if not is_receive_only_launch_item(raw):
            name = _clean(raw.get("name")) or "Unnamed application"
            raise ValueError(
                f"{name} is not approved for a receive-only SDR launch stack. "
                "Observer profiles cannot launch unreviewed or standard-scoped radio software."
            )
        _validate_receiver_launch_target(
            _clean(raw.get("name")),
            _clean(raw.get("launch_path_override")),
            _clean(raw.get("launch_command_override")),
        )


__all__ = [
    "RECEIVE_ONLY_EXECUTION_SCOPE",
    "RECEIVER_STACK_APPLICATIONS",
    "STANDARD_EXECUTION_SCOPE",
    "build_receiver_launch_items",
    "execution_scope",
    "is_observer_profile",
    "is_receive_only_launch_item",
    "receiver_stack_application_names",
    "validate_observer_launch_items",
]
