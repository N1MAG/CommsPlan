from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping

from freqinout.core.commstat_config import load_commstat_group_state
from freqinout.core.js8_storage import canonicalize_storage_path, resolve_js8_storage
from freqinout.core.protocol_capabilities import capabilities_dict_for, provenance_hint_for, scope_hint_for


_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _hearing_configuration_key(profile):
    from freqinout.core.subspace_hearing import hearing_configuration_key
    return hearing_configuration_key(profile)


def normalize_ingest_path(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return str(Path(raw).expanduser().resolve())
    except Exception:
        return str(Path(raw).expanduser())


def stable_source_id(*parts: object, prefix: str = "source") -> str:
    normalized = "|".join(str(part or "").strip() for part in parts if str(part or "").strip())
    if not normalized:
        normalized = "default"
    readable = _SAFE_ID_RE.sub("_", normalized).strip("_.-").lower()
    readable = readable[:48] if readable else "default"
    digest = hashlib.sha1(normalized.encode("utf-8", errors="ignore")).hexdigest()[:10]
    return f"{prefix}_{readable}_{digest}"


@dataclass(frozen=True)
class AppInstanceDescriptor:
    source_id: str
    family: str
    label: str
    radio_id: str = ""
    enabled: bool = True
    api_host: str = ""
    api_port: int = 0
    paths: Mapping[str, str] = field(default_factory=dict)
    db_path: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)
    capabilities: Mapping[str, bool] = field(default_factory=dict)
    provenance: str = ""
    scope_hint: str = ""

    def __post_init__(self) -> None:
        if not self.capabilities:
            object.__setattr__(self, "capabilities", capabilities_dict_for(self.family, metadata=self.metadata))
        if not self.provenance:
            object.__setattr__(self, "provenance", provenance_hint_for(self.family, self.metadata))
        if not self.scope_hint:
            object.__setattr__(self, "scope_hint", scope_hint_for(self.family, self.metadata))


@dataclass(frozen=True)
class IngestSourceDescriptor:
    source_id: str
    family: str
    source_type: str
    label: str
    app_instance_id: str = ""
    radio_id: str = ""
    path: str = ""
    endpoint: str = ""
    checkpoint_key: str = ""
    enabled: bool = True
    metadata: Mapping[str, object] = field(default_factory=dict)
    capabilities: Mapping[str, bool] = field(default_factory=dict)
    provenance: str = ""
    scope_hint: str = ""

    def __post_init__(self) -> None:
        if not self.capabilities:
            object.__setattr__(
                self,
                "capabilities",
                capabilities_dict_for(self.family, self.source_type, self.metadata),
            )
        if not self.provenance:
            object.__setattr__(self, "provenance", provenance_hint_for(self.family, self.metadata))
        if not self.scope_hint:
            object.__setattr__(self, "scope_hint", scope_hint_for(self.family, self.metadata))


@dataclass(frozen=True)
class IngestSourceInventory:
    app_instances: tuple[AppInstanceDescriptor, ...] = ()
    ingest_sources: tuple[IngestSourceDescriptor, ...] = ()

    def sources_for_family(self, family: str) -> tuple[IngestSourceDescriptor, ...]:
        wanted = str(family or "").strip().lower()
        return tuple(source for source in self.ingest_sources if source.family == wanted)


def source_checkpoint_key(source: IngestSourceDescriptor) -> str:
    if source.checkpoint_key:
        return source.checkpoint_key
    identity = "|".join(
        (
            source.family,
            source.source_type,
            source.source_id,
            source.path,
            source.endpoint,
        )
    )
    return f"ingest_checkpoint_{hashlib.sha1(identity.encode('utf-8', errors='ignore')).hexdigest()[:16]}"


