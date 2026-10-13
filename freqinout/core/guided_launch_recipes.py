"""Pure managed launch recipes for unsaved guided software drafts.

The resolver performs no filesystem, process, socket, or persistence work.  It
turns one already-reviewed distinct draft into an exact component plan that is
used by Add Radio, Software Administration, Review, and atomic persistence.
Unknown application families/versions fail to an explicit operator action
instead of inventing a launch command.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import ntpath
import os
from pathlib import Path
import posixpath
import re
import shlex
from typing import Any, Mapping, Sequence

from freqinout.core.js8_storage import (
    js8_application_name,
    native_managed_rig_name,
    normalize_variant_family,
    normalize_rig_name,
    qt_config_path_candidates,
    qt_data_root_candidates,
)
from freqinout.core.managed_directory_contract import parent_directory_text


_KNOWN_JS8_VERSIONS = {
    "js8call_2_2": "2.2.0",
    "js8call_improved_3_0_3": "3.0.3",
    "js8call_subspace_4_1": "4.1.0.478",
}
_KEY_RE = re.compile(r"[^a-z0-9_.-]+")
SETTINGS_IDENTITY_DEFAULT = "default"
SETTINGS_IDENTITY_ISOLATED = "isolated"


def _text(value: object) -> str:
    return str(value or "").strip()


def _key(value: object) -> str:
    return _KEY_RE.sub("-", _text(value).casefold()).strip("-._")


def _join(root: str, *parts: str) -> str:
    joiner = ntpath if "\\" in root and "/" not in root else posixpath
    return joiner.join(root, *parts)


def _native_identity_child(draft: Mapping[str, Any]) -> str:
    """Return the portable radio-name child for native Fast Light data.

    FIO's opaque draft/application keys belong in persistence and manifests,
    never in an operator-facing application directory.  Display labels may
    contain spaces, while the native path segment uses a conservative portable
    spelling so it remains easy to type on Linux, macOS, and Windows.
    """

    label = re.sub(
        r"[^A-Za-z0-9_.-]+",
        "-",
        _text(draft.get("owner_label") or draft.get("instance_name") or "Radio"),
    ).strip("-._")[:48] or "Radio"
    return label


def settings_identity_mode_from_mapping(
    draft: Mapping[str, Any],
    *,
    family_key: object | None = None,
) -> str:
    """Return the explicit or safely inferred native settings identity.

    Older records predate this field, so inference is intentionally narrow:
    migrated application keys and complete structured recipes are
    authoritative. New or incomplete drafts remain isolated by default.
    """

    explicit = _key(
        draft.get("settings_identity_mode")
        or draft.get("native_settings_identity")
        or draft.get("profile_identity_mode")
    )
    if explicit in {SETTINGS_IDENTITY_DEFAULT, SETTINGS_IDENTITY_ISOLATED}:
        return explicit
    family = _key(family_key or draft.get("family_key"))
    system_key = _key(
        draft.get("application_system_key")
        or draft.get("imported_system_key")
        or draft.get("system_key")
    )
    if family == "js8call" and system_key == "default_js8_instance":
        return SETTINGS_IDENTITY_DEFAULT
    if family == "fast_light" and system_key == "default_fast_light":
        return SETTINGS_IDENTITY_DEFAULT
    recipe = draft.get("launch_recipe")
    if not isinstance(recipe, Mapping):
        return SETTINGS_IDENTITY_ISOLATED
    components = tuple(
        item for item in recipe.get("components", ()) if isinstance(item, Mapping)
    )
    if not components:
        return SETTINGS_IDENTITY_ISOLATED
    if family == "js8call":
        js8 = next(
            (item for item in components if _key(item.get("component_key")) == "js8call"),
            None,
        )
        if js8 is not None:
            arguments = tuple(_text(item) for item in js8.get("arguments", ()) or ())
            has_selector = any(
                item in {"-r", "--rig-name"}
                or item.startswith("--rig-name=")
                or item.startswith("-r=")
                for item in arguments
            )
            return SETTINGS_IDENTITY_ISOLATED if has_selector else SETTINGS_IDENTITY_DEFAULT
    if family == "fast_light":
        relevant = tuple(
            item
            for item in components
            if _key(item.get("component_key")) in {"flrig", "fldigi", "flmsg", "flamp"}
        )
        if relevant:
            selector_options = {"--config-dir", "--flmsg-dir"}
            has_selector = any(
                any(
                    argument in selector_options
                    or any(argument.startswith(f"{option}=") for option in selector_options)
                    for argument in (
                        _text(value) for value in component.get("arguments", ()) or ()
                    )
                )
                for component in relevant
            )
            return SETTINGS_IDENTITY_ISOLATED if has_selector else SETTINGS_IDENTITY_DEFAULT
    return SETTINGS_IDENTITY_ISOLATED


def managed_instance_window_title(application_name: object, radio_label: object) -> str:
    """Return the operator-facing title for one radio-owned child process.

    Window titles are presentation metadata, never instance selectors.  Keep
    the radio's human label (including spaces) while removing control
    characters that do not belong in a native title bar or process argv.
    """

    application = re.sub(r"[\x00-\x1f\x7f]+", " ", _text(application_name))
    label = re.sub(r"[\x00-\x1f\x7f]+", " ", _text(radio_label))
    application = " ".join(application.split())[:48] or "Application"
    label = " ".join(label.split())[:80] or "Radio"
    return f"{application} — {label}"


def _fast_light_native_roots(
    draft: Mapping[str, Any],
    *,
    platform: object | None = None,
    storage_home: Path | None = None,
) -> dict[str, str]:
    """Derive application-native Fast Light roots without probing or writing.

    Explicit reviewed native bases win.  Otherwise FIO uses the conventional
    FLRig, FLDigi, and NBEMS locations for the platform.  These paths remain
    usable and discoverable if the operator later stops using FIO.
    """

    system = str(platform or os.sys.platform or "").strip().casefold()
    home = Path(storage_home) if storage_home is not None else Path.home()
    child = _native_identity_child(draft)

    flrig_base = Path(
        _text(draft.get("flrig_native_base")) or str(home / ".flrig")
    ).expanduser()
    fldigi_base = Path(
        _text(draft.get("fldigi_native_base")) or str(home / ".fldigi")
    ).expanduser()
    default_nbems = home / "NBEMS.files" if system in {"windows", "win", "win32", "cygwin"} else home / ".nbems"
    nbems_base = Path(
        _text(draft.get("nbems_native_base")) or str(default_nbems)
    ).expanduser()

    # A reviewed explicit root is retained; normal preparation derives one
    # collision-resistant child below the familiar application directory.
    flrig_root = Path(_text(draft.get("flrig_native_root")) or flrig_base / "instances" / child)
    fldigi_root = Path(_text(draft.get("fldigi_native_root")) or fldigi_base / "instances" / child)
    flmsg_root = Path(_text(draft.get("flmsg_native_root")) or nbems_base / "instances" / child)
    # FLMsg and FLAmp share the radio's familiar NBEMS instance root.  Their
    # executable installations remain shared, while native state and traffic
    # stay attributable to the selected radio.
    flamp_root = Path(_text(draft.get("flamp_native_root")) or flmsg_root)
    flamp_receive = Path(
        _text(draft.get("flamp_receive_path")) or flamp_root / "FLAMP" / "rx"
    )
    flamp_outgoing = Path(
        _text(draft.get("flamp_outgoing_path")) or flamp_root / "FLAMP" / "tx"
    )
    return {
        "flrig_root": str(flrig_root),
        "fldigi_root": str(fldigi_root),
        "fldigi_logs": str(fldigi_root / "logs"),
        "flmsg_root": str(flmsg_root),
        "flmsg_messages": str(flmsg_root / "ICS" / "messages"),
        "flmsg_templates": str(flmsg_root / "ICS" / "templates"),
        "flmsg_auto": str(flmsg_root / "WRAP" / "auto"),
        "flamp_root": str(flamp_root),
        "flamp_receive": str(flamp_receive),
        "flamp_outgoing": str(flamp_outgoing),
        "nbems_base": str(nbems_base),
    }


def _fast_light_default_roots(
    draft: Mapping[str, Any],
    *,
    platform: object | None = None,
    storage_home: Path | None = None,
) -> dict[str, str]:
    """Describe application default roots without granting write ownership."""

    system = str(platform or os.sys.platform or "").strip().casefold()
    home = Path(storage_home) if storage_home is not None else Path.home()
    flrig_root = Path(
        _text(draft.get("configuration_path") or draft.get("flrig_native_root"))
        or home / ".flrig"
    ).expanduser()
    fldigi_root = Path(
        _text(draft.get("secondary_configuration_path") or draft.get("fldigi_native_root"))
        or home / ".fldigi"
    ).expanduser()
    default_nbems = home / "NBEMS.files" if system in {"windows", "win", "win32", "cygwin"} else home / ".nbems"
    nbems_root = Path(
        _text(draft.get("flmsg_native_root") or draft.get("nbems_native_base"))
        or default_nbems
    ).expanduser()
    logs = Path(_text(draft.get("storage_path")) or fldigi_root / "logs").expanduser()
    checkins = Path(
        _text(draft.get("secondary_storage_path")) or nbems_root / "WRAP" / "auto"
    ).expanduser()
    return {
        "flrig_root": str(flrig_root),
        "fldigi_root": str(fldigi_root),
        "fldigi_logs": str(logs),
        "flmsg_root": str(nbems_root),
        "flmsg_messages": str(nbems_root / "ICS" / "messages"),
        "flmsg_templates": str(nbems_root / "ICS" / "templates"),
        "flmsg_auto": str(checkins),
        "flamp_root": str(nbems_root),
        "flamp_receive": str(nbems_root / "FLAMP" / "rx"),
        "flamp_outgoing": str(nbems_root / "FLAMP" / "tx"),
        "nbems_base": str(nbems_root),
    }


def _command_text(values: Sequence[str]) -> str:
    return shlex.join(tuple(_text(value) for value in values if _text(value)))


def _scope(draft: Mapping[str, Any]) -> str:
    return "receive_only" if _text(draft.get("radio_role")).casefold() == "observer" else "standard"


def _js8_variant_hint(draft: Mapping[str, Any], executable: str) -> str:
    """Use explicit identity first, then only an app-specific executable name.

    A JS8 executable name is enough to select the reviewed launch-argument
    family, but never enough to invent an exact version.  An arbitrary Browse
    target therefore remains launch-pending instead of being treated as safe
    merely because the version field is blank.
    """

    explicit = _text(draft.get("variant"))
    if explicit:
        return explicit
    basename = ntpath.basename(executable.replace("/", "\\")).casefold()
    if "js8call" not in basename:
        return ""
    if "subspace" in basename:
        return "js8call_subspace_4_1"
    if "improved" in basename:
        return "js8call_improved_3_0_3"
    return "js8call_2_2"


def canonical_js8_version(variant: object, version: object) -> str:
    """Return the exact writer/recipe version represented by discovered evidence."""

    normalized = normalize_variant_family(variant, version)
    expected = _KNOWN_JS8_VERSIONS.get(normalized, "")
    observed = _text(version)
    if not expected or not observed:
        return ""
    pattern = rf"(?<![0-9]){re.escape(expected)}(?![0-9])"
    return expected if re.search(pattern, observed) else ""


@dataclass(frozen=True)
class GuidedLaunchComponent:
    component_key: str
    label: str
    executable: str
    arguments: tuple[str, ...] = ()
    working_directory: str = ""
    dependencies: tuple[str, ...] = ()
    profile_selector: str = ""
    configuration_roots: tuple[str, ...] = ()
    data_roots: tuple[str, ...] = ()
    # Exact directory targets this FIO-managed component authorizes FIO to
    # create.  Configuration/data roots can also be files or shared paths and
    # therefore must never be inferred as mkdir targets by consumers.
    managed_directories: tuple[str, ...] = ()
    endpoints: tuple[Mapping[str, Any], ...] = ()
    readiness: Mapping[str, Any] = field(default_factory=dict)
    execution_scope: str = "standard"
    launch_at_startup: bool = False
    operator_starts: bool = False
    # Retain discovery provenance beside the generated launch facts so audit
    # and first-launch reconciliation do not have to reconstruct it.  These
    # fields are last to preserve positional construction compatibility.
    evidence: Mapping[str, Any] = field(default_factory=dict)
    confidence: str = "verified"

    @property
    def effective_command(self) -> tuple[str, ...]:
        return (self.executable, *self.arguments) if self.executable and not self.operator_starts else ()

    @property
    def effective_command_text(self) -> str:
        return _command_text(self.effective_command)

    def to_mapping(self) -> dict[str, Any]:
        value = {
            "component_key": self.component_key,
            "label": self.label,
            "executable": self.executable,
            "arguments": list(self.arguments),
            "effective_command": list(self.effective_command),
            "effective_command_text": self.effective_command_text,
            "working_directory": self.working_directory,
            "dependencies": list(self.dependencies),
            "profile_selector": self.profile_selector,
            "configuration_roots": list(self.configuration_roots),
            "data_roots": list(self.data_roots),
            "endpoints": [dict(item) for item in self.endpoints],
            "readiness": dict(self.readiness),
            "evidence": dict(self.evidence),
            "confidence": self.confidence,
            "execution_scope": self.execution_scope,
            "launch_at_startup": self.launch_at_startup,
            "operator_starts": self.operator_starts,
        }
        # Omit the new field only for imported legacy components.  That keeps
        # their established fingerprints stable while new managed recipes
        # persist the exact directory authority explicitly.
        if self.managed_directories:
            value["managed_directories"] = list(self.managed_directories)
        return value


@dataclass(frozen=True)
class GuidedLaunchRecipeResolution:
    family_key: str
    status: str
    components: tuple[GuidedLaunchComponent, ...] = ()
    raw_override_allowed: bool = False
    recovery_action: str = ""
    summary: str = ""
    confidence: str = "verified"
    evidence: Mapping[str, Any] = field(default_factory=dict)
    blocker_code: str = ""
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        payload = {
            "family_key": self.family_key,
            "status": self.status,
            "components": [item.to_mapping() for item in self.components],
            "raw_override_allowed": self.raw_override_allowed,
            "recovery_action": self.recovery_action,
            "summary": self.summary,
            "confidence": self.confidence,
            "evidence": dict(self.evidence),
            "blocker_code": self.blocker_code,
        }
        object.__setattr__(
            self,
            "fingerprint",
            hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        )

    @property
    def qualified(self) -> bool:
        # Legacy callers use ``qualified`` as the signal that generated paths
        # may replace editable fields.  Warning and launch-pending plans are
        # complete isolated plans and therefore remain persistable.
        return self.status in {"qualified_managed", "ready_with_warnings", "launch_pending"}

    @property
    def persistable(self) -> bool:
        return self.qualified

    @property
    def launch_ready(self) -> bool:
        return self.status in {"qualified_managed", "ready_with_warnings"}

    @property
    def blocked_for_safety(self) -> bool:
        return self.status == "blocked_for_safety"

    @property
    def outcome(self) -> str:
        """GRS-9 operator-facing outcome while retaining legacy ``status``."""

        return {
            "qualified_managed": "Ready",
            "ready_with_warnings": "Ready with warnings",
            "launch_pending": "Saved; launch setup pending",
            "blocked_for_safety": "Blocked for safety",
        }.get(self.status, self.status.replace("_", " ").title())

    def to_mapping(self) -> dict[str, Any]:
        return {
            "family_key": self.family_key,
            "status": self.status,
            "components": [item.to_mapping() for item in self.components],
            "raw_override_allowed": self.raw_override_allowed,
            "recovery_action": self.recovery_action,
            "summary": self.summary,
            "confidence": self.confidence,
            "evidence": dict(self.evidence),
            "blocker_code": self.blocker_code,
            "outcome": self.outcome,
            "fingerprint": self.fingerprint,
        }


def recipe_resolution_from_mapping(value: Mapping[str, Any]) -> GuidedLaunchRecipeResolution:
    components = tuple(
        GuidedLaunchComponent(
            component_key=_text(item.get("component_key")),
            label=_text(item.get("label")),
            executable=_text(item.get("executable")),
            arguments=tuple(_text(part) for part in item.get("arguments", ()) if _text(part)),
            working_directory=_text(item.get("working_directory")),
            dependencies=tuple(_text(part) for part in item.get("dependencies", ()) if _text(part)),
            profile_selector=_text(item.get("profile_selector")),
            configuration_roots=tuple(_text(part) for part in item.get("configuration_roots", ()) if _text(part)),
            data_roots=tuple(_text(part) for part in item.get("data_roots", ()) if _text(part)),
            managed_directories=tuple(_text(part) for part in item.get("managed_directories", ()) if _text(part)),
            endpoints=tuple(dict(endpoint) for endpoint in item.get("endpoints", ()) if isinstance(endpoint, Mapping)),
            readiness=dict(item.get("readiness") or {}),
            evidence=dict(item.get("evidence") or {}),
            confidence=_text(item.get("confidence")) or "verified",
            execution_scope=_text(item.get("execution_scope")) or "standard",
            launch_at_startup=bool(item.get("launch_at_startup", False)),
            operator_starts=bool(item.get("operator_starts", False)),
        )
        for item in value.get("components", ())
        if isinstance(item, Mapping)
    )
    return GuidedLaunchRecipeResolution(
        family_key=_text(value.get("family_key")),
        status=_text(value.get("status")) or "incomplete",
        components=components,
        raw_override_allowed=bool(value.get("raw_override_allowed", False)),
        recovery_action=_text(value.get("recovery_action")),
        summary=_text(value.get("summary")),
        confidence=_text(value.get("confidence")) or "verified",
        evidence=dict(value.get("evidence") or {}),
        blocker_code=_text(value.get("blocker_code")),
    )


def _unsupported(family: str, detail: str) -> GuidedLaunchRecipeResolution:
    return GuidedLaunchRecipeResolution(
        family_key=family,
        status="unsupported",
        raw_override_allowed=True,
        recovery_action=detail,
        summary="Operator setup required; the radio draft remains inactive.",
    )


def _safety_block(family: str, detail: str, *, code: str = "resource_collision") -> GuidedLaunchRecipeResolution:
    """Return the only resolution class that is allowed to block Save."""

    return GuidedLaunchRecipeResolution(
        family_key=family,
        status="blocked_for_safety",
        raw_override_allowed=False,
        recovery_action=detail,
        summary="CommsPlan cannot safely create this isolated launch plan until the collision is resolved.",
        confidence="blocked",
        blocker_code=code,
    )


def _truth(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


def _safe_port(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _collision_detail(draft: Mapping[str, Any], instance_key: str) -> tuple[str, str] | None:
    """Read explicit inventory safety findings without inferring reuse.

    Existing-profile discovery is evidence, not a reason to block a distinct
    profile.  Only an explicit collision/reuse/overwrite finding supplied by
    inventory or the reviewed draft is considered destructive risk.
    """

    for key, code, message in (
        ("safety_blocked", "safety_blocked", "The reviewed launch plan is marked unsafe."),
        ("overwrite_existing", "existing_profile_overwrite", "The requested launch would overwrite an existing profile."),
        ("reuse_existing_profile", "existing_profile_reuse", "The requested launch would reuse an existing profile; choose a distinct managed instance."),
        ("existing_profile_reuse", "existing_profile_reuse", "The requested launch would reuse an existing profile; choose a distinct managed instance."),
        ("path_collision", "path_collision", "A managed configuration path collides with another instance."),
        ("endpoint_collision", "endpoint_collision", "A launch endpoint is already claimed by another instance."),
        ("resource_collision", "resource_collision", "A managed launch resource is already claimed by another instance."),
        ("collision", "resource_collision", "A managed launch resource is already claimed by another instance."),
    ):
        if _truth(draft.get(key)):
            return code, message
    source_mode = _text(draft.get("source_mode") or draft.get("instance_source_mode") or draft.get("ownership_mode"))
    if source_mode.casefold() in {"use_existing_instance", "existing", "reuse_existing"}:
        return "existing_profile_reuse", "The requested launch would reuse an existing profile; choose a distinct managed instance."
    for key, code, message in (
        ("collisions", "resource_collision", "A managed launch resource is already claimed by another instance."),
        ("resource_collisions", "resource_collision", "A managed launch resource is already claimed by another instance."),
        ("path_collisions", "path_collision", "A managed configuration path collides with another instance."),
        ("endpoint_collisions", "endpoint_collision", "A launch endpoint is already claimed by another instance."),
        ("inventory_conflicts", "resource_collision", "A managed launch resource is already claimed by another instance."),
        ("resource_claim_conflicts", "resource_collision", "A managed launch resource is already claimed by another instance."),
        ("conflicts", "resource_collision", "A managed launch resource is already claimed by another instance."),
    ):
        value = draft.get(key)
        if isinstance(value, (str, bytes)):
            if value.strip():
                return code, message
        elif value:
            return code, message
    requested_keys = draft.get("existing_instance_keys")
    if requested_keys and instance_key and instance_key in {str(item).strip() for item in requested_keys}:
        return "instance_key_collision", "CommsPlan could not allocate a distinct managed instance identity."
    return None


def _version_evidence(draft: Mapping[str, Any], *, variant: str, version: str) -> dict[str, Any]:
    exact = bool(canonical_js8_version(variant, version))
    source = _text(
        draft.get("version_evidence_source")
        or draft.get("version_source")
        or ("saved_identity" if draft.get("saved_version_evidence") else "")
    )
    return {
        "variant": variant,
        "version": version,
        "source": source or ("reviewed_identity" if exact else "unverified"),
        "confidence": "verified" if exact else "unverified",
        "exact": exact,
    }


def _executable_evidence(draft: Mapping[str, Any], key: str, executable: str) -> dict[str, Any]:
    raw = draft.get(f"{key}_evidence") or draft.get("executable_evidence")
    evidence = dict(raw) if isinstance(raw, Mapping) else {}
    evidence.setdefault("path", executable)
    evidence.setdefault(
        "source",
        _text(draft.get(f"{key}_source") or draft.get("application_source"))
        or ("reviewed_identity" if executable else "missing"),
    )
    evidence.setdefault(
        "confidence",
        _text(draft.get(f"{key}_confidence") or draft.get("application_confidence"))
        or ("verified" if executable else "missing"),
    )
    evidence["exists"] = bool(executable)
    return evidence


def resolve_js8_managed_recipe(
    draft: Mapping[str, Any],
    *,
    managed_root: str,
    platform: object | None = None,
    storage_home: Path | None = None,
) -> GuidedLaunchRecipeResolution:
    executable = _text(draft.get("application_path"))
    variant_hint = _js8_variant_hint(draft, executable)
    variant = normalize_variant_family(variant_hint, draft.get("version"))
    version = _text(draft.get("version"))
    qualified_version = canonical_js8_version(variant, version)
    instance_key = _text(draft.get("draft_instance_key") or draft.get("instance_key"))
    if not instance_key:
        return _safety_block(
            "js8call",
            "CommsPlan could not allocate a distinct JS8Call identity. Return to Identity and prepare again.",
            code="missing_distinct_identity",
        )
    collision = _collision_detail(draft, instance_key)
    if collision is not None:
        code, detail = collision
        return _safety_block("js8call", detail, code=code)
    # A known family with incomplete version evidence is safe to launch with a
    # warning.  A genuinely unknown family is still safe to save in its own
    # generated roots, but launch remains pending until the operator confirms
    # that it supports the reviewed --rig-name identity contract.
    normalized_variant_hint = variant_hint.casefold().replace("-", "_").replace(" ", "_")
    known_variant_hint = normalized_variant_hint in {
        "js8call_2_2", "stock", "js8call", "legacy",
        "js8call_improved", "js8call_improved_3_0_3",
        "js8call_subspace", "js8call_subspace_4_1",
    }
    unknown_variant = bool(
        executable and (variant not in _KNOWN_JS8_VERSIONS or not known_variant_hint)
    )
    identity_mode = settings_identity_mode_from_mapping(draft, family_key="js8call")
    use_default_settings = identity_mode == SETTINGS_IDENTITY_DEFAULT
    radio_name = _text(draft.get("owner_label") or draft.get("instance_name") or "Radio")
    if use_default_settings:
        rig_name = ""
    else:
        try:
            rig_name = normalize_rig_name(draft.get("rig_name")) or native_managed_rig_name(radio_name)
        except ValueError:
            return _safety_block(
                "js8call",
                "The reviewed JS8Call rig name contains a slash, backslash, or comma. Return to Identity and choose a valid distinct name.",
                code="invalid_rig_name",
            )
    application_name = js8_application_name(rig_name)
    data_root = str(
        qt_data_root_candidates(
            application_name=application_name,
            platform=platform,
            home=storage_home,
        )[0]
    )
    profile_path = str(
        qt_config_path_candidates(
            application_name=application_name,
            platform=platform,
            home=storage_home,
        )[0]
    )
    save_root = _join(data_root, "save")
    forms_root = _join(data_root, "forms")
    host = _text(draft.get("host")) or "127.0.0.1"
    tcp_port = _safe_port(draft.get("port"))
    udp_port = _safe_port(draft.get("udp_port"))
    if not tcp_port or not udp_port:
        return _safety_block(
            "js8call",
            "CommsPlan could not allocate distinct JS8Call TCP and UDP endpoints. Return to Connections and prepare again.",
            code="missing_distinct_endpoint",
        )
    version_evidence = _version_evidence(draft, variant=variant, version=version)
    executable_evidence = _executable_evidence(draft, "js8call", executable)
    confidence = (
        "verified"
        if executable and qualified_version
        else "pending"
        if not executable or unknown_variant
        else "warning"
    )
    status = (
        "qualified_managed"
        if confidence == "verified"
        else "ready_with_warnings"
        if confidence == "warning"
        else "launch_pending"
    )
    if not executable:
        recovery = "Browse for the JS8Call executable to enable launch; the reviewed settings choice can still be saved."
        summary = "Default JS8Call settings prepared; launch setup is pending the executable." if use_default_settings else f"JS8Call profile {rig_name} prepared; launch setup is pending the executable."
    elif unknown_variant:
        recovery = (
            f"Confirm that {executable} supports CommsPlan's "
            f"{'default settings launch without --rig-name' if use_default_settings else 'distinct --rig-name launch contract'}, "
            f"then enable launch. The {'default settings choice' if use_default_settings else 'isolated profile'} can still be saved."
        )
        summary = "Default JS8Call settings prepared; launch is pending variant confirmation." if use_default_settings else f"JS8Call profile {rig_name} prepared; launch is pending variant confirmation."
    elif not qualified_version:
        recovery = (
            f"JS8Call executable found at {executable}; verify its exact supported version after first launch."
        )
        summary = "Default JS8Call settings prepared with unverified version evidence; review before launch." if use_default_settings else f"JS8Call profile {rig_name} prepared with unverified version evidence; review before launch."
    else:
        recovery = ""
        summary = (
            f"Launch {variant} {qualified_version} with existing default settings on {host}:{tcp_port}."
            if use_default_settings
            else f"Launch {variant} {qualified_version} as {rig_name} on {host}:{tcp_port}."
        )
    component = GuidedLaunchComponent(
        component_key="js8call",
        label="JS8Call",
        executable=executable,
        arguments=() if use_default_settings else ("--rig-name", rig_name),
        profile_selector=rig_name,
        working_directory="",
        configuration_roots=(profile_path,),
        data_roots=(data_root, save_root, forms_root),
        managed_directories=() if use_default_settings else tuple(
            dict.fromkeys((parent_directory_text(profile_path), data_root, save_root, forms_root))
        ),
        endpoints=(
            {"name": "JS8Call API", "protocol": "tcp", "host": host, "port": tcp_port},
            {"name": "JS8Call UDP", "protocol": "udp", "host": host, "port": udp_port},
        ),
        readiness={"kind": "js8_api", "host": host, "port": tcp_port, "require_api": True},
        evidence={"executable": executable_evidence, "version": version_evidence, "profile": {"source": "js8call_qt_native", "confidence": "existing_default" if use_default_settings else "isolated", "root": profile_path}, "settings_identity_mode": identity_mode},
        confidence=confidence,
        execution_scope=_scope(draft),
        launch_at_startup=bool(draft.get("launch_at_startup", False)) and status != "launch_pending",
        operator_starts=status == "launch_pending",
    )
    return GuidedLaunchRecipeResolution(
        family_key="js8call",
        status=status,
        components=(component,),
        recovery_action=recovery,
        summary=summary,
        confidence=confidence,
        evidence={"executable": executable_evidence, "version": version_evidence, "profile_root": profile_path, "data_root": data_root, "settings_identity_mode": identity_mode},
    )


def resolve_fast_light_managed_recipe(
    draft: Mapping[str, Any],
    *,
    managed_root: str,
    platform: object | None = None,
    storage_home: Path | None = None,
) -> GuidedLaunchRecipeResolution:
    observer = _scope(draft) == "receive_only"
    flrig = _text(draft.get("application_path"))
    fldigi = _text(draft.get("secondary_application_path"))
    instance_key = _text(draft.get("draft_instance_key") or draft.get("instance_key"))
    if not instance_key:
        return _safety_block(
            "fast_light",
            "CommsPlan could not allocate a distinct Fast Light identity. Return to Identity and prepare again.",
            code="missing_distinct_identity",
        )
    collision = _collision_detail(draft, instance_key)
    if collision is not None:
        code, detail = collision
        return _safety_block("fast_light", detail, code=code)
    identity_mode = settings_identity_mode_from_mapping(draft, family_key="fast_light")
    use_default_settings = identity_mode == SETTINGS_IDENTITY_DEFAULT
    native = (
        _fast_light_default_roots(draft, platform=platform, storage_home=storage_home)
        if use_default_settings
        else _fast_light_native_roots(draft, platform=platform, storage_home=storage_home)
    )
    radio_label = _text(draft.get("owner_label") or draft.get("instance_name")) or "Radio"
    flrig_profile = native["flrig_root"]
    fldigi_profile = native["fldigi_root"]
    logs = native["fldigi_logs"]
    checkins = native["flmsg_auto"]
    host = _text(draft.get("host")) or "127.0.0.1"
    flrig_port = _safe_port(draft.get("port"))
    fldigi_port = _safe_port(draft.get("secondary_port"))
    arq_port = _safe_port(draft.get("arq_port") or draft.get("fldigi_arq_port"))
    if not fldigi_port or (not observer and not flrig_port):
        return _safety_block(
            "fast_light",
            "CommsPlan could not allocate distinct Fast Light endpoints. Return to Connections and prepare again.",
            code="missing_distinct_endpoint",
        )
    startup = bool(draft.get("launch_at_startup", False))
    flrig_evidence = _executable_evidence(draft, "flrig", flrig)
    fldigi_evidence = _executable_evidence(draft, "fldigi", fldigi)
    selected_flmsg = (
        _truth(draft.get("use_flmsg"))
        if "use_flmsg" in draft
        else bool(_text(draft.get("flmsg_application_path")))
    )
    selected_flamp = (
        _truth(draft.get("use_flamp"))
        if "use_flamp" in draft
        else bool(_text(draft.get("flamp_application_path")))
    )
    flamp_arq_port = arq_port if selected_flamp else 0
    flmsg_path = _text(draft.get("flmsg_application_path"))
    flamp_path = _text(draft.get("flamp_application_path"))
    missing_required = (
        not fldigi
        or (not observer and not flrig)
        or (selected_flmsg and not flmsg_path)
        or (selected_flamp and (not flamp_path or not flamp_arq_port))
    )
    confidence = "pending" if missing_required else "verified"
    status = "launch_pending" if missing_required else "qualified_managed"
    missing_labels = []
    if not fldigi:
        missing_labels.append("FLDigi")
    if not observer and not flrig:
        missing_labels.append("FLRig")
    if selected_flmsg and not flmsg_path:
        missing_labels.append("FLMsg")
    if selected_flamp and not flamp_path:
        missing_labels.append("FLAmp")
    if selected_flamp and not flamp_arq_port:
        missing_labels.append("FLDigi ARQ port")
    recovery = (
        "Browse for " + " and ".join(missing_labels) + " to enable launch; this Fast Light settings choice can still be saved."
        if missing_labels
        else ""
    )
    components: list[GuidedLaunchComponent] = []
    if not observer:
        components.append(
            GuidedLaunchComponent(
                component_key="flrig",
                label="FLRig",
                executable=flrig,
                arguments=() if use_default_settings else ("--config-dir", flrig_profile),
                working_directory="" if use_default_settings else flrig_profile,
                profile_selector="" if use_default_settings else flrig_profile,
                configuration_roots=(flrig_profile,),
                managed_directories=() if use_default_settings else (flrig_profile,),
                endpoints=({"name": "FLRig XML-RPC", "protocol": "tcp", "host": host, "port": flrig_port},),
                readiness={"kind": "xmlrpc", "host": host, "port": flrig_port, "require_service": True},
                evidence={"executable": flrig_evidence, "profile": {"source": "application_default" if use_default_settings else "generated", "confidence": "existing_default" if use_default_settings else "isolated", "root": flrig_profile}, "settings_identity_mode": identity_mode},
                confidence="pending" if not flrig else "verified",
                launch_at_startup=startup and not missing_required,
                operator_starts=missing_required,
            )
        )
    fldigi_arguments = (
        *(("--config-dir", fldigi_profile) if not use_default_settings else ()),
        "--xmlrpc-server-address", host,
        "--xmlrpc-server-port", str(fldigi_port),
        *(("--arq-server-address", host, "--arq-server-port", str(flamp_arq_port)) if flamp_arq_port else ()),
        *(("--flmsg-dir", native["flmsg_root"]) if selected_flmsg and not use_default_settings else ()),
        *(("--auto-dir", native["flmsg_auto"]) if not use_default_settings else ()),
    )
    components.append(
        GuidedLaunchComponent(
            component_key="fldigi",
            label="FLDigi",
            executable=fldigi,
            arguments=fldigi_arguments,
            working_directory="" if use_default_settings else fldigi_profile,
            dependencies=() if observer else ("flrig",),
            profile_selector="" if use_default_settings else fldigi_profile,
            configuration_roots=(fldigi_profile,),
            data_roots=(logs, checkins),
            managed_directories=() if use_default_settings else (fldigi_profile, logs, checkins),
            endpoints=({"name": "FLDigi XML-RPC", "protocol": "tcp", "host": host, "port": fldigi_port},),
            readiness={"kind": "xmlrpc", "host": host, "port": fldigi_port, "require_service": True},
            evidence={"executable": fldigi_evidence, "profile": {"source": "application_default" if use_default_settings else "generated", "confidence": "existing_default" if use_default_settings else "isolated", "root": fldigi_profile}, "logs": logs, "checkins": checkins, "settings_identity_mode": identity_mode},
            confidence="pending" if not fldigi else "verified",
            execution_scope="receive_only" if observer else "standard",
            launch_at_startup=startup and not missing_required,
            operator_starts=missing_required,
        )
    )
    if selected_flmsg:
        components.append(
            GuidedLaunchComponent(
                component_key="flmsg",
                label="FLMsg",
                executable=flmsg_path,
                # FLMsg 4.0.24 advertises --auto-dir, but its parser does not
                # accept it.  --flmsg-dir is the supported multi-instance
                # selector; FLDigi receives the same root plus --auto-dir.
                arguments=(
                    *(("--flmsg-dir", native["flmsg_root"]) if not use_default_settings else ()),
                    "-title", managed_instance_window_title("FLMsg", radio_label),
                ),
                working_directory="" if use_default_settings else native["flmsg_root"],
                dependencies=("fldigi",),
                profile_selector="" if use_default_settings else native["flmsg_root"],
                configuration_roots=(native["flmsg_root"],),
                data_roots=(
                    native["flmsg_messages"],
                    native["flmsg_templates"],
                    native["flmsg_auto"],
                ),
                managed_directories=() if use_default_settings else (
                    native["flmsg_root"],
                    native["flmsg_messages"],
                    native["flmsg_templates"],
                    native["flmsg_auto"],
                ),
                evidence={
                    "source": "nbems_native_radio_root",
                    "confidence": "existing_default" if use_default_settings else "isolated",
                    "settings_identity_mode": identity_mode,
                    "nbems_base": native["nbems_base"],
                    "fldigi_host": host,
                    "fldigi_xmlrpc_port": fldigi_port,
                },
                readiness={
                    "kind": "process",
                    "window_title": managed_instance_window_title("FLMsg", radio_label),
                },
                confidence="pending" if not flmsg_path else "verified",
                execution_scope=_scope(draft),
                launch_at_startup=startup and bool(flmsg_path),
                operator_starts=not bool(flmsg_path),
            )
        )
    if selected_flamp:
        components.append(
            GuidedLaunchComponent(
                component_key="flamp",
                label="FLAmp",
                executable=flamp_path,
                arguments=(
                    *(("--config-dir", native["flamp_root"]) if not use_default_settings else ()),
                    "--arq-server-address", host,
                    "--arq-server-port", str(flamp_arq_port),
                    "--xmlrpc-server-address", host,
                    "--xmlrpc-server-port", str(fldigi_port),
                    "-title", managed_instance_window_title("FLAmp", radio_label),
                ) if flamp_arq_port else (),
                working_directory="" if use_default_settings else native["flamp_root"],
                dependencies=("fldigi",),
                profile_selector="" if use_default_settings else native["flamp_root"],
                configuration_roots=(native["flamp_root"],),
                data_roots=(native["flamp_receive"], native["flamp_outgoing"]),
                managed_directories=() if use_default_settings else (
                    native["flamp_root"],
                    native["flamp_receive"],
                    native["flamp_outgoing"],
                    str(Path(native["flamp_root"]) / "FLAMP" / "scripts"),
                    str(Path(native["flamp_root"]) / "FLAMP" / "relay"),
                ),
                endpoints=(
                    {"name": "FLDigi ARQ pairing", "protocol": "tcp", "host": host, "port": flamp_arq_port},
                ) if flamp_arq_port else (),
                readiness={
                    "kind": "process",
                    "fldigi_host": host,
                    "fldigi_xmlrpc_port": fldigi_port,
                    "fldigi_arq_port": flamp_arq_port,
                    "window_title": managed_instance_window_title("FLAmp", radio_label),
                },
                evidence={
                    "source": "flamp_config_dir_and_endpoint_pair",
                    "confidence": "existing_default" if use_default_settings and flamp_arq_port else "isolated" if flamp_arq_port else "pending",
                    "attribution": "radio_scoped",
                    "settings_identity_mode": identity_mode,
                },
                confidence="pending" if not flamp_path or not flamp_arq_port else "verified",
                execution_scope=_scope(draft),
                launch_at_startup=startup and bool(flamp_path) and bool(flamp_arq_port),
                operator_starts=not bool(flamp_path) or not bool(flamp_arq_port),
            )
        )
    return GuidedLaunchRecipeResolution(
        family_key="fast_light",
        status=status,
        components=tuple(components),
        recovery_action=recovery,
        summary=(
            f"Fast Light receive-only settings prepared on {host}:{fldigi_port}; launch setup is pending."
            if observer and missing_required
            else f"Fast Light transceiver profile prepared; launch setup is pending {', '.join(missing_labels)}."
            if missing_required
            else f"Launch FLDigi receive-only with {'existing default' if use_default_settings else 'isolated'} settings on {host}:{fldigi_port}."
            if observer
            else f"Launch FLRig on {host}:{flrig_port}, then FLDigi on {host}:{fldigi_port}."
        ),
        confidence=confidence,
        evidence={
            "flrig_executable": flrig_evidence,
            "fldigi_executable": fldigi_evidence,
            "flrig_root": flrig_profile,
            "fldigi_root": fldigi_profile,
            "logs_root": logs,
            "checkins_root": checkins,
            "flmsg_root": native["flmsg_root"],
            "flmsg_messages": native["flmsg_messages"],
            "flmsg_templates": native["flmsg_templates"],
            "flmsg_auto": native["flmsg_auto"],
            "flamp_root": native["flamp_root"],
            "flamp_receive": native["flamp_receive"],
            "flamp_outgoing": native["flamp_outgoing"],
            "fldigi_arq_port": flamp_arq_port,
            "native_layout": "application_standard",
            "settings_identity_mode": identity_mode,
        },
    )


def resolve_guided_launch_recipe(
    draft: Mapping[str, Any],
    *,
    managed_root: str,
    platform: object | None = None,
    storage_home: Path | None = None,
) -> GuidedLaunchRecipeResolution:
    family = _key(draft.get("family_key"))
    if _text(draft.get("mode")).casefold() != "managed":
        return GuidedLaunchRecipeResolution(
            family_key=family,
            status="operator_start",
            raw_override_allowed=True,
            recovery_action="Review the imported or manual application's exact launch behavior in Advanced.",
            summary="Operator-managed launch; CommsPlan does not rewrite this application's identity.",
        )
    if family == "js8call":
        return resolve_js8_managed_recipe(
            draft,
            managed_root=managed_root,
            platform=platform,
            storage_home=storage_home,
        )
    if family == "fast_light":
        return resolve_fast_light_managed_recipe(
            draft,
            managed_root=managed_root,
            platform=platform,
            storage_home=storage_home,
        )
    return GuidedLaunchRecipeResolution(
        family_key=family,
        status="incomplete",
        raw_override_allowed=True,
        recovery_action="Use the family-specific Advanced setup.",
        summary="No managed launch recipe is available for this family.",
    )


def recipe_draft_updates(resolution: GuidedLaunchRecipeResolution) -> dict[str, Any]:
    """Project one complete resolution into the draft/store schema.

    The legacy ``launch_command`` remains empty for FIO-managed recipes so
    older launch adapters do not mistake it for a custom override.  The exact
    command, working directory, roots, endpoints, and evidence are retained
    in the canonical recipe and in explicit draft fields for audit and
    reconciliation.
    """

    value = resolution.to_mapping()
    updates: dict[str, Any] = {
        "launch_recipe": value,
        "launch_recipe_status": resolution.status,
        "launch_recipe_fingerprint": resolution.fingerprint,
        "launch_recipe_confidence": resolution.confidence,
        "launch_recipe_evidence": dict(resolution.evidence),
        "launch_recipe_blocker_code": resolution.blocker_code,
        "launch_ready": resolution.launch_ready,
        "launch_setup_pending": resolution.status == "launch_pending",
        "launch_components": [component.to_mapping() for component in resolution.components],
        "launch_component_recipes": {
            component.component_key: component.to_mapping() for component in resolution.components
        },
    }
    if not resolution.persistable:
        return updates
    by_key = {component.component_key: component for component in resolution.components}
    if resolution.family_key == "js8call":
        component = by_key["js8call"]
        endpoints = {str(item.get("protocol")): item for item in component.endpoints}
        updates.update(
            rig_name=component.profile_selector,
            configuration_path=component.configuration_roots[0],
            storage_path=component.data_roots[0],
            port=int(endpoints["tcp"]["port"]),
            udp_port=int(endpoints["udp"]["port"]),
            working_directory=component.working_directory,
            launch_arguments=list(component.arguments),
            effective_launch_command=component.effective_command_text,
            launch_working_directory=component.working_directory,
            launch_endpoints=[dict(item) for item in component.endpoints],
            launch_evidence=dict(component.evidence),
            launch_command="",
        )
    elif resolution.family_key == "fast_light":
        fldigi = by_key["fldigi"]
        updates.update(
            secondary_configuration_path=fldigi.configuration_roots[0],
            storage_path=fldigi.data_roots[0],
            secondary_storage_path=fldigi.data_roots[1],
            secondary_port=int(fldigi.endpoints[0]["port"]),
            working_directory=fldigi.working_directory,
            launch_arguments=list(fldigi.arguments),
            effective_launch_command=fldigi.effective_command_text,
            launch_working_directory=fldigi.working_directory,
            launch_endpoints=[dict(item) for item in fldigi.endpoints],
            launch_evidence=dict(fldigi.evidence),
            launch_command="",
        )
        flrig = by_key.get("flrig")
        if flrig is not None:
            updates.update(
                configuration_path=flrig.configuration_roots[0],
                port=int(flrig.endpoints[0]["port"]),
                launch_component_working_directories={
                    key: component.working_directory
                    for key, component in by_key.items()
                },
            )
        claims: list[dict[str, Any]] = []
        claim_names = {
            "flrig": (("flrig_configuration", 0, True),),
            "fldigi": (
                ("fldigi_configuration", 0, True),
                ("fldigi_logs", 0, True),
                ("fldigi_checkins", 1, True),
            ),
            "flmsg": (
                ("flmsg_root", 0, True),
                ("flmsg_messages", 0, True),
                ("flmsg_templates", 1, True),
                ("flmsg_auto", 2, True),
            ),
            "flamp": (
                ("flamp_root", 0, True),
                ("flamp_receive", 0, True),
                ("flamp_outgoing", 1, True),
            ),
        }
        for component_key, component in by_key.items():
            if component.executable and component_key in {"flmsg", "flamp"}:
                claims.append(
                    {
                        "kind": f"{component_key}_application",
                        "value": component.executable,
                        "exclusive": False,
                    }
                )
            for kind, index, exclusive in claim_names.get(component_key, ()):
                roots = component.configuration_roots if "configuration" in kind or kind in {"flmsg_root", "flamp_root"} else component.data_roots
                if index < len(roots) and roots[index]:
                    claims.append({"kind": kind, "value": roots[index], "exclusive": exclusive})
        updates["resource_claims"] = claims
        updates["ports"] = [
            dict(endpoint)
            for component in by_key.values()
            for endpoint in component.endpoints
        ]
        updates["arq_port"] = int(
            resolution.evidence.get("fldigi_arq_port", 0) or 0
        )
        updates["fldigi_arq_port"] = updates["arq_port"]
        flmsg = by_key.get("flmsg")
        if flmsg is not None:
            updates.update(
                flmsg_application_path=flmsg.executable,
                flmsg_native_root=flmsg.configuration_roots[0],
                flmsg_message_path=flmsg.data_roots[0],
                flmsg_templates_path=flmsg.data_roots[1],
                flmsg_auto_path=flmsg.data_roots[2],
            )
        flamp = by_key.get("flamp")
        if flamp is not None:
            updates.update(
                flamp_application_path=flamp.executable,
                flamp_native_root=flamp.configuration_roots[0],
                flamp_receive_path=flamp.data_roots[0],
                flamp_outgoing_path=flamp.data_roots[1],
            )
    return updates


__all__ = [
    "canonical_js8_version",
    "GuidedLaunchComponent",
    "GuidedLaunchRecipeResolution",
    "managed_instance_window_title",
    "recipe_draft_updates",
    "recipe_resolution_from_mapping",
    "resolve_fast_light_managed_recipe",
    "resolve_guided_launch_recipe",
    "resolve_js8_managed_recipe",
]
