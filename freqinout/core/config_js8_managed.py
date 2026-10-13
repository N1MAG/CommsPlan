from __future__ import annotations

import configparser
import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence, Tuple

from freqinout.core.config_autodiscovery import LOCALHOST, RadioInstanceProposal
from freqinout.core.js8_storage import (
    js8_application_name,
    native_managed_rig_name,
    qt_config_path_candidates,
    qt_data_root_candidates,
)


@dataclass(frozen=True)
class JS8CallManagedProfilePlan:
    profile_name: str
    instance_name: str
    executable_path: str
    config_dir: Path
    settings_path: Path
    save_dir: Path
    forms_dir: Path
    directed_path: Path
    all_path: Path
    inbox_path: Path
    application_data_root: Path
    rig_name: str
    application_name: str
    flrig_host: str
    flrig_port: int
    tcp_host: str
    tcp_port: int
    udp_port: int
    control_route: str = "flrig"
    rig_summary: str = ""
    settings: Mapping[str, str] = field(default_factory=dict)


def build_js8call_managed_profile_plans(
    proposals: Sequence[RadioInstanceProposal],
    *,
    config_root: Path,
    js8call_path: str = "",
    callsign: str = "",
    grid: str = "",
    control_route: str = "flrig",
    radio_label: str = "",
    platform: str | None = None,
    storage_home: Path | None = None,
) -> Tuple[JS8CallManagedProfilePlan, ...]:
    plans = []
    used_rig_names: set[str] = set()
    route_key = str(control_route or "flrig").strip().lower()
    for proposal in proposals:
        if "js8call" not in proposal.enabled_apps:
            continue
        ports = _ports_by_service(proposal)
        base_rig_name = native_managed_rig_name(proposal.name)
        rig_name = base_rig_name
        suffix = 2
        while rig_name.casefold() in used_rig_names:
            rig_name = f"{base_rig_name[:43]}-{suffix}"
            suffix += 1
        used_rig_names.add(rig_name.casefold())
        application_name = js8_application_name(rig_name)
        settings_path = qt_config_path_candidates(
            application_name=application_name,
            platform=platform,
            home=storage_home,
        )[0]
        profile_root = settings_path.parent
        application_data_root = qt_data_root_candidates(
            application_name=application_name,
            platform=platform,
            home=storage_home,
        )[0]
        save_dir = application_data_root / "save"
        forms_dir = application_data_root / "forms"
        directed_path = application_data_root / "DIRECTED.TXT"
        all_path = application_data_root / "ALL.TXT"
        inbox_path = application_data_root / "inbox.db3"
        flrig_port = ports.get("flrig", 12345)
        tcp_port = ports.get("js8call", 2442)
        udp_port = ports.get("js8call_udp", 2242)
        settings = {
            "TCPEnabled": "true",
            # Modern JS8Call variants separate listening from command
            # authorization.  Without this flag they accept the socket but
            # silently ignore FIO's API requests.
            "AcceptTCPRequests": "true",
            "TCPServer": LOCALHOST,
            "TCPServerPort": str(tcp_port),
            "TCPMaxConnections": "2",
            "UDPEnabled": "true",
            "UDPServerPort": str(udp_port),
            "SaveDir": str(save_dir),
        }
        rig_summary = "JS8Call radio/CAT selection requires operator review."
        if route_key == "flrig":
            settings["Rig"] = "FLRig FLRig"
            settings["CATNetworkPort"] = f"{LOCALHOST}:{flrig_port}"
            rig_summary = f"FLRig {LOCALHOST}:{flrig_port}"
        elif route_key == "js8call":
            rig_text = str(radio_label or proposal.name or "").strip()
            if rig_text:
                rig_summary = f"JS8Call controls {rig_text}; confirm the radio in JS8Call."
            else:
                rig_summary = "JS8Call controls the radio; confirm the radio in JS8Call."
        elif route_key in {"none", "manual", "later"}:
            rig_summary = "No CommsPlan-managed JS8Call frequency control."
        if callsign.strip():
            settings["MyCall"] = callsign.strip().upper()
        if grid.strip():
            settings["MyGrid"] = grid.strip().upper()
        plans.append(
            JS8CallManagedProfilePlan(
                profile_name=rig_name,
                instance_name=proposal.instance_name,
                executable_path=js8call_path,
                config_dir=profile_root,
                settings_path=settings_path,
                save_dir=save_dir,
                forms_dir=forms_dir,
                directed_path=directed_path,
                all_path=all_path,
                inbox_path=inbox_path,
                application_data_root=application_data_root,
                rig_name=rig_name,
                application_name=application_name,
                flrig_host=LOCALHOST,
                flrig_port=flrig_port,
                tcp_host=LOCALHOST,
                tcp_port=tcp_port,
                udp_port=udp_port,
                control_route=route_key,
                rig_summary=rig_summary,
                settings=settings,
            )
        )
    return tuple(plans)


def create_js8call_managed_directories(plans: Sequence[JS8CallManagedProfilePlan]) -> Tuple[Path, ...]:
    created_or_ready = []
    seen = set()
    for plan in plans:
        for path in (plan.config_dir, plan.save_dir, plan.forms_dir):
            key = str(path)
            if key in seen:
                continue
            path.mkdir(parents=True, exist_ok=True)
            seen.add(key)
            created_or_ready.append(path)
    return tuple(created_or_ready)


def render_js8call_multisettings_ini(
    existing_ini_text: str,
    plans: Sequence[JS8CallManagedProfilePlan],
) -> str:
    if len(plans) != 1:
        raise ValueError(
            "A JS8Call rig identity owns one native settings file; render exactly one plan per file."
        )
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    if existing_ini_text.strip():
        parser.read_string(existing_ini_text)
    # ``--rig-name`` already selects a distinct application/settings file.
    # JS8Call reads the active values from the root Configuration group.
    # MultiSettings/<name> is reserved for alternatives selected with the
    # separate ``--config`` option; writing only there leaves this launch's
    # prepared values inactive.
    plan = plans[0]
    section = "Configuration"
    if not parser.has_section(section):
        parser.add_section(section)
    for key, value in plan.settings.items():
        parser.set(section, key, str(value))
    output = io.StringIO()
    parser.write(output)
    return output.getvalue()


def apply_js8call_multisettings_plan(
    plan: JS8CallManagedProfilePlan,
    *,
    ini_path: Path,
) -> Path:
    """Apply one managed JS8Call profile to an explicit JS8Call.ini path."""

    target = Path(ini_path).expanduser()
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    rendered = render_js8call_multisettings_ini(existing, (plan,))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered, encoding="utf-8")
    return target


def verify_js8call_multisettings_plan(
    plan: JS8CallManagedProfilePlan,
    *,
    ini_path: Path,
) -> bool:
    """Read back one exact MultiSettings section after a qualified write."""

    target = Path(ini_path).expanduser()
    if not target.is_file():
        return False
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    try:
        parser.read_string(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, configparser.Error):
        return False
    section = "Configuration"
    if not parser.has_section(section):
        return False
    return all(parser.get(section, key, fallback=None) == str(value) for key, value in plan.settings.items())


def _ports_by_service(proposal: RadioInstanceProposal) -> Mapping[str, int]:
    return {assignment.service: int(assignment.assigned_port) for assignment in proposal.ports}