def app_instance_from_device_profile(profile: Mapping[str, Any], family: str) -> AppInstanceDescriptor | None:
    family_key = str(family or "").strip().lower()
    radio_id = str(profile.get("id", "") or profile.get("system_key", "") or "").strip()
    radio_name = str(profile.get("name", "") or radio_id or "Radio").strip()
    if family_key == "js8call":
        if not _truthy(profile.get("use_js8call", False), False) and not _truthy(profile.get("use_js8spotter", False), False):
            return None
        host = str(profile.get("js8_host", "") or "").strip() or "127.0.0.1"
        port = _int_or_zero(profile.get("js8_port")) or 2442
        storage = resolve_js8_storage(profile)
        legacy_directed = normalize_ingest_path(profile.get("js8_directed_path", ""))
        explicit_inbox = _first_normalized_path(profile, ("js8_inbox_path", "inbox_path", "js8call_inbox_path"))
        modern_explicit_root = bool(
            str(profile.get("application_data_root", profile.get("js8_message_storage_root", "")) or "").strip()
        )
        # Existing device profiles explicitly selected these paths before FIO
        # learned modern Qt storage namespaces.  They remain authoritative
        # until the operator saves a modern application-data root; ambient
        # files elsewhere on the machine must not silently redirect them.
        legacy_explicit = bool(legacy_directed or explicit_inbox) and not modern_explicit_root
        directed = legacy_directed if legacy_explicit and legacy_directed else normalize_ingest_path(storage.directed_path)
        all_path = (
            str(Path(legacy_directed).with_name("ALL.TXT")) if legacy_directed else ""
        ) if legacy_explicit else normalize_ingest_path(storage.all_path)
        if legacy_explicit and explicit_inbox:
            inbox = explicit_inbox
        elif legacy_explicit:
            legacy_inbox = Path(legacy_directed).with_name("inbox.db3") if legacy_directed else None
            inbox = str(legacy_inbox) if legacy_inbox is not None and legacy_inbox.is_file() else ""
        elif storage.verified:
            inbox = normalize_ingest_path(storage.inbox_path)
        elif explicit_inbox:
            inbox = explicit_inbox
        elif legacy_directed:
            # A legacy profile explicitly owns its text-log path but says
            # nothing about the modern Qt data root.  Do not invent an inbox
            # source beside a platform candidate until that root is verified.
            inbox = ""
        else:
            inbox = normalize_ingest_path(storage.inbox_path)
        effective_storage_mode = "rig_scoped" if legacy_explicit else storage.storage_mode
        effective_storage_verified = True if legacy_explicit else storage.verified
        effective_storage_evidence = "legacy_explicit_path" if legacy_explicit else storage.evidence
        instance_token = str(profile.get("js8_instance_id", "") or radio_id or radio_name)
        return AppInstanceDescriptor(
            source_id=stable_source_id("js8call", instance_token, radio_id, host, port, directed, prefix="app"),
            family="js8call",
            label=f"{radio_name} JS8Call",
            radio_id=radio_id,
            enabled=True,
            api_host=host,
            api_port=port,
            paths={
                "directed": directed,
                "all": all_path,
                "inbox": inbox,
                "data_root": storage.data_root,
                "save": storage.save_dir,
            },
            metadata={
                "profile": radio_name,
                "js8_instance_id": instance_token,
                "hearing_configuration_key": _hearing_configuration_key(profile),
                "variant_family": storage.variant_family,
                "variant_version": storage.variant_version,
                "rig_name": storage.rig_name,
                "rig_name_source": storage.rig_name_source,
                "storage_mode": effective_storage_mode,
                "expected_storage_mode": storage.expected_mode,
                "storage_verified": effective_storage_verified,
                "storage_evidence": effective_storage_evidence,
                "storage_display_state": "Isolated · legacy path" if legacy_explicit else storage.display_state,
                "application_data_root": storage.data_root,
            },
        )
    if family_key == "varac":
        if not _truthy(profile.get("use_varac", False), False):
            return None
        db_path = normalize_ingest_path(profile.get("varac_db_path", ""))
        if not db_path:
            return None
        return AppInstanceDescriptor(
            source_id=stable_source_id("varac", radio_id or radio_name, db_path, prefix="app"),
            family="varac",
            label=f"{radio_name} VarAC",
            radio_id=radio_id,
            enabled=True,
            db_path=db_path,
            metadata={"profile": radio_name},
        )
    if family_key == "commstat":
        if not _truthy(profile.get("use_commstat", False), False):
            return None
        launch_path = normalize_ingest_path(profile.get("commstat_launch_path", "") or profile.get("path_commstat", ""))
        settings_view = dict(profile)
        if launch_path and not settings_view.get("path_commstat"):
            settings_view["path_commstat"] = launch_path
        group_state = load_commstat_group_state(settings_view)
        db_path = normalize_ingest_path(group_state.db_path or "")
        config_path = normalize_ingest_path(group_state.config_path or "")
        if not launch_path and not db_path and not config_path:
            return None
        return AppInstanceDescriptor(
            source_id=stable_source_id("commstat", radio_id or radio_name, launch_path, db_path, prefix="app"),
            family="commstat",
            label=f"{radio_name} CommStat",
            radio_id=radio_id,
            enabled=True,
            paths={"launch": launch_path, "config": config_path},
            db_path=db_path,
            metadata={
                "profile": radio_name,
                "configured_groups": tuple(sorted(group_state.configured_groups)),
                "active_groups": tuple(sorted(group_state.active_groups)),
                "show_other_groups": group_state.show_other_groups,
            },
        )
    return None


