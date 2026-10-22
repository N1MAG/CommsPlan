from __future__ import annotations

import json
import time
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from freqinout.core.dependency_health import get_dependency_health_registry
from freqinout.core.ingest_runtime_status import runtime_source_view_rows_from_skip_reasons
from freqinout.core.scheduler_events import load_recent_scheduler_events


ScopeResolver = Callable[[str, Mapping[str, object]], str]


def _int_value(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _float_value(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _format_duration_ms(value: object) -> str:
    duration = _float_value(value)
    if duration <= 0:
        return ""
    if duration >= 1000:
        return f"{duration / 1000.0:.1f}s"
    return f"{duration:.0f} ms"


def _format_age(ts: object, *, now: Optional[float] = None) -> str:
    stamp = _float_value(ts)
    if stamp <= 0:
        return ""
    age = max(0.0, (time.monotonic() if now is None else now) - stamp)
    if age < 1:
        return "just now"
    if age < 60:
        return f"{age:.0f}s ago"
    if age < 3600:
        return f"{age / 60.0:.0f}m ago"
    return f"{age / 3600.0:.1f}h ago"


def _format_issue_since(ts: object, *, now: Optional[float] = None) -> str:
    age = _format_age(ts, now=now)
    return f"since {age}" if age else ""


def _format_cooldown(value: object) -> str:
    cooldown = _float_value(value)
    if cooldown <= 0:
        return ""
    if cooldown < 60:
        return f"{cooldown:.0f}s"
    return f"{cooldown / 60.0:.1f}m"


def _shorten_error(value: object) -> str:
    text = str(value or "").strip()
    if len(text) <= 140:
        return text
    return text[:137].rstrip() + "..."


def _operator_error_text(key: str, value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    key_norm = str(key or "").strip().lower().replace("_", "-")
    lower = text.lower()
    if "settingsmanager used from a different thread" in lower:
        if key_norm == "scheduler:fldigi-busy-check":
            return "An old FLDigi receive-activity check could not finish cleanly. CommsPlan is not currently blocked by this check."
        return "A background check could not finish cleanly because an internal settings handle was used from the wrong worker thread."
    if key_norm == "scheduler:fldigi-busy-check" and "could not verify fldigi receive activity" in lower:
        return "CommsPlan could not confirm FLDigi receive activity during an earlier schedule check. The schedule was allowed to continue."
    return text


def _is_fldigi_busy_check_key(key: str) -> bool:
    return str(key or "").strip().lower().replace("_", "-") == "scheduler:fldigi-busy-check"


def _is_scheduler_hold_key(key: str) -> bool:
    return ":".join(str(key or "").strip().lower().replace("_", "-").split(":")[:2]) in {
        "scheduler:fldigi-busy",
        "scheduler:js8-busy",
        "scheduler:varac-busy",
        "scheduler:flrig-ptt",
        "scheduler:manual-qsy",
    }


def _is_expired_transient_scheduler_key(key: str, last_checked_ts: float, *, now: Optional[float] = None) -> bool:
    key_norm = str(key or "").strip().lower().replace("_", "-")
    if key_norm not in {"scheduler:fldigi-busy-check"}:
        return False
    if last_checked_ts <= 0:
        return False
    return ((time.monotonic() if now is None else now) - last_checked_ts) > 300.0


def _is_js8_diagnostic_only(key: str, metadata: Mapping[str, object]) -> bool:
    key_norm = str(key or "").strip().lower().replace("_", "-")
    return key_norm == "scheduler:js8-shadow" and bool(metadata.get("diagnostic_only"))


def _js8_capability_mode(metadata: Mapping[str, object]) -> str:
    return str(metadata.get("capability_mode", "") or metadata.get("mode", "") or "").strip().lower()


def _is_ingest_source_health_snapshot(
    key: str,
    service: str,
    owner: str,
    metadata: Mapping[str, object],
) -> bool:
    key_norm = str(key or "").strip().lower().replace("-", "_")
    if service == "ingest_source":
        return True
    if key_norm.startswith(("ingest_source:", "ingest_source_", "ingest_app_")):
        return True
    source_type = str(metadata.get("source_type", "") or "").strip()
    family = str(metadata.get("family", "") or "").strip()
    if str(owner or "").strip() == "BackgroundIngest" and (source_type or family) and service != "background_ingest":
        return True
    return False


def _is_placeholder_error_text(value: object) -> bool:
    text = str(value or "").strip().lower()
    return text in {"", "failure", "none", "null"}


def _dependency_label(key: str, owner: str = "") -> str:
    parts = [part for part in str(key or "").split(":") if part]
    if not parts:
        return str(owner or "Dependency").strip() or "Dependency"
    service = parts[0].upper().replace("-", "_")
    if service == "BACKGROUND_INGEST":
        job = _background_job_label(parts[1] if len(parts) > 1 else "")
        return f"Background ingest: {job}" if job else "Background ingest jobs"
    if service == "SCHEDULER":
        return _scheduler_health_label(parts[1] if len(parts) > 1 else "")
    labels = {
        "JS8CALL": "JS8Call API",
        "FLRIG": "FLRig API",
        "FLDIGI": "FLDigi XML-RPC",
        "COMMSTAT": "CommStat data",
        "VARAC": "VarAC data",
        "VARAC_BBS_VAULT_ALIASES": "Managed BBS Library aliases",
        "INGEST_SOURCE": "Ingest Source",
        "RIGCTLD": "rigctld",
        "OBSERVER": "Observer SDR",
        "BACKGROUND_INGEST": "Background ingest",
    }
    if service.startswith("INGEST_APP_"):
        return f"Ingest Source ({parts[0]})"
    label = labels.get(service, service.replace("_", " ").title())
    endpoint = ""
    if len(parts) >= 3 and parts[1] not in {"false", "true"}:
        endpoint_host = _display_host(parts[1])
        endpoint = f" ({endpoint_host}:{parts[2]})"
    return f"{label}{endpoint}"


def _scheduler_health_label(value: object) -> str:
    raw = str(value or "").strip().replace("-", "_")
    labels = {
        "fldigi_busy": "Scheduler hold: FLDigi RX activity",
        "js8_busy": "Scheduler hold: JS8Call busy",
        "js8_shadow": "JS8Call native diagnostic",
        "varac_busy": "Scheduler hold: VarAC busy",
        "flrig_ptt": "Scheduler hold: FLRig PTT",
        "status_snapshot": "Scheduler status snapshot",
        "control_task": "Scheduler control task",
    }
    if not raw:
        return "Scheduler"
    return labels.get(raw.lower(), f"Scheduler: {raw.replace('_', ' ').title()}")


def _display_host(value: object) -> str:
    text = str(value or "").strip()
    if text.lower() in {"loopback", "localhost", "::1"}:
        return "127.0.0.1"
    return text


def _canonical_endpoint_key(key: object) -> str:
    parts = [part for part in str(key or "").split(":") if part]
    if len(parts) >= 3:
        service = parts[0].strip().lower().replace("-", "_")
        if service in {"js8call", "flrig", "fldigi", "rigctld", "observer"}:
            host = _display_host(parts[1]).strip().lower()
            port = parts[2].strip()
            return f"{service}:{host}:{port}"
    return str(key or "").strip().lower()


def _background_job_label(value: object) -> str:
    raw = str(value or "").strip().replace("-", "_")
    labels = {
        "js8_links": "JS8 links",
        "js8_messages": "JS8 messages",
        "js8_spotter": "JS8Spotter",
        "commstat": "CommStat",
        "commstat_messages": "CommStat messages",
        "varac_vault": "Managed BBS",
        "varac_messages": "VarAC messages",
        "fldigi_messages": "FLDigi messages",
        "flamp_messages": "FLAMP messages",
    }
    if not raw:
        return ""
    return labels.get(raw.lower(), raw.replace("_", " ").title())


def _scope_from_metadata(metadata: Mapping[str, object]) -> str:
    for key in ("radio_name", "radio", "profile_name", "device_name"):
        text = str(metadata.get(key, "") or "").strip()
        if text:
            return text
    explicit_scope = str(metadata.get("scope") or "").strip()
    if explicit_scope and explicit_scope != "Station-wide":
        return explicit_scope
    for key in ("radio_id", "device_profile_id", "profile_id"):
        value = metadata.get(key)
        if value not in (None, ""):
            return "Radio " + str(value).removeprefix("radio_")
    identity = str(metadata.get("radio_profile_id") or "")
    return identity.replace("radio_", "Radio ") if identity else "Station-wide"


def _item_from_snapshot(
    key: str,
    snapshot: Mapping[str, object],
    *,
    scope_resolver: Optional[ScopeResolver] = None,
    now: Optional[float] = None,
) -> Dict[str, object]:
    metadata = snapshot.get("metadata", {})
    if not isinstance(metadata, Mapping):
        metadata = {}
    owner = str(snapshot.get("owner", "") or "").strip()
    failures = _int_value(snapshot.get("consecutive_failures"))
    slow = _int_value(snapshot.get("consecutive_slow"))
    cooldown = _float_value(snapshot.get("cooldown_remaining_sec"))
    service = str(key or "").split(":", 1)[0].strip().lower().replace("-", "_")
    backoff = cooldown > 0 or (failures >= 3 and service != "scheduler")
    last_checked_ts = _float_value(snapshot.get("last_checked_ts"))
    stale_ok = bool(
        service != "scheduler"
        and last_checked_ts > 0
        and failures <= 0
        and slow <= 0
        and (now or time.monotonic()) - last_checked_ts > 600.0
    )
    last_error = _operator_error_text(str(key or ""), snapshot.get("last_error", ""))
    scheduler_hold = _is_scheduler_hold_key(str(key or ""))
    active_scheduler_hold = scheduler_hold and bool(metadata.get("active_hold"))
    fldigi_busy_check = _is_fldigi_busy_check_key(str(key or ""))
    js8_diagnostic_only = _is_js8_diagnostic_only(str(key or ""), metadata)
    js8_mode = _js8_capability_mode(metadata)
    ingest_source_health = _is_ingest_source_health_snapshot(str(key or ""), service, owner, metadata)
    if ingest_source_health and _is_placeholder_error_text(last_error):
        last_error = ""
    expired_transient = _is_expired_transient_scheduler_key(str(key or ""), last_checked_ts, now=now)
    warning = (
        (stale_ok and not expired_transient)
        or failures > 0
        or slow > 0
        or bool(last_error)
    )
    if js8_diagnostic_only:
        severity = "info"
        state = "Diagnostic"
        action = "Native JS8Call diagnostic differs from the existing JS8 path; CommsPlan is still using the existing path."
        warning = False
        backoff = False
    elif js8_mode in {"api_basic", "api_full"} and not last_error and not slow:
        severity = "info" if js8_mode == "api_basic" else "ok"
        state = "Ready (basic)" if js8_mode == "api_basic" else "OK"
        action = "JS8Call API is reachable; CommsPlan will keep compatibility fallbacks available."
        warning = False
        backoff = False
    elif fldigi_busy_check:
        severity = "ok"
        state = "OK"
        action = "No schedule move is waiting on this FLDigi activity check."
        warning = False
        backoff = False
    elif active_scheduler_hold and str(key).startswith("scheduler:manual-qsy"):
        severity = "info"
        state = "Hold"
        action = "Manual QSY holds this radio until Resume Schedule or its timer expires."
        warning = False
        backoff = False
    elif backoff:
        severity = "warning" if warning else "info"
        state = "Retry waiting"
        action = "Waiting before retry to keep CommsPlan responsive"
    elif expired_transient and warning:
        severity = "ok"
        state = "OK"
        action = "No active issue; the last transient scheduler check is old"
        warning = False
    elif active_scheduler_hold and not backoff:
        severity = "info"
        state = "Hold"
        action = "A scheduled frequency change is waiting for current station activity to clear."
        warning = False
    elif scheduler_hold:
        severity = "ok"
        state = "OK"
        action = "No scheduled frequency change is waiting on this activity check."
        warning = False
    elif stale_ok:
        severity = "info"
        state = "Not recent"
        action = "Last known check was OK; CommsPlan has not needed a fresh check recently"
        warning = False
    elif warning and ingest_source_health and not last_error:
        severity = "info"
        state = "Waiting"
        action = "Waiting for the next normal check"
        warning = False
    elif warning:
        severity = "warning"
        state = "Warning"
        action = ("Recent checks are slow: " + _format_duration_ms(snapshot.get("last_duration_ms"))
                  if slow else "The last check failed; waiting for the next retry")
    else:
        severity = "ok"
        state = "OK"
        action = "Normal"
    action_override = str(metadata.get("action", "") or "").strip()
    if service == "js8call" and js8_mode in {"api_basic", "api_full"} and not last_error and slow:
        action_override = "JS8Call API is reachable, but its recent checks are slow. " + _format_duration_ms(snapshot.get("last_duration_ms"))
    if service == "js8call" and warning and last_error:
        action_override = "JS8Call status check failed: " + _shorten_error(last_error)
    if action_override and not fldigi_busy_check and (not scheduler_hold or active_scheduler_hold):
        action = action_override
    scope = ""
    if scope_resolver is not None:
        try:
            scope = str(scope_resolver(str(key or ""), metadata) or "").strip()
        except Exception:
            scope = ""
    if not scope:
        scope = _scope_from_metadata(metadata)
    return {
        "key": str(key or ""),
        "scope": scope,
        "dependency": _dependency_label(str(key or ""), owner),
        "state": state,
        "severity": severity,
        "action": action,
        "last_issue": _shorten_error(last_error) if warning or backoff else "",
        "issue_since": _format_issue_since(snapshot.get("issue_started_ts"), now=now) if warning else "",
        "cooldown": _format_cooldown(cooldown),
        "last_check": _format_age(snapshot.get("last_checked_ts"), now=now),
        "last_check_ts": _float_value(snapshot.get("last_checked_ts")),
        "last_duration": _format_duration_ms(snapshot.get("last_duration_ms")),
        "failures": failures,
        "slow": slow,
        "is_issue": severity in {"danger", "warning"},
        "group": "background_ingest" if str(key or "").strip().lower().startswith("background-ingest:") else "",
    }


def _collapse_background_ingest_ok_items(items: List[Dict[str, object]]) -> List[Dict[str, object]]:
    collapsed: List[Dict[str, object]] = []
    groups: Dict[str, List[Dict[str, object]]] = {}
    for item in items:
        if item.get("group") == "background_ingest" and not item.get("is_issue"):
            groups.setdefault(str(item.get("scope", "") or "Station-wide"), []).append(item)
        else:
            collapsed.append(item)
    for scope, group_items in groups.items():
        if not group_items:
            continue
        latest = max(group_items, key=lambda item: _float_value(item.get("last_check_ts")))
        row = dict(latest)
        row["scope"] = scope
        row["dependency"] = "Background ingest jobs"
        row["action"] = f"Normal ({len(group_items)} jobs healthy)"
        row["last_issue"] = ""
        row["issue_since"] = ""
        row["cooldown"] = ""
        collapsed.append(row)
    return collapsed


def _merge_equivalent_endpoint_items(items: List[Dict[str, object]]) -> List[Dict[str, object]]:
    severity_rank = {"danger": 0, "warning": 1, "info": 2, "ok": 3}
    merged: Dict[str, Dict[str, object]] = {}
    for item in items:
        key = _canonical_endpoint_key(item.get("key", ""))
        existing = merged.get(key)
        if existing is None:
            merged[key] = item
            continue
        if key.startswith("js8call:"):
            # TCP/fallback and capability probes publish separate records for
            # one endpoint. A recovered endpoint must not retain an older
            # failure simply because warnings sort ahead of healthy results.
            checked = _float_value(item.get("last_check_ts"))
            existing_checked = _float_value(existing.get("last_check_ts"))
            if checked != existing_checked:
                if checked > existing_checked:
                    merged[key] = item
                continue
        item_rank = severity_rank.get(str(item.get("severity", "ok")), 3)
        existing_rank = severity_rank.get(str(existing.get("severity", "ok")), 3)
        if item_rank < existing_rank:
            merged[key] = item
        elif item_rank == existing_rank and _float_value(item.get("last_check_ts")) >= _float_value(
            existing.get("last_check_ts")
        ):
            merged[key] = item
    return list(merged.values())


def _observability_item(
    *,
    key: str,
    scope: str,
    dependency: str,
    state: str,
    severity: str,
    action: str,
    last_issue: str = "",
    is_issue: bool = False,
) -> Dict[str, object]:
    return {
        "key": key,
        "scope": scope,
        "dependency": dependency,
        "state": state,
        "severity": severity,
        "action": action,
        "last_issue": _shorten_error(last_issue),
        "issue_since": "",
        "cooldown": "",
        "last_check": "",
        "last_check_ts": 0.0,
        "last_duration": "",
        "failures": 0,
        "slow": 0,
        "is_issue": bool(is_issue),
        "group": "runtime_observability",
    }


def runtime_observability_items(
    *,
    station_poll_metrics: Optional[Mapping[str, object]] = None,
    scheduler_poll_metrics: Optional[Mapping[str, object]] = None,
    scheduler_companion_status: Optional[Mapping[str, object]] = None,
    assigned_schedule_status: Optional[Sequence[Mapping[str, object]]] = None,
    background_job_status: Optional[Mapping[str, object]] = None,
    js8_registry_status: Optional[Sequence[Mapping[str, object]]] = None,
    runtime_source_rows: Optional[Sequence[Mapping[str, object]]] = None,
) -> List[Dict[str, object]]:
    items: List[Dict[str, object]] = []
    source_rows = runtime_source_rows or ()
    covered_source_ids = _runtime_source_ids(source_rows)
    if station_poll_metrics:
        items.append(_poll_metrics_item("runtime:poll:station", "Station-wide", "Station runtime polling", station_poll_metrics))
    if scheduler_poll_metrics:
        items.append(_poll_metrics_item("runtime:poll:scheduler", "Station-wide", "Scheduler status polling", scheduler_poll_metrics))
    if scheduler_companion_status:
        items.extend(_scheduler_companion_status_items(scheduler_companion_status))
    if assigned_schedule_status:
        items.extend(assigned_schedule_observability_items(assigned_schedule_status))
    if background_job_status:
        items.append(_background_job_status_item(background_job_status))
        source_skip_item = _background_source_skips_item(background_job_status, excluded_source_ids=covered_source_ids)
        if source_skip_item:
            items.append(source_skip_item)
    source_view_item = _runtime_source_view_item(source_rows)
    if source_view_item:
        items.append(source_view_item)
    covered_js8_api_endpoints = _runtime_source_js8_api_endpoints(source_rows)
    for row in js8_registry_status or ():
        if isinstance(row, Mapping):
            if _js8_registry_status_endpoint(row) in covered_js8_api_endpoints:
                continue
            items.append(_js8_registry_item(row))
    return items


def assigned_schedule_observability_items(assignments: Sequence[Mapping[str, object]]) -> List[Dict[str, object]]:
    items: List[Dict[str, object]] = []
    for raw in assignments or ():
        if not isinstance(raw, Mapping):
            continue
        validation_raw = raw.get("validation_status_json", raw.get("validation", ""))
        validation: Mapping[str, object]
        if isinstance(validation_raw, Mapping):
            validation = validation_raw
        else:
            try:
                loaded = json.loads(str(validation_raw or "{}"))
                validation = loaded if isinstance(loaded, Mapping) else {}
            except Exception:
                validation = {}
        state = str(validation.get("state") or "").strip().lower()
        if state not in {"blocked", "warning"}:
            continue
        radio_name = str(
            raw.get("device_name")
            or raw.get("radio_name")
            or raw.get("name")
            or f"Radio {raw.get('device_profile_id') or ''}"
        ).strip()
        plan_name = str(raw.get("frequency_plan_name") or raw.get("plan_name") or "assigned Frequency Plan").strip()
        messages = [
            str(item).strip()
            for key in ("blocked", "warnings", "messages")
            for item in (validation.get(key) or [])
            if str(item or "").strip()
        ]
        detail = messages[0] if messages else "RF Guard reported an assigned schedule issue."
        dependency = "Schedule Assignment RF Guard"
        label = f"{radio_name} / {plan_name}".strip(" /")
        items.append(
            _observability_item(
                key=f"runtime:schedule-assignment-rf-guard:{raw.get('device_profile_id') or radio_name}:{raw.get('frequency_plan_id') or plan_name}",
                scope=radio_name or "Station-wide",
                dependency=dependency,
                state="Blocked" if state == "blocked" else "Warning",
                severity="danger" if state == "blocked" else "warning",
                action="Review assigned Frequency Plan before schedule changes",
                last_issue=f"{label}: {detail}" if label else detail,
                is_issue=True,
            )
        )
    return items


def _scheduler_companion_status_items(status: Mapping[str, object]) -> List[Dict[str, object]]:
    items: List[Dict[str, object]] = []
    if bool(status.get("rf_conflict_warning")):
        summary = str(status.get("rf_conflict_summary", "") or "").strip()
        detail = str(status.get("rf_conflict_detail", "") or "").strip()
        peer_detail = str(status.get("rf_conflict_peer_status_detail", "") or "").strip()
        peer_name = str(status.get("rf_conflict_peer_name", "") or "").strip()
        if peer_detail:
            detail = f"{detail}; {peer_detail}" if detail else peer_detail
        if not detail:
            detail = summary or "RF Guard detected a condition that needs operator review."
        if peer_name and peer_name not in detail:
            detail = f"{peer_name}: {detail}"
        state = "Verify" if bool(status.get("rf_conflict_peer_status_unknown")) or bool(status.get("rf_conflict_peer_status_stale")) else "Warning"
        items.append(
            _observability_item(
                key="runtime:scheduler:rf-guard",
                scope="Station-wide",
                dependency="RF Guard",
                state=state,
                severity="warning",
                action="Review RF Guard before changing frequency",
                last_issue=detail,
                is_issue=True,
            )
        )
    companions = (
        ("js8", "JS8Call status", "Verify JS8Call status"),
        ("varac", "VarAC status", "Verify VarAC status"),
    )
    for prefix, dependency, action in companions:
        if not bool(status.get(f"{prefix}_status_stale")):
            continue
        detail = str(status.get(f"{prefix}_status_detail", "") or "").strip()
        if not detail:
            detail = f"{dependency} is stale or unavailable."
        items.append(
            _observability_item(
                key=f"runtime:companion:{prefix}",
                scope="Station-wide",
                dependency=dependency,
                state="Verify",
                severity="warning",
                action=action,
                last_issue=detail,
                is_issue=True,
            )
        )
    return items


def _runtime_source_ids(rows: Sequence[Mapping[str, object]]) -> set[str]:
    ids: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        source_id = str(row.get("source_id", "") or "").strip()
        if source_id:
            ids.add(source_id)
    return ids


def _poll_metrics_item(key: str, scope: str, dependency: str, metrics: Mapping[str, object]) -> Dict[str, object]:
    started = _int_value(metrics.get("polls_started"))
    succeeded = _int_value(metrics.get("polls_succeeded"))
    failed = _int_value(metrics.get("polls_failed"))
    cache_hits = _int_value(metrics.get("cache_hits"))
    backoff_hits = _int_value(metrics.get("backoff_hits"))
    inflight_hits = _int_value(metrics.get("inflight_hits"))
    snapshots = _int_value(metrics.get("snapshot_count"))
    parts = [
        f"{started} polls",
        f"{succeeded} ok",
        f"{cache_hits} cache hits",
    ]
    if backoff_hits:
        parts.append(f"{backoff_hits} backoff returns")
    if inflight_hits:
        parts.append(f"{inflight_hits} shared in-flight returns")
    if snapshots:
        parts.append(f"{snapshots} cached snapshots")
    severity = "info" if failed else "ok"
    state = "Observed" if failed else "OK"
    last_issue = f"{failed} failed poll{'s' if failed != 1 else ''}" if failed else ""
    if failed:
        parts.insert(2, f"{failed} failed")
    return _observability_item(
        key=key,
        scope=scope,
        dependency=dependency,
        state=state,
        severity=severity,
        action=", ".join(parts),
        last_issue=last_issue,
        is_issue=False,
    )


def _background_job_status_item(status: Mapping[str, object]) -> Dict[str, object]:
    queued = status.get("queued_jobs", {})
    realtime = status.get("realtime_jobs", {})
    skipped = status.get("skipped_counts", {})
    skip_reasons = status.get("skip_reasons", {})
    if not isinstance(skip_reasons, Mapping) or not skip_reasons:
        skip_reasons = status.get("refresh_skip_reasons", {})
    timeout_warned = tuple(status.get("timeout_warned", ()) or ())
    running_names: List[str] = []
    if isinstance(queued, Mapping):
        running_names.extend(str(name) for name, value in queued.items() if isinstance(value, Mapping) and not bool(value.get("done")))
    if isinstance(realtime, Mapping):
        running_names.extend(str(name) for name, value in realtime.items() if isinstance(value, Mapping) and not bool(value.get("done")))
    skipped_total = sum(_int_value(value) for value in (skipped.values() if isinstance(skipped, Mapping) else ()))
    if timeout_warned:
        return _observability_item(
            key="runtime:ingest:jobs",
            scope="Station-wide",
            dependency="Background ingest controller",
            state="Warning",
            severity="warning",
            action=f"Long-running job warning: {', '.join(str(name) for name in timeout_warned)}",
            last_issue="One or more background ingest jobs exceeded the watchdog threshold.",
            is_issue=True,
        )
    if running_names:
        action = f"Running: {', '.join(running_names[:4])}"
        if len(running_names) > 4:
            action += f" +{len(running_names) - 4} more"
    else:
        action = "Idle"
    if skipped_total:
        reason_bits: List[str] = []
        if isinstance(skip_reasons, Mapping):
            for name, reason in sorted(skip_reasons.items()):
                reason_txt = str(reason or "").strip()
                if reason_txt:
                    reason_bits.append(f"{_background_job_label(name) or name}: {_background_skip_reason_label(reason_txt)}")
        reason_suffix = f" ({'; '.join(reason_bits[:3])})" if reason_bits else ""
        action = f"{action}; {skipped_total} trigger{'s' if skipped_total != 1 else ''} skipped{reason_suffix}"
    decision_summary = _background_refresh_decision_summary(status.get("refresh_decisions", {}))
    if decision_summary:
        action = f"{action}; Latest: {decision_summary}"
    return _observability_item(
        key="runtime:ingest:jobs",
        scope="Station-wide",
        dependency="Background ingest controller",
        state="OK",
        severity="ok",
        action=action,
        is_issue=False,
    )


def _background_refresh_decision_summary(value: object) -> str:
    if not isinstance(value, Mapping) or not value:
        return ""
    parts: List[str] = []
    for name, raw_decision in sorted(value.items()):
        if not isinstance(raw_decision, Mapping):
            continue
        label = _background_job_label(name) or str(name or "").strip()
        if not label:
            continue
        reason = str(raw_decision.get("reason", "") or "").strip()
        should_run = bool(raw_decision.get("should_run", False))
        text = _background_refresh_decision_label(reason, should_run=should_run)
        if text:
            parts.append(f"{label}: {text}")
        if len(parts) >= 4:
            break
    return "; ".join(parts)


def _background_refresh_decision_label(reason: object, *, should_run: bool) -> str:
    key = str(reason or "").strip().lower().replace("-", "_")
    if should_run:
        labels = {
            "forced": "manual refresh queued",
            "realtime_source": "realtime source refresh queued",
            "first_run": "initial refresh queued",
            "source_changed": "source changed, refresh queued",
            "cadence": "scheduled refresh queued",
        }
        return labels.get(key, f"{key.replace('_', ' ')} queued" if key else "refresh queued")
    labels = {
        "unchanged": "no source changes",
        "cadence": "waiting for next scheduled refresh",
        "backoff": "waiting before retry",
        "already_running": "already running",
    }
    return labels.get(key, key.replace("_", " ") if key else "not queued")


def _background_skip_reason_label(value: object) -> str:
    key = str(value or "").strip().lower().replace("-", "_")
    labels = {
        "unchanged": "unchanged",
        "already_running": "already running",
        "backoff": "cooldown",
        "cadence": "cadence pass",
        "forced": "manual refresh",
        "source_changed": "source changed",
        "first_run": "first run",
        "realtime_source": "realtime source",
    }
    return labels.get(key, key.replace("_", " ") if key else "skipped")


def _runtime_source_view_item(rows: Sequence[Mapping[str, object]]) -> Optional[Dict[str, object]]:
    clean_rows = [row for row in rows if isinstance(row, Mapping)]
    if not clean_rows:
        return None
    issue_rows = [
        row
        for row in clean_rows
        if str(row.get("severity", "") or "").strip().lower() == "warning"
        and str(row.get("state", "") or "").strip().lower() not in {"backoff"}
    ]
    labels: List[str] = []
    for row in clean_rows[:5]:
        title = str(row.get("title", "") or row.get("source_id", "") or "Source").strip()
        state = str(row.get("state_label", "") or row.get("state", "") or "Observed").strip()
        labels.append(f"{title}: {state}")
    if len(clean_rows) > 5:
        labels.append(f"+{len(clean_rows) - 5} more")
    severity = "warning" if issue_rows else "info"
    state = "Warning" if issue_rows else "Observed"
    if issue_rows:
        noun = "source" if len(issue_rows) == 1 else "sources"
        prefix = f"{len(issue_rows)} {noun} need{'s' if len(issue_rows) == 1 else ''} attention"
    else:
        prefix = f"{len(clean_rows)} source{'s' if len(clean_rows) != 1 else ''} observed"
    projection_summary = _runtime_source_projection_summary(clean_rows)
    action_parts = [f"{prefix}: " + "; ".join(labels)]
    if projection_summary:
        action_parts.append(f"Projected Data: {projection_summary}")
    return _observability_item(
        key="runtime:ingest:source-view",
        scope="Station-wide",
        dependency="Runtime ingest sources",
        state=state,
        severity=severity,
        action=". ".join(action_parts),
        last_issue="One or more ingest sources need attention." if issue_rows else "",
        is_issue=bool(issue_rows),
    )


def _runtime_source_projection_summary(rows: Sequence[Mapping[str, object]]) -> str:
    message_count = 0
    artifact_count = 0
    link_count = 0
    pair_count = 0
    for row in rows:
        kind = str(row.get("source_kind", "") or "").strip().lower()
        count = _int_value(row.get("projection_count"))
        if count > 0:
            if "commstat" in kind:
                artifact_count += count
            else:
                message_count += count
        metadata = row.get("metadata")
        if not isinstance(metadata, Mapping):
            continue
        link_summary = metadata.get("link_projection_summary")
        if not isinstance(link_summary, Mapping):
            continue
        link_count += _int_value(link_summary.get("total"))
        pair_count += _int_value(link_summary.get("station_pairs"))
    parts: List[str] = []
    if message_count:
        parts.append(f"{message_count} message{'s' if message_count != 1 else ''}")
    if artifact_count:
        parts.append(f"{artifact_count} artifact{'s' if artifact_count != 1 else ''}")
    if link_count:
        link_text = f"{link_count} link{'s' if link_count != 1 else ''}"
        if pair_count:
            link_text += f" / {pair_count} pair{'s' if pair_count != 1 else ''}"
        parts.append(link_text)
    return ", ".join(parts)


def _runtime_source_js8_api_endpoints(rows: Sequence[Mapping[str, object]]) -> set[str]:
    endpoints: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        kind = str(row.get("source_kind", "") or "").strip().lower()
        if kind != "js8call api":
            continue
        endpoint = _canonical_endpoint_key(row.get("location", ""))
        if endpoint:
            endpoints.add(endpoint)
    return endpoints


def _background_source_skips_item(
    status: Mapping[str, object],
    *,
    excluded_source_ids: Iterable[str] = (),
) -> Optional[Dict[str, object]]:
    source_skips = status.get("source_skip_reasons", {})
    if not isinstance(source_skips, Mapping) or not source_skips:
        return None
    excluded = {str(source_id or "").strip() for source_id in excluded_source_ids if str(source_id or "").strip()}
    filtered_skips = {
        key: row
        for key, row in source_skips.items()
        if isinstance(row, Mapping)
        and str(row.get("source_id", "") or "").strip() not in excluded
    }
    if not filtered_skips:
        return None
    view_rows = runtime_source_view_rows_from_skip_reasons(filtered_skips)
    rows: List[Mapping[str, object]] = [row for row in filtered_skips.values() if isinstance(row, Mapping)]
    if not rows:
        return None
    issue_rows = [row for row in view_rows if row.severity == "warning" and row.state != "backoff"]
    labels: List[str] = []
    for row in view_rows[:4]:
        labels.append(f"{row.title}: {row.detail or row.state_label}")
    if len(view_rows) > 4:
        labels.append(f"+{len(view_rows) - 4} more")
    severity = "warning" if issue_rows else "info"
    state = "Warning" if issue_rows else "Observed"
    return _observability_item(
        key="runtime:ingest:sources",
        scope="Station-wide",
        dependency="Background ingest sources",
        state=state,
        severity=severity,
        action="; ".join(labels),
        last_issue="One or more ingest sources could not be read." if issue_rows else "",
        is_issue=bool(issue_rows),
    )


def _js8_registry_item(status: Mapping[str, object]) -> Dict[str, object]:
    key = str(status.get("key", "") or "").strip()
    name = str(status.get("name", "") or "").strip() or key or "JS8Call API"
    connected = bool(status.get("connected", False))
    running = bool(status.get("running", False))
    listeners = _int_value(status.get("listener_count"))
    pending = _int_value(status.get("pending_request_count"))
    queued = _int_value(status.get("queued_event_count"))
    last_error = str(status.get("last_error", "") or "").strip()
    if connected:
        return _observability_item(
            key=f"runtime:js8:{key}",
            scope=name,
            dependency="Shared JS8Call API client",
            state="OK",
            severity="ok",
            action=f"Connected; {listeners} listeners, {pending} pending requests, {queued} queued events",
            is_issue=False,
        )
    if running:
        return _observability_item(
            key=f"runtime:js8:{key}",
            scope=name,
            dependency="Shared JS8Call API client",
            state="Warning",
            severity="warning",
            action="Client is running but not connected",
            last_issue=last_error or "JS8Call API client is reconnecting.",
            is_issue=True,
        )
    return _observability_item(
        key=f"runtime:js8:{key}",
        scope=name,
        dependency="Shared JS8Call API client",
        state="Idle",
        severity="info",
        action="Registered but not running",
        is_issue=False,
    )


def _js8_registry_status_endpoint(status: Mapping[str, object]) -> str:
    key = str(status.get("key", "") or "").strip()
    if key:
        return _canonical_endpoint_key(key)
    host = str(status.get("host", "") or "").strip()
    port = status.get("port")
    if host and port not in (None, ""):
        return _canonical_endpoint_key(f"{host}:{port}")
    return ""


def _scheduler_event_is_issue(item: Mapping[str, object]) -> bool:
    event_type = str(item.get("event_type", "") or "").strip().lower()
    code = str(item.get("code", "") or "").strip().lower()
    if code in {"fldigi_busy_check_failed", "fldigi_busy_check_queued", "already_applied", "matching_readback_adopted", "post_apply_on_schedule"}:
        return False
    if code in {"rf_safety_guard_block", "rf_safety_guard_warning", "coordination_conflict"}:
        return True
    if event_type in {"failed", "blocked", "warning", "hold", "skip", "watchdog", "breakaway"}:
        return True
    return any(token in code for token in ("failed", "error", "timeout", "busy", "backoff", "stuck"))


def _scheduler_event_is_success(item: Mapping[str, object]) -> bool:
    if _scheduler_event_is_issue(item):
        return False
    event_type = str(item.get("event_type", "") or "").strip().lower()
    code = str(item.get("code", "") or "").strip().lower()
    if code in {"fldigi_busy_check_failed", "fldigi_busy_check_queued"}:
        return False
    return event_type in {"applied", "resume", "verified", "status"} or code in {
        "already_applied",
        "post_apply_on_schedule",
        "fldigi_busy_check_result",
    }


def _summarize_scheduler_events(events: List[Dict[str, object]], *, issue_limit: int = 24) -> List[Dict[str, object]]:
    latest_success: Dict[str, Dict[str, object]] = {}
    issues: List[Dict[str, object]] = []
    for raw in events:
        if not isinstance(raw, Mapping):
            continue
        item = dict(raw)
        if _scheduler_event_is_issue(item):
            item["_station_health_kind"] = "issue"
            issues.append(item)
            continue
        metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
        identity = str(item.get("radio_profile_id") or metadata.get("radio_profile_id") or "unattributed")
        if identity not in latest_success and _scheduler_event_is_success(item):
            item["_station_health_kind"] = "latest_success"
            latest_success[identity] = item
    # Bounded input/history; keep each radio's latest success instead of one
    # radio hiding its peers' successful schedule transitions.
    out: List[Dict[str, object]] = list(latest_success.values())
    out.extend(issues[: max(0, int(issue_limit or 24))])
    return out


def summarize_station_health(
    registry_snapshot: Optional[Mapping[str, object]] = None,
    *,
    include_ok: bool = True,
    include_scheduler_events: bool = False,
    scope_resolver: Optional[ScopeResolver] = None,
    extra_items: Optional[Iterable[Mapping[str, object]]] = None,
) -> Dict[str, object]:
    """
    Build a low-impact Station Health summary from the existing health registry.

    This intentionally does not probe external software. It only reports what
    existing FIO work has already learned about dependency responsiveness.
    """
    if registry_snapshot is None:
        try:
            registry_snapshot = get_dependency_health_registry().snapshot()
        except Exception:
            registry_snapshot = {}
    now = time.monotonic()
    items: List[Dict[str, object]] = []
    if isinstance(registry_snapshot, Mapping):
        iterable: Iterable[tuple[str, object]] = registry_snapshot.items()
    else:
        iterable = []
    for key, raw_snapshot in iterable:
        if not isinstance(raw_snapshot, Mapping):
            continue
        item = _item_from_snapshot(str(key or ""), raw_snapshot, scope_resolver=scope_resolver, now=now)
        items.append(item)
    for raw_item in extra_items or ():
        if not isinstance(raw_item, Mapping):
            continue
        item = dict(raw_item)
        items.append(item)
    items = _collapse_background_ingest_ok_items(items)
    items = _merge_equivalent_endpoint_items(items)
    # Recovery evidence must participate in endpoint merging even when the
    # caller only wants current issues (including the shell issue counter).
    if not include_ok:
        items = [item for item in items if item.get("is_issue")]
    items.sort(
        key=lambda item: (
            {"danger": 0, "warning": 1, "info": 2, "ok": 3}.get(str(item.get("severity")), 4),
            str(item.get("scope", "")),
            str(item.get("dependency", "")),
        )
    )
    issue_items = [item for item in items if item.get("is_issue")]
    severity = "ok"
    if any(str(item.get("severity")) == "danger" for item in issue_items):
        severity = "danger"
    elif issue_items:
        severity = "warning"
    recent_scheduler_events = (
        _summarize_scheduler_events(load_recent_scheduler_events(limit=100)) if include_scheduler_events else []
    )
    return {
        "severity": severity,
        "issue_count": len(issue_items),
        "items": items,
        "issue_items": issue_items,
        "recent_scheduler_events": recent_scheduler_events,
    }
