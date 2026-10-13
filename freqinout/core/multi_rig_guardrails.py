from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Optional


@dataclass(frozen=True)
class MultiRigGuardrailWarning:
    warning_type: str
    resource_type: str
    resource_value: str
    affected_radio_ids: tuple[int, ...]
    affected_radio_names: tuple[str, ...]
    severity: str = "warning"
    message: str = ""

    def __post_init__(self) -> None:
        if not self.message:
            names = ", ".join(self.affected_radio_names)
            object.__setattr__(
                self,
                "message",
                f"Duplicate {self.resource_type} {self.resource_value} on active radios: {names}.",
            )


def _coerce_text(value: Any, default: str = "") -> str:
    try:
        return str(value if value is not None else default).strip()
    except Exception:
        return str(default or "").strip()


def _coerce_optional_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        if value in (None, ""):
            return default
        return int(value)
    except Exception:
        return default


def _coerce_bool_int(value: Any, default: bool = False) -> int:
    if value in (None, ""):
        return 1 if default else 0
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return 1 if int(value) != 0 else 0
    return 1 if str(value).strip().lower() in {"1", "true", "yes", "on"} else 0


def _fetchall_dicts(conn: sqlite3.Connection, query: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    original_row_factory = conn.row_factory
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(query, tuple(params))
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.row_factory = original_row_factory


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (str(table),),
    ).fetchone()
    return row is not None


def _normalized_endpoint_key(host: Any, port: Any) -> Optional[str]:
    host_text = _coerce_text(host, "").lower()
    port_int = _coerce_optional_int(port)
    if not host_text or port_int is None or port_int <= 0:
        return None
    return f"{host_text}:{int(port_int)}"


def _normalized_path_key(path: Any) -> Optional[str]:
    text = _coerce_text(path, "")
    if not text:
        return None
    return str(Path(text).expanduser()).rstrip("/\\").lower()


def _js8_profile_scope(path: Any, *, directed: bool = False) -> str:
    """Return the instance identity encoded by a JS8 config or data path.

    Native JS8Call keeps configuration under the platform config root and
    message files under the data root.  Those roots are deliberately siblings
    on Linux (``~/.config`` and ``~/.local/share``), so containment is not a
    useful ownership test.  The application/rig directory or INI stem is the
    stable scope shared by both locations.
    """
    text = _coerce_text(path, "")
    if not text:
        return ""
    # Normalize separators lexically so Windows paths are handled correctly
    # even when readiness is inspected from a non-Windows host.
    parts = [part for part in text.replace("\\", "/").rstrip("/").split("/") if part]
    if not parts:
        return ""
    leaf = parts[-1]
    if leaf.casefold().endswith(".ini") and not directed:
        return leaf[:-4].strip().casefold()
    if directed or leaf.casefold() == "directed.txt":
        parts = parts[:-1]
        if not parts:
            return ""
        leaf = parts[-1]
    # SaveDir is commonly a child of the per-rig profile directory.
    if leaf.casefold() in {"save", "data", "logs", "log"} and len(parts) > 1:
        leaf = parts[-2]
    return leaf.strip().casefold()


def _js8_paths_share_profile_scope(profile_path: Any, directed_path: Any) -> bool:
    profile_scope = _js8_profile_scope(profile_path)
    directed_scope = _js8_profile_scope(directed_path, directed=True)
    return bool(profile_scope and directed_scope and profile_scope == directed_scope)


def _js8_endpoint_guidance(rows: Iterable[Mapping[str, Any]]) -> list[MultiRigGuardrailWarning]:
    js8_rows = [
        row
        for row in rows
        if _coerce_bool_int(row.get("use_js8call"), False)
        and _normalized_endpoint_key(row.get("js8_host"), row.get("js8_port"))
    ]
    endpoints = {
        _normalized_endpoint_key(row.get("js8_host"), row.get("js8_port")) or ""
        for row in js8_rows
    }
    if len(endpoints) <= 1:
        return []
    sorted_rows = sorted(js8_rows, key=lambda item: (_coerce_text(item.get("name", ""), ""), int(item.get("id", 0) or 0)))
    names = tuple(_coerce_text(row.get("name"), "") or f"Radio {row.get('id')}" for row in sorted_rows)
    ids = tuple(int(row.get("id", 0) or 0) for row in sorted_rows)
    return [
        MultiRigGuardrailWarning(
            warning_type="js8_legacy_multi_endpoint",
            resource_type="JS8Call legacy control fallback",
            resource_value=", ".join(sorted(endpoint for endpoint in endpoints if endpoint)),
            affected_radio_ids=ids,
            affected_radio_names=names,
            severity="info",
            message=(
                "Multiple active JS8Call TCP endpoints are configured. Native CommsPlan JS8 control is endpoint-scoped, "
                "but the legacy js8net fallback can attach to only one endpoint at a time."
            ),
        )
    ]