def js8_ingest_sources(instance: AppInstanceDescriptor) -> tuple[IngestSourceDescriptor, ...]:
    if instance.family != "js8call":
        return ()
    sources: list[IngestSourceDescriptor] = []
    instance_metadata = dict(instance.metadata or {})
    storage_mode = str(instance_metadata.get("storage_mode", "unverified") or "unverified")
    storage_verified = bool(instance_metadata.get("storage_verified", False))
    attributable = storage_mode == "rig_scoped" and storage_verified
    file_radio_id = instance.radio_id if attributable else ""
    file_instance_id = instance.source_id if attributable else ""
    for role in ("directed", "all"):
        path = normalize_ingest_path(instance.paths.get(role, ""))
        if not path:
            continue
        source_id = stable_source_id(instance.source_id, role, path, prefix="ingest")
        sources.append(
            IngestSourceDescriptor(
                source_id=source_id,
                family="js8call",
                source_type="file",
                label=f"{instance.label} {role.upper()}",
                app_instance_id=file_instance_id,
                radio_id=file_radio_id,
                path=path,
                checkpoint_key=f"{source_id}_offset",
                enabled=instance.enabled,
                metadata={
                    "role": role,
                    "storage_mode": storage_mode,
                    "storage_verified": storage_verified,
                    "application_data_root": str(instance.paths.get("data_root", "") or ""),
                    "candidate_app_instance_ids": (instance.source_id,),
                    "candidate_radio_ids": (instance.radio_id,) if instance.radio_id else (),
                    "js8_instance_id": str(instance_metadata.get("js8_instance_id", "") or ""),
                },
            )
        )
    inbox_path = normalize_ingest_path(instance.paths.get("inbox", ""))
    if inbox_path:
        source_id = stable_source_id(instance.source_id, "inbox", inbox_path, prefix="ingest")
        sources.append(
            IngestSourceDescriptor(
                source_id=source_id,
                family="js8call",
                source_type="sqlite",
                label=f"{instance.label} Inbox",
                app_instance_id=file_instance_id,
                radio_id=file_radio_id,
                path=inbox_path,
                checkpoint_key=f"{source_id}_last_id",
                enabled=instance.enabled,
                metadata={
                    "role": "inbox",
                    "storage_mode": storage_mode,
                    "storage_verified": storage_verified,
                    "application_data_root": str(instance.paths.get("data_root", "") or ""),
                    "candidate_app_instance_ids": (instance.source_id,),
                    "candidate_radio_ids": (instance.radio_id,) if instance.radio_id else (),
                    "js8_instance_id": str(instance_metadata.get("js8_instance_id", "") or ""),
                },
            )
        )
    if instance.api_host and instance.api_port:
        endpoint = f"{instance.api_host}:{instance.api_port}"
        source_id = stable_source_id(instance.source_id, "api", endpoint, prefix="ingest")
        sources.append(
            IngestSourceDescriptor(
                source_id=source_id,
                family="js8call",
                source_type="api",
                label=f"{instance.label} API",
                app_instance_id=instance.source_id,
                radio_id=instance.radio_id,
                endpoint=endpoint,
                checkpoint_key=f"{source_id}_last_id",
                enabled=instance.enabled,
                metadata={"role": "api", "js8_instance_id": str(instance_metadata.get("js8_instance_id", "") or ""),
                          "hearing_configuration_key": str(instance_metadata.get("hearing_configuration_key", "") or "")},
            )
        )
    return tuple(sources)


