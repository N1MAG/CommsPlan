from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from freqinout.core.config_autodiscovery import (
    DEFAULT_PORT_PLAN,
    JS8CALL_COMMAND_NAMES,
    js8call_file_profile_operator_label,
    select_js8call_file_profile,
)
from freqinout.core.software_path_detector import PathDetectionResult


def next_default_instance_port(
    service: str,
    profiles: Sequence[Mapping[str, Any]],
    *,
    existing_profile_id: int = 0,
) -> str:
    service_key = str(service or "").strip().lower()
    profile_key_by_service = {
        "flrig": "flrig_port",
        "fldigi": "fldigi_port",
        "js8call": "js8_port",
    }
    profile_key = profile_key_by_service.get(service_key)
    defaults = tuple(DEFAULT_PORT_PLAN.get(service_key, ()))
    if not profile_key or not defaults:
        return ""
    used: set[int] = set()
    for row in profiles or ():
        if not isinstance(row, Mapping):
            continue
        try:
            row_id = int(row.get("id", 0) or 0)
        except Exception:
            row_id = 0
        if existing_profile_id > 0 and row_id == int(existing_profile_id):
            continue
        raw = str(row.get(profile_key, "") or "").strip()
        if not raw:
            continue
        try:
            used.add(int(raw))
        except ValueError:
            continue
    for default_port in defaults:
        if int(default_port) not in used:
            return str(default_port)
    candidate = int(defaults[-1]) + 1
    while candidate <= 65535:
        if candidate not in used:
            return str(candidate)
        candidate += 1
    return str(defaults[-1])


def guided_js8_profile_review_text(
    profiles: Sequence[Any],
    *,
    tcp_port: str = "",
    profile_name: str = "",
) -> str:
    usable = [profile for profile in profiles or () if str(getattr(profile, "directed_path", "") or "").strip()]
    if not usable:
        return "No JS8Call profile with DIRECTED.TXT was found."
    port_txt = str(tcp_port or "").strip()
    if port_txt:
        return f"No JS8Call profile with DIRECTED.TXT matched TCP port {port_txt}."
    name_txt = str(profile_name or "").strip()
    if len(usable) > 1:
        hint = f" for {name_txt}" if name_txt else ""
        return f"Multiple JS8Call profiles have DIRECTED.TXT{hint}. Enter the JS8Call TCP port to choose the correct one."
    return "JS8Call profile was found, but it could not be matched safely."


def guided_detection_path(results: Mapping[str, PathDetectionResult], key: str) -> str:
    result = results.get(key)
    if result is None or result.confidence == "not_found":
        return ""
    return str(result.path or "").strip()


def guided_app_candidate_identity(candidate: Any) -> Tuple[str, str]:
    path_text = str(getattr(candidate, "path", "") or "").strip()
    path = Path(path_text)
    identity_path = path
    if path.suffix.lower() == ".app":
        app_id = str(getattr(candidate, "app_id", "") or "").strip().lower()
        command_names = {
            "flrig": ("FLRig", "flrig"),
            "fldigi": ("FLDigi", "fldigi"),
            "flmsg": ("FLMsg", "flmsg"),
            "flamp": ("FLAmp", "flamp"),
            "js8call": JS8CALL_COMMAND_NAMES,
            "js8spotter": ("JS8Spotter", "js8spotter"),
            "commstat": ("CommStat", "commstat"),
            "varac": ("VarAC", "varac"),
        }.get(app_id, (path.stem, path.stem.lower()))
        for command in command_names:
            candidate_exe = path / "Contents" / "MacOS" / command
            if candidate_exe.exists():
                identity_path = candidate_exe
                break
    try:
        identity = str(identity_path.resolve(strict=False))
    except Exception:
        identity = str(identity_path)
    key = os.path.normcase(os.path.normpath(identity))
    return key, path_text


def guided_single_install_path(
    candidates: Sequence[Any],
    app_id: str,
    fallback_results: Mapping[str, PathDetectionResult],
    result_key: str,
    label: str,
    review: List[str],
) -> str:
    matches = [
        candidate
        for candidate in candidates or ()
        if str(getattr(candidate, "app_id", "") or "") == app_id
        and bool(getattr(candidate, "executable", False))
        and str(getattr(candidate, "path", "") or "").strip()
    ]
    unique_paths: List[str] = []
    seen: set[str] = set()
    for candidate in matches:
        key, path_text = guided_app_candidate_identity(candidate)
        if key in seen:
            continue
        seen.add(key)
        unique_paths.append(path_text)
    if len(unique_paths) > 1:
        labels = _guided_install_review_labels(app_id, unique_paths)
        detail = f": {', '.join(labels[:4])}" if labels else ""
        if len(labels) > 4:
            detail += f", +{len(labels) - 4} more"
        review.append(f"Multiple {label} installs found{detail}. Choose the correct app path manually.")
        return ""
    if len(unique_paths) == 1:
        return unique_paths[0]
    return guided_detection_path(fallback_results, result_key)