def _js8_profile_path_warnings(rows: Iterable[Mapping[str, Any]]) -> list[MultiRigGuardrailWarning]:
    warnings: list[MultiRigGuardrailWarning] = []
    for row in rows:
        if not _coerce_bool_int(row.get("use_js8call"), False):
            continue
        profile_path = _coerce_text(row.get("js8_profile_path"), "")
        directed_path = _coerce_text(row.get("js8_directed_path"), "")
        if not profile_path or not directed_path:
            continue
        if _js8_paths_share_profile_scope(profile_path, directed_path):
            continue
        radio_id = int(row.get("id", 0) or 0)
        radio_name = _coerce_text(row.get("name"), "") or f"Radio {radio_id}"
        warnings.append(
            MultiRigGuardrailWarning(
                warning_type="js8_profile_directed_path_mismatch",
                resource_type="JS8Call profile/DIRECTED.TXT path",
                resource_value=directed_path,
                affected_radio_ids=(radio_id,),
                affected_radio_names=(radio_name,),
                severity="warning",
                message=(
                    f"{radio_name}: JS8Call DIRECTED.TXT does not match this radio's JS8 profile identity. "
                    "Review JS8Call Settings so traffic imports stay scoped to the correct radio."
                ),
            )
        )
    return warnings


def _varac_shared_db_is_cluster_scoped(
    conn: sqlite3.Connection,
    matches: Iterable[Mapping[str, Any]],
    db_path: str,
) -> bool:
    """Whether every duplicated DB user is an enabled member of one matching cluster."""
    if not (_table_exists(conn, "varac_clusters") and _table_exists(conn, "varac_cluster_members")):
        return False
    radio_ids = {int(row.get("id", 0) or 0) for row in matches}
    if not radio_ids or 0 in radio_ids:
        return False
    cluster_members: dict[int, set[int]] = {}
    for row in _fetchall_dicts(
        conn,
        """
        SELECT c.id AS cluster_id, c.shared_db_path, m.device_profile_id
          FROM varac_clusters c
          JOIN varac_cluster_members m ON m.cluster_id=c.id
         WHERE m.enabled=1
        """,
    ):
        if _normalized_path_key(row.get("shared_db_path")) != db_path:
            continue
        cluster_id = int(row.get("cluster_id", 0) or 0)
        member_id = int(row.get("device_profile_id", 0) or 0)
        if cluster_id and member_id:
            cluster_members.setdefault(cluster_id, set()).add(member_id)
    return any(radio_ids.issubset(member_ids) for member_ids in cluster_members.values())


def _duplicate_value_warnings(
    rows: Iterable[Mapping[str, Any]],
    *,
    warning_type: str,
    resource_type: str,
    value_getter: Callable[[Mapping[str, Any]], Optional[str]],
) -> list[MultiRigGuardrailWarning]:
    by_value: Dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        value = value_getter(row)
        if not value:
            continue
        by_value.setdefault(str(value), []).append(row)
    warnings: list[MultiRigGuardrailWarning] = []
    for value, matches in sorted(by_value.items()):
        if len(matches) < 2:
            continue
        sorted_matches = sorted(matches, key=lambda item: (_coerce_text(item.get("name", ""), ""), int(item.get("id", 0) or 0)))
        ids = tuple(int(item.get("id", 0) or 0) for item in sorted_matches)
        names = tuple(_coerce_text(item.get("name", ""), "") or f"Radio {item.get('id')}" for item in sorted_matches)
        warnings.append(
            MultiRigGuardrailWarning(
                warning_type=warning_type,
                resource_type=resource_type,
                resource_value=value,
                affected_radio_ids=ids,
                affected_radio_names=names,
            )
        )
    return warnings