def file_message_sources_from_device_profile(profile: Mapping[str, Any]) -> tuple[IngestSourceDescriptor, ...]:
    radio_id = str(profile.get("id", "") or profile.get("system_key", "") or "").strip()
    radio_name = str(profile.get("name", "") or radio_id or "Radio").strip()
    specs = (
        ("flmsg", "use_flmsg", "flmsg_message_path"),
        ("flamp", "use_flamp", "flamp_message_path"),
    )
    sources: list[IngestSourceDescriptor] = []
    for family, enabled_key, path_key in specs:
        if not _truthy(profile.get(enabled_key, False), False):
            continue
        path = normalize_ingest_path(profile.get(path_key, ""))
        if not path:
            continue
        source_id = stable_source_id(family, radio_id or radio_name, path, prefix="ingest")
        sources.append(
            IngestSourceDescriptor(
                source_id=source_id,
                family=family,
                source_type="directory",
                label=f"{radio_name} {family.upper()} Messages",
                radio_id=radio_id,
                path=path,
                checkpoint_key=f"{source_id}_fingerprint",
                enabled=True,
            )
        )
    return tuple(sources)


def varac_ingest_sources(instance: AppInstanceDescriptor) -> tuple[IngestSourceDescriptor, ...]:
    if instance.family != "varac" or not instance.db_path:
        return ()
    source_id = stable_source_id(instance.source_id, "sqlite", instance.db_path, prefix="ingest")
    return (
        IngestSourceDescriptor(
            source_id=source_id,
            family="varac",
            source_type="sqlite",
            label=f"{instance.label} DB",
            app_instance_id=instance.source_id,
            radio_id=instance.radio_id,
            path=instance.db_path,
            checkpoint_key=f"{source_id}_last_id",
            enabled=instance.enabled,
        ),
    )


def commstat_ingest_sources(instance: AppInstanceDescriptor) -> tuple[IngestSourceDescriptor, ...]:
    if instance.family != "commstat" or not instance.db_path:
        return ()
    source_id = stable_source_id(instance.source_id, "sqlite", instance.db_path, prefix="ingest")
    return (
        IngestSourceDescriptor(
            source_id=source_id,
            family="commstat",
            source_type="sqlite",
            label=f"{instance.label} DB",
            app_instance_id=instance.source_id,
            radio_id=instance.radio_id,
            path=instance.db_path,
            checkpoint_key=f"{source_id}_last_id",
            enabled=instance.enabled,
            metadata={
                "configured_groups": instance.metadata.get("configured_groups", ()),
                "active_groups": instance.metadata.get("active_groups", ()),
                "show_other_groups": instance.metadata.get("show_other_groups", False),
            },
        ),
    )