def _guided_install_review_labels(app_id: str, paths: Sequence[str]) -> Tuple[str, ...]:
    labels: List[str] = []
    seen: set[str] = set()
    for path_text in paths:
        variant = guided_app_candidate_variant_label(app_id, path_text)
        label = variant or Path(str(path_text or "")).name or str(path_text or "").strip()
        if not label or label in seen:
            continue
        seen.add(label)
        labels.append(label)
    return tuple(labels)


def guided_app_candidate_choices(candidates: Sequence[Any], app_id: str) -> Tuple[Tuple[str, str], ...]:
    choices: List[Tuple[str, str]] = []
    seen: set[str] = set()
    for candidate in candidates or ():
        if str(getattr(candidate, "app_id", "") or "") != str(app_id or ""):
            continue
        if not bool(getattr(candidate, "executable", False)):
            continue
        path_text = str(getattr(candidate, "path", "") or "").strip()
        if not path_text:
            continue
        key, path_text = guided_app_candidate_identity(candidate)
        if key in seen:
            continue
        seen.add(key)
        source = str(getattr(candidate, "source", "") or "").strip()
        display_prefix = guided_app_candidate_variant_label(app_id, path_text)
        path_label = f"{display_prefix} - {path_text}" if display_prefix else path_text
        label = f"{path_label} ({source})" if source else path_label
        choices.append((label, path_text))
    return tuple(choices)


def guided_app_candidate_variant_label(app_id: str, path_text: str) -> str:
    if str(app_id or "").strip().lower() != "js8call":
        return ""
    lowered = str(path_text or "").strip().lower()
    if not lowered:
        return ""
    if "subspace" in lowered:
        return "JS8Call Subspace"
    if "improved" in lowered:
        return "JS8Call-improved"
    if "2.2.0" in lowered or "js8_22" in lowered or "js8-22" in lowered:
        return "JS8Call 2.2.0"
    return ""


def guided_js8_profile_choices(profiles: Sequence[Any]) -> Tuple[Tuple[str, Dict[str, str]], ...]:
    choices: List[Tuple[str, Dict[str, str]]] = []
    seen: set[Tuple[str, str, str]] = set()
    for profile in profiles or ():
        directed_path = str(getattr(profile, "directed_path", "") or "").strip()
        if not directed_path:
            continue
        name = js8call_file_profile_operator_label(profile)
        port = str(getattr(profile, "tcp_server_port", "") or "").strip()
        save_dir = str(getattr(profile, "save_dir", "") or "").strip()
        key = (port, os.path.normcase(os.path.normpath(save_dir)), os.path.normcase(os.path.normpath(directed_path)))
        if key in seen:
            continue
        seen.add(key)
        parts = [name]
        if save_dir:
            parts.append(save_dir)
        choices.append(
            (
                " - ".join(parts),
                {
                    "port": port,
                    "profile_path": save_dir,
                    "directed_path": directed_path,
                },
            )
        )
    return tuple(choices)


def guided_port_prompt_keys(
    *,
    current: Mapping[str, str],
    selected: Mapping[str, bool],
    backend: str,
    observer_mode: bool,
) -> Tuple[str, ...]:
    if observer_mode:
        return tuple()
    backend_key = str(backend or "").strip().lower()
    checks = (
        ("flrig", "flrig_port", bool(selected.get("flrig")) or backend_key == "flrig"),
        ("fldigi", "fldigi_port", bool(selected.get("fldigi"))),
        ("js8call", "js8_port", bool(selected.get("js8call")) or backend_key == "js8call"),
    )
    missing = []
    for _service, field_key, enabled in checks:
        if enabled and not str(current.get(field_key, "") or "").strip():
            missing.append(field_key)
    return tuple(missing)