def collect_multi_rig_guardrail_warnings(conn: sqlite3.Connection) -> tuple[MultiRigGuardrailWarning, ...]:
    """Return structured non-fatal configuration warnings for active multi-rig profiles."""
    if not _table_exists(conn, "device_profiles"):
        return ()
    rows = _fetchall_dicts(
        conn,
        """
        SELECT *
          FROM device_profiles
         WHERE enabled=1
           AND runtime_active=1
      ORDER BY display_order ASC, id ASC
        """,
    )
    tx_rows = [
        row
        for row in rows
        if _coerce_text(row.get("device_class", "tx_rx"), "tx_rx").lower() != "observer"
    ]
    warnings: list[MultiRigGuardrailWarning] = []
    warnings.extend(
        _duplicate_value_warnings(
            rows,
            warning_type="duplicate_js8_endpoint",
            resource_type="JS8Call API endpoint",
            value_getter=lambda row: _normalized_endpoint_key(row.get("js8_host"), row.get("js8_port"))
            if _coerce_bool_int(row.get("use_js8call"), False)
            else None,
        )
    )
    warnings.extend(_js8_endpoint_guidance(rows))
    warnings.extend(_js8_profile_path_warnings(rows))
    warnings.extend(
        _duplicate_value_warnings(
            rows,
            warning_type="duplicate_fldigi_endpoint",
            resource_type="FLDigi XML-RPC endpoint",
            value_getter=lambda row: _normalized_endpoint_key(row.get("fldigi_host"), row.get("fldigi_port"))
            if _coerce_bool_int(row.get("use_fldigi"), False)
            else None,
        )
    )
    warnings.extend(
        _duplicate_value_warnings(
            tx_rows,
            warning_type="duplicate_flrig_endpoint",
            resource_type="FLRig control endpoint",
            value_getter=lambda row: _normalized_endpoint_key(row.get("flrig_host"), row.get("flrig_port"))
            if _coerce_text(row.get("control_backend"), "").lower() == "flrig"
            or _coerce_bool_int(row.get("use_flrig"), False)
            else None,
        )
    )
    warnings.extend(
        _duplicate_value_warnings(
            tx_rows,
            warning_type="duplicate_rigctld_endpoint",
            resource_type="rigctld control endpoint",
            value_getter=lambda row: _normalized_endpoint_key(row.get("rig_host"), row.get("rig_port"))
            if _coerce_text(row.get("control_backend"), "").lower() == "rigctld"
            else None,
        )
    )
    warnings.extend(
        _duplicate_value_warnings(
            rows,
            warning_type="duplicate_varac_bbs_dir",
            resource_type="VarAC live BBS directory",
            value_getter=lambda row: _normalized_path_key(row.get("varac_bbs_dir"))
            if _coerce_bool_int(row.get("use_varac"), False)
            else None,
        )
    )
    varac_db_warnings = _duplicate_value_warnings(
        rows,
        warning_type="duplicate_varac_db_path",
        resource_type="VarAC database path",
        value_getter=lambda row: _normalized_path_key(row.get("varac_db_path"))
        if _coerce_bool_int(row.get("use_varac"), False)
        else None,
    )
    rows_by_id = {int(row.get("id", 0) or 0): row for row in rows}
    warnings.extend(
        warning
        for warning in varac_db_warnings
        if not _varac_shared_db_is_cluster_scoped(
            conn,
            tuple(rows_by_id[radio_id] for radio_id in warning.affected_radio_ids if radio_id in rows_by_id),
            warning.resource_value,
        )
    )
    warnings.extend(
        _duplicate_value_warnings(
            rows,
            warning_type="duplicate_flamp_message_path",
            resource_type="FLAMP message path",
            value_getter=lambda row: _normalized_path_key(row.get("flamp_message_path"))
            if _coerce_bool_int(row.get("use_flamp"), False)
            else None,
        )
    )
    warnings.extend(
        _duplicate_value_warnings(
            rows,
            warning_type="duplicate_flmsg_message_path",
            resource_type="FLMSG message path",
            value_getter=lambda row: _normalized_path_key(row.get("flmsg_message_path"))
            if _coerce_bool_int(row.get("use_flmsg"), False)
            else None,
        )
    )
    return tuple(warnings)


def format_multi_rig_guardrail_warnings(
    warnings: Iterable[MultiRigGuardrailWarning],
    *,
    include_info: bool = False,
) -> tuple[str, ...]:
    """Format review-required warnings, optionally including informational notices."""
    return tuple(
        dict.fromkeys(
            warning.message
            for warning in warnings
            if include_info or _coerce_text(warning.severity, "warning").casefold() != "info"
        )
    )