def dedupe_ingest_sources(sources: Iterable[IngestSourceDescriptor]) -> tuple[IngestSourceDescriptor, ...]:
    seen: set[tuple[str, str, str, str]] = set()
    out: list[IngestSourceDescriptor] = []
    for source in sources:
        key = (
            source.family,
            source.source_type,
            normalize_ingest_path(source.path),
            source.endpoint.strip().lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(source)
    return tuple(out)


def build_ingest_source_inventory(profiles: Iterable[Mapping[str, Any]]) -> IngestSourceInventory:
    app_instances: list[AppInstanceDescriptor] = []
    ingest_sources: list[IngestSourceDescriptor] = []
    for profile in profiles:
        js8_instance = app_instance_from_device_profile(profile, "js8call")
        if js8_instance is not None:
            app_instances.append(js8_instance)
            ingest_sources.extend(js8_ingest_sources(js8_instance))
        varac_instance = app_instance_from_device_profile(profile, "varac")
        if varac_instance is not None:
            app_instances.append(varac_instance)
            ingest_sources.extend(varac_ingest_sources(varac_instance))
        commstat_instance = app_instance_from_device_profile(profile, "commstat")
        if commstat_instance is not None:
            app_instances.append(commstat_instance)
            ingest_sources.extend(commstat_ingest_sources(commstat_instance))
        ingest_sources.extend(file_message_sources_from_device_profile(profile))
    ingest_sources = list(_coalesce_js8_storage_sources(ingest_sources))
    return IngestSourceInventory(
        app_instances=tuple(app_instances),
        ingest_sources=dedupe_ingest_sources(ingest_sources),
    )


def _coalesce_js8_storage_sources(
    sources: Iterable[IngestSourceDescriptor],
) -> tuple[IngestSourceDescriptor, ...]:
    """Represent each JS8 file once and never invent shared-root attribution."""

    passthrough: list[IngestSourceDescriptor] = []
    grouped: dict[tuple[str, str], list[IngestSourceDescriptor]] = {}
    for source in sources:
        if source.family != "js8call" or source.source_type not in {"file", "sqlite"} or not source.path:
            passthrough.append(source)
            continue
        role = str((source.metadata or {}).get("role", "") or source.source_type)
        canonical = canonicalize_storage_path(source.path)
        grouped.setdefault((role, canonical.casefold()), []).append(source)

    for (role, _canonical_key), rows in grouped.items():
        first = rows[0]
        metadata_rows = [dict(row.metadata or {}) for row in rows]
        shared = len(rows) > 1 or any(str(meta.get("storage_mode", "")) == "shared" for meta in metadata_rows)
        verified_isolated = (
            len(rows) == 1
            and str(metadata_rows[0].get("storage_mode", "")) == "rig_scoped"
            and bool(metadata_rows[0].get("storage_verified", False))
        )
        if verified_isolated:
            passthrough.append(first)
            continue
        app_ids = tuple(
            dict.fromkeys(
                value
                for row, meta in zip(rows, metadata_rows)
                for value in (
                    *(str(item) for item in meta.get("candidate_app_instance_ids", ()) if str(item or "")),
                    str(row.app_instance_id or ""),
                )
                if value
            )
        )
        radio_ids = tuple(
            dict.fromkeys(
                value
                for row, meta in zip(rows, metadata_rows)
                for value in (
                    *(str(item) for item in meta.get("candidate_radio_ids", ()) if str(item or "")),
                    str(row.radio_id or ""),
                )
                if value
            )
        )
        mode = "shared" if shared else "unverified"
        source_id = stable_source_id("js8call", mode, role, first.path, prefix="ingest")
        metadata = dict(metadata_rows[0])
        metadata.update(
            {
                "role": role,
                "storage_mode": mode,
                "storage_verified": False,
                "candidate_app_instance_ids": app_ids,
                "candidate_radio_ids": radio_ids,
                "attribution": "shared" if shared else "unverified",
                "js8_instance_id": "",
            }
        )
        passthrough.append(
            replace(
                first,
                source_id=source_id,
                label=f"{'Shared JS8 storage' if shared else 'JS8 source unverified'} {role.upper()}",
                app_instance_id="",
                radio_id="",
                checkpoint_key=f"{source_id}_{'last_id' if first.source_type == 'sqlite' else 'offset'}",
                metadata=metadata,
                provenance="shared JS8 storage" if shared else "JS8 source unverified",
                scope_hint="shared" if shared else "unverified",
            )
        )
    return tuple(passthrough)


def js8_api_endpoint_collisions(inventory: IngestSourceInventory) -> dict[str, tuple[str, ...]]:
    labels_by_endpoint: dict[str, list[str]] = {}
    for instance in inventory.app_instances:
        if instance.family != "js8call" or not instance.enabled:
            continue
        host = str(instance.api_host or "").strip() or "127.0.0.1"
        port = _int_or_zero(instance.api_port) or 2442
        endpoint = f"{host.lower()}:{port}"
        label = str(instance.label or instance.radio_id or instance.source_id or endpoint).strip()
        labels_by_endpoint.setdefault(endpoint, []).append(label)
    return {
        endpoint: tuple(labels)
        for endpoint, labels in labels_by_endpoint.items()
        if len(labels) > 1
    }


def _truthy(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    txt = str(value or "").strip().lower()
    if txt in {"1", "true", "yes", "on", "enabled"}:
        return True
    if txt in {"0", "false", "no", "off", "disabled"}:
        return False
    return bool(default)


def _int_or_zero(value: object) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def _first_normalized_path(profile: Mapping[str, Any], keys: Iterable[str]) -> str:
    for key in keys:
        path = normalize_ingest_path(profile.get(key, ""))
        if path:
            return path
    return ""