def guided_radio_autofill_suggestions(
    *,
    current: Mapping[str, str],
    selected: Mapping[str, bool],
    backend: str,
    observer_mode: bool,
    install_candidates: Sequence[Any],
    fast_results: Mapping[str, PathDetectionResult],
    js8_results: Mapping[str, PathDetectionResult],
    varac_results: Mapping[str, PathDetectionResult],
    js8_file_profiles: Sequence[Any],
    default_ports: Mapping[str, str],
    profile_name: str = "",
    source_modes: Mapping[str, str] | None = None,
) -> Tuple[Dict[str, str], Tuple[str, ...]]:
    suggestions: Dict[str, str] = {}
    review: List[str] = []

    def _suggest(key: str, value: object) -> None:
        text = str(value or "").strip()
        if text:
            suggestions[key] = text

    def _current(key: str) -> str:
        return str(current.get(key, "") or "").strip()

    def _source(family: str) -> str:
        value = str((source_modes or {}).get(family, "") or "").strip().casefold()
        return value or "existing"  # compatibility for older direct callers

    backend_key = str(backend or "").strip().lower()
    if observer_mode:
        _suggest("sdr_host", "127.0.0.1")
        review.append("Observer SDR endpoint was prepared when blank.")

    if not observer_mode and (bool(selected.get("flrig")) or backend_key == "flrig"):
        _suggest("flrig_host", "127.0.0.1")
        _suggest("flrig_port", default_ports.get("flrig", ""))
        _suggest(
            "flrig_path",
            guided_single_install_path(install_candidates, "flrig", fast_results, "path_flrig", "FLRig", review),
        )
    if bool(selected.get("fldigi")):
        _suggest("fldigi_host", "127.0.0.1")
        _suggest("fldigi_port", default_ports.get("fldigi", ""))
        _suggest(
            "fldigi_path",
            guided_single_install_path(install_candidates, "fldigi", fast_results, "path_fldigi", "FLDigi", review),
        )
    if bool(selected.get("flmsg")):
        _suggest(
            "flmsg_path",
            guided_single_install_path(install_candidates, "flmsg", fast_results, "path_flmsg", "FLMsg", review),
        )
    if bool(selected.get("flamp")):
        _suggest(
            "flamp_path",
            guided_single_install_path(install_candidates, "flamp", fast_results, "path_flamp", "FLAmp", review),
        )
    if bool(selected.get("js8call")) or backend_key == "js8call":
        initial_js8_port = _current("js8_port")
        js8_source = _source("js8call")
        use_existing_js8 = js8_source in {"existing", "discover", "import"}
        selected_js8_profile = (
            select_js8call_file_profile(
                js8_file_profiles,
                tcp_port=initial_js8_port,
                profile_name=profile_name,
            )
            if use_existing_js8
            else None
        )
        _suggest("js8_host", "127.0.0.1")
        if selected_js8_profile is not None and getattr(selected_js8_profile, "tcp_server_port", ""):
            _suggest("js8_port", getattr(selected_js8_profile, "tcp_server_port", ""))
        elif js8_source in {"create", "managed", "new"}:
            _suggest("js8_port", default_ports.get("js8call", ""))
        if js8_source not in {"manual", "remote"}:
            _suggest(
                "js8_install_path",
                guided_single_install_path(install_candidates, "js8call", js8_results, "path_js8call", "JS8Call", review),
            )
        if selected_js8_profile is not None:
            _suggest("js8_directed_path", getattr(selected_js8_profile, "directed_path", ""))
            _suggest("js8_profile_path", getattr(selected_js8_profile, "save_dir", ""))
        elif use_existing_js8:
            review.append(
                guided_js8_profile_review_text(
                    js8_file_profiles,
                    tcp_port=initial_js8_port,
                    profile_name=profile_name,
                )
            )
    if bool(selected.get("fio_spotter")):
        _suggest("js8_forms_path", guided_detection_path(js8_results, "js8_forms_path"))
    external_js8spotter_selected = bool(selected.get("external_js8spotter")) or bool(selected.get("js8spotter"))
    if external_js8spotter_selected:
        _suggest(
            "spotter_launch_path",
            guided_single_install_path(
                install_candidates,
                "js8spotter",
                js8_results,
                "path_js8spotter",
                "External JS8Spotter",
                review,
            ),
        )
    if bool(selected.get("commstat")):
        _suggest(
            "commstat_launch_path",
            guided_single_install_path(
                install_candidates,
                "commstat",
                js8_results,
                "path_commstat",
                "CommStat",
                review,
            ),
        )
    if bool(selected.get("varac")) and not observer_mode:
        varac_source = _source("varac")
        if varac_source not in {"manual", "remote"}:
            _suggest(
                "varac_install_path",
                guided_single_install_path(install_candidates, "varac", varac_results, "varac_path", "VarAC", review),
            )
        if varac_source in {"existing", "discover", "import"}:
            _suggest("varac_db_path", guided_detection_path(varac_results, "varac_db_path"))
            _suggest("varac_ini_path", guided_detection_path(varac_results, "varac_ini_path"))
            _suggest("varac_incoming_path", guided_detection_path(varac_results, "message_paths.varac"))
            _suggest("varac_outbox_dir", guided_detection_path(varac_results, "varac_outbox_dir"))
            _suggest("varac_bbs_dir", guided_detection_path(varac_results, "varac_bbs_dir"))
            _suggest("varac_bbs_archive_dir", guided_detection_path(varac_results, "varac_bbs_archive_dir"))
        review.append(
            "VarAC references were stored for CommsPlan monitoring and BBS features only. "
            "CommsPlan did not write VarAC.ini, VarAC.db, or VarAC cluster membership."
        )

    return suggestions, tuple(review)
