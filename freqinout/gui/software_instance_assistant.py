"""Guided, cache-only Software instance setup surface.

The assistant deliberately stops at a reviewed payload.  Settings (and the
multi-radio store) remain the persistence and discovery authorities.  This
keeps opening the assistant safe and makes it possible for a host to add its
own discovery adapter without teaching a widget about databases, files, or
processes.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
import re
import uuid
from typing import Any, Iterable, Mapping, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from freqinout.core.guided_instance_inventory import (
    GuidedInstanceInventorySnapshot,
    build_guided_instance_inventory,
    distinct_draft_seed,
    source_identity_fingerprint,
    stable_application_system_key,
    stable_draft_instance_key,
)
from freqinout.core.guided_launch_recipes import (
    GuidedLaunchRecipeResolution,
    recipe_draft_updates,
    recipe_resolution_from_mapping,
    resolve_guided_launch_recipe,
    settings_identity_mode_from_mapping,
)
from freqinout.core.varac_native_preparation import native_draft_fingerprint
from freqinout.gui.current_page_stack import CurrentPageStack
from freqinout.gui.theme import active_app_theme, button_height_for_font, button_style, label_style


SUPPORTED_INSTANCE_FAMILIES: tuple[tuple[str, str], ...] = (
    ("js8call", "JS8Call"),
    ("fast_light", "Fast Light"),
    ("varac", "VarAC"),
)

_DEFAULT_PORTS = {"js8call": 2442, "fast_light": 12345, "varac": 0}
_FAMILY_DETAILS = {
    "js8call": ("JS8Call instance", "API endpoint, profile, message storage, and launch details."),
    "fast_light": ("Fast Light instance", "FLRig/FLDigi endpoints, application paths, and message folders."),
    "varac": ("VarAC instance", "Runtime, configuration, inbox/outbox, and optional cluster ownership."),
}
_FAMILY_FIELDS = {
    "js8call": frozenset(
        {"instance_name", "ownership", "settings_identity_mode", "variant", "version", "rig_name", "host", "port", "udp_port", "offset_hz", "application_path", "configuration_path", "storage_path", "launch_command", "launch_at_startup", "notes"}
    ),
    "fast_light": frozenset(
        {"instance_name", "ownership", "settings_identity_mode", "host", "port", "secondary_port", "arq_port", "application_path", "secondary_application_path", "flmsg_application_path", "flamp_application_path", "configuration_path", "secondary_configuration_path", "storage_path", "secondary_storage_path", "launch_command", "launch_at_startup", "advanced_tx_requested", "advanced_tx_acknowledged", "notes"}
    ),
    "varac": frozenset(
        {"instance_name", "ownership", "application_path", "configuration_path", "storage_path", "secondary_storage_path", "outbox_path", "working_directory", "cluster_path", "cluster_id", "cluster_name", "cluster_shared_database", "cluster_instance_number", "existing_standalone_node_id", "existing_standalone_device_profile_id", "existing_standalone_member_number", "email_gateway_sender_choice", "cluster_gateway", "cluster_ptt_lock", "launch_command", "launch_at_startup", "notes"}
    ),
}
_FAMILY_FIELD_LABELS = {
    "js8call": {
        "settings_identity_mode": "How should this radio use JS8Call settings?",
        "variant": "JS8Call variant",
        "version": "Verified application version",
        "rig_name": "JS8Call rig name",
        "host": "JS8Call API host",
        "port": "JS8Call TCP API port",
        "udp_port": "JS8Call UDP port",
        "offset_hz": "Default audio offset (Hz)",
        "application_path": "JS8Call application",
        "configuration_path": "JS8Call profile/configuration folder",
        "storage_path": "JS8Call application-data folder",
        "launch_command": "Custom launch command (advanced)",
    },
    "fast_light": {
        "settings_identity_mode": "How should this radio use the Fast Light applications?",
        "host": "Local service host",
        "port": "FLRig XML-RPC port",
        "secondary_port": "FLDigi XML-RPC port",
        "arq_port": "FLDigi ARQ port",
        "application_path": "FLRig application",
        "secondary_application_path": "FLDigi application",
        "flmsg_application_path": "FLMsg application",
        "flamp_application_path": "FLAmp application",
        "configuration_path": "FLRig profile folder",
        "secondary_configuration_path": "FLDigi profile folder",
        "storage_path": "FLDigi log folder",
        "secondary_storage_path": "FLDigi check-in folder",
        "launch_command": "FLDigi custom launch command (advanced)",
    },
    "varac": {
        "application_path": "VarAC application / launcher",
        "configuration_path": "VarAC INI file",
        "storage_path": "VarAC database",
        "secondary_storage_path": "VarAC incoming folder",
        "outbox_path": "VarAC outbox folder",
        "working_directory": "VarAC working directory",
        "cluster_path": "Cluster setup",
        "cluster_id": "VarAC cluster",
        "cluster_name": "New cluster name",
        "cluster_shared_database": "Cluster shared database",
        "cluster_instance_number": "Cluster instance number",
        "launch_command": "Instance-specific launch command",
    },
}


@dataclass(frozen=True)
class InstanceConflict:
    """A deterministic review finding; it does not perform a check itself."""

    code: str
    severity: str
    title: str
    detail: str


@dataclass(frozen=True)
class VarACNativePresentation:
    """Immutable, UI-safe projection of a prepared native VarAC plan.

    The Settings host owns discovery, process checks, the native writer, and
    persistence.  This small projection deliberately contains only already
    prepared facts that the widget can render without performing I/O.  A host
    may replace it as a worker publishes a newer generation.
    """

    state: str = "not_prepared"
    why: str = "Choose an arrangement, then prepare VarAC before reviewing native details."
    arrangement: str = ""
    affected_radios: tuple[str, ...] = ()
    shared_database_summary: str = "Not proposed"
    member_numbers_summary: str = "Not proposed"
    ptt_lock_summary: str = "Not proposed"
    email_gateway_sender_summary: str = "No email gateway"
    writer_version: str = ""
    writer_platform: str = ""
    writer_operation: str = ""
    writer_qualified: bool = False
    application_path: str = ""
    varac_ini_path: str = ""
    storage_path: str = ""
    secondary_storage_path: str = ""
    outbox_path: str = ""
    working_directory: str = ""
    vara_runtime_path: str = ""
    vara_ini_path: str = ""
    launch_command: str = ""
    launch_argv: tuple[str, ...] = ()
    launch_environment: Mapping[str, str] = field(default_factory=dict)
    port: int = 0
    secondary_port: int = 0
    udp_port: int = 0
    ports_summary: str = ""
    fingerprints_summary: str = ""
    plan_fingerprint: str = ""
    draft_fingerprint: str = ""
    generation: int = 0

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | "VarACNativePresentation" | None) -> "VarACNativePresentation":
        if isinstance(value, cls):
            return value
        row = dict(value or {})
        radios = row.get("affected_radios") or row.get("radios") or ()
        if isinstance(radios, str):
            radios = (radios,)
        return cls(
            state=_text(row.get("state") or row.get("status") or "not_prepared").lower(),
            why=_text(row.get("why") or row.get("summary") or cls.why),
            arrangement=_text(row.get("arrangement") or row.get("cluster_path")),
            affected_radios=tuple(_text(item) for item in radios if _text(item)),
            shared_database_summary=_text(row.get("shared_database_summary") or row.get("shared_database") or "Not proposed"),
            member_numbers_summary=_text(row.get("member_numbers_summary") or row.get("member_numbers") or "Not proposed"),
            ptt_lock_summary=_text(row.get("ptt_lock_summary") or row.get("ptt_lock") or "Not proposed"),
            email_gateway_sender_summary=_text(row.get("email_gateway_sender_summary") or row.get("email_gateway_sender") or "No email gateway"),
            writer_version=_text(row.get("writer_version") or row.get("version")),
            writer_platform=_text(row.get("writer_platform") or row.get("platform")),
            writer_operation=_text(row.get("writer_operation") or row.get("operation")),
            writer_qualified=_bool(row.get("writer_qualified") or row.get("qualified")),
            application_path=_text(row.get("application_path") or row.get("varac_install_path")),
            varac_ini_path=_text(row.get("varac_ini_path") or row.get("configuration_path")),
            storage_path=_text(row.get("storage_path") or row.get("varac_db_path") or row.get("db_path")),
            secondary_storage_path=_text(
                row.get("secondary_storage_path") or row.get("varac_incoming_path") or row.get("incoming_path")
            ),
            outbox_path=_text(row.get("outbox_path") or row.get("varac_outbox_dir")),
            working_directory=_text(row.get("working_directory") or row.get("working_dir")),
            vara_runtime_path=_text(row.get("vara_runtime_path") or row.get("vara_runtime")),
            vara_ini_path=_text(row.get("vara_ini_path")),
            launch_command=_text(row.get("launch_command")),
            launch_argv=tuple(_text(item) for item in (row.get("launch_argv") or ()) if _text(item)),
            launch_environment={
                str(key): _text(value)
                for key, value in dict(row.get("launch_environment") or {}).items()
            },
            port=_int(row.get("port") or row.get("vara_command_port")) or 0,
            secondary_port=_int(row.get("secondary_port") or row.get("vara_kiss_port")) or 0,
            udp_port=_int(row.get("udp_port") or row.get("vara_monitor_port")) or 0,
            ports_summary=_text(row.get("ports_summary") or row.get("ports")),
            fingerprints_summary=_text(row.get("fingerprints_summary") or row.get("fingerprints")),
            plan_fingerprint=_text(row.get("plan_fingerprint") or row.get("fingerprints_summary")),
            draft_fingerprint=_text(row.get("draft_fingerprint")),
            generation=_int(row.get("generation")) or 0,
        )

    def payload(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "why": self.why,
            "arrangement": self.arrangement,
            "affected_radios": self.affected_radios,
            "shared_database_summary": self.shared_database_summary,
            "member_numbers_summary": self.member_numbers_summary,
            "ptt_lock_summary": self.ptt_lock_summary,
            "email_gateway_sender_summary": self.email_gateway_sender_summary,
            "writer_version": self.writer_version,
            "writer_platform": self.writer_platform,
            "writer_operation": self.writer_operation,
            "writer_qualified": self.writer_qualified,
            "application_path": self.application_path,
            "varac_ini_path": self.varac_ini_path,
            "configuration_path": self.varac_ini_path,
            "storage_path": self.storage_path,
            "secondary_storage_path": self.secondary_storage_path,
            "outbox_path": self.outbox_path,
            "working_directory": self.working_directory,
            "vara_runtime_path": self.vara_runtime_path,
            "vara_ini_path": self.vara_ini_path,
            "launch_command": self.launch_command,
            "launch_argv": self.launch_argv,
            "launch_environment": dict(self.launch_environment),
            "port": self.port,
            "secondary_port": self.secondary_port,
            "udp_port": self.udp_port,
            "ports_summary": self.ports_summary,
            "fingerprints_summary": self.fingerprints_summary,
            "plan_fingerprint": self.plan_fingerprint,
            "draft_fingerprint": self.draft_fingerprint,
            "generation": self.generation,
        }


@dataclass(frozen=True)
class SoftwareInstanceDraft:
    """Stable UI-to-host payload for one new or imported instance."""

    family_key: str = ""
    instance_name: str = ""
    radio_id: Optional[int] = None
    owner_draft_key: str = ""
    owner_label: str = ""
    draft_instance_key: str = ""
    application_system_key: str = ""
    inventory_generation: int = 0
    inventory_fingerprint: str = ""
    source_fingerprint: str = ""
    source_locked: bool = False
    launch_recipe: Mapping[str, Any] = field(default_factory=dict)
    launch_recipe_status: str = ""
    launch_recipe_fingerprint: str = ""
    radio_role: str = "tx_rx"
    mode: str = "managed"
    ownership: str = "fio-managed"
    settings_identity_mode: str = "isolated"
    variant: str = ""
    version: str = ""
    writer_platform: str = ""
    writer_operation: str = "create"
    host: str = "127.0.0.1"
    port: int = 0
    udp_port: int = 0
    offset_hz: int = 0
    secondary_port: int = 0
    arq_port: int = 0
    rig_name: str = ""
    application_path: str = ""
    secondary_application_path: str = ""
    flmsg_application_path: str = ""
    flamp_application_path: str = ""
    configuration_path: str = ""
    secondary_configuration_path: str = ""
    storage_path: str = ""
    secondary_storage_path: str = ""
    outbox_path: str = ""
    launch_command: str = ""
    launch_at_startup: bool = False
    working_directory: str = ""
    vara_runtime_path: str = ""
    vara_ini_path: str = ""
    advanced_tx_requested: bool = False
    advanced_tx_acknowledged: bool = False
    cluster_path: str = "standalone"
    cluster_id: str = ""
    cluster_name: str = ""
    cluster_shared_database: str = ""
    cluster_instance_number: int = 0
    existing_standalone_node_id: int = 0
    existing_standalone_device_profile_id: int = 0
    existing_standalone_member_number: int = 0
    # New native-cluster intent.  Do not derive this from legacy
    # ``cluster_gateway`` compatibility evidence.
    email_gateway_sender_choice: str = "none"
    email_gateway_sender_member_id: str = ""
    cluster_gateway: bool = False
    cluster_ptt_lock: bool = False
    notes: str = ""
    imported_id: Optional[int] = None
    imported_system_key: str = ""
    replace_existing: bool = False
    replacement_instance_id: Optional[int] = None

    def payload(self) -> dict[str, Any]:
        """Return a JSON-friendly copy with stable keys for persistence adapters."""
        # The first keys are the UI contract.  The explicit manifest aliases
        # let a Settings adapter hand this payload to the additive core model
        # without losing the operator-friendly names shown in Review.
        management_mode = {
            "fio-managed": "fio_managed",
            "operator-managed": "operator",
            "remote": "remote",
        }.get(self.ownership, "operator")
        provenance = "detected" if self.mode == "discover" else ("managed" if self.mode == "managed" else "manual")
        ports: list[dict[str, Any]] = []
        resources: list[dict[str, Any]] = []
        if self.family_key == "js8call":
            if self.port:
                ports.append({"name": "JS8Call API", "protocol": "tcp", "host": self.host, "port": self.port})
            if self.udp_port:
                ports.append({"name": "JS8Call UDP", "protocol": "udp", "host": self.host, "port": self.udp_port})
            if self.configuration_path:
                resources.append({"kind": "settings_profile", "value": self.configuration_path, "exclusive": True})
            if self.storage_path:
                resources.append({"kind": "message_storage", "value": self.storage_path, "exclusive": True})
            if self.rig_name:
                resources.append({"kind": "rig_name", "value": self.rig_name, "exclusive": True})
        elif self.family_key == "fast_light":
            if self.port:
                ports.append({"name": "FLRig XML-RPC", "protocol": "tcp", "host": self.host, "port": self.port})
            if self.secondary_port:
                ports.append({"name": "FLDigi XML-RPC", "protocol": "tcp", "host": self.host, "port": self.secondary_port})
            if self.arq_port:
                ports.append({"name": "FLDigi ARQ", "protocol": "tcp", "host": self.host, "port": self.arq_port})
            for kind, value in (
                ("flrig_configuration", self.configuration_path),
                ("fldigi_configuration", self.secondary_configuration_path),
                ("fldigi_logs", self.storage_path),
                ("fldigi_checkins", self.secondary_storage_path),
                ("flmsg_application", self.flmsg_application_path),
                ("flamp_application", self.flamp_application_path),
            ):
                if value:
                    resources.append(
                        {
                            "kind": kind,
                            "value": value,
                            "exclusive": kind not in {"flmsg_application", "flamp_application"},
                        }
                    )
        else:
            clustered = self.cluster_path in {"create_cluster", "join_cluster"}
            for kind, value in (
                ("varac_ini", self.configuration_path),
                ("varac_database", self.storage_path),
                ("varac_incoming", self.secondary_storage_path),
                ("varac_outbox", self.outbox_path),
                ("vara_runtime", self.vara_runtime_path),
                ("vara_ini", self.vara_ini_path),
            ):
                if value:
                    resources.append(
                        {
                            "kind": kind,
                            "value": value,
                            "exclusive": not (clustered and kind == "varac_database"),
                        }
                    )
            if self.application_path:
                resources.append(
                    {
                        "kind": "varac_executable",
                        "value": self.application_path,
                        "exclusive": False,
                    }
                )
            if self.cluster_id and self.cluster_instance_number:
                resources.append(
                    {
                        "kind": "varac_cluster_instance",
                        "value": f"{self.cluster_id}:{self.cluster_instance_number}",
                        "exclusive": True,
                    }
                )
        payload = {
            "family_key": self.family_key,
            "instance_name": self.instance_name,
            "radio_id": self.radio_id,
            "mode": self.mode,
            "ownership": self.ownership,
            "management_mode": management_mode,
            "provenance": provenance,
            "settings_identity_mode": self.settings_identity_mode,
            "variant": self.variant,
            "version": self.version,
            "writer_platform": self.writer_platform,
            "writer_operation": self.writer_operation,
            "owner_draft_key": self.owner_draft_key,
            "owner_label": self.owner_label,
            "draft_instance_key": self.draft_instance_key,
            "inventory_generation": int(self.inventory_generation or 0),
            "inventory_fingerprint": self.inventory_fingerprint,
            "source_fingerprint": self.source_fingerprint,
            "source_locked": self.source_locked,
            "launch_recipe": dict(self.launch_recipe),
            "launch_recipe_status": self.launch_recipe_status,
            "launch_recipe_fingerprint": self.launch_recipe_fingerprint,
            "radio_role": self.radio_role,
            "instance_key": (
                f"{self.family_key}:{self.imported_system_key}"
                if self.imported_system_key
                else (
                    f"{self.family_key}:{self.application_system_key}"
                    if self.application_system_key
                    else (
                        f"{self.family_key}:{self.imported_id}"
                        if self.imported_id
                        else self.draft_instance_key
                    )
                )
            ),
            "application_system_key": self.imported_system_key or self.application_system_key,
            "host": self.host,
            "port": int(self.port or 0),
            "udp_port": int(self.udp_port or 0),
            "offset_hz": int(self.offset_hz or 0),
            "secondary_port": int(self.secondary_port or 0),
            "arq_port": int(self.arq_port or 0),
            "rig_name": self.rig_name,
            "ports": ports,
            "application_path": self.application_path,
            "secondary_application_path": self.secondary_application_path,
            "flmsg_application_path": self.flmsg_application_path,
            "flamp_application_path": self.flamp_application_path,
            "executable_path": self.application_path,
            "configuration_path": self.configuration_path,
            "configuration_root": self.configuration_path,
            "secondary_configuration_path": self.secondary_configuration_path,
            "storage_path": self.storage_path,
            "data_root": self.storage_path,
            "secondary_storage_path": self.secondary_storage_path,
            "outbox_path": self.outbox_path,
            "launch_command": self.launch_command,
            "launch_at_startup": self.launch_at_startup,
            "working_directory": self.working_directory,
            "vara_runtime_path": self.vara_runtime_path,
            "vara_ini_path": self.vara_ini_path,
            "advanced_tx_requested": self.advanced_tx_requested,
            "advanced_tx_acknowledged": self.advanced_tx_acknowledged,
            "execution_scope": "receive_only" if self.radio_role == "observer" else "standard",
            "cluster_path": self.cluster_path,
            "cluster_id": self.cluster_id,
            "cluster_name": self.cluster_name,
            "cluster_shared_database": self.cluster_shared_database,
            "cluster_instance_number": int(self.cluster_instance_number or 0),
            "existing_standalone_node_id": int(self.existing_standalone_node_id or 0),
            "existing_standalone_device_profile_id": int(
                self.existing_standalone_device_profile_id or 0
            ),
            "existing_standalone_member_number": int(
                self.existing_standalone_member_number or 0
            ),
            "email_gateway_sender_choice": self.email_gateway_sender_choice,
            "email_gateway_sender_member_id": self.email_gateway_sender_member_id,
            "cluster_gateway": self.cluster_gateway,
            "cluster_ptt_lock": self.cluster_ptt_lock,
            "resource_claims": resources,
            "notes": self.notes,
            "imported_id": self.imported_id,
            "imported_system_key": self.imported_system_key,
            "replace_existing": bool(self.replace_existing),
            "replacement_instance_id": self.replacement_instance_id,
        }
        if self.family_key == "js8call":
            payload.update(
                {
                    "js8_rig_name": self.rig_name,
                    "js8_variant_family": self.variant,
                    "js8_variant_version": self.version,
                    "js8_tcp_port": int(self.port or 0),
                    "js8_udp_port": int(self.udp_port or 0),
                    "js8_offset_hz": int(self.offset_hz or 0),
                    "js8_application_path": self.application_path,
                    "js8_profile_path": self.configuration_path,
                    "js8_data_path": self.storage_path,
                }
            )
        elif self.family_key == "fast_light":
            payload.update(
                {
                    "flrig_port": int(self.port or 0),
                    "fldigi_port": int(self.secondary_port or 0),
                    "fldigi_arq_port": int(self.arq_port or 0),
                    "flrig_path": self.application_path,
                    "fldigi_path": self.secondary_application_path,
                    "flmsg_path": self.flmsg_application_path,
                    "flamp_path": self.flamp_application_path,
                    "flrig_config_path": self.configuration_path,
                    "fldigi_config_path": self.secondary_configuration_path,
                    "fldigi_log_path": self.storage_path,
                    "fldigi_checkin_dir": self.secondary_storage_path,
                }
            )
        else:
            payload.update(
                {
                    "varac_install_path": self.application_path,
                    "varac_ini_path": self.configuration_path,
                    "varac_db_path": self.storage_path,
                    "varac_incoming_path": self.secondary_storage_path,
                    "varac_outbox_dir": self.outbox_path,
                    "varac_cluster_id": self.cluster_id,
                    "varac_cluster_instance_number": int(self.cluster_instance_number or 0),
                    "varac_cluster_path": self.cluster_path,
                    "varac_cluster_name": self.cluster_name,
                    "varac_cluster_shared_database": self.cluster_shared_database,
                    "varac_existing_standalone_node_id": int(self.existing_standalone_node_id or 0),
                    "varac_existing_standalone_device_profile_id": int(
                        self.existing_standalone_device_profile_id or 0
                    ),
                    "varac_existing_standalone_member_number": int(
                        self.existing_standalone_member_number or 0
                    ),
                    "varac_email_gateway_sender_choice": self.email_gateway_sender_choice,
                    "varac_email_gateway_sender_member_id": self.email_gateway_sender_member_id,
                }
            )
        return payload


def _text(value: Any) -> str:
    return str(value or "").strip()


def _int(value: Any) -> Optional[int]:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() not in {"", "0", "false", "no", "off"}
    return bool(value)


def normalize_instance_draft(value: Mapping[str, Any] | SoftwareInstanceDraft) -> SoftwareInstanceDraft:
    """Normalize host/discovery data without probing or changing it."""

    if isinstance(value, SoftwareInstanceDraft):
        return value
    row = dict(value or {})
    family = _text(row.get("family_key") or row.get("family")).lower()
    provenance = _text(row.get("provenance")).lower()
    mode = _text(row.get("mode") or ("discover" if provenance == "detected" else "managed")).lower()
    ownership = _text(
        row.get("ownership") or ("operator-managed" if mode == "discover" else "fio-managed")
    ).lower()
    family_storage = (
        row.get("varac_db_path") or row.get("db_path")
        if family == "varac"
        else row.get("js8_data_path") or row.get("fldigi_log_path") or row.get("data_path") or row.get("incoming_path")
    )
    family_secondary_storage = (
        row.get("varac_incoming_path") or row.get("incoming_path")
        if family == "varac"
        else row.get("fldigi_checkin_dir")
    )
    source_fingerprint = _text(row.get("source_fingerprint"))
    imported_id = _int(row.get("imported_id") or row.get("id"))
    source_locked = _bool(row.get("source_locked", bool(imported_id and mode == "discover")))
    if source_locked and not source_fingerprint and family in dict(SUPPORTED_INSTANCE_FAMILIES):
        source_fingerprint = source_identity_fingerprint(family, row)
    return SoftwareInstanceDraft(
        family_key=family,
        instance_name=_text(row.get("instance_name") or row.get("name")),
        radio_id=_int(row.get("radio_id")),
        owner_draft_key=_text(row.get("owner_draft_key")),
        owner_label=_text(row.get("owner_label")),
        draft_instance_key=_text(row.get("draft_instance_key") or row.get("instance_key")),
        application_system_key=_text(row.get("application_system_key")),
        inventory_generation=_int(row.get("inventory_generation")) or 0,
        inventory_fingerprint=_text(row.get("inventory_fingerprint")),
        source_fingerprint=source_fingerprint,
        source_locked=source_locked,
        launch_recipe=(
            dict(row.get("launch_recipe") or {})
            if isinstance(row.get("launch_recipe"), Mapping)
            else {}
        ),
        launch_recipe_status=_text(row.get("launch_recipe_status")),
        launch_recipe_fingerprint=_text(row.get("launch_recipe_fingerprint")),
        radio_role=_text(row.get("radio_role") or row.get("device_class") or "tx_rx").lower(),
        mode=mode,
        ownership=ownership,
        settings_identity_mode=settings_identity_mode_from_mapping(row, family_key=family),
        variant=_text(row.get("variant") or row.get("variant_family") or row.get("js8_variant_family")),
        version=_text(row.get("version") or row.get("variant_version") or row.get("js8_variant_version")),
        writer_platform=_text(row.get("writer_platform") or row.get("js8_writer_platform")),
        writer_operation=_text(row.get("writer_operation") or row.get("js8_writer_operation") or "create").lower(),
        host=_text(row.get("host") or "127.0.0.1"),
        port=_int(row.get("port") or row.get("js8_tcp_port") or row.get("flrig_port")) or 0,
        udp_port=_int(row.get("udp_port") or row.get("js8_udp_port")) or 0,
        offset_hz=_int(row.get("offset_hz") or row.get("js8_offset_hz")) or 0,
        secondary_port=_int(row.get("secondary_port") or row.get("fldigi_port")) or 0,
        arq_port=_int(row.get("arq_port") or row.get("fldigi_arq_port")) or 0,
        rig_name=_text(row.get("rig_name") or row.get("js8_rig_name")),
        application_path=_text(row.get("application_path") or row.get("install_path") or row.get("path") or row.get("js8_application_path") or row.get("flrig_path") or row.get("varac_install_path")),
        secondary_application_path=_text(row.get("secondary_application_path") or row.get("fldigi_path")),
        flmsg_application_path=_text(row.get("flmsg_application_path") or row.get("flmsg_path")),
        flamp_application_path=_text(row.get("flamp_application_path") or row.get("flamp_path")),
        configuration_path=_text(row.get("configuration_path") or row.get("ini_path") or row.get("profile_path") or row.get("js8_profile_path") or row.get("flrig_config_path") or row.get("varac_ini_path")),
        secondary_configuration_path=_text(row.get("secondary_configuration_path") or row.get("fldigi_config_path")),
        storage_path=_text(row.get("storage_path") or family_storage),
        secondary_storage_path=_text(row.get("secondary_storage_path") or family_secondary_storage),
        outbox_path=_text(row.get("outbox_path") or row.get("outbox_dir") or row.get("varac_outbox_dir")),
        launch_command=_text(row.get("launch_command") or row.get("launch_cmd")),
        launch_at_startup=_bool(row.get("launch_at_startup", False)),
        working_directory=_text(row.get("working_directory") or row.get("working_dir")),
        vara_runtime_path=_text(row.get("vara_runtime_path")),
        vara_ini_path=_text(row.get("vara_ini_path")),
        advanced_tx_requested=_bool(row.get("advanced_tx_requested", False)),
        advanced_tx_acknowledged=_bool(row.get("advanced_tx_acknowledged", False)),
        cluster_path=_text(row.get("cluster_path") or row.get("varac_cluster_path") or "standalone").lower(),
        cluster_id=_text(row.get("cluster_id") or row.get("varac_cluster_id")),
        cluster_name=_text(row.get("cluster_name") or row.get("varac_cluster_name")),
        cluster_shared_database=_text(
            row.get("cluster_shared_database") or row.get("varac_cluster_shared_database")
        ),
        cluster_instance_number=_int(row.get("cluster_instance_number") or row.get("varac_cluster_instance_number")) or 0,
        existing_standalone_node_id=_int(
            row.get("existing_standalone_node_id") or row.get("varac_existing_standalone_node_id")
        ) or 0,
        existing_standalone_device_profile_id=_int(
            row.get("existing_standalone_device_profile_id")
            or row.get("varac_existing_standalone_device_profile_id")
        ) or 0,
        existing_standalone_member_number=_int(
            row.get("existing_standalone_member_number")
            or row.get("varac_existing_standalone_member_number")
        ) or 0,
        email_gateway_sender_choice=_text(
            row.get("email_gateway_sender_choice")
            or row.get("varac_email_gateway_sender_choice")
            or "none"
        ).lower(),
        email_gateway_sender_member_id=_text(
            row.get("email_gateway_sender_member_id")
            or row.get("varac_email_gateway_sender_member_id")
        ),
        cluster_gateway=_bool(row.get("cluster_gateway", False)),
        cluster_ptt_lock=_bool(row.get("cluster_ptt_lock", False)),
        notes=_text(row.get("notes")),
        imported_id=imported_id,
        imported_system_key=_text(row.get("imported_system_key") or row.get("system_key")),
        replace_existing=_bool(row.get("replace_existing", False)),
        replacement_instance_id=_int(row.get("replacement_instance_id")),
    )


def _endpoint(row: Mapping[str, Any]) -> tuple[str, int]:
    host = _text(row.get("host") or row.get("flrig_host") or row.get("fldigi_host"))
    port = _int(row.get("port") or row.get("flrig_port") or row.get("fldigi_port")) or 0
    return host, port


_USABLE_EXISTING_CANDIDATE_CLASSES = frozenset(
    {"usable", "usable_existing", "complete"}
)
_DIAGNOSTIC_ONLY_CANDIDATE_CLASSES = frozenset(
    {
        "diagnostic_only",
        "incomplete",
        "orphaned",
        "orphaned_incomplete",
        "provenance_unknown",
        "recovery_only",
        "unlinked",
        "unusable",
        "unknown",
    }
)


def _candidate_classification(row: Mapping[str, Any]) -> str:
    return _text(
        row.get("candidate_classification")
        or row.get("classification")
        or row.get("candidate_status")
    ).casefold().replace("-", "_").replace(" ", "_")


def _existing_candidate_is_recovery(row: Mapping[str, Any]) -> bool:
    return _bool(row.get("recovery_only")) or _candidate_classification(row) == "recovery_only"


def _existing_candidate_is_usable(row: Mapping[str, Any]) -> bool:
    """Use core classification only; unknown inventory evidence fails closed.

    The UI must not rediscover or infer usability from enabled flags, paths, or
    endpoints.  The inventory classifier owns that decision and publishes a
    usable flag/classification with its immutable snapshot.  Older rows with no
    such evidence remain visible only as diagnostics until reviewed recovery.
    """

    if _bool(row.get("diagnostic_only")):
        return False
    # A complete, source-evidenced but currently unassigned bundle is offered
    # only inside this explicit Find/import recovery picker.  It is not a
    # recommendation and cannot launch until final reviewed assignment.
    if _existing_candidate_is_recovery(row):
        return True
    for key in ("usable_existing", "candidate_usable", "is_usable_candidate"):
        if key in row:
            return _bool(row.get(key))
    classification = _candidate_classification(row)
    if classification in _DIAGNOSTIC_ONLY_CANDIDATE_CLASSES:
        return False
    return classification in _USABLE_EXISTING_CANDIDATE_CLASSES


def _diagnostic_candidate_reason(row: Mapping[str, Any]) -> str:
    """Return core-provided recovery evidence without reconstructing a bundle."""

    for key in (
        "candidate_reasons",
        "classification_reasons",
        "diagnostic_reasons",
        "completeness_reasons",
        "candidate_reason",
        "classification_reason",
        "diagnostic_reason",
    ):
        raw = row.get(key)
        if isinstance(raw, Mapping):
            parts = [_text(value) for value in raw.values()]
        elif isinstance(raw, (list, tuple, set, frozenset)):
            parts = [_text(value) for value in raw]
        else:
            parts = [_text(raw)]
        visible = [part for part in parts if part]
        if visible:
            return "; ".join(visible)
    classification = _text(
        row.get("candidate_classification")
        or row.get("classification")
        or row.get("candidate_status")
    ).replace("_", " ")
    return (
        f"Core classification: {classification}."
        if classification
        else "Core classification is unavailable for this record."
    )


def instance_conflicts(
    draft: Mapping[str, Any] | SoftwareInstanceDraft,
    existing_instances: Iterable[Mapping[str, Any]] = (),
) -> tuple[InstanceConflict, ...]:
    """Return name/endpoint/path conflicts against an already-loaded inventory."""

    current = normalize_instance_draft(draft)
    conflicts: list[InstanceConflict] = []
    if not current.family_key:
        conflicts.append(InstanceConflict("family_required", "error", "Choose software", "Select JS8Call, Fast Light, or VarAC."))
    if not current.instance_name:
        conflicts.append(InstanceConflict("name_required", "error", "Name this instance", "Use a short name that distinguishes this instance from the others."))
    if current.radio_id is None and not current.owner_draft_key:
        conflicts.append(InstanceConflict("radio_required", "error", "Choose a radio", "Every software instance must be assigned to a radio before it can be added."))
    if current.mode == "remote" and not current.host:
        conflicts.append(InstanceConflict("host_required", "error", "Remote host required", "Enter the host name or address for the remote application."))
    for port_name, port in (
        ("TCP", current.port),
        ("UDP", current.udp_port),
        ("secondary", current.secondary_port),
        ("FLDigi ARQ", current.arq_port),
    ):
        if port and not 1 <= port <= 65535:
            conflicts.append(InstanceConflict("port_invalid", "error", f"{port_name} port is invalid", "Use a port from 1 through 65535."))
    if current.family_key == "js8call" and current.offset_hz and not 1800 <= current.offset_hz <= 2700:
        conflicts.append(
            InstanceConflict(
                "js8_offset_invalid",
                "error",
                "JS8Call default offset is outside the supported range",
                "Use an audio offset from 1800 through 2700 Hz.",
            )
        )
    if (
        current.family_key == "js8call"
        and current.mode == "managed"
        and current.settings_identity_mode == "isolated"
        and not current.rig_name
    ):
        severity = "error" if current.launch_at_startup else "warning"
        conflicts.append(InstanceConflict("rig_name_required", severity, "JS8 rig name is not set", "Each independently launched JS8Call instance needs a unique --rig-name."))
    if current.mode == "managed" and not current.application_path:
        conflicts.append(InstanceConflict("application_missing", "warning", "Application path is not set", "The instance can be saved as a draft, but it cannot be launched until its application is configured."))
    if current.launch_at_startup and not (current.application_path or current.launch_command):
        conflicts.append(InstanceConflict("launch_target_required", "error", "Launch target is not set", "Choose an application or an explicit launch command before enabling startup."))
    if current.family_key == "fast_light":
        observer_mode = current.radio_role == "observer"
        if observer_mode and current.application_path:
            conflicts.append(
                InstanceConflict(
                    "observer_fast_light_flrig",
                    "error",
                    "FLRig is unavailable for a receive-only radio",
                    "Use FLDigi and approved receive/file helpers only; CAT, PTT, and transmit controls remain disabled.",
                )
            )
        if observer_mode and current.advanced_tx_requested:
            conflicts.append(
                InstanceConflict(
                    "observer_fast_light_tx",
                    "error",
                    "Advanced TX is unavailable",
                    "An observer / SDR can never receive Fast Light transmit authority.",
                )
            )
        if current.advanced_tx_requested and not current.advanced_tx_acknowledged:
            conflicts.append(
                InstanceConflict(
                    "fast_light_tx_acknowledgement",
                    "error",
                    "Advanced TX acknowledgement is required",
                    "Acknowledge the operating-model, RF Guard, and final-preflight requirements or leave Fast Light receive-safe.",
                )
            )
        fast_light_ports = tuple(
            port for port in (current.port, current.secondary_port, current.arq_port) if port
        )
        if len(set(fast_light_ports)) != len(fast_light_ports):
            conflicts.append(
                InstanceConflict(
                    "fast_light_endpoint_overlap",
                    "error",
                    "Fast Light endpoints overlap",
                    "Assign different local TCP ports to FLRig, FLDigi XML-RPC, and FLDigi ARQ.",
                )
            )
        missing_launch_profile = (
            not current.secondary_configuration_path
            if observer_mode
            else not current.configuration_path or not current.secondary_configuration_path
        )
        if (
            current.launch_at_startup
            and current.settings_identity_mode == "isolated"
            and missing_launch_profile
        ):
            conflicts.append(
                InstanceConflict(
                    "fast_light_profile_required",
                    "error",
                    "Fast Light profile folders are required",
                    (
                        "Startup needs a distinct FLDigi configuration folder so the receive-only instance cannot reuse the default profile."
                        if observer_mode
                        else "Startup needs distinct FLRig and FLDigi configuration folders so a second instance cannot reuse the default profile."
                    ),
                )
            )
    if current.family_key == "varac":
        if current.radio_role == "observer":
            conflicts.append(
                InstanceConflict(
                    "observer_varac_forbidden",
                    "error",
                    "VarAC is unavailable for this radio",
                    "Observer / SDR profiles cannot use standalone or Cluster VarAC.",
                )
            )
        if current.cluster_path not in {"standalone", "create_cluster", "join_cluster"}:
            conflicts.append(InstanceConflict("cluster_path_invalid", "error", "Choose a VarAC cluster path", "Use standalone, create a cluster, or join an existing cluster."))
        if current.cluster_path == "join_cluster" and not current.cluster_id:
            conflicts.append(InstanceConflict("cluster_required", "error", "Choose an existing cluster", "Select the cluster this VarAC node should join."))
        if current.cluster_path == "create_cluster" and not (current.cluster_name or current.cluster_id):
            conflicts.append(InstanceConflict("cluster_name_required", "error", "Name the new cluster", "Enter a distinct cluster name or ID."))
        if current.cluster_path == "create_cluster":
            if current.email_gateway_sender_choice not in {
                "none",
                "existing_member",
                "new_member",
            }:
                conflicts.append(
                    InstanceConflict(
                        "email_gateway_sender_required",
                        "error",
                        "Choose the email gateway sender",
                        "Choose No email gateway, the existing member, or the new member.",
                    )
                )
            elif (
                current.email_gateway_sender_choice == "existing_member"
                and not current.existing_standalone_node_id
            ):
                conflicts.append(
                    InstanceConflict(
                        "email_gateway_existing_member_unavailable",
                        "error",
                        "Existing email gateway sender is unavailable",
                        "Choose the new member or No email gateway for this new cluster.",
                    )
                )
        if current.cluster_path != "standalone" and not current.cluster_instance_number:
            conflicts.append(InstanceConflict("cluster_instance_required", "error", "Cluster instance number is required", "Choose a positive instance number for this VarAC node."))
        if (
            current.cluster_path != "standalone"
            and current.mode != "managed"
            and not current.launch_command
        ):
            conflicts.append(InstanceConflict("cluster_launch_required", "error", "Cluster launch command is required", "Use an instance-specific VarAC launch command so CommsPlan cannot start the default node by mistake."))
        if current.cluster_path == "standalone" and (current.cluster_id or current.cluster_instance_number):
            conflicts.append(InstanceConflict("standalone_cluster_fields", "error", "Standalone VarAC has cluster fields", "Clear cluster identity and instance number or choose a cluster setup path."))
        if current.launch_at_startup and not current.working_directory:
            conflicts.append(InstanceConflict("varac_working_directory", "error", "Working directory is required", "VarAC launch identity includes its exact working directory."))

    wanted_name = current.instance_name.casefold()
    wanted_endpoint = (current.host.casefold(), current.port) if current.host and current.port else None
    wanted_path = current.application_path.casefold()
    for raw in existing_instances:
        row = raw if isinstance(raw, Mapping) else {}
        existing_draft_key = _text(row.get("draft_instance_key") or row.get("instance_key"))
        if current.draft_instance_key and existing_draft_key == current.draft_instance_key:
            continue
        existing_id = _int(row.get("id"))
        if current.imported_id and existing_id == current.imported_id:
            continue
        existing_name = _text(row.get("name") or row.get("instance_name"))
        existing_family = _text(row.get("family_key") or row.get("family")).casefold()
        if not existing_family:
            existing_family = current.family_key
        if (
            current.family_key in {"js8call", "fast_light"}
            and existing_family == current.family_key
            and current.settings_identity_mode == "default"
            and settings_identity_mode_from_mapping(row, family_key=current.family_key) == "default"
        ):
            family_label = "JS8Call" if current.family_key == "js8call" else "Fast Light"
            conflicts.append(
                InstanceConflict(
                    "duplicate_default_settings_identity",
                    "error",
                    f"{family_label} default settings are already assigned",
                    f"{existing_name or 'Another configured instance'} already uses the existing default {family_label} settings. Choose isolated settings for this radio or edit the current owner.",
                )
            )
        if wanted_name and existing_name.casefold() == wanted_name:
            conflicts.append(InstanceConflict("duplicate_name", "error", "Name already in use", f"{existing_name} already identifies another {current.family_key or 'software'} instance."))
        host, port = _endpoint(row)
        if wanted_endpoint and (host.casefold(), port) == wanted_endpoint:
            conflicts.append(InstanceConflict("duplicate_endpoint", "error", "Endpoint already in use", f"{host}:{port} is already assigned to {existing_name or 'another instance'}."))
        if current.family_key == "js8call" and current.udp_port:
            existing_udp = _int(row.get("udp_port") or row.get("js8_udp_port")) or 0
            if (host.casefold(), existing_udp) == (current.host.casefold(), current.udp_port):
                conflicts.append(InstanceConflict("duplicate_udp_endpoint", "error", "UDP endpoint already in use", f"{current.host}:{current.udp_port} is already assigned to {existing_name or 'another instance'}."))
        if current.family_key == "fast_light" and current.secondary_port:
            existing_fldigi = _int(row.get("fldigi_port") or row.get("secondary_port")) or 0
            existing_fldigi_host = _text(row.get("fldigi_host") or row.get("host"))
            if (existing_fldigi_host.casefold(), existing_fldigi) == (current.host.casefold(), current.secondary_port):
                conflicts.append(InstanceConflict("duplicate_secondary_endpoint", "error", "FLDigi endpoint already in use", f"{current.host}:{current.secondary_port} is already assigned to {existing_name or 'another instance'}."))
        existing_path = _text(row.get("application_path") or row.get("install_path") or row.get("path"))
        if wanted_path and existing_path and existing_path.casefold() == wanted_path:
            conflicts.append(InstanceConflict("duplicate_path", "warning", "Application path is shared", "Sharing an executable is expected. Confirm that this radio's selected settings identity and endpoints are correct."))
        current_paths = {
            path.casefold()
            for path in (
                current.configuration_path,
                current.secondary_configuration_path,
                current.storage_path,
                current.secondary_storage_path,
                current.outbox_path,
            )
            if path
        }
        existing_paths = {
            _text(row.get(key)).casefold()
            for key in (
                "configuration_path", "ini_path", "profile_path", "db_path", "storage_path",
                "incoming_path", "outbox_path", "outbox_dir", "fldigi_log_path", "fldigi_checkin_dir",
            )
            if _text(row.get(key))
        }
        # A first-cluster conversion intentionally points the new member at
        # the existing standalone database, and a join intentionally points
        # at the selected cluster database.  That one cluster-owned resource
        # is shared by design; every member-local INI, VARA runtime, inbox,
        # outbox, and endpoint remains exclusive.  Do not weaken the generic
        # collision rule for private storage or for a different cluster.
        if current.family_key == "varac" and current.cluster_path in {"create_cluster", "join_cluster"}:
            shared_path = (current.cluster_shared_database or current.storage_path).casefold()
            existing_shared_path = _text(
                row.get("cluster_shared_database")
                or row.get("varac_cluster_shared_database")
                or row.get("db_path")
                or row.get("storage_path")
            ).casefold()
            same_cluster = bool(
                shared_path
                and shared_path == existing_shared_path
                and (
                    (
                        current.cluster_id
                        and _text(row.get("cluster_id") or row.get("varac_cluster_id")).casefold()
                        == current.cluster_id.casefold()
                    )
                    or (
                        current.existing_standalone_node_id
                        and _int(row.get("varac_node_id") or row.get("node_id"))
                        == current.existing_standalone_node_id
                    )
                )
            )
            if same_cluster:
                current_paths.discard(shared_path)
                existing_paths.discard(shared_path)
        if current_paths & existing_paths:
            conflicts.append(InstanceConflict("duplicate_storage", "error", "Configuration or storage is shared", "Each independently launched instance needs its own native profile, database, inbox, outbox, or log path."))
        if current.family_key == "varac" and current.cluster_id and current.cluster_instance_number:
            existing_cluster = f"{_text(row.get('cluster_id'))}:{_int(row.get('cluster_instance_number')) or 0}"
            if existing_cluster == f"{current.cluster_id}:{current.cluster_instance_number}":
                conflicts.append(InstanceConflict("duplicate_cluster_instance", "error", "VarAC cluster instance is already used", "Choose a distinct cluster instance number before saving."))
    return tuple(conflicts)


class SoftwareInstanceAssistant(QWidget):
    """Seven-step instance flow: purpose, source, identity, files, and review."""

    completed = Signal(object)
    cancelled = Signal()
    remove_family_requested = Signal(str)
    discover_requested = Signal(str)
    create_radio_requested = Signal()
    validation_requested = Signal(object)
    # Host-owned worker seams.  The assistant emits the immutable prepared
    # presentation plus the current draft and never starts native I/O itself.
    varac_native_prepare_requested = Signal(object)
    varac_native_apply_requested = Signal(object)
    STEP_TITLES = ("Purpose", "Find or create", "Identity", "Connections", "Files", "Launch", "Review")

    def __init__(
        self,
        family_key: str = "",
        *,
        radios: Iterable[Mapping[str, Any]] = (),
        existing_instances: Iterable[Mapping[str, Any]] = (),
        inventory_snapshot: GuidedInstanceInventorySnapshot | None = None,
        varac_clusters: Iterable[Mapping[str, Any]] = (),
        selected_radio_id: Optional[int] = None,
        unsaved_owner_key: str = "",
        unsaved_radio_label: str = "",
        radio_role: str = "tx_rx",
        initial_draft: Mapping[str, Any] | SoftwareInstanceDraft | None = None,
        editing_existing: bool = False,
        replacement_requested: bool = False,
        launch_recipe_resolution: GuidedLaunchRecipeResolution | Mapping[str, Any] | None = None,
        varac_native_presentation: VarACNativePresentation | Mapping[str, Any] | None = None,
        managed_root: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        requested_family = _text(family_key).lower()
        self._family_locked = bool(requested_family)
        self._family_key = requested_family
        self._existing_instances = tuple(dict(row) for row in existing_instances if isinstance(row, Mapping))
        inventory_family = requested_family or "js8call"
        self._inventory_snapshot = (
            inventory_snapshot
            if isinstance(inventory_snapshot, GuidedInstanceInventorySnapshot)
            else build_guided_instance_inventory(
                {inventory_family: self._existing_instances},
                generation=0,
            )
        )
        self._radios = tuple(dict(row) for row in radios if isinstance(row, Mapping))
        self._varac_clusters = tuple(dict(row) for row in varac_clusters if isinstance(row, Mapping))
        self._selected_radio_id = _int(selected_radio_id)
        self._editing_existing = bool(editing_existing)
        # The named Replace action is already explicit operator intent, scoped
        # to the original radio/assignment. It cannot authorize another target.
        self._replacement_requested = bool(replacement_requested or editing_existing)
        self._replacement_radio_id = self._selected_radio_id
        self._replacement_family_key = requested_family
        self._replacement_target_instance_id: int | None = None
        self._unsaved_owner_key = _text(unsaved_owner_key)
        self._assistant_draft_owner_key = self._unsaved_owner_key or f"assistant-{uuid.uuid4().hex}"
        self._unsaved_radio_label = _text(unsaved_radio_label) or "Unsaved radio draft"
        self._radio_role = _text(radio_role).lower() or "tx_rx"
        self._radio_assignments: dict[int, Mapping[str, Any]] = {}
        self._replacement_instance: Optional[Mapping[str, Any]] = None
        self._replacement_confirmed = False
        self._imported_id: Optional[int] = None
        self._imported_system_key = ""
        self._draft_instance_key = ""
        self._application_system_key = ""
        self._inventory_fingerprint = self._inventory_snapshot.fingerprint
        self._source_fingerprint = ""
        self._source_locked = False
        self._varac_arrangement_metadata: dict[str, int] = {}
        self._legacy_cluster_gateway = False
        self._selected_source_payload: dict[str, Any] = {}
        self._loading_draft = False
        self._discovery_selected = False
        self._writer_platform = ""
        self._writer_operation = "create"
        self._vara_runtime_path = ""
        self._vara_ini_path = ""
        self._managed_root = _text(managed_root)
        self._launch_recipe_resolution: GuidedLaunchRecipeResolution | None = None
        self._launch_recipe_resolution_supplied = launch_recipe_resolution is not None
        self._varac_native_presentation = VarACNativePresentation.from_mapping(
            varac_native_presentation
        )
        self._resolving_launch_recipe = False
        self._discovery_results: tuple[Mapping[str, Any], ...] = ()
        self._recovery_discovery_results: tuple[Mapping[str, Any], ...] = ()
        self._diagnostic_discovery_results: tuple[Mapping[str, Any], ...] = ()
        self._step = 0
        self._field_widgets: dict[str, QWidget] = {}
        self._field_labels: dict[str, QWidget] = {}
        # Disclosure and viewport state belong to the family task, not to a
        # transient refresh.  The host may publish a newer prepared plan while
        # this editor remains open; rebuilding the visible decision surface or
        # jumping the operator back to the top would be disruptive.
        self._details_expanded_by_family: dict[str, bool] = {}
        self._body_scroll_positions: dict[int, int] = {}
        self._view_restore_token = 0
        self._build_ui()
        if self._selected_radio_id is not None:
            selected_index = self.radio_combo.findData(self._selected_radio_id)
            if selected_index >= 0:
                self.radio_combo.setCurrentIndex(selected_index)
        self._load_family(self._family_key)
        if initial_draft is not None:
            seeded = normalize_instance_draft(
                {
                    **(
                        initial_draft.payload()
                        if isinstance(initial_draft, SoftwareInstanceDraft)
                        else dict(initial_draft)
                    ),
                    "family_key": self._family_key,
                    "owner_draft_key": self._unsaved_owner_key,
                    "owner_label": self._unsaved_radio_label,
                    "radio_role": self._radio_role,
                }
            )
            self._set_draft(seeded)
        if self._editing_existing:
            self.title_label.setText("Edit software configuration")
            self.guidance_label.setText(
                "Edit this saved instance, review conflicts and affected launch details, then save it as one transaction."
            )
        if self._editing_existing and self._replacement_instance is not None:
            self.replacement_checkbox.setChecked(True)
        if launch_recipe_resolution is not None:
            self.set_launch_recipe_resolution(launch_recipe_resolution)
        # A workspace-launched operation is intentionally scoped to exactly
        # one family; changing family would mix the supplied inventory and
        # radio link columns.  A standalone assistant (no family argument)
        # may still choose its family.
        self.family_combo.setEnabled(not self._family_locked)
        if self._family_locked:
            self.family_combo.setToolTip(
                "Family is fixed for this operation; start another Add Instance flow to choose a different family."
            )
        self._refresh()

    def _build_ui(self) -> None:
        self.setAccessibleName("Add software instance assistant")
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(7)
        self.title_label = QLabel("Add a software instance")
        self.title_label.setStyleSheet(label_style("text", active_app_theme(), weight=700))
        root.addWidget(self.title_label)
        self.guidance_label = QLabel("Set up one distinct application instance. Nothing is saved until you review and confirm.")
        self.guidance_label.setWordWrap(True)
        root.addWidget(self.guidance_label)
        self.operation_status_label = QLabel()
        self.operation_status_label.setWordWrap(True)
        self.operation_status_label.setAccessibleName("Software instance operation status")
        self.status_label = self.operation_status_label
        root.addWidget(self.operation_status_label)
        self.replacement_banner = QLabel()
        self.replacement_banner.setWordWrap(True)
        self.replacement_banner.setObjectName("softwareInstanceReplacementBanner")
        self.replacement_banner.setAccessibleName("Software instance replacement status")
        root.addWidget(self.replacement_banner)
        self.step_label = QLabel()
        self.step_label.setAccessibleName("Software instance setup step")
        root.addWidget(self.step_label)

        self.step_buttons: list[QPushButton] = []
        step_grid = QGridLayout()
        step_grid.setContentsMargins(0, 0, 0, 0)
        step_grid.setHorizontalSpacing(6)
        step_grid.setVerticalSpacing(6)
        for index, title in enumerate(self.STEP_TITLES):
            button = QPushButton(f"{index + 1}. {title}")
            button.setObjectName(f"softwareInstanceStep_{index + 1}")
            button.setAccessibleName(f"Software instance step {index + 1}: {title}")
            button.setToolTip(f"Show {title.lower()} setup")
            button.setCheckable(True)
            button.setMinimumHeight(button_height_for_font(button))
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            button.clicked.connect(
                lambda _checked=False, target=index: self._select_step(target)
            )
            self.step_buttons.append(button)
            step_grid.addWidget(button, index // 4, index % 4)
        for column in range(4):
            step_grid.setColumnStretch(column, 1)
        root.addLayout(step_grid)

        # Header content above and the action row below intentionally remain
        # outside this scroll area.  Ordinary setup pages must share this one
        # vertical owner rather than each creating a nested form scroll area.
        self.body_scroll = QScrollArea()
        self.body_scroll.setObjectName("softwareInstanceAssistantBodyScroll")
        self.body_scroll.setAccessibleName("Software instance setup body")
        self.body_scroll.setWidgetResizable(True)
        self.body_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.body_scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.body_content = QWidget()
        self.body_content.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        body_layout = QVBoxLayout(self.body_content)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(7)

        self.prepared_summary_group = QGroupBox("Prepared setup")
        self.prepared_summary_group.setObjectName("softwareInstancePreparedSummary")
        self.prepared_summary_group.setAccessibleName("Prepared setup summary")
        prepared_layout = QVBoxLayout(self.prepared_summary_group)
        prepared_layout.setContentsMargins(8, 8, 8, 8)
        prepared_layout.setSpacing(4)
        self.prepared_facts_label = QLabel()
        self.prepared_facts_label.setObjectName("softwareInstancePreparedFacts")
        self.prepared_facts_label.setAccessibleName("Prepared setup facts")
        self.prepared_facts_label.setWordWrap(True)
        prepared_layout.addWidget(self.prepared_facts_label)
        self.prepared_why_label = QLabel()
        self.prepared_why_label.setObjectName("softwareInstancePreparedWhy")
        self.prepared_why_label.setAccessibleName("Why this setup is proposed")
        self.prepared_why_label.setWordWrap(True)
        prepared_layout.addWidget(self.prepared_why_label)
        self.prepared_impact_label = QLabel()
        self.prepared_impact_label.setObjectName("softwareInstancePreparedImpact")
        self.prepared_impact_label.setAccessibleName("Existing configuration impact")
        self.prepared_impact_label.setWordWrap(True)
        prepared_layout.addWidget(self.prepared_impact_label)
        self.prepared_safety_label = QLabel()
        self.prepared_safety_label.setObjectName("softwareInstancePreparedSafety")
        self.prepared_safety_label.setAccessibleName("Safety and confirmation status")
        self.prepared_safety_label.setWordWrap(True)
        prepared_layout.addWidget(self.prepared_safety_label)
        self.varac_native_group = QGroupBox("Native VarAC cluster")
        self.varac_native_group.setObjectName("softwareInstanceVaracNativePresentation")
        self.varac_native_group.setAccessibleName("Native VarAC cluster preparation")
        native_layout = QVBoxLayout(self.varac_native_group)
        native_layout.setContentsMargins(8, 8, 8, 8)
        native_layout.setSpacing(3)
        self.varac_native_status_label = QLabel()
        self.varac_native_status_label.setObjectName("softwareInstanceVaracNativeStatus")
        self.varac_native_status_label.setAccessibleName("Native VarAC preparation status")
        self.varac_native_status_label.setWordWrap(True)
        native_layout.addWidget(self.varac_native_status_label)
        self.varac_native_summary_label = QLabel()
        self.varac_native_summary_label.setObjectName("softwareInstanceVaracNativeSummary")
        self.varac_native_summary_label.setAccessibleName("Native VarAC cluster summary")
        self.varac_native_summary_label.setWordWrap(True)
        native_layout.addWidget(self.varac_native_summary_label)
        self.varac_native_why_label = QLabel()
        self.varac_native_why_label.setObjectName("softwareInstanceVaracNativeWhy")
        self.varac_native_why_label.setAccessibleName("Why native VarAC preparation is available")
        self.varac_native_why_label.setWordWrap(True)
        native_layout.addWidget(self.varac_native_why_label)
        self.varac_native_prepare_button = QPushButton("Prepare VarAC")
        self.varac_native_prepare_button.setObjectName("softwareInstancePrepareVarac")
        self.varac_native_prepare_button.setAccessibleName("Prepare native VarAC cluster configuration")
        self.varac_native_prepare_button.setToolTip(
            "Ask the Settings host to prepare a reviewed native VarAC plan in its worker."
        )
        self.varac_native_prepare_button.clicked.connect(self._request_varac_native_prepare)
        native_layout.addWidget(self.varac_native_prepare_button, 0, Qt.AlignLeft)
        prepared_layout.addWidget(self.varac_native_group)
        self.prepared_details_button = QPushButton("Show details")
        self.prepared_details_button.setObjectName("softwareInstanceShowDetails")
        self.prepared_details_button.setCheckable(True)
        self.prepared_details_button.toggled.connect(self._set_prepared_details_expanded)
        prepared_layout.addWidget(self.prepared_details_button, 0, Qt.AlignLeft)
        self.prepared_details_group = QGroupBox()
        self.prepared_details_group.setObjectName("softwareInstancePreparedDetails")
        self.prepared_details_group.setAccessibleName("Prepared technical details")
        details_layout = QVBoxLayout(self.prepared_details_group)
        details_layout.setContentsMargins(8, 8, 8, 8)
        self.prepared_details_label = QLabel()
        self.prepared_details_label.setObjectName("softwareInstancePreparedDetailsText")
        self.prepared_details_label.setWordWrap(True)
        self.prepared_details_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        details_layout.addWidget(self.prepared_details_label)
        prepared_layout.addWidget(self.prepared_details_group)
        body_layout.addWidget(self.prepared_summary_group)

        self.pages = CurrentPageStack()
        self.pages.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self.pages.setAccessibleName("Software instance setup pages")
        body_layout.addWidget(self.pages)
        self.body_scroll.setWidget(self.body_content)
        root.addWidget(self.body_scroll, 1)
        self._build_choose_page()
        self._build_source_page()
        self._build_identity_page()
        self._build_connections_page()
        self._build_files_page()
        self._build_launch_page()
        self._build_review_page()

        actions = QHBoxLayout()
        self.action_footer = QWidget()
        self.action_footer.setObjectName("softwareInstanceActionFooter")
        self.action_footer.setAccessibleName("Software instance setup actions")
        self.action_footer.setLayout(actions)
        self.cancel_button = QPushButton("Back without changes")
        self.cancel_button.setAccessibleName("Back without changes")
        self.cancel_button.setToolTip(
            "Discard edits made in this editor and keep this software family selected for the radio."
        )
        self.cancel_button.clicked.connect(self.cancelled.emit)
        self.remove_family_button = QPushButton()
        self.remove_family_button.setObjectName("softwareInstanceRemoveFamily")
        family_title = dict(SUPPORTED_INSTANCE_FAMILIES).get(self._family_key, "software")
        self.remove_family_button.setText(f"Remove {family_title} from this radio")
        self.remove_family_button.setAccessibleName(
            f"Remove {family_title} from this radio"
        )
        self.remove_family_button.setToolTip(
            "Remove this family and discard its prepared draft, reservations, and native plan."
        )
        self.remove_family_button.setVisible(bool(self._unsaved_owner_key and self._family_key))
        self.remove_family_button.clicked.connect(
            lambda: self.remove_family_requested.emit(self._family_key)
        )
        self.back_button = QPushButton("Back")
        self.back_button.setAccessibleName("Back one software setup step")
        self.back_button.clicked.connect(self._back)
        self.next_button = QPushButton("Next")
        self.next_button.setAccessibleName("Continue software setup")
        self.next_button.setDefault(True)
        self.next_button.clicked.connect(self._next)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.remove_family_button)
        actions.addStretch(1)
        actions.addWidget(self.back_button)
        actions.addWidget(self.next_button)
        root.addWidget(self.action_footer)

    def _radio_context_label(self) -> str:
        """Return the stable radio wording that belongs in prepared facts."""

        if self._unsaved_owner_key:
            return self._unsaved_radio_label
        label = self.radio_combo.currentText().strip()
        return label.split(" — ", 1)[0] if label else "No radio selected"

    def _details_expanded(self) -> bool:
        return bool(self._details_expanded_by_family.get(self._family_key, False))

    def _set_prepared_details_expanded(self, expanded: bool) -> None:
        """Toggle the family-scoped technical evidence without moving the task."""

        self._body_scroll_positions[self._step] = self.body_scroll.verticalScrollBar().value()
        self._details_expanded_by_family[self._family_key] = bool(expanded)
        self._update_prepared_presentation()
        self._restore_body_view_state(
            self._body_scroll_positions.get(
                self._step,
                self.body_scroll.verticalScrollBar().value(),
            ),
            QApplication.focusWidget(),
        )

    def _prepared_endpoint_summary(self, draft: SoftwareInstanceDraft) -> str:
        if draft.family_key == "js8call":
            endpoint = f"API {draft.host}:{draft.port}" if draft.port else "API not allocated"
            return endpoint + (f" · UDP {draft.udp_port}" if draft.udp_port else "")
        if draft.family_key == "fast_light":
            parts = []
            if draft.radio_role != "observer":
                parts.append(f"FLRig {draft.host}:{draft.port}" if draft.port else "FLRig not allocated")
            parts.append(
                f"FLDigi {draft.host}:{draft.secondary_port}"
                if draft.secondary_port
                else "FLDigi not allocated"
            )
            return " · ".join(parts)
        arrangement = draft.cluster_path.replace("_", " ").title()
        return f"VarAC {arrangement}"

    def _prepared_why(self, draft: SoftwareInstanceDraft) -> str:
        resolution = self._launch_recipe_resolution
        if resolution is not None and resolution.summary:
            return resolution.summary
        if draft.source_locked:
            return (
                "Imported settings remain source-locked. CommsPlan will not alter their "
                "identity, endpoints, profiles, data paths, or launch details."
            )
        if draft.family_key == "varac":
            return (
                "The selected arrangement remains a reviewed intent; CommsPlan does not "
                "create or join a cluster until the radio transaction is confirmed."
            )
        if draft.mode == "managed":
            if draft.settings_identity_mode == "default":
                return (
                    "CommsPlan will use the application's existing default settings and will not add a native profile selector."
                )
            return "CommsPlan will allocate a distinct local identity and retain existing configuration as review evidence."
        return "This source remains operator-managed until a reviewed, supported action is explicitly confirmed."

    def _prepared_existing_impact(self, draft: SoftwareInstanceDraft) -> str:
        if self._replacement_instance is not None:
            name = _text(
                self._replacement_instance.get("name")
                or self._replacement_instance.get("instance_name")
            ) or "existing instance"
            if self._replacement_confirmed:
                return (
                    f"Existing impact: {name} will remain recorded; its radio assignment "
                    "will be replaced only when the final transaction succeeds."
                )
            return (
                f"Existing impact: {name} is assigned to this radio. Confirm replacement "
                "before applying any new instance."
            )
        if self._unsaved_owner_key:
            return (
                "Existing impact: This is an unsaved inactive Add Radio draft. "
                "No existing application configuration changes until Save Radio and Software."
            )
        if draft.source_locked:
            return (
                "Existing impact: Imported configuration is evidence only and remains "
                "operator-owned unless a separately supported apply is reviewed."
            )
        return "Existing impact: No existing software assignment is changed by this draft."

    def varac_native_presentation(self) -> VarACNativePresentation:
        """Return the current cached native-plan presentation for a host worker."""

        return self._varac_native_presentation

    def set_varac_native_presentation(
        self,
        presentation: VarACNativePresentation | Mapping[str, Any] | None,
    ) -> None:
        """Publish an already-prepared native-plan result without doing I/O.

        Settings should call this only from its generation-fenced worker result
        callback.  Publication intentionally preserves the current draft,
        disclosure state, focus, and scroll position.
        """

        native = VarACNativePresentation.from_mapping(presentation)
        # A qualified native result is the prepared bundle, not merely a
        # technical summary.  Hydrate the same draft that Files, Review, and
        # the completed payload read before they can render it.  Keeping this
        # here (rather than in a page-specific presenter) also makes a late
        # worker publication harmless: it updates one current family draft.
        if self._family_key == "varac" and native.state.replace("_", " ").strip().lower() == "ready":
            current = self.draft().payload()
            prepared_values = {
                "application_path": native.application_path,
                "configuration_path": native.varac_ini_path,
                "storage_path": native.storage_path,
                "cluster_shared_database": native.storage_path,
                "secondary_storage_path": native.secondary_storage_path,
                "outbox_path": native.outbox_path,
                "working_directory": native.working_directory,
                "launch_command": native.launch_command,
                "port": native.port,
                "secondary_port": native.secondary_port,
                "udp_port": native.udp_port,
            }
            current.update(
                {
                    key: value
                    for key, value in prepared_values.items()
                    if value not in {"", 0, None}
                }
            )
            current["vara_runtime_path"] = native.vara_runtime_path
            current["vara_ini_path"] = native.vara_ini_path
            self._set_draft(normalize_instance_draft(current))
            # The worker fingerprint names the pre-hydration intent.  The
            # presentation must name the now-authoritative hydrated bundle so
            # validation and host apply do not mistake generated facts for an
            # operator edit.
            native = replace(
                native,
                draft_fingerprint=native_draft_fingerprint(self.draft().payload()),
            )
        self._varac_native_presentation = native
        self._refresh_email_gateway_sender_choices()
        self._update_prepared_presentation()
        self._refresh_review_if_needed()

    def varac_native_worker_payload(self) -> dict[str, Any]:
        """Return the cache-only payload for host-owned Prepare or final Apply."""

        draft_payload = self.draft().payload()
        native_payload = self._varac_native_presentation.payload()
        draft_payload["varac_native_presentation"] = native_payload
        draft_payload["varac_native_generation"] = int(native_payload["generation"] or 0)
        # The outer Add Radio transaction uses these cache-only facts to look
        # up the prepared plan and begin native apply at its final save
        # boundary.  They are deliberately not an apply result or a durable
        # store migration.
        draft_payload["varac_native_plan_fingerprint"] = str(
            native_payload.get("plan_fingerprint")
            or native_payload.get("fingerprints_summary")
            or ""
        )
        return {
            "draft": draft_payload,
            "native_presentation": native_payload,
        }

    def _native_varac_apply_required(self, draft: SoftwareInstanceDraft) -> bool:
        return (
            draft.family_key == "varac"
            and draft.mode == "managed"
            and draft.cluster_path in {"create_cluster", "join_cluster"}
        )

    def request_varac_native_apply(self) -> None:
        """Let the final-review host request native apply in its own worker."""

        if self._family_key == "varac":
            self.varac_native_apply_requested.emit(self.varac_native_worker_payload())

    def _request_varac_native_prepare(self) -> None:
        if self._family_key == "varac":
            self.varac_native_prepare_requested.emit(self.varac_native_worker_payload())

    def _update_varac_native_presentation(self, draft: SoftwareInstanceDraft) -> None:
        """Render a concise native cluster card from cached host facts only."""

        group = self.varac_native_group
        visible = self._native_varac_apply_required(draft)
        group.setVisible(visible)
        if not visible:
            return
        native = self._varac_native_presentation
        state = native.state.replace("_", " ").strip().lower()
        if (
            native.draft_fingerprint
            and native.draft_fingerprint != native_draft_fingerprint(draft.payload())
        ):
            state = "needs attention"
        # A version may be detected without being safe to write.  Do not turn
        # that evidence into a native-managed Ready claim.
        if state == "ready" and native.writer_version and not native.writer_qualified:
            state = "manual setup required"
        state_copy = {
            "not prepared": "Prepare VarAC",
            "preparing": "Preparing VarAC…",
            "stop required": "Stop VarAC to continue",
            "stop varac required": "Stop VarAC to continue",
            "stop varac": "Stop VarAC to continue",
            "needs attention": "Needs attention",
            "ready": "Ready",
            "manual setup required": "Manual setup required",
            "recovery required": "Recovery required",
        }.get(state, native.state.replace("_", " ").title() or "Prepare VarAC")
        arrangement = native.arrangement or draft.cluster_path.replace("_", " ")
        radios = ", ".join(native.affected_radios) or self._radio_context_label()
        writer = ""
        if native.writer_version:
            qualification = "qualified" if native.writer_qualified else "not qualified"
            writer = f"VarAC {native.writer_version} writer {qualification}"
            if native.writer_platform:
                writer += f" · {native.writer_platform}"
        elif draft.mode == "managed":
            writer = "Native writer status pending preparation"
        else:
            writer = "Operator-managed native configuration"
        shared_database = native.shared_database_summary
        if draft.cluster_path == "standalone" and shared_database == "Not proposed":
            shared_database = "Not used for standalone"
        email_sender = native.email_gateway_sender_summary
        if draft.cluster_path == "create_cluster":
            email_sender = {
                "none": "No email gateway",
                "existing_member": "Existing member",
                "new_member": "New member",
            }.get(draft.email_gateway_sender_choice, "Choose a sender")
        self.varac_native_status_label.setText(f"{state_copy} — {writer}")
        self.varac_native_summary_label.setText(
            " · ".join(
                (
                    f"Arrangement: {arrangement.title() or 'Not selected'}",
                    f"Radios: {radios}",
                    f"Shared VarAC database: {shared_database}",
                    f"Members: {native.member_numbers_summary}",
                    f"PTT lock: {native.ptt_lock_summary}",
                    f"Email gateway sender: {email_sender}",
                )
            )
        )
        self.varac_native_why_label.setText(f"Why: {native.why}")
        button_text = "Prepare VarAC"
        enabled = state in {
            "not prepared",
            "needs attention",
            "stop required",
            "stop varac required",
            "stop varac",
        }
        if state == "preparing":
            button_text = "Preparing VarAC…"
        elif state in {"stop required", "stop varac required", "stop varac"}:
            button_text = "Retry after closing VarAC"
        elif state == "ready":
            button_text = "Ready for Review & Save"
        elif state == "manual setup required":
            button_text = "Manual setup required"
        elif state == "recovery required":
            button_text = "Recovery required"
        self.varac_native_prepare_button.setText(button_text)
        self.varac_native_prepare_button.setEnabled(enabled)
        self.varac_native_prepare_button.setVisible(state not in {"manual setup required", "recovery required"})

    def _varac_native_technical_lines(self) -> list[str]:
        native = self._varac_native_presentation
        if not native.varac_ini_path and not native.vara_runtime_path and not native.vara_ini_path:
            return []
        lines = ["Native VarAC / VARA details"]
        if native.varac_ini_path:
            lines.append(f"VarAC INI: {native.varac_ini_path}")
        if native.vara_runtime_path:
            lines.append(f"VARA runtime: {native.vara_runtime_path}")
        if native.vara_ini_path:
            lines.append(f"VARA INI: {native.vara_ini_path}")
        if native.launch_command:
            lines.append(f"Native launch command: {native.launch_command}")
        if native.launch_argv:
            lines.append(f"Native launch argv: {list(native.launch_argv)}")
        if native.launch_environment:
            lines.append(f"Launch environment: {dict(native.launch_environment)}")
        if native.ports_summary:
            lines.append(f"VARA ports: {native.ports_summary}")
        if native.fingerprints_summary:
            lines.append(f"Fingerprints: {native.fingerprints_summary}")
        return lines

    def _refresh_email_gateway_sender_choices(self) -> None:
        """Populate explicit new-cluster sender intent from already-cached facts."""

        combo = getattr(self, "email_gateway_sender_combo", None)
        if not isinstance(combo, QComboBox):
            return
        prior_choice = str(combo.currentData() or "none")
        combo.blockSignals(True)
        try:
            combo.clear()
            combo.addItem("No email gateway", "none")
            existing_id = int(
                self._varac_arrangement_metadata.get("existing_standalone_node_id", 0)
                or 0
            )
            native_radios = self._varac_native_presentation.affected_radios
            if existing_id:
                existing_label = native_radios[0] if native_radios else "existing member"
                combo.addItem(f"Existing member — {existing_label}", "existing_member")
            new_label = self._radio_context_label()
            combo.addItem(f"New member — {new_label}", "new_member")
            index = combo.findData(prior_choice)
            combo.setCurrentIndex(index if index >= 0 else 0)
        finally:
            combo.blockSignals(False)

    def _prepared_technical_lines(self, draft: SoftwareInstanceDraft) -> list[str]:
        """Read-only evidence deliberately kept out of the normal decision path."""

        lines = [
            f"Family: {dict(SUPPORTED_INSTANCE_FAMILIES).get(draft.family_key, draft.family_key)}",
            f"Radio: {self._radio_context_label()}",
            f"Inventory: generation {draft.inventory_generation} · {draft.inventory_fingerprint or 'No fingerprint'}",
        ]
        if draft.source_fingerprint:
            lines.append(f"Source fingerprint: {draft.source_fingerprint}")
        paths = (
            ("Application", draft.application_path),
            ("Secondary application", draft.secondary_application_path),
            ("Configuration", draft.configuration_path),
            ("Secondary configuration", draft.secondary_configuration_path),
            ("Data", draft.storage_path),
            ("Secondary data", draft.secondary_storage_path),
            ("Outbox", draft.outbox_path),
            ("Working directory", draft.working_directory),
        )
        for label, value in paths:
            if value:
                lines.append(f"{label}: {value}")
        if draft.launch_command:
            lines.append(f"Advanced launch override: {draft.launch_command}")
        resolution = self._launch_recipe_resolution
        if resolution is not None:
            lines.extend(
                (
                    f"Recipe status: {resolution.status.replace('_', ' ').title()}",
                    f"Recipe fingerprint: {resolution.fingerprint or 'No fingerprint'}",
                    *self._launch_component_lines(resolution),
                )
            )
            if resolution.recovery_action:
                lines.append(f"Recovery: {resolution.recovery_action}")
        if draft.family_key == "varac":
            lines.extend(self._varac_native_technical_lines())
        return lines

    def _update_prepared_presentation(self) -> None:
        """Keep the compact decision facts visible while details stay optional."""

        # The managed source radio button is initialized while the form is
        # still being built.  Do not try to materialize a draft until the
        # shared ownership field (and therefore the full seven-step surface)
        # exists.
        if (
            not hasattr(self, "prepared_summary_group")
            or "ownership" not in self._field_widgets
        ):
            return
        draft = self.draft()
        family = dict(SUPPORTED_INSTANCE_FAMILIES).get(draft.family_key, draft.family_key)
        mode = {
            "managed": "New CommsPlan-guided instance",
            "discover": "Imported configuration",
            "remote": "Manual or remote configuration",
        }.get(draft.mode, draft.mode.replace("-", " ").title())
        launch = "Launch with CommsPlan" if draft.launch_at_startup else "Operator starts application"
        readiness = "Ready"
        if self._launch_recipe_resolution is not None:
            readiness = self._launch_recipe_resolution.status.replace("_", " ").title()
        # Keep ordinary status publication constant-time.  Full conflict
        # validation still runs at its existing navigation/final-action gates;
        # the persistent card calls out only immediately known blocking intent.
        if (
            (draft.mode == "discover" and not self._discovery_selected)
            or (self._replacement_instance is not None and not self._replacement_confirmed)
            or (not self._unsaved_owner_key and not self._selected_radio_id)
        ):
            readiness = "Needs attention"
        self.prepared_facts_label.setText(
            " · ".join(
                (
                    family,
                    mode,
                    self._radio_context_label(),
                    self._prepared_endpoint_summary(draft),
                    launch,
                    readiness,
                )
            )
        )
        self.prepared_why_label.setText(f"Why: {self._prepared_why(draft)}")
        self.prepared_impact_label.setText(self._prepared_existing_impact(draft))
        self.prepared_safety_label.setText(
            "Safety: CommsPlan does not change an existing application, assignment, or "
            "third-party configuration until you review and explicitly confirm the final action."
        )
        self._update_varac_native_presentation(draft)
        radio = self._radio_context_label()
        expanded = self._details_expanded()
        blocked = self.prepared_details_button.blockSignals(True)
        self.prepared_details_button.setChecked(expanded)
        self.prepared_details_button.blockSignals(blocked)
        self.prepared_details_button.setText("Hide details" if expanded else "Show details")
        self.prepared_details_button.setAccessibleName(
            f"{'Hide' if expanded else 'Show'} {family} details for {radio}"
        )
        self.prepared_details_button.setAccessibleDescription(
            "Show or hide read-only paths, commands, dependencies, fingerprints, and diagnostics."
        )
        self.prepared_details_button.setToolTip(
            "Read-only technical evidence for this family and radio."
        )
        self.prepared_details_group.setTitle(f"{family} technical details")
        self.prepared_details_group.setAccessibleName(f"{family} technical details for {radio}")
        self.prepared_details_label.setText("\n".join(self._prepared_technical_lines(draft)))
        self.prepared_details_group.setVisible(expanded)

    def _remember_body_scroll_position(self) -> None:
        if hasattr(self, "body_scroll"):
            self._body_scroll_positions[self._step] = (
                self.body_scroll.verticalScrollBar().value()
            )

    def _restore_body_view_state(
        self,
        scroll_value: int,
        focus_widget: QWidget | None,
    ) -> None:
        """Restore the active page's position after its layout settles once."""

        if not hasattr(self, "body_scroll"):
            return
        self._view_restore_token += 1
        token = self._view_restore_token

        def restore() -> None:
            if token != self._view_restore_token or not hasattr(self, "body_scroll"):
                return
            bar = self.body_scroll.verticalScrollBar()
            bar.setValue(max(bar.minimum(), min(int(scroll_value), bar.maximum())))
            if (
                focus_widget is not None
                and self.isAncestorOf(focus_widget)
                and focus_widget.isVisible()
                and focus_widget.isEnabled()
            ):
                focus_widget.setFocus(Qt.OtherFocusReason)

        QTimer.singleShot(0, restore)

    def _build_choose_page(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.family_scope_label = QLabel("Choose the application family for this instance.")
        self.family_scope_label.setWordWrap(True)
        self.family_scope_label.setAccessibleName("Software family scope guidance")
        layout.addWidget(self.family_scope_label)
        self.family_combo = QComboBox()
        self.family_combo.setAccessibleName("Software family")
        for key, label in SUPPORTED_INSTANCE_FAMILIES:
            self.family_combo.addItem(label, key)
        self.family_combo.currentIndexChanged.connect(lambda _index: self._load_family(str(self.family_combo.currentData() or "")))
        layout.addWidget(self.family_combo)
        radio_group = QGroupBox("Radio context (required)")
        radio_layout = QVBoxLayout(radio_group)
        self.radio_guidance_label = QLabel()
        self.radio_guidance_label.setWordWrap(True)
        self.radio_guidance_label.setAccessibleName("Radio selection guidance")
        radio_layout.addWidget(self.radio_guidance_label)
        self.radio_combo = QComboBox()
        self.radio_combo.setAccessibleName("Radio for software instance")
        self.radio_combo.currentIndexChanged.connect(self._on_radio_changed)
        radio_layout.addWidget(self.radio_combo)
        self.create_radio_button = QPushButton("Create a radio first…")
        self.create_radio_button.setAccessibleName("Create a radio before adding a software instance")
        self.create_radio_button.setToolTip(
            "Open Radio Profiles to create a radio, then return to this guided setup"
        )
        self.create_radio_button.clicked.connect(self.create_radio_requested.emit)
        radio_layout.addWidget(self.create_radio_button)
        self.replacement_checkbox = QCheckBox("Replace the existing instance assigned to this radio")
        self.replacement_checkbox.setAccessibleName("Confirm replacement of existing software instance")
        self.replacement_checkbox.setToolTip(
            "Replacement updates the radio-to-instance mapping; the existing application record is retained."
        )
        self.replacement_checkbox.toggled.connect(self._set_replacement_confirmed)
        radio_layout.addWidget(self.replacement_checkbox)
        layout.addWidget(radio_group)
        layout.addStretch(1)
        self.pages.addWidget(page)

    def _build_source_page(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("Choose how this instance will be used. You can import a detected configuration or enter it manually."))
        self.source_buttons: dict[str, QRadioButton] = {}
        for key, text in (
            ("managed", "Set up a new local instance (CommsPlan-guided)"),
            ("discover", "Find or import an existing installation"),
            ("remote", "Connect to a manual or remote instance"),
        ):
            button = QRadioButton(text)
            button.setAccessibleName(text)
            button.toggled.connect(self._refresh_source)
            self.source_buttons[key] = button
            layout.addWidget(button)
        self.discovery_hint = QLabel("Discovery is explicit. The host may provide results without this page performing a scan.")
        self.discovery_hint.setWordWrap(True)
        layout.addWidget(self.discovery_hint)
        self.discover_button = QPushButton("Find existing configurations")
        self.discover_button.clicked.connect(lambda: self.discover_requested.emit(self._family_key))
        layout.addWidget(self.discover_button)
        self.discovery_list = QListWidget()
        self.discovery_list.setAccessibleName("Discovered software configurations")
        self.discovery_list.itemSelectionChanged.connect(self._import_selected_discovery)
        layout.addWidget(self.discovery_list, 1)
        self.discovery_diagnostics_label = QLabel()
        self.discovery_diagnostics_label.setObjectName("softwareInstanceDiscoveryDiagnostics")
        self.discovery_diagnostics_label.setAccessibleName("Diagnostic-only incomplete software records")
        self.discovery_diagnostics_label.setWordWrap(True)
        self.discovery_diagnostics_label.setVisible(False)
        layout.addWidget(self.discovery_diagnostics_label)
        self.source_lock_banner = QLabel()
        self.source_lock_banner.setObjectName("softwareInstanceSourceLockBanner")
        self.source_lock_banner.setAccessibleName("Imported software identity status")
        self.source_lock_banner.setWordWrap(True)
        layout.addWidget(self.source_lock_banner)
        self.clone_distinct_button = QPushButton("Clone as a distinct instance")
        self.clone_distinct_button.setObjectName("softwareInstanceCloneDistinct")
        self.clone_distinct_button.setAccessibleName("Clone imported software as a distinct instance")
        self.clone_distinct_button.setToolTip(
            "Keep only shared executable and version evidence, then allocate a new identity, ports, profiles, and data paths."
        )
        self.clone_distinct_button.clicked.connect(self._clone_as_distinct)
        layout.addWidget(self.clone_distinct_button)
        self.source_buttons["managed"].setChecked(True)
        self._set_source_locked(False)
        self.pages.addWidget(page)

    def _new_form_page(self, heading: str, fields: tuple[tuple[str, str, str], ...]) -> QFormLayout:
        page = QWidget()
        outer = QVBoxLayout(page)
        hint = QLabel(heading)
        hint.setWordWrap(True)
        outer.addWidget(hint)
        form_widget = QWidget()
        form_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        form = QFormLayout(form_widget)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self._active_form = form
        for key, label, placeholder in fields:
            self._add_line(key, label, placeholder)
        outer.addWidget(form_widget)
        outer.addStretch(1)
        self.pages.addWidget(page)
        return form

    def _build_identity_page(self) -> None:
        self._new_form_page(
            "Identity: give this instance a distinct name and confirm who manages it.",
            (
                ("instance_name", "Instance name", "A distinct name for this application instance"),
                ("settings_identity_mode", "Settings use", "Choose existing default or separate settings"),
                ("variant", "JS8Call variant", "Choose the exact installed JS8Call family"),
                ("version", "Verified application version", "Exact version, for example 2.2.0"),
            ),
        )
        ownership = QComboBox()
        ownership.addItem(
            "CommsPlan-managed identity and launch (native settings only when exactly qualified)",
            "fio-managed",
        )
        ownership.addItem("Operator-managed (observe/import only)", "operator-managed")
        ownership.addItem("Remote (observe endpoint only)", "remote")
        ownership.setAccessibleName("Software instance ownership")
        self._field_widgets["ownership"] = ownership
        # The helper's most recently created form is the identity form.
        ownership_label = QLabel("Ownership")
        self._field_labels["ownership"] = ownership_label
        self._active_form.addRow(ownership_label, ownership)
        self.settings_identity_help_label = QLabel()
        self.settings_identity_help_label.setObjectName("softwareInstanceSettingsIdentityHelp")
        self.settings_identity_help_label.setAccessibleName("Settings identity explanation and preview")
        self.settings_identity_help_label.setWordWrap(True)
        self.settings_identity_help_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._active_form.insertRow(2, "What this means", self.settings_identity_help_label)

    def _build_connections_page(self) -> None:
        self._new_form_page(
            "Connections: endpoints identify the instance at runtime. Use unique local ports for independently launched instances. This draft screen makes no external change; a qualified native writer runs only after outer Save Radio and Software.",
            (
                ("rig_name", "JS8 rig name", "Unique --rig-name (JS8Call only)"),
                ("host", "FLRig/JS8 host", "127.0.0.1"),
                ("port", "FLRig or JS8 TCP port", "TCP port"),
                ("udp_port", "JS8 UDP port", "UDP port"),
                ("offset_hz", "Default audio offset", "1800 through 2700 Hz"),
                ("secondary_port", "FLDigi XML-RPC port", "TCP port"),
                ("arq_port", "FLDigi ARQ port", "TCP port"),
            ),
        )

    def _build_files_page(self) -> None:
        self._new_form_page(
            "Files: choose application executables and operator-owned paths. For a qualified CommsPlan-managed JS8Call or Fast Light recipe, CommsPlan derives profile and data roots from the stable instance identity and shows them in Launch and Review; unsupported recipes expose the Advanced path fields for recovery.",
            (
                ("application_path", "Application / FLRig path", "Path to the application or launcher"),
                ("secondary_application_path", "FLDigi path", "Fast Light FLDigi application (optional)"),
                ("flmsg_application_path", "FLMsg path", "Shared installation; radio-scoped NBEMS launch identity"),
                ("flamp_application_path", "FLAmp path", "Shared installation; radio-scoped NBEMS and endpoint identity"),
                ("configuration_path", "Configuration / profile / INI", "Profile, configuration, or VarAC INI"),
                ("secondary_configuration_path", "FLDigi configuration", "FLDigi profile/configuration"),
                ("storage_path", "Data / database / log folder", "Instance-owned data, database, or FLDigi logs"),
                ("secondary_storage_path", "Incoming / check-in folder", "VarAC incoming or FLDigi check-in folder"),
                ("outbox_path", "VarAC outbox", "VarAC outbox folder"),
                ("working_directory", "VarAC working directory", "Exact working directory for this VarAC node"),
                ("cluster_path", "Cluster setup", "Standalone, create, or join"),
                ("cluster_id", "VarAC cluster ID", "Optional cluster identifier"),
                ("cluster_name", "New cluster name", "Name for a new VarAC cluster"),
                ("cluster_shared_database", "Cluster shared database", "Optional cluster-owned shared database"),
                ("cluster_instance_number", "VarAC cluster instance", "Optional positive instance number"),
            ),
        )
        self.varac_cluster_why_label = QLabel(
            "Standalone is the safe default even when another VarAC node or cluster is detected. "
            "Choose Create cluster or Join cluster only when coordinated cluster routing and shared BBS behavior are intentional."
        )
        self.varac_cluster_why_label.setObjectName("softwareInstanceVaracClusterWhy")
        self.varac_cluster_why_label.setAccessibleName("Why VarAC defaults to standalone")
        self.varac_cluster_why_label.setWordWrap(True)
        self._active_form.addRow("Why", self.varac_cluster_why_label)
        self.email_gateway_sender_combo = QComboBox()
        self.email_gateway_sender_combo.setObjectName("softwareInstanceEmailGatewaySender")
        self.email_gateway_sender_combo.setAccessibleName("Email gateway sender")
        self.email_gateway_sender_combo.setToolTip(
            "Choose no sender when VarAC email relay is off, or explicitly select one cluster member."
        )
        self.email_gateway_sender_combo.currentIndexChanged.connect(
            lambda _index: (
                self._update_prepared_presentation(),
                self._refresh_review_if_needed(),
            )
        )
        self._field_widgets["email_gateway_sender_choice"] = self.email_gateway_sender_combo
        email_sender_label = QLabel("Email gateway sender")
        email_sender_label.setWordWrap(True)
        self._field_labels["email_gateway_sender_choice"] = email_sender_label
        self._active_form.addRow(email_sender_label, self.email_gateway_sender_combo)
        for key, text in (
            ("cluster_ptt_lock", "Enable cluster PTT lock"),
        ):
            checkbox = QCheckBox(text)
            checkbox.setAccessibleName(text)
            checkbox.toggled.connect(lambda _checked: self._refresh_review_if_needed())
            self._field_widgets[key] = checkbox
            label = QLabel("Cluster policy")
            self._field_labels[key] = label
            self._active_form.addRow(label, checkbox)

    def _build_launch_page(self) -> None:
        self._new_form_page(
            "Launch: review the resolved application recipe. Qualified managed recipes show exact component commands; unsupported applications retain an Advanced recovery override.",
            (("launch_command", "Advanced launch override", "Operator-provided recovery command"), ("notes", "Notes", "Why this instance exists or what it is connected to")),
        )
        self.launch_recipe_status_label = QLabel()
        self.launch_recipe_status_label.setObjectName("softwareInstanceLaunchRecipeStatus")
        self.launch_recipe_status_label.setAccessibleName("Resolved launch recipe status")
        self.launch_recipe_status_label.setWordWrap(True)
        self.launch_recipe_components_label = QLabel()
        self.launch_recipe_components_label.setObjectName("softwareInstanceLaunchRecipeComponents")
        self.launch_recipe_components_label.setAccessibleName("Resolved launch recipe components")
        self.launch_recipe_components_label.setWordWrap(True)
        self.launch_recipe_components_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.launch_recipe_recovery_label = QLabel()
        self.launch_recipe_recovery_label.setObjectName("softwareInstanceLaunchRecipeRecovery")
        self.launch_recipe_recovery_label.setAccessibleName("Launch recipe recovery action")
        self.launch_recipe_recovery_label.setWordWrap(True)
        self.launch_recipe_status_heading = QLabel("Resolved recipe")
        self.launch_recipe_status_heading.setAccessibleName("Resolved launch recipe heading")
        self.launch_recipe_components_heading = QLabel("Effective components")
        self.launch_recipe_components_heading.setAccessibleName("Effective launch components heading")
        self.launch_recipe_recovery_heading = QLabel("Recovery")
        self.launch_recipe_recovery_heading.setAccessibleName("Launch recipe recovery heading")
        self._active_form.insertRow(0, self.launch_recipe_status_heading, self.launch_recipe_status_label)
        self._active_form.insertRow(1, self.launch_recipe_components_heading, self.launch_recipe_components_label)
        self._active_form.insertRow(2, self.launch_recipe_recovery_heading, self.launch_recipe_recovery_label)
        launch = QCheckBox("Launch this instance at startup")
        launch.setAccessibleName("Launch software instance at startup")
        launch.toggled.connect(self._on_launch_policy_changed)
        self._field_widgets["launch_at_startup"] = launch
        launch_label = QLabel("Startup policy")
        self._field_labels["launch_at_startup"] = launch_label
        self._active_form.addRow(launch_label, launch)
        advanced_tx = QCheckBox("Enable Advanced Fast Light TX")
        advanced_tx.setAccessibleName("Enable Advanced Fast Light transmit mode")
        advanced_tx.setToolTip(
            "Receive-safe is the default. Advanced TX also requires a compatible Operating Model, RF Guard, and final preflight."
        )
        advanced_ack = QCheckBox(
            "I understand Advanced TX remains subject to Operating Model, RF Guard, and final preflight"
        )
        advanced_ack.setAccessibleName("Acknowledge Advanced Fast Light transmit safeguards")
        advanced_tx.toggled.connect(advanced_ack.setEnabled)
        advanced_tx.toggled.connect(lambda _checked: self._refresh_review_if_needed())
        advanced_ack.toggled.connect(lambda _checked: self._refresh_review_if_needed())
        advanced_ack.setEnabled(False)
        self._field_widgets["advanced_tx_requested"] = advanced_tx
        self._field_widgets["advanced_tx_acknowledged"] = advanced_ack
        advanced_label = QLabel("Fast Light mode")
        acknowledgement_label = QLabel("Advanced TX acknowledgement")
        self._field_labels["advanced_tx_requested"] = advanced_label
        self._field_labels["advanced_tx_acknowledged"] = acknowledgement_label
        self._active_form.addRow(advanced_label, advanced_tx)
        self._active_form.addRow(acknowledgement_label, advanced_ack)

    @staticmethod
    def _launch_component_lines(resolution: GuidedLaunchRecipeResolution) -> list[str]:
        """Format core-resolved component facts without reconstructing recipes."""

        lines: list[str] = []
        for component in resolution.components:
            lines.append(component.label or component.component_key or "Application")
            lines.append(
                f"  Effective command: {component.effective_command_text or 'Operator starts this application'}"
            )
            lines.append(
                f"  Working directory: {component.working_directory or 'Application default'}"
            )
            lines.append(
                "  Dependencies: "
                + (", ".join(component.dependencies) if component.dependencies else "None")
            )
            if component.profile_selector:
                lines.append(f"  Profile selector: {component.profile_selector}")
            lines.append(
                "  Configuration roots: "
                + (", ".join(component.configuration_roots) if component.configuration_roots else "None")
            )
            lines.append(
                "  Data roots: "
                + (", ".join(component.data_roots) if component.data_roots else "None")
            )
            endpoints = []
            for endpoint in component.endpoints:
                name = _text(endpoint.get("name")) or "Endpoint"
                protocol = _text(endpoint.get("protocol")) or "service"
                host = _text(endpoint.get("host")) or "application-defined"
                port = _int(endpoint.get("port")) or 0
                target = f"{protocol}://{host}:{port}" if port else f"{protocol}://{host}"
                endpoints.append(f"{name} {target}")
            lines.append("  Endpoints: " + (", ".join(endpoints) if endpoints else "None"))
            readiness = ", ".join(
                f"{key}={value}" for key, value in sorted(component.readiness.items())
            )
            lines.append(f"  Readiness policy: {readiness or 'Operator-confirmed'}")
            lines.append(
                "  Launch policy: "
                + (
                    "Operator starts this application"
                    if component.operator_starts
                    else "Launch at CommsPlan startup"
                    if component.launch_at_startup
                    else "Available for manual launch from CommsPlan"
                )
            )
        return lines

    def _on_launch_policy_changed(self, _checked: bool) -> None:
        if not self._loading_draft:
            self._resolve_launch_recipe()
        self._refresh_review_if_needed()

    def set_launch_recipe_resolution(
        self,
        resolution: GuidedLaunchRecipeResolution | Mapping[str, Any] | None,
    ) -> None:
        """Apply one already-resolved core recipe to this presentation surface."""

        self._launch_recipe_resolution_supplied = resolution is not None
        if resolution is None:
            self._launch_recipe_resolution = None
        elif isinstance(resolution, GuidedLaunchRecipeResolution):
            self._launch_recipe_resolution = resolution
        elif isinstance(resolution, Mapping):
            self._launch_recipe_resolution = recipe_resolution_from_mapping(resolution)
        else:
            raise TypeError("resolution must be a GuidedLaunchRecipeResolution, mapping, or None")
        self._apply_launch_recipe_updates()
        self._apply_launch_recipe_presentation()
        self._refresh_review_if_needed()

    def _resolve_launch_recipe(self) -> None:
        if self._resolving_launch_recipe or self._family_key not in {"js8call", "fast_light"}:
            self._apply_launch_recipe_presentation()
            return
        if self._launch_recipe_resolution_supplied and self._launch_recipe_resolution is not None:
            self._apply_launch_recipe_presentation()
            return
        self._resolving_launch_recipe = True
        try:
            resolution = resolve_guided_launch_recipe(
                self.draft().payload(),
                managed_root=self._managed_root,
            )
            self._launch_recipe_resolution = resolution
            self._apply_launch_recipe_updates()
            self._apply_launch_recipe_presentation()
        finally:
            self._resolving_launch_recipe = False

    def _apply_launch_recipe_updates(self) -> None:
        resolution = self._launch_recipe_resolution
        if resolution is None:
            return
        updates = recipe_draft_updates(resolution)
        if not resolution.qualified:
            return
        self._loading_draft = True
        try:
            for key, value in updates.items():
                widget = self._field_widgets.get(key)
                if isinstance(widget, QLineEdit):
                    widget.setText(str(value if value is not None else ""))
                elif isinstance(widget, QCheckBox):
                    widget.setChecked(bool(value))
                elif isinstance(widget, QComboBox):
                    index = widget.findData(value)
                    if index >= 0:
                        widget.setCurrentIndex(index)
        finally:
            self._loading_draft = False

    def _apply_launch_recipe_presentation(self) -> None:
        if not hasattr(self, "launch_recipe_status_label"):
            return
        resolution = self._launch_recipe_resolution
        supported_family = self._family_key in {"js8call", "fast_light"}
        self.launch_recipe_status_label.setVisible(supported_family)
        self.launch_recipe_status_heading.setVisible(supported_family)
        # Exact commands, roots, dependencies, and readiness clauses are
        # technical evidence.  The compact recipe status stays on Launch;
        # the family-scoped disclosure above the page owns the raw detail.
        self.launch_recipe_components_label.setVisible(False)
        self.launch_recipe_components_heading.setVisible(False)
        self.launch_recipe_recovery_label.setVisible(
            supported_family and resolution is not None and bool(resolution.recovery_action)
        )
        self.launch_recipe_recovery_heading.setVisible(
            supported_family and resolution is not None and bool(resolution.recovery_action)
        )
        if resolution is None:
            self.launch_recipe_status_label.setText(
                "Recipe pending. Complete Identity, Connections, and Files, then return here."
            )
            self.launch_recipe_components_label.clear()
            self.launch_recipe_recovery_label.clear()
        else:
            status = resolution.status.replace("_", " ").title()
            self.launch_recipe_status_label.setText(
                f"{status} — {resolution.summary or 'Review the resolved launch details below.'}"
            )
            self.launch_recipe_components_label.setText(
                "\n".join(self._launch_component_lines(resolution))
            )
            self.launch_recipe_recovery_label.setText(resolution.recovery_action)
        command = self._field_widgets.get("launch_command")
        command_label = self._field_labels.get("launch_command")
        raw_override_visible = (
            not supported_family
            or resolution is None
            or bool(resolution.raw_override_allowed)
        )
        if command is not None:
            command.setVisible(raw_override_visible)
            command.setEnabled(raw_override_visible and not self._source_locked)
        if command_label is not None:
            command_label.setVisible(raw_override_visible)
        qualified_managed = bool(
            supported_family and resolution is not None and resolution.qualified
        )
        recipe_owned_paths = {
            "js8call": ("configuration_path", "storage_path"),
            "fast_light": (
                "configuration_path",
                "secondary_configuration_path",
                "storage_path",
                "secondary_storage_path",
            ),
        }.get(self._family_key, ())
        for key in recipe_owned_paths:
            widget = self._field_widgets.get(key)
            label = self._field_labels.get(key)
            base_visible = key in _FAMILY_FIELDS.get(self._family_key, frozenset())
            if (
                self._family_key == "fast_light"
                and self._radio_role == "observer"
                and key == "configuration_path"
            ):
                base_visible = False
            visible = base_visible and not qualified_managed
            if widget is not None:
                widget.setVisible(visible)
                widget.setEnabled(visible and not self._source_locked)
            if label is not None:
                label.setVisible(visible)
        self._update_prepared_presentation()

    def _add_line(self, key: str, label: str, placeholder: str) -> None:
        if key == "settings_identity_mode":
            combo = QComboBox()
            combo.setObjectName("softwareInstanceSettingsIdentityMode")
            combo.setAccessibleName("How this radio uses application settings")
            combo.addItem("Use my current application settings", "default")
            combo.addItem("Keep this radio's settings separate", "isolated")
            combo.setCurrentIndex(1)
            combo.currentIndexChanged.connect(self._on_settings_identity_changed)
            self._field_widgets[key] = combo
            label_widget = QLabel(label)
            label_widget.setWordWrap(True)
            self._field_labels[key] = label_widget
            self._active_form.addRow(label_widget, combo)
            return
        if key == "variant":
            combo = QComboBox()
            combo.setAccessibleName(label)
            combo.addItem("Choose the verified JS8Call variant", "")
            combo.addItem("JS8Call 2.2", "js8call_2_2")
            combo.addItem("JS8Call Improved 3.0.3", "js8call_improved_3_0_3")
            combo.addItem("JS8Call Subspace 4.1", "js8call_subspace_4_1")
            combo.currentIndexChanged.connect(lambda _index: self._refresh_review_if_needed())
            self._field_widgets[key] = combo
            label_widget = QLabel(label)
            label_widget.setWordWrap(True)
            self._field_labels[key] = label_widget
            self._active_form.addRow(label_widget, combo)
            return
        if key == "cluster_path":
            combo = QComboBox()
            combo.setObjectName("softwareInstanceClusterPath")
            combo.setAccessibleName(label)
            combo.setToolTip(
                "Standalone is the default. Create or join a cluster only when this node intentionally participates in coordinated routing or shared BBS behavior."
            )
            combo.addItem("Standalone VarAC node", "standalone")
            combo.addItem("Create a new cluster", "create_cluster")
            combo.addItem("Join an existing cluster", "join_cluster")
            combo.currentIndexChanged.connect(lambda _index: self._sync_family_fields())
            combo.currentIndexChanged.connect(lambda _index: self._refresh_review_if_needed())
            self._field_widgets[key] = combo
            label_widget = QLabel(label)
            label_widget.setWordWrap(True)
            self._field_labels[key] = label_widget
            self._active_form.addRow(label_widget, combo)
            return
        if key == "cluster_id":
            combo = QComboBox()
            combo.setEditable(True)
            combo.setAccessibleName(label)
            combo.setToolTip("Choose a configured cluster, or leave this blank for a standalone VarAC node.")
            combo.addItem("Choose or enter a cluster", "")
            for cluster in self._varac_clusters:
                public_id = _text(cluster.get("cluster_id"))
                name = _text(cluster.get("name")) or public_id
                if public_id:
                    combo.addItem(f"{name} · {public_id}", public_id)
            combo.currentTextChanged.connect(lambda _text: self._refresh_review_if_needed())
            self._field_widgets[key] = combo
            label_widget = QLabel(label)
            label_widget.setWordWrap(True)
            self._field_labels[key] = label_widget
            self._active_form.addRow(label_widget, combo)
            return
        edit = QLineEdit()
        edit.setAccessibleName(label)
        edit.setPlaceholderText(placeholder)
        edit.textEdited.connect(lambda _text, _key=key: self._refresh_review_if_needed())
        self._field_widgets[key] = edit
        label_widget = QLabel(label)
        label_widget.setAccessibleName(f"Label for {label}")
        label_widget.setWordWrap(True)
        self._field_labels[key] = label_widget
        self._active_form.addRow(label_widget, edit)

    def _build_review_page(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.review_summary_label = QLabel()
        self.review_summary_label.setObjectName("softwareInstanceReviewSummary")
        self.review_summary_label.setAccessibleName("Software instance review summary")
        self.review_summary_label.setWordWrap(True)
        layout.addWidget(self.review_summary_label)
        self.review_label = QLabel()
        self.review_label.setWordWrap(True)
        self.review_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.review_label.setAccessibleName("Software instance review")
        # Retain the detailed review text for copy/review integrations, while
        # the normal page keeps raw paths and commands behind the shared,
        # family-scoped disclosure.
        self.review_label.setVisible(False)
        layout.addWidget(self.review_label)
        self.conflict_label = QLabel()
        self.conflict_label.setWordWrap(True)
        self.conflict_label.setAccessibleName("Software instance conflicts")
        layout.addWidget(self.conflict_label)
        layout.addStretch(1)
        self.pages.addWidget(page)

    def _load_family(self, family_key: str) -> None:
        normalized = _text(family_key).lower()
        if normalized not in {key for key, _label in SUPPORTED_INSTANCE_FAMILIES}:
            normalized = "js8call"
        family_changed = normalized != self._family_key
        if (
            self._launch_recipe_resolution is not None
            and self._launch_recipe_resolution.family_key != normalized
            and not self._launch_recipe_resolution_supplied
        ):
            self._launch_recipe_resolution = None
        self._family_key = normalized
        if family_changed or not self._draft_instance_key:
            self._draft_instance_key = stable_draft_instance_key(
                self._assistant_draft_owner_key,
                normalized,
            )
        if family_changed or not self._application_system_key:
            self._application_system_key = stable_application_system_key(
                self._assistant_draft_owner_key,
                normalized,
            )
        if hasattr(self, "family_combo"):
            index = self.family_combo.findData(normalized)
            if index >= 0 and self.family_combo.currentIndex() != index:
                blocked = self.family_combo.blockSignals(True)
                self.family_combo.setCurrentIndex(index)
                self.family_combo.blockSignals(blocked)
        title, detail = _FAMILY_DETAILS[normalized]
        if hasattr(self, "title_label"):
            action = "Replace" if self._replacement_requested else "Add"
            self.title_label.setText(f"{action} {title}")
            self.guidance_label.setText(
                f"Set up one distinct {title.lower()} for the selected radio. "
                + detail
                + " Existing settings are evidence for review. CommsPlan does not claim to write third-party application configuration unless a supported, explicit apply is provided."
            )
            if getattr(self, "_family_locked", False):
                self.family_scope_label.setText(
                    f"Family fixed by the selected workspace: {dict(SUPPORTED_INSTANCE_FAMILIES).get(normalized, title)}. "
                    "This operation creates exactly one instance in this family."
                )
        host = self._field_widgets.get("host")
        port = self._field_widgets.get("port")
        if isinstance(host, QLineEdit) and not host.text():
            host.setText("127.0.0.1")
        if isinstance(port, QLineEdit) and not port.text() and _DEFAULT_PORTS[normalized]:
            port.setText(str(_DEFAULT_PORTS[normalized]))
        secondary = self._field_widgets.get("secondary_port")
        if isinstance(secondary, QLineEdit) and not secondary.text() and normalized == "fast_light":
            secondary.setText(str(self._next_port(7362, "fldigi_port", "secondary_port")))
        arq = self._field_widgets.get("arq_port")
        if isinstance(arq, QLineEdit) and not arq.text() and normalized == "fast_light":
            arq.setText(str(self._next_port(7322, "fldigi_arq_port", "arq_port")))
        udp = self._field_widgets.get("udp_port")
        if isinstance(udp, QLineEdit) and not udp.text() and normalized == "js8call":
            udp.setText(str(self._next_port(2242, "udp_port", "js8_udp_port")))
        if isinstance(port, QLineEdit) and normalized == "js8call" and port.text() == str(_DEFAULT_PORTS[normalized]):
            port.setText(str(self._next_port(2442, "port")))
        elif isinstance(port, QLineEdit) and normalized == "fast_light" and port.text() == str(_DEFAULT_PORTS[normalized]):
            port.setText(str(self._next_port(12345, "flrig_port", "port")))
        self._sync_family_fields()
        if normalized == "fast_light" and self._radio_role == "observer":
            self.guidance_label.setText(
                "Configure one receive-only Fast Light identity for this SDR. FLDigi and approved file helpers are available; "
                "FLRig, CAT, PTT, transmit, and automatic send remain disabled."
            )
        elif normalized == "varac" and self._radio_role == "observer":
            self.guidance_label.setText(
                "VarAC is unavailable for an observer / SDR. Return to Add Radio and choose a receive-only application."
            )
        self._rebuild_radio_choices()

    def _next_port(self, base: int, *keys: str) -> int:
        used: set[int] = set()
        for row in self._existing_instances:
            for key in keys:
                parsed = _int(row.get(key))
                if parsed is not None:
                    used.add(parsed)
        candidate = int(base)
        while candidate in used and candidate < 65535:
            candidate += 1
        return candidate

    def _apply_identity_defaults(self) -> None:
        if self._family_key != "js8call":
            return
        rig_widget = self._field_widgets.get("rig_name")
        if not isinstance(rig_widget, QLineEdit) or rig_widget.text().strip():
            return
        name_widget = self._field_widgets.get("instance_name")
        radio = self.radio_combo.currentText().strip()
        source = name_widget.text().strip() if isinstance(name_widget, QLineEdit) else ""
        source = source or radio or "JS8"
        candidate = re.sub(r"[^A-Za-z0-9_-]+", "-", source).strip("-")[:48] or "JS8"
        used = {_text(row.get("rig_name")).casefold() for row in self._existing_instances}
        base = candidate
        suffix = 2
        while candidate.casefold() in used:
            candidate = f"{base[:43]}-{suffix}"
            suffix += 1
        rig_widget.setText(candidate)

    def _family_link_column(self) -> str:
        return {
            "js8call": "js8_instance_id",
            "fast_light": "fast_light_config_id",
            "varac": "varac_node_id",
        }.get(self._family_key, "")

    def _rebuild_radio_choices(self) -> None:
        """Render every known radio with same-family ownership status."""

        if self._unsaved_owner_key:
            self.radio_combo.blockSignals(True)
            self.radio_combo.clear()
            self.radio_combo.addItem(
                f"{self._unsaved_radio_label} — inactive setup draft",
                None,
            )
            self.radio_combo.setToolTip(
                "This software instance belongs to the current unsaved radio draft. "
                "Nothing is persisted until Save Radio and Software."
            )
            self.radio_combo.setEnabled(False)
            self.radio_combo.blockSignals(False)
            self._selected_radio_id = None
            self._radio_assignments = {}
            self._replacement_instance = None
            self._refresh_radio_context()
            return

        assignments: dict[int, Mapping[str, Any]] = {}
        by_id = {
            _int(row.get("id")): row
            for row in self._existing_instances
            if _int(row.get("id")) is not None
        }
        link_column = self._family_link_column()
        for row in self._radios:
            radio_id = _int(row.get("id") or row.get("radio_id"))
            if radio_id is None:
                continue
            assigned_id = _int(row.get(link_column)) if link_column else None
            direct = _int(row.get("instance_id"))
            assigned_id = assigned_id or direct
            if assigned_id is not None:
                assignments[radio_id] = by_id.get(
                    assigned_id,
                    {"id": assigned_id, "name": f"Instance {assigned_id}"},
                )
            else:
                direct_row = next(
                    (
                        candidate for candidate in self._existing_instances
                        if _int(candidate.get("radio_id")) == radio_id
                    ),
                    None,
                )
                if direct_row is not None:
                    assignments[radio_id] = direct_row
        self._radio_assignments = assignments

        prior_id = _int(self.radio_combo.currentData())
        desired_id = self._selected_radio_id if self._selected_radio_id in {
            _int(row.get("id") or row.get("radio_id")) for row in self._radios
        } else prior_id
        self.radio_combo.blockSignals(True)
        self.radio_combo.clear()
        candidate_rows = [
            row for row in self._radios
            if _int(row.get("id") or row.get("radio_id")) is not None
        ]
        if candidate_rows:
            self.radio_combo.addItem("Choose an existing radio…", None)
        seen: set[int] = set()
        for row in self._radios:
            radio_id = _int(row.get("id") or row.get("radio_id"))
            if radio_id is None or radio_id in seen:
                continue
            seen.add(radio_id)
            name = _text(row.get("name")) or f"Radio {radio_id}"
            assigned = assignments.get(radio_id)
            instance_name = _text(assigned.get("name") or assigned.get("instance_name")) if assigned else ""
            label = f"{name} — Assigned to {instance_name}" if assigned else f"{name} — Available"
            self.radio_combo.addItem(label, radio_id)
            index = self.radio_combo.count() - 1
            tooltip = (
                "This radio already has a %s instance. Replacement mode is explicit and retains the existing record."
                % (instance_name or "software")
                if assigned
                else "This radio has no instance in the selected software family."
            )
            self.radio_combo.setItemData(index, tooltip, Qt.ToolTipRole)
        if self.radio_combo.count():
            if desired_id is None and len(candidate_rows) == 1:
                only_id = _int(candidate_rows[0].get("id") or candidate_rows[0].get("radio_id"))
                index = self.radio_combo.findData(only_id)
            else:
                index = self.radio_combo.findData(desired_id)
            self.radio_combo.setCurrentIndex(index if index >= 0 else 0)
        self.radio_combo.blockSignals(False)
        self._selected_radio_id = _int(self.radio_combo.currentData())
        self._refresh_radio_context()

    def _set_replacement_confirmed(self, checked: bool) -> None:
        self._replacement_confirmed = bool(checked)
        self._refresh_radio_context()
        self._refresh()

    def _on_radio_changed(self, _index: int) -> None:
        """Refresh both guidance and step navigation for an explicit choice."""

        self._refresh_radio_context()
        self._refresh()

    def _refresh_radio_context(self) -> None:
        if self._unsaved_owner_key:
            self._selected_radio_id = None
            self._replacement_instance = None
            self._replacement_confirmed = False
            self.create_radio_button.setVisible(False)
            self.radio_combo.setEnabled(False)
            self.replacement_checkbox.setVisible(False)
            self.replacement_checkbox.setEnabled(False)
            self.radio_guidance_label.setText(
                f"Radio draft selected: {self._unsaved_radio_label}. "
                "Apply returns the reviewed software bundle to Add Radio; it does not save it."
            )
            self.replacement_banner.setText(
                "Inactive setup draft — Save Radio and Software remains required."
            )
            return
        radio_id = _int(self.radio_combo.currentData())
        self._selected_radio_id = radio_id
        replacement = self._radio_assignments.get(radio_id) if radio_id is not None else None
        prior = self._replacement_instance
        self._replacement_instance = replacement
        if replacement is not prior:
            self._replacement_confirmed = False
            if hasattr(self, "replacement_checkbox"):
                blocked = self.replacement_checkbox.blockSignals(True)
                self.replacement_checkbox.setChecked(False)
                self.replacement_checkbox.blockSignals(blocked)
        has_radios = any(
            _int(row.get("id") or row.get("radio_id")) is not None
            for row in self._radios
        )
        self.create_radio_button.setVisible(not has_radios)
        self.radio_combo.setEnabled(has_radios)
        if not has_radios:
            self.radio_guidance_label.setText(
                "No radios exist yet. Create a radio first in Radio Profiles, then return here; "
                "a software instance cannot be created without an existing radio."
            )
            self.replacement_checkbox.setVisible(False)
            self.replacement_banner.setText("Radio required before entering instance data.")
        elif radio_id is None:
            self.radio_guidance_label.setText(
                "Choose an existing radio before entering instance data."
            )
            self.replacement_checkbox.setVisible(False)
            self.replacement_checkbox.setEnabled(False)
            self.replacement_banner.setText("Choose a radio before entering instance data.")
        elif replacement is not None:
            replacement_id = _int(replacement.get("id") or replacement.get("instance_id"))
            requested_target = (self._replacement_requested and radio_id == self._replacement_radio_id
                                and self._family_key == self._replacement_family_key)
            if requested_target and self._replacement_target_instance_id is None:
                self._replacement_target_instance_id = replacement_id
            explicit_intent = bool(requested_target and replacement_id is not None
                                   and replacement_id == self._replacement_target_instance_id)
            if explicit_intent:
                self._replacement_confirmed = True
                blocked = self.replacement_checkbox.blockSignals(True)
                self.replacement_checkbox.setChecked(True)
                self.replacement_checkbox.blockSignals(blocked)
            name = _text(replacement.get("name") or replacement.get("instance_name")) or "existing instance"
            self.radio_guidance_label.setText(
                "This radio is already assigned to this software family. "
                + ("Replacement selected; the existing record is retained until Save."
                   if explicit_intent else
                   "Choose replacement mode before entering instance data; the existing record is retained.")
            )
            self.replacement_checkbox.setText(f"Replace the existing instance assigned to {self.radio_combo.currentText().split(' — ', 1)[0]}")
            self.replacement_checkbox.setVisible(not explicit_intent)
            self.replacement_checkbox.setEnabled(not explicit_intent)
            mode = "active" if self._replacement_confirmed else "required"
            self.replacement_banner.setText(
                f"Replacement mode {mode}: {name} is currently assigned. "
                "Review will compare the current instance with the proposed one; no manual disassociate is required."
            )
        else:
            self.radio_guidance_label.setText(
                "Choose an existing radio. Available radios can accept one instance of each software family."
            )
            self.replacement_checkbox.setVisible(False)
            self.replacement_checkbox.setEnabled(False)
            self.replacement_banner.setText(
                "Radio selected: Available for this software family."
            )

    def _sync_family_fields(self) -> None:
        visible = _FAMILY_FIELDS.get(self._family_key, frozenset())
        labels = _FAMILY_FIELD_LABELS.get(self._family_key, {})
        observer_mode = self._radio_role == "observer"
        cluster_path_widget = self._field_widgets.get("cluster_path")
        cluster_path = (
            str(cluster_path_widget.currentData() or "standalone")
            if isinstance(cluster_path_widget, QComboBox)
            else "standalone"
        )
        self._refresh_email_gateway_sender_choices()
        for key, widget in self._field_widgets.items():
            shown = key in visible
            if self._family_key == "fast_light" and observer_mode and key in {
                "port",
                "application_path",
                "configuration_path",
                "advanced_tx_requested",
                "advanced_tx_acknowledged",
            }:
                shown = False
            if self._family_key == "varac":
                if key in {"cluster_id", "cluster_name", "cluster_shared_database", "cluster_instance_number", "email_gateway_sender_choice", "cluster_ptt_lock"}:
                    shown = cluster_path != "standalone"
                if key in {"cluster_name", "cluster_shared_database", "email_gateway_sender_choice", "cluster_ptt_lock"}:
                    shown = cluster_path == "create_cluster"
            widget.setVisible(shown)
            label = self._field_labels.get(key)
            if label is not None:
                if key in labels and isinstance(label, QLabel):
                    label.setText(labels[key])
                label.setVisible(shown)
        if self._family_key == "fast_light" and observer_mode:
            for key in ("advanced_tx_requested", "advanced_tx_acknowledged"):
                widget = self._field_widgets.get(key)
                if isinstance(widget, QCheckBox):
                    widget.setChecked(False)
        default_identity = self._settings_identity_mode() == "default"
        if self._family_key == "js8call" and default_identity:
            rig_widget = self._field_widgets.get("rig_name")
            rig_label = self._field_labels.get("rig_name")
            if rig_widget is not None:
                rig_widget.setVisible(False)
            if rig_label is not None:
                rig_label.setVisible(False)
        self._sync_settings_identity_presentation()
        self.varac_cluster_why_label.setVisible(self._family_key == "varac")
        self._apply_launch_recipe_presentation()

    def _settings_identity_mode(self) -> str:
        widget = self._field_widgets.get("settings_identity_mode")
        if isinstance(widget, QComboBox):
            value = str(widget.currentData() or "").strip().lower()
            if value in {"default", "isolated"}:
                return value
        return "isolated"

    def _on_settings_identity_changed(self, _index: int) -> None:
        if not self._loading_draft and not self._launch_recipe_resolution_supplied:
            self._launch_recipe_resolution = None
        self._sync_settings_identity_presentation()
        self._sync_family_fields()
        self._refresh_review_if_needed()

    def _sync_settings_identity_presentation(self) -> None:
        if not hasattr(self, "settings_identity_help_label"):
            return
        combo = self._field_widgets.get("settings_identity_mode")
        supported = self._family_key in {"js8call", "fast_light"}
        managed = any(
            key == "managed" and button.isChecked()
            for key, button in self.source_buttons.items()
        )
        shown = supported and managed
        self.settings_identity_help_label.setVisible(shown)
        if combo is not None:
            combo.setVisible(shown)
        label = self._field_labels.get("settings_identity_mode")
        if label is not None:
            label.setVisible(shown)
        if not shown or not isinstance(combo, QComboBox):
            return
        if self._family_key == "js8call":
            combo.setItemText(0, "Use my current JS8Call setup")
            combo.setItemText(1, "Keep this radio's JS8Call setup separate")
            if self._settings_identity_mode() == "default":
                detail = (
                    "Uses JS8Call's normal settings without a --rig-name selector. "
                    "Best for the primary or only radio. Only one configured JS8Call instance can use this choice."
                )
            else:
                detail = (
                    "Uses a named JS8Call identity with separate settings and data. "
                    "Required when multiple JS8Call radios run at the same time."
                )
        else:
            combo.setItemText(0, "Use my current Fast Light setup")
            combo.setItemText(1, "Keep this radio's Fast Light setup separate")
            if self._settings_identity_mode() == "default":
                detail = (
                    "Uses the existing FLRig, FLDigi, FLMsg, and FLAmp settings without separate configuration folders. "
                    "Best for the primary or only radio.\n"
                    f"FLRig: existing settings · port {self._line_value('port') or 'not set'}\n"
                    f"FLDigi: existing settings · port {self._line_value('secondary_port') or 'not set'}\n"
                    "FLMsg/FLAmp: existing NBEMS workspace. Only one configured Fast Light instance can use this choice."
                )
            else:
                detail = (
                    "Uses separate settings folders, ports, logs, and message folders for this radio. "
                    f"FLRig: separate settings · port {self._line_value('port') or 'not set'}\n"
                    f"FLDigi: separate settings · port {self._line_value('secondary_port') or 'not set'}\n"
                    "FLMsg/FLAmp: separate NBEMS workspace. Required when multiple radios run Fast Light applications simultaneously."
                )
        self.settings_identity_help_label.setText(detail)

    def _line_value(self, key: str) -> str:
        widget = self._field_widgets.get(key)
        if isinstance(widget, QLineEdit):
            return widget.text().strip()
        return ""

    def _set_source_locked(self, locked: bool) -> None:
        """Keep imported identity fields immutable until the operator clones."""

        self._source_locked = bool(locked)
        for key, widget in self._field_widgets.items():
            if key == "notes":
                continue
            widget.setEnabled(not self._source_locked)
        if hasattr(self, "source_lock_banner"):
            self.source_lock_banner.setVisible(self._source_locked)
            self.source_lock_banner.setText(
                "Imported identity is source-locked. Endpoints, profiles, data paths, and launch details "
                "remain unchanged. Choose Clone as a distinct instance to allocate a separate identity."
                if self._source_locked else ""
            )
        if hasattr(self, "clone_distinct_button"):
            self.clone_distinct_button.setVisible(self._source_locked)
            self.clone_distinct_button.setEnabled(self._source_locked)
        self._apply_launch_recipe_presentation()

    def _clone_as_distinct(self) -> None:
        """Replace an imported bundle with one safe core-generated draft seed."""

        if not self._source_locked:
            return
        current = self.draft()
        source = dict(self._selected_source_payload or current.payload())
        proposal_owner = self._unsaved_owner_key or f"software-assistant-{id(self):x}"
        seed = distinct_draft_seed(
            self._family_key,
            owner_draft_key=proposal_owner,
            snapshot=self._inventory_snapshot,
            source=source,
        )
        family_title = dict(SUPPORTED_INSTANCE_FAMILIES).get(self._family_key, "Software")
        seed.update(
            {
                "instance_name": (
                    self._unsaved_radio_label
                    if self._unsaved_owner_key
                    else f"{current.instance_name or family_title} copy"
                ),
                "owner_draft_key": self._unsaved_owner_key,
                "owner_label": self._unsaved_radio_label if self._unsaved_owner_key else "",
                "radio_role": self._radio_role,
                "launch_at_startup": current.launch_at_startup,
                "notes": current.notes,
            }
        )
        self._selected_source_payload = {}
        self._set_draft(normalize_instance_draft(seed))
        self.set_operation_status(
            "Distinct draft created. Review the newly allocated identity, ports, profiles, and data paths before applying."
        )
        self._refresh()

    def _refresh_source(self) -> None:
        selected_modes = [key for key, button in self.source_buttons.items() if button.isChecked()]
        if not selected_modes:
            return
        discover = self.source_buttons["discover"].isChecked()
        mode = selected_modes[0]
        if not self._loading_draft and mode == "managed" and self._source_locked:
            self._clone_as_distinct()
            return
        if not self._loading_draft and mode == "remote" and self._source_locked:
            self._loading_draft = True
            try:
                self.source_buttons["discover"].setChecked(True)
            finally:
                self._loading_draft = False
            self.set_operation_status(
                "The imported identity remains locked. Clone it as a distinct instance before changing to manual or remote setup.",
                error=True,
            )
            return
        ownership = self._field_widgets.get("ownership")
        if isinstance(ownership, QComboBox):
            desired_ownership = {
                "managed": "fio-managed",
                "discover": "operator-managed",
                "remote": "remote",
            }.get(mode, "operator-managed")
            index = ownership.findData(desired_ownership)
            if index >= 0:
                ownership.setCurrentIndex(index)
        if not discover and self._discovery_selected:
            self._imported_id = None
            self._imported_system_key = ""
            self._source_fingerprint = ""
            self._source_locked = False
            self._discovery_selected = False
            self._set_source_locked(False)
        self.discover_button.setVisible(discover)
        self.discovery_list.setVisible(
            discover
            and bool(self._discovery_results or self._diagnostic_discovery_results)
        )
        self.discovery_diagnostics_label.setVisible(
            discover and bool(self._diagnostic_discovery_results)
        )
        self.discovery_hint.setText(
            "Discovery is explicit. Choose a result to import its values; review remains required."
            if discover else
            "You can change these values later in Software Administration. Nothing is saved on this step."
        )
        self._sync_settings_identity_presentation()
        self._update_prepared_presentation()

    def _import_selected_discovery(self) -> None:
        item = self.discovery_list.currentItem()
        if item is None:
            return
        value = item.data(Qt.UserRole)
        if isinstance(value, Mapping):
            source = dict(value)
            fingerprint = _text(source.get("source_fingerprint")) or source_identity_fingerprint(
                self._family_key,
                source,
            )
            source.update(
                {
                    "family_key": self._family_key,
                    "mode": "discover",
                    "ownership": "operator-managed",
                    "inventory_fingerprint": self._inventory_snapshot.fingerprint,
                    "source_fingerprint": fingerprint,
                    "source_locked": True,
                }
            )
            self._selected_source_payload = dict(source)
            imported = normalize_instance_draft(source)
            self._discovery_selected = True
            self._set_draft(imported)

    def set_discovery_results(self, results: Iterable[Mapping[str, Any]]) -> None:
        """Render immutable host results; diagnostic rows never become imports."""

        usable_rows: list[Mapping[str, Any]] = []
        recovery_rows: list[Mapping[str, Any]] = []
        diagnostic_rows: list[Mapping[str, Any]] = []
        for raw_row in results:
            if not isinstance(raw_row, Mapping):
                continue
            row = dict(raw_row)
            if _existing_candidate_is_usable(row):
                usable_rows.append(row)
                if _existing_candidate_is_recovery(row):
                    recovery_rows.append(row)
            else:
                diagnostic_rows.append(row)
        self._discovery_results = tuple(usable_rows)
        self._recovery_discovery_results = tuple(recovery_rows)
        self._diagnostic_discovery_results = tuple(diagnostic_rows)
        self._discovery_selected = bool(self._source_locked)
        self.discovery_list.clear()
        for row in self._discovery_results:
            name = _text(row.get("name") or row.get("instance_name")) or "Unnamed configuration"
            host, port = _endpoint(row)
            detail = f"{name} · {host}:{port}" if host and port else name
            if _existing_candidate_is_recovery(row):
                detail = f"Recovery candidate — {detail}"
            elif _bool(row.get("provenance_unverified")):
                detail = f"{detail} · Configuration provenance unverified"
            item = QListWidgetItem(detail)
            item.setData(Qt.UserRole, row)
            evidence = [
                _text(row.get("application_path") or row.get("install_path") or row.get("path")),
                _text(row.get("configuration_path") or row.get("profile_path") or row.get("ini_path")),
                _text(row.get("storage_path") or row.get("application_data_root") or row.get("db_path")),
            ]
            evidence_lines = [part for part in evidence if part]
            if _existing_candidate_is_recovery(row):
                evidence_lines.insert(
                    0,
                    "Complete source-evidenced configuration is currently unassigned. Importing it requires final reviewed assignment.",
                )
            elif _bool(row.get("provenance_unverified")):
                evidence_lines.insert(
                    0,
                    "Configuration provenance unverified. The durable radio link makes this existing bundle usable, but CommsPlan does not claim native ownership.",
                )
            item.setToolTip("\n".join(evidence_lines))
            self.discovery_list.addItem(item)
        for row in self._diagnostic_discovery_results:
            name = _text(row.get("name") or row.get("instance_name")) or "Unnamed configuration"
            item = QListWidgetItem(f"Diagnostic only — {name}")
            item.setData(Qt.UserRole, None)
            item.setFlags(item.flags() & ~Qt.ItemIsEnabled & ~Qt.ItemIsSelectable)
            item.setToolTip(
                "This incomplete or unlinked record is recovery evidence only. "
                "It cannot be imported, recommended, assigned, or launched from this workflow.\n\n"
                + _diagnostic_candidate_reason(row)
            )
            self.discovery_list.addItem(item)
        diagnostic_count = len(self._diagnostic_discovery_results)
        if diagnostic_count:
            noun = "record" if diagnostic_count == 1 else "records"
            self.discovery_diagnostics_label.setText(
                f"{diagnostic_count} incomplete or unlinked {noun} shown as diagnostic evidence only. "
                "They cannot be imported, recommended, assigned, or launched from this workflow."
            )
            self.discovery_diagnostics_label.setToolTip(
                "Open the item tooltip to review the core classification evidence. "
                "Recovery or cleanup requires a separately reviewed maintenance workflow."
            )
        else:
            self.discovery_diagnostics_label.clear()
            self.discovery_diagnostics_label.setToolTip("")
        self._refresh_source()

    def _set_draft(self, draft: SoftwareInstanceDraft) -> None:
        values = draft.payload()
        self._radio_role = draft.radio_role or self._radio_role
        self._imported_id = draft.imported_id
        self._imported_system_key = draft.imported_system_key
        self._draft_instance_key = draft.draft_instance_key or self._draft_instance_key
        self._application_system_key = (
            draft.application_system_key or self._application_system_key
        )
        self._inventory_fingerprint = draft.inventory_fingerprint or self._inventory_snapshot.fingerprint
        self._source_fingerprint = draft.source_fingerprint
        self._source_locked = bool(draft.source_locked)
        self._varac_arrangement_metadata = {
            "existing_standalone_node_id": int(draft.existing_standalone_node_id or 0),
            "existing_standalone_device_profile_id": int(
                draft.existing_standalone_device_profile_id or 0
            ),
            "existing_standalone_member_number": int(
                draft.existing_standalone_member_number or 0
            ),
        }
        self._legacy_cluster_gateway = bool(draft.cluster_gateway)
        self._refresh_email_gateway_sender_choices()
        if self._source_locked:
            self._discovery_selected = True
            if not self._selected_source_payload:
                self._selected_source_payload = dict(values)
        self._writer_platform = draft.writer_platform
        self._writer_operation = draft.writer_operation or "create"
        self._vara_runtime_path = draft.vara_runtime_path
        self._vara_ini_path = draft.vara_ini_path
        if draft.launch_recipe and not self._launch_recipe_resolution_supplied:
            self._launch_recipe_resolution = recipe_resolution_from_mapping(draft.launch_recipe)
        elif not draft.launch_recipe and not self._launch_recipe_resolution_supplied:
            self._launch_recipe_resolution = None
        self._loading_draft = True
        try:
            source_button = self.source_buttons.get(draft.mode)
            if source_button is not None:
                source_button.setChecked(True)
            for key, widget in self._field_widgets.items():
                value = values.get(key, "")
                if isinstance(widget, QLineEdit):
                    widget.setText(str(value if value is not None else ""))
                elif isinstance(widget, QCheckBox):
                    widget.setChecked(bool(value))
                elif isinstance(widget, QComboBox):
                    index = widget.findData(value)
                    if index >= 0:
                        widget.setCurrentIndex(index)
                    elif widget.isEditable():
                        widget.setCurrentText(str(value if value is not None else ""))
            radio_index = self.radio_combo.findData(draft.radio_id)
            if radio_index >= 0:
                self.radio_combo.setCurrentIndex(radio_index)
            # A discovered row is evidence owned by the operator until an
            # explicit supported managed apply is reviewed and confirmed.
            if draft.imported_id is not None or draft.source_locked:
                self.source_buttons["discover"].setChecked(True)
                operator_index = self._field_widgets["ownership"].findData("operator-managed")
                if operator_index >= 0 and draft.ownership == "fio-managed":
                    self._field_widgets["ownership"].setCurrentIndex(operator_index)
        finally:
            self._loading_draft = False
        self._sync_family_fields()
        self._set_source_locked(self._source_locked)
        self._apply_launch_recipe_presentation()
        self._update_prepared_presentation()

    def draft(self) -> SoftwareInstanceDraft:
        def value(key: str) -> str:
            widget = self._field_widgets.get(key)
            if isinstance(widget, QLineEdit):
                return widget.text().strip()
            if isinstance(widget, QComboBox):
                data = widget.currentData()
                if widget.isEditable() and not str(data or "").strip():
                    # An editable combo may use a labeled blank first item.
                    # Treat that item as blank, but preserve actual operator text.
                    if widget.currentIndex() >= 0:
                        return ""
                    return widget.currentText().strip()
                return str(data or "").strip()
            return ""

        def number(key: str) -> int:
            return _int(value(key)) or 0

        def checked(key: str) -> bool:
            widget = self._field_widgets.get(key)
            return bool(widget.isChecked()) if isinstance(widget, QCheckBox) else False

        radio_id = _int(self.radio_combo.currentData())
        resolution = self._launch_recipe_resolution
        cluster_path = value("cluster_path") or "standalone"
        existing_standalone_create = cluster_path == "create_cluster"
        email_sender_choice = (
            value("email_gateway_sender_choice")
            if existing_standalone_create
            else "none"
        ) or "none"
        if email_sender_choice == "existing_member":
            email_sender_member_id = str(
                self._varac_arrangement_metadata.get("existing_standalone_node_id", 0)
                or ""
            )
        elif email_sender_choice == "new_member":
            email_sender_member_id = self._draft_instance_key or self._assistant_draft_owner_key
        else:
            email_sender_member_id = ""
        return SoftwareInstanceDraft(
            family_key=self._family_key,
            instance_name=value("instance_name"),
            radio_id=radio_id,
            owner_draft_key=self._unsaved_owner_key,
            owner_label=self._unsaved_radio_label if self._unsaved_owner_key else "",
            draft_instance_key=self._draft_instance_key,
            application_system_key=self._application_system_key,
            inventory_generation=int(self._inventory_snapshot.generation),
            inventory_fingerprint=self._inventory_fingerprint or self._inventory_snapshot.fingerprint,
            source_fingerprint=self._source_fingerprint,
            source_locked=self._source_locked,
            launch_recipe=resolution.to_mapping() if resolution is not None else {},
            launch_recipe_status=resolution.status if resolution is not None else "",
            launch_recipe_fingerprint=resolution.fingerprint if resolution is not None else "",
            radio_role=self._radio_role,
            mode=next((key for key, button in self.source_buttons.items() if button.isChecked()), "managed"),
            ownership=str(self._field_widgets["ownership"].currentData() or "fio-managed"),
            settings_identity_mode=value("settings_identity_mode") or "isolated",
            variant=value("variant"),
            version=value("version"),
            writer_platform=self._writer_platform,
            writer_operation=self._writer_operation,
            host=value("host"),
            port=number("port"),
            udp_port=number("udp_port"),
            offset_hz=number("offset_hz"),
            secondary_port=number("secondary_port"),
            arq_port=number("arq_port"),
            rig_name=value("rig_name"),
            application_path=value("application_path"),
            secondary_application_path=value("secondary_application_path"),
            flmsg_application_path=value("flmsg_application_path"),
            flamp_application_path=value("flamp_application_path"),
            configuration_path=value("configuration_path"),
            secondary_configuration_path=value("secondary_configuration_path"),
            storage_path=value("storage_path"),
            secondary_storage_path=value("secondary_storage_path"),
            outbox_path=value("outbox_path"),
            launch_command=value("launch_command"),
            launch_at_startup=checked("launch_at_startup"),
            working_directory=value("working_directory"),
            vara_runtime_path=value("vara_runtime_path") or self._vara_runtime_path,
            vara_ini_path=value("vara_ini_path") or self._vara_ini_path,
            advanced_tx_requested=checked("advanced_tx_requested"),
            advanced_tx_acknowledged=checked("advanced_tx_acknowledged"),
            cluster_path=cluster_path,
            cluster_id=value("cluster_id"),
            cluster_name=value("cluster_name"),
            cluster_shared_database=value("cluster_shared_database"),
            cluster_instance_number=number("cluster_instance_number"),
            existing_standalone_node_id=int(
                self._varac_arrangement_metadata.get("existing_standalone_node_id", 0) or 0
            ) if existing_standalone_create else 0,
            existing_standalone_device_profile_id=int(
                self._varac_arrangement_metadata.get(
                    "existing_standalone_device_profile_id", 0
                )
                or 0
            ) if existing_standalone_create else 0,
            existing_standalone_member_number=int(
                self._varac_arrangement_metadata.get(
                    "existing_standalone_member_number", 0
                )
                or 0
            ) if existing_standalone_create else 0,
            email_gateway_sender_choice=email_sender_choice,
            email_gateway_sender_member_id=email_sender_member_id,
            # Preserve legacy compatibility evidence exactly; the new native
            # sender choice is independent and never infers this field.
            cluster_gateway=self._legacy_cluster_gateway,
            cluster_ptt_lock=checked("cluster_ptt_lock"),
            notes=value("notes"),
            imported_id=self._imported_id,
            imported_system_key=self._imported_system_key,
            replace_existing=bool(self._replacement_instance and self._replacement_confirmed),
            replacement_instance_id=(
                _int(self._replacement_instance.get("id"))
                if self._replacement_instance is not None else None
            ),
        )

    def set_operation_status(self, message: str, error: bool = False) -> None:
        """Show host operation feedback without implying that a scan or save occurred."""

        self.operation_status_label.setText(_text(message))
        self.operation_status_label.setProperty("error", bool(error))
        self.operation_status_label.style().unpolish(self.operation_status_label)
        self.operation_status_label.style().polish(self.operation_status_label)

    def validation(self) -> tuple[InstanceConflict, ...]:
        draft = self.draft()
        findings = list(instance_conflicts(draft, self._existing_instances))
        if self._native_varac_apply_required(draft):
            native = self._varac_native_presentation
            native_state = native.state.replace("_", " ").strip().lower()
            native_is_current = (
                not native.draft_fingerprint
                or native.draft_fingerprint == native_draft_fingerprint(draft.payload())
            )
            if native_state != "ready" or not native_is_current:
                findings.append(
                    InstanceConflict(
                        "varac_native_prepare_required",
                        "error",
                        "Prepare VarAC before Review & Save",
                        "Native VarAC configuration must be prepared and ready before CommsPlan can review the final apply plan.",
                    )
                )
            if not native.writer_qualified:
                findings.append(
                    InstanceConflict(
                        "varac_native_writer_unqualified",
                        "error",
                        "Manual VarAC configuration required",
                        "The detected VarAC writer is not exactly qualified. Review the operator action instead of applying native configuration.",
                    )
                )
        if draft.mode == "discover" and not self._discovery_selected:
            findings.append(
                InstanceConflict(
                    "discovery_selection_required",
                    "error",
                    "Choose a discovered configuration",
                    "Run Find existing configurations and select one result, or choose the new local or remote setup path.",
                )
            )
        if (
            draft.source_locked
            and draft.imported_id is not None
            and not self._inventory_snapshot.source_is_current(
                draft.family_key,
                draft.imported_id,
                draft.source_fingerprint,
            )
        ):
            findings.append(
                InstanceConflict(
                    "import_source_stale",
                    "error",
                    "Imported identity changed",
                    "Refresh discovery and review the complete source identity before applying it.",
                )
            )
        return tuple(findings)

    def _refresh_review_if_needed(self) -> None:
        if self._step == len(self.STEP_TITLES) - 1:
            self._refresh_review()

    def _refresh_review(self) -> None:
        self._resolve_launch_recipe()
        draft = self.draft()
        family = dict(SUPPORTED_INSTANCE_FAMILIES).get(draft.family_key, draft.family_key)
        radio = self.radio_combo.currentText()
        endpoint = f"{draft.host}:{draft.port}" if draft.port else f"{draft.host} (application-defined endpoint)"
        lines = [
            family,
            "",
            f"Name: {draft.instance_name or 'Not set'}",
            f"Radio: {radio}",
            f"Source: {draft.mode.replace('-', ' ').title()}",
            f"Ownership: {draft.ownership.replace('-', ' ').title()}",
        ]
        resolution = self._launch_recipe_resolution
        qualified_managed = bool(
            draft.mode == "managed"
            and resolution is not None
            and resolution.qualified
        )
        if self._replacement_instance is not None:
            old = self._replacement_instance
            old_name = _text(old.get("name") or old.get("instance_name")) or "existing instance"
            old_id = _int(old.get("id"))
            old_endpoint = _endpoint(old)
            old_detail = f"{old_name} (id {old_id})" if old_id else old_name
            if old_endpoint[0] and old_endpoint[1]:
                old_detail += f" · {old_endpoint[0]}:{old_endpoint[1]}"
            lines.extend(
                (
                    "Replacement comparison:",
                    f"Current assignment: {old_detail}",
                    f"Proposed assignment: {draft.instance_name or 'Not set'}",
                )
            )
        if draft.family_key == "js8call":
            lines.extend(
                (
                    "Settings: Existing default JS8Call setup" if draft.settings_identity_mode == "default" else "Settings: Separate JS8Call setup for this radio",
                    f"Variant/version: {draft.variant or 'Not verified'} · {draft.version or 'Not verified'}",
                    f"Rig name: {draft.rig_name}" if draft.settings_identity_mode == "isolated" and draft.rig_name else "Rig name: Not used (default JS8Call settings)" if draft.settings_identity_mode == "default" else "Rig name: Not set",
                    f"TCP API: {endpoint}",
                    f"UDP: {draft.host}:{draft.udp_port}" if draft.udp_port else "UDP: Not configured",
                    f"Default audio offset: {draft.offset_hz} Hz" if draft.offset_hz else "Default audio offset: Not configured",
                )
            )
            if not qualified_managed:
                lines.extend(
                    (
                        f"Application: {draft.application_path or 'Not set'}",
                        f"Settings profile: {draft.configuration_path or 'Not set'}",
                        f"Message data: {draft.storage_path or 'Not set'}",
                    )
                )
        elif draft.family_key == "fast_light":
            lines.append(
                "Settings: Existing default Fast Light setup"
                if draft.settings_identity_mode == "default"
                else "Settings: Separate Fast Light setup for this radio"
            )
            if draft.radio_role == "observer":
                lines.extend(
                    (
                        "Scope: Receive-only; FLRig, CAT, PTT, TX, and automatic send unavailable",
                        f"FLDigi endpoint: {draft.host}:{draft.secondary_port}" if draft.secondary_port else "FLDigi endpoint: Not configured",
                    )
                )
                if not qualified_managed:
                    lines.extend(
                        (
                            f"FLDigi application/config: {draft.secondary_application_path or 'Not set'} · {draft.secondary_configuration_path or 'Not set'}",
                            f"FLMsg/FLAmp: {draft.flmsg_application_path or 'Operator start / not selected'} · {draft.flamp_application_path or 'Operator start / not selected'}",
                            f"FLDigi logs/check-ins: {draft.storage_path or 'Not set'} · {draft.secondary_storage_path or 'Not set'}",
                        )
                    )
            else:
                lines.extend(
                    (
                        f"FLRig endpoint: {endpoint}",
                        f"FLDigi endpoint: {draft.host}:{draft.secondary_port}" if draft.secondary_port else "FLDigi endpoint: Not configured",
                        "Fast Light mode: Advanced TX requested"
                        if draft.advanced_tx_requested
                        else "Fast Light mode: Receive-safe",
                    )
                )
                if not qualified_managed:
                    lines.extend(
                        (
                            f"FLRig application/config: {draft.application_path or 'Not set'} · {draft.configuration_path or 'Not set'}",
                            f"FLDigi application/config: {draft.secondary_application_path or 'Not set'} · {draft.secondary_configuration_path or 'Not set'}",
                            f"FLMsg/FLAmp: {draft.flmsg_application_path or 'Operator start / not selected'} · {draft.flamp_application_path or 'Operator start / not selected'}",
                            f"FLDigi logs/check-ins: {draft.storage_path or 'Not set'} · {draft.secondary_storage_path or 'Not set'}",
                        )
                    )
        else:
            native = self._varac_native_presentation
            argv = tuple(native.launch_argv)
            executable = argv[0] if argv else draft.application_path
            arguments = argv[1:] if len(argv) > 1 else ()
            lines.extend(
                (
                    f"Executable: {executable or 'Not set'}",
                    f"Arguments: {list(arguments) if arguments else 'None'}",
                    f"VarAC INI: {draft.configuration_path or 'Not set'}",
                    f"Database: {draft.storage_path or 'Not set'}",
                    f"Incoming/outbox: {draft.secondary_storage_path or 'Not set'} · {draft.outbox_path or 'Not set'}",
                    f"Working directory: {draft.working_directory or 'Not set'}",
                    f"VARA runtime/INI: {draft.vara_runtime_path or 'Not set'} · {draft.vara_ini_path or 'Not set'}",
                    f"Cluster path: {draft.cluster_path.replace('_', ' ').title()}",
                    f"Cluster: {draft.cluster_id or draft.cluster_name or 'Not assigned'}"
                    + (f" · instance {draft.cluster_instance_number}" if draft.cluster_instance_number else ""),
                    "Email gateway sender: "
                    + {
                        "none": "No email gateway",
                        "existing_member": "Existing member",
                        "new_member": "New member",
                    }.get(draft.email_gateway_sender_choice, "Needs attention"),
                )
            )
        if draft.family_key in {"js8call", "fast_light"} and resolution is not None:
            lines.extend(
                (
                    "",
                    "Launch recipe",
                    f"Status: {resolution.status.replace('_', ' ').title()}",
                    f"Summary: {resolution.summary or 'No recipe summary available'}",
                    *self._launch_component_lines(resolution),
                )
            )
            if resolution.recovery_action:
                lines.append(f"Recovery: {resolution.recovery_action}")
            if resolution.raw_override_allowed:
                lines.append(
                    f"Advanced launch override: {draft.launch_command or 'Not set'}"
                )
        elif draft.family_key == "varac":
            native = self._varac_native_presentation
            argv = tuple(native.launch_argv)
            executable = argv[0] if argv else draft.application_path
            arguments = argv[1:] if len(argv) > 1 else ()
            lines.extend(
                (
                    "Launch recipe",
                    f"Executable: {executable or 'Not set'}",
                    f"Arguments: {list(arguments) if arguments else 'None'}",
                    f"Working directory: {draft.working_directory or 'Not set'}",
                    f"Environment: {dict(native.launch_environment) if native.launch_environment else 'None'}",
                    f"Launch at CommsPlan startup: {'Yes' if draft.launch_at_startup else 'No'}",
                )
            )
        else:
            lines.extend(
                (
                    f"Launch command: {draft.launch_command or 'Use configured application path'}",
                    f"Launch at CommsPlan startup: {'Yes' if draft.launch_at_startup else 'No'}",
                )
            )
        if draft.family_key == "varac" and self._varac_native_presentation.writer_qualified:
            lines.append("External configuration: qualified native writer will run only at outer Save Radio and Software.")
        else:
            lines.append(
                "External configuration: eligible for reviewed native apply"
                if draft.family_key == "js8call" and draft.variant and draft.version and draft.configuration_path
                else "External configuration: operator action required unless an exact supported writer is qualified"
            )
        self.review_label.setText("\n".join(lines))
        compact_lines = [
            family,
            f"Name: {draft.instance_name or 'Not set'}",
            f"Radio: {radio}",
            f"Source: {draft.mode.replace('-', ' ').title()}",
            f"Endpoint: {self._prepared_endpoint_summary(draft)}",
            "Review exact paths, commands, dependencies, and diagnostics with Show details.",
        ]
        if self._replacement_instance is not None:
            compact_lines.append(self._prepared_existing_impact(draft))
        self.review_summary_label.setText("\n".join(compact_lines))
        findings = self.validation()
        if findings:
            self.conflict_label.setText("Review findings:\n" + "\n".join(f"• {item.severity.title()}: {item.title} — {item.detail}" for item in findings))
        else:
            self.conflict_label.setText("No name, endpoint, or path conflicts found in the loaded inventory. Confirm before saving.")

    def _refresh(self) -> None:
        prior_page = self.pages.currentIndex()
        focus_widget = QApplication.focusWidget()
        same_page = prior_page == self._step
        scroll_value = (
            self.body_scroll.verticalScrollBar().value()
            if same_page else self._body_scroll_positions.get(self._step, 0)
        )
        # Files is the first page that presents recipe-owned paths. Resolve the
        # pure prepared recipe before that page is painted so a qualified
        # managed instance never appears as a blank path-entry task.
        if self._step in {4, 5, len(self.STEP_TITLES) - 1}:
            self._resolve_launch_recipe()
        self.pages.setCurrentIndex(self._step)
        self.step_label.setText(f"Step {self._step + 1} of {len(self.STEP_TITLES)} · {self.STEP_TITLES[self._step]}")
        self.back_button.setEnabled(self._step > 0)
        last_step = len(self.STEP_TITLES) - 1
        draft = self.draft()
        # Nested Software Administration returns a non-mutating canonical
        # bundle to Add Radio.  The parent owns the one final native apply.
        final_action = (
            "Save as draft"
            if self._unsaved_owner_key
            else "Save configuration"
            if self._editing_existing
            else "Add instance"
        )
        self.next_button.setText(final_action if self._step == last_step else "Next")
        blocked_for_radio = not self._selected_radio_id and not self._unsaved_owner_key
        blocked_for_replacement = self._replacement_instance is not None and not self._replacement_confirmed
        self.next_button.setEnabled(
            not blocked_for_radio
            and not blocked_for_replacement
            and not (self._step == last_step and any(item.severity == "error" for item in self.validation()))
        )
        theme = active_app_theme()
        for index, button in enumerate(self.step_buttons):
            current = index == self._step
            next_available = index == self._step + 1 and self.next_button.isEnabled()
            button.setChecked(current)
            button.setEnabled(index <= self._step or next_available)
            button.setStyleSheet(
                button_style(
                    "primary" if current else "secondary" if index < self._step else "muted",
                    theme,
                )
            )
        if self._step == 1:
            self._refresh_source()
        if self._step == last_step:
            self._refresh_review()
        self._update_prepared_presentation()
        self._restore_body_view_state(scroll_value, focus_widget if same_page else None)

    def _select_step(self, target: int) -> None:
        """Navigate through the visible step strip without skipping gates."""

        index = int(target)
        if index < 0 or index >= len(self.STEP_TITLES):
            return
        if index == self._step:
            self._refresh()
            return
        if index > self._step + 1:
            return
        if index == self._step + 1 and not self.next_button.isEnabled():
            return
        if self._step == 2 and index > self._step:
            self._apply_identity_defaults()
        self._remember_body_scroll_position()
        self._step = index
        self._refresh()

    def _next(self) -> None:
        if self._step < len(self.STEP_TITLES) - 1:
            if self._step == 2:
                self._apply_identity_defaults()
            self._remember_body_scroll_position()
            self._step += 1
            self._refresh()
            return
        draft = self.draft()
        payload = draft.payload()
        native_apply_required = self._native_varac_apply_required(draft)
        native_worker_payload: dict[str, Any] | None = None
        if native_apply_required:
            native_worker_payload = self.varac_native_worker_payload()
            payload = dict(native_worker_payload["draft"])
            # Keep the exact host worker request beside the draft so the outer
            # Add Radio save can apply the already-prepared plan at its final
            # transaction boundary.  It is transient and must be popped by
            # the persistence owner; no native writer runs here.
            payload["_varac_native_apply_request"] = native_worker_payload
        self.validation_requested.emit(payload)
        if not any(item.severity == "error" for item in self.validation()):
            # Software Administration is a draft editor.  It returns the
            # prepared canonical bundle to Add Radio and never invokes the
            # native writer.  Final native apply belongs exclusively to the
            # outer Add Radio Save Radio and Software transaction.
            self.completed.emit(payload)

    def _back(self) -> None:
        if self._step > 0:
            self._remember_body_scroll_position()
            self._step -= 1
            self._refresh()


__all__ = [
    "InstanceConflict",
    "SUPPORTED_INSTANCE_FAMILIES",
    "SoftwareInstanceAssistant",
    "SoftwareInstanceDraft",
    "VarACNativePresentation",
    "instance_conflicts",
    "normalize_instance_draft",
]
