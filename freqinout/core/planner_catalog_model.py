"""Worker-only compilation of a detached Planner catalogue.

Existing SQL normalization and shared context/RF rules run against a private
in-memory copy, never live UI stores. Only detached values leave this module.
"""
from __future__ import annotations
import copy
import datetime
import json
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, Optional

from freqinout.core.planner_input_catalog import PlannerInputCatalog
from freqinout.core.schedule_source_inputs import ScheduleSourceError
from freqinout.core.multi_radio_store import MultiRadioStore, _GuidedTransactionConnection
from freqinout.core.plan_context_service import PlanContextService, PlanContextSnapshot
from freqinout.core.shared_state_persistence import SharedStatePersistenceAdapter
from freqinout.core.schedule_source_sets import (
    NO_NET_SOURCE_SET_ID, NO_NET_SOURCE_SET_LABEL,
    SELECTED_HF_DAILY_SOURCE_SET_KEY, SELECTED_HF_NET_SOURCE_SET_KEY,
    source_sets_for_category, plan_source_usage_summary,
    HF_DAILY_SOURCE_SETS_KEY, HF_NET_SOURCE_SETS_KEY,
    HF_DAILY_SOURCE_CATEGORY, HF_NET_SOURCE_CATEGORY,
)


def validation_key(radio_id: int, payload: dict) -> str:
    clean = {k: v for k, v in payload.items() if not k.startswith("_catalog_")}
    return str(int(radio_id)) + ":" + json.dumps(clean, sort_keys=True, default=str, separators=(",", ":"))


class DetachedSettings:
    def __init__(self, values: dict, config_dir: Path):
        self._data = copy.deepcopy(values)
        self.config_dir = Path(config_dir)
        self.db_path = self.config_dir / "freqinout.db"

    def get(self, key, default=None):
        return self._data.get(key, default)

    def all(self):
        return copy.deepcopy(self._data)


class _MemoryStore(MultiRadioStore):
    def __init__(self, connection):
        # No schema assurance, filesystem probes or live connection is owned by
        # this adapter. Shared helpers see only this worker's private copy.
        self.connection = connection

    def _connect(self):
        return _GuidedTransactionConnection(self.connection)

    def _connect_readonly(self):
        return _GuidedTransactionConnection(self.connection)


class _BorrowedConnection:
    def __init__(self, connection):
        self.connection = connection

    def close(self):
        return None

    def __getattr__(self, name):
        return getattr(self.connection, name)


class _SourceReader:
    def __init__(self, catalog, connections):
        self.catalog = catalog
        self.config_dir = catalog.config_dir
        self.settings = DetachedSettings(catalog.settings, catalog.config_dir)
        self.connections = connections

    def has_database(self, filename):
        return ("settings" if filename == "freqinout.db" else "nets") in self.catalog.present_databases

    def connect(self, filename):
        return _BorrowedConnection(self.connections["settings" if filename == "freqinout.db" else "nets"])

    def read_table(self, database, names):
        for name in names:
            if f"{database}:{name}" in self.catalog.tables:
                return name, self.catalog.rows(name, database)
        return None

    @staticmethod
    def _normalize_condition_levels(value: Any) -> str:
        raw = str(value or "").strip().upper()
        if not raw or raw == "ALL":
            return "ALL"
        vals: List[int] = []
        for token in raw.replace(";", ",").replace("|", ",").split(","):
            token = token.strip()
            if not token:
                continue
            if token == "ALL":
                return "ALL"
            try:
                lvl = int(token)
            except Exception:
                continue
            if 1 <= lvl <= 5:
                vals.append(lvl)
        if not vals:
            return "ALL"
        return ",".join(str(v) for v in sorted(set(vals)))


    def _condition_level_map(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        rows = self.settings.get("operating_groups", []) or []
        if not isinstance(rows, list):
            return out
        for row in rows:
            if not isinstance(row, dict):
                continue
            group = str(row.get("group", "") or "").strip().upper()
            if not group:
                continue
            if not bool(row.get("use_condition_levels", False)):
                continue
            try:
                level = int(row.get("condition_level", 0) or 0)
            except Exception:
                level = 0
            if not (1 <= level <= 5):
                continue
            prev = out.get(group)
            if prev is None or level < prev:
                out[group] = level
        return out


    @classmethod
    def _condition_level_match(cls, condition_levels: str, group_level: Optional[int]) -> bool:
        normalized = cls._normalize_condition_levels(condition_levels)
        if normalized == "ALL":
            return True
        if group_level is None:
            return True
        allowed: Set[int] = set()
        for token in normalized.split(","):
            token = token.strip()
            if not token:
                continue
            try:
                lvl = int(token)
            except Exception:
                continue
            if 1 <= lvl <= 5:
                allowed.add(lvl)
        if not allowed:
            return True
        return int(group_level) in allowed


    @staticmethod
    def _parse_iso_utc(value: str) -> Optional[datetime.datetime]:
        txt = str(value or "").strip()
        if not txt:
            return None
        try:
            parsed = datetime.datetime.fromisoformat(txt.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=datetime.timezone.utc)
            return parsed.astimezone(datetime.timezone.utc)
        except Exception:
            return None


    def _load_net_sop_policies_from_db(self) -> Optional[List[Dict[str, Any]]]:
        conn: sqlite3.Connection | None = None
        try:
            db_path = self.config_dir / "freqinout_nets.db"
            if not self.has_database(db_path.name):
                return None
            conn = self.connect(db_path.name)
            cur = conn.cursor()
            exists = cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='sop_net_conflict_policy'"
            ).fetchone()
            if not exists:
                return []
            cur.execute(
                """
                SELECT
                    policy,
                    window_start_utc,
                    window_end_utc,
                    net_row_signature,
                    sop_row_signature
                FROM sop_net_conflict_policy
                WHERE COALESCE(active, 1) = 1
                ORDER BY COALESCE(updated_utc, '') DESC, id DESC
                """
            )
            rows = cur.fetchall()
            out: List[Dict[str, Any]] = []
            for policy, start_utc, end_utc, net_sig, sop_sig in rows:
                pol = str(policy or "").strip().upper()
                if pol not in {"SOP_PRIORITY", "NET_PRIORITY"}:
                    continue
                start_dt = self._parse_iso_utc(str(start_utc or ""))
                end_dt = self._parse_iso_utc(str(end_utc or ""))
                if not isinstance(start_dt, datetime.datetime) or not isinstance(end_dt, datetime.datetime):
                    continue
                if end_dt <= start_dt:
                    continue
                out.append(
                    {
                        "policy": pol,
                        "start_utc": start_dt,
                        "end_utc": end_dt,
                        "net_row_signature": str(net_sig or "").strip(),
                        "sop_row_signature": str(sop_sig or "").strip(),
                    }
                )
            return out
        except Exception as exc:
            raise ScheduleSourceError(f"Cannot read Plan Builder input: {exc}") from exc
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


    def _load_hf_from_db(self) -> Optional[List[Dict]]:
        result = self.read_table("settings", ("daily_schedule_tab",))
        if result is None:
            return None
        table, rows = result
        return [dict(row, id=int(row.get("id") or 0), source_row_id=int(row.get("id") or 0), source_table=table,
                     day_utc=row.get("day_utc") or "ALL", vfo=str(row.get("vfo") or "A").strip().upper() or "A",
                     frequency=str(row.get("frequency") or ""), group_name=row.get("group_name") or "",
                     band=row.get("band") or "", mode=row.get("mode") or "",
                     start_utc=row.get("start_utc") or "", end_utc=row.get("end_utc") or "",
                     auto_tune=bool(row.get("auto_tune"))) for row in rows]


    def _load_net_from_db(self) -> Optional[List[Dict]]:
        result = self.read_table("nets", ("net_schedule_tab", "net_schedule"))
        if result is None:
            return None
        table, rows = result
        return [dict(row, id=int(row.get("id") or 0), source_row_id=int(row.get("id") or 0), source_table=table,
                     day_utc=row.get("day_utc") or "ALL", recurrence=row.get("recurrence") or "Weekly",
                     biweekly_offset_weeks=row.get("biweekly_offset_weeks") or 0,
                     month_weeks=row.get("month_weeks") or "", vfo=str(row.get("vfo") or "A").strip().upper() or "A",
                     frequency=str(row.get("frequency") or ""), early_checkin=int(row.get("early_checkin") or 0),
                     group_name=row.get("group_name") or row.get("primary_js8call_group") or "",
                     band=row.get("band") or "", mode=row.get("mode") or "",
                     start_utc=row.get("start_utc") or "", end_utc=row.get("end_utc") or "",
                     resource_id=int(row["resource_id"]) if row.get("resource_id") not in (None, "") else None,
                     primary_js8call_group=row.get("primary_js8call_group") or "",
                     comment=row.get("comment") or "", net_name=row.get("net_name") or "",
                     target_device_profile_id=row.get("target_device_profile_id"),
                     target_operating_profile_id=row.get("target_operating_profile_id"),
                     target_scope=row.get("target_scope") or "station") for row in rows]


    def _load_net_resources_from_db(self) -> Optional[List[Dict[str, Any]]]:
        """
        Load known net resources from config/freqinout_nets.db.
        These may be included in SOP Schedule Plans even when they are not folded into HF Nets.
        """
        conn: sqlite3.Connection | None = None
        try:
            db_path = self.config_dir / "freqinout_nets.db"
            if not self.has_database(db_path.name):
                return None
            conn = self.connect(db_path.name)
            cur = conn.cursor()
            if not cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='net_resources'").fetchone():
                return None
            exists = cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='net_resources'"
            ).fetchone()
            if not exists:
                return []
            cur.execute(
                """
                SELECT
                    id, resource_set, source_type, source_ref, readonly, day_utc, recurrence,
                    biweekly_offset_weeks, month_weeks, group_name, band, mode, frequency,
                    start_utc, end_utc, early_checkin, primary_js8call_group, coverage, comment, net_name,
                    fldigi_mode, fldigi_offset, updated_utc
                  FROM net_resources
                """
            )
            out: List[Dict[str, Any]] = []
            for row in cur.fetchall():
                out.append(
                    {
                        "id": int(row[0] or 0),
                        "resource_id": int(row[0] or 0),
                        "source_table": "net_resources",
                        "source_key": f"NET_RESOURCE:{int(row[0] or 0)}" if int(row[0] or 0) > 0 else "",
                        "resource_set": row[1] or "Custom",
                        "source_type": row[2] or "manual",
                        "source_ref": row[3] or "",
                        "readonly": int(row[4] or 0),
                        "day_utc": row[5] or "ALL",
                        "recurrence": row[6] or "Weekly",
                        "biweekly_offset_weeks": int(row[7] or 0),
                        "month_weeks": row[8] or "",
                        "group_name": row[9] or row[16] or "",
                        "band": row[10] or "",
                        "mode": row[11] or "",
                        "frequency": str(row[12] or ""),
                        "start_utc": row[13] or "",
                        "end_utc": row[14] or "",
                        "early_checkin": str(row[15] if row[15] is not None else 0),
                        "primary_js8call_group": row[16] or "",
                        "coverage": row[17] or "",
                        "comment": row[18] or "",
                        "net_name": row[19] or "",
                        "fldigi_mode": row[20] or "",
                        "fldigi_offset": row[21] or "",
                        "updated_utc": row[22] or "",
                    }
                )
            return out
        except Exception as exc:
            raise ScheduleSourceError(f"Cannot read Plan Builder input: {exc}") from exc
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


    def _load_sop_layer_from_db(self) -> Optional[List[Dict[str, Any]]]:
        """
        Load HF SOP schedule-layer rows from config/freqinout_nets.db.
        Only active SOP profiles are considered for planner overlay rows.
        """
        conn: sqlite3.Connection | None = None
        try:
            db_path = self.config_dir / "freqinout_nets.db"
            if not self.has_database(db_path.name):
                return None
            conn = self.connect(db_path.name)
            cur = conn.cursor()
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not ({"sop_profiles", "sop_schedule_layer"} & tables):
                return None
            if not {"sop_profiles", "sop_schedule_layer"}.issubset(tables):
                raise ScheduleSourceError("SOP input schema is incomplete")
            profile_cols: set[str] = set()
            try:
                cur.execute("PRAGMA table_info(sop_profiles)")
                profile_cols = {str(r[1] or "").strip().lower() for r in cur.fetchall() if len(r) > 1}
            except Exception as exc:
                raise ScheduleSourceError(f"Cannot inspect SOP profiles: {exc}") from exc
            has_secondary_group = "secondary_group" in profile_cols
            layer_cols: set[str] = set()
            try:
                cur.execute("PRAGMA table_info(sop_schedule_layer)")
                layer_cols = {str(r[1] or "").strip().lower() for r in cur.fetchall() if len(r) > 1}
            except Exception as exc:
                raise ScheduleSourceError(f"Cannot inspect SOP layer: {exc}") from exc
            condition_expr = "COALESCE(l.condition_levels, 'ALL')" if "condition_levels" in layer_cols else "'ALL'"
            profile_group_expr = (
                "COALESCE(NULLIF(TRIM(p.operating_group), ''), NULLIF(TRIM(p.secondary_group), ''),"
                " NULLIF(TRIM(p.name), ''), '')"
                if has_secondary_group
                else "COALESCE(NULLIF(TRIM(p.operating_group), ''), NULLIF(TRIM(p.name), ''), '')"
            )
            layer_group_expr = (
                f"COALESCE(NULLIF(TRIM(l.group_name), ''), {profile_group_expr})"
                if "group_name" in layer_cols
                else profile_group_expr
            )
            base_sql = """
                SELECT
                    COALESCE(l.id, 0),
                    COALESCE(l.profile_id, 0),
                    COALESCE(l.day_utc, 'ALL'),
                    COALESCE(l.recurrence, 'Weekly'),
                    COALESCE(l.biweekly_offset_weeks, 0),
                    COALESCE(l.month_weeks, ''),
                    {condition_expr},
                    COALESCE(l.band, ''),
                    COALESCE(l.mode, ''),
                    COALESCE(l.vfo, 'A'),
                    COALESCE(l.frequency, ''),
                    COALESCE(l.start_utc, ''),
                    COALESCE(l.end_utc, ''),
                    {layer_group_expr},
                    COALESCE(p.name, '')
                FROM sop_schedule_layer l
                JOIN sop_profiles p ON p.id = l.profile_id
                WHERE COALESCE(l.enabled, 1) = 1
                  AND (
                        TRIM(COALESCE(l.band, '')) <> ''
                        OR TRIM(COALESCE(l.frequency, '')) <> ''
                  )
                  {active_clause}
                ORDER BY p.operating_group COLLATE NOCASE, l.day_utc, l.start_utc
            """
            cur.execute(
                base_sql.format(
                    layer_group_expr=layer_group_expr,
                    condition_expr=condition_expr,
                    active_clause="AND COALESCE(p.active, 0) = 1",
                )
            )
            rows = cur.fetchall()
            out: List[Dict[str, Any]] = []
            cond_map = self._condition_level_map()
            for (
                layer_id,
                profile_id,
                day_utc,
                recurrence,
                biweekly_offset_weeks,
                month_weeks,
                condition_levels,
                band,
                mode,
                vfo,
                freq,
                start_utc,
                end_utc,
                operating_group,
                profile_name,
            ) in rows:
                group_name = str(operating_group or "").strip().upper()
                group_level = cond_map.get(group_name)
                if not self._condition_level_match(str(condition_levels or "ALL"), group_level):
                    continue
                out.append(
                    {
                        "id": int(layer_id or 0),
                        "source_row_id": int(layer_id or 0),
                        "source_table": "sop_schedule_layer",
                        "sop_layer_id": int(layer_id or 0),
                        "sop_profile_id": int(profile_id or 0),
                        "day_utc": day_utc or "ALL",
                        "recurrence": recurrence or "Weekly",
                        "biweekly_offset_weeks": biweekly_offset_weeks or 0,
                        "month_weeks": month_weeks or "",
                        "condition_levels": self._normalize_condition_levels(condition_levels),
                        "band": band or "",
                        "mode": mode or "",
                        "vfo": (vfo or "A").strip().upper() or "A",
                        "frequency": str(freq or ""),
                        "start_utc": start_utc or "",
                        "end_utc": end_utc or "",
                        "group_name": group_name,
                        "profile_name": str(profile_name or "").strip(),
                    }
                )
            return out
        except Exception as exc:
            raise ScheduleSourceError(f"Cannot read Plan Builder input: {exc}") from exc
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass



@dataclass(frozen=True)
class PlannerCatalogModel:
    catalog: PlannerInputCatalog
    plans: tuple[dict, ...]
    devices: tuple[dict, ...]
    assignments: tuple[dict, ...]
    context: PlanContextSnapshot
    sources: dict[str, tuple[dict, ...]]
    live: tuple[tuple[dict, ...], tuple[dict, ...], tuple[dict, ...], tuple[dict, ...]]
    resources: tuple[dict, ...]
    usage: dict[tuple[str, str], dict]
    validations: dict[str, dict]
    plan_index: dict[int, dict]
    device_index: dict[int, dict]
    source_index: dict[tuple[str, str], dict]
    guard_reviews: dict[str, dict]
    choices: dict[str, tuple]
    choice_indices: dict[str, dict]
    source_impacts: tuple = ()
    acknowledged_mutation: bool = False


class PlannerModelCompiler:
    """A bounded worker cache; its private SQLite copies never cross threads."""
    def __init__(self):
        self._identity = None
        self._connections = {}
        self._base = None
        self._store = None

    def close(self):
        for connection in self._connections.values():
            connection.close()
        self._connections = {}
        self._base = None
        self._identity = None
        self._store = None

    @staticmethod
    def _quote(value):
        return '"' + str(value).replace('"', '""') + '"'

    def _copy_database(self, catalog, database):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        try:
            for key, columns in catalog.columns.items():
                owner, name = key.split(":", 1)
                if owner != database:
                    continue
                definitions = []
                for column in columns:
                    definition = self._quote(column)
                    if column == "id":
                        definition += " INTEGER PRIMARY KEY"
                    elif name == "kv" and column == "key":
                        definition += " TEXT PRIMARY KEY"
                    definitions.append(definition)
                connection.execute(f'CREATE TABLE {self._quote(name)} ({",".join(definitions)})')
                rows = catalog.tables[key]
                if rows:
                    # Timestamps excluded from canonical input rows remain null
                    # bookkeeping columns in this private compatibility model.
                    connection.executemany(
                        f'INSERT INTO {self._quote(name)} VALUES ({",".join("?" for _ in columns)})',
                        (tuple(row.get(column) for column in columns) for row in rows),
                    )
            connection.commit()
            return connection
        except Exception:
            connection.close()
            raise

    @staticmethod
    def _refs(value):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError) as exc:
                raise ScheduleSourceError("A saved schedule contains unreadable references; review its source") from exc
        if not isinstance(value, (list, tuple)) or any(not isinstance(row, dict) for row in value):
            raise ScheduleSourceError("A saved schedule contains invalid source rows; review its source")
        return tuple(dict(row) for row in value)

    def build(self, catalog: PlannerInputCatalog, *, source_ids: dict[str, str] | None = None,
              validation_requests: tuple = (), runtime_status=None,
              guard_context=None, guard_payloads=()) -> PlannerCatalogModel:
        if runtime_status is None:
            raise ScheduleSourceError("Established runtime context is unavailable")
        identity = (catalog.revision, catalog.publication, runtime_status)
        if identity != self._identity:
            self.close()
            try:
                self._connections = {name: self._copy_database(catalog, name) for name in ("settings", "nets")}
                store = _MemoryStore(self._connections["settings"])
                settings = DetachedSettings(catalog.settings, catalog.config_dir)
                source_reader = _SourceReader(catalog, self._connections)
                hf = source_reader._load_hf_from_db()
                net = source_reader._load_net_from_db()
                sop = source_reader._load_sop_layer_from_db()
                policies = source_reader._load_net_sop_policies_from_db()
                hf = hf if hf is not None else settings.get("hf_schedule") or settings.get("daily_schedule") or []
                net = net if net is not None else settings.get("net_schedule") or []
                sop, policies = sop or [], policies or []
                for label, rows in (("Daily", hf), ("Net", net), ("SOP", sop), ("policy", policies)):
                    if not isinstance(rows, (list, tuple)) or any(not isinstance(row, dict) for row in rows):
                        raise ScheduleSourceError(f"Invalid {label} schedule input")
                plans = store.list_frequency_plans()
                for plan in plans:
                    # Legacy non-source plans may contain display references
                    # rather than row mappings; preserve their established
                    # interpretation instead of invalidating unrelated sources.
                    raw_refs = plan.get("schedule_refs_json", "[]")
                    try:
                        plan["_catalog_schedule_refs"] = self._refs(raw_refs)
                    except ScheduleSourceError as exc:
                        plan["_catalog_schedule_refs"] = ()
                        plan["_catalog_error"] = f"{plan.get('name') or plan['id']}: {exc}"
                from freqinout.core.planner_radio_projection import PlannerRadioRules
                radio_rules = PlannerRadioRules()
                assignments = tuple(store.list_effective_assigned_plans())
                assigned_ids = {}
                for assignment in assignments:
                    assigned_ids.setdefault(int(assignment.get("frequency_plan_id") or 0), set()).add(int(assignment.get("device_profile_id") or 0))
                for plan in plans:
                    plan["_catalog_assigned_ids"] = tuple(sorted(assigned_ids.get(int(plan["id"]), set()) - {0}))
                    plan["_catalog_lane_ids"] = tuple(sorted({radio_rules._radio_id_for_schedule_ref(row)
                                                              for row in plan["_catalog_schedule_refs"]} - {0}))
                    plan["_catalog_guard_token"] = (catalog.revision, int(plan["id"]))
                    plan["_catalog_guard_name"] = plan.get("name")
                    try:
                        source_refs = json.loads(plan.get("source_refs_json") or "[]")
                        plan["_catalog_source_refs"] = tuple(str(value) for value in source_refs if str(value or "").strip()) if isinstance(source_refs, list) else ()
                    except (ValueError, TypeError):
                        plan["_catalog_source_refs"] = ()
                    plan["_catalog_sop_refs"] = tuple(row for row in plan["_catalog_schedule_refs"]
                                                       if str(row.get("source") or "").strip().upper() == "SOP")
                sources = {
                    HF_DAILY_SOURCE_SETS_KEY: source_sets_for_category(settings, HF_DAILY_SOURCE_SETS_KEY, HF_DAILY_SOURCE_CATEGORY, strict=True, store=store),
                    HF_NET_SOURCE_SETS_KEY: source_sets_for_category(settings, HF_NET_SOURCE_SETS_KEY, HF_NET_SOURCE_CATEGORY, strict=True, store=store),
                }
                plan_by_id = {int(row["id"]): row for row in plans}
                for rows in sources.values():
                    for row in rows:
                        if row.get("storage") == "db":
                            plan = plan_by_id[int(row["db_id"])]
                            row["rows"] = plan["_catalog_schedule_refs"]
                            if plan.get("_catalog_error"):
                                row["_catalog_error"] = plan["_catalog_error"]
                        else:
                            try:
                                row["rows"] = self._refs(row.get("rows", []))
                            except ScheduleSourceError as exc:
                                row["rows"] = ()
                                row["_catalog_error"] = str(exc)
                # Runtime classification comes from the established host/worker
                # startup state, never from the deliberately narrowed KV copy.
                shared = SharedStatePersistenceAdapter(store).snapshot(runtime_status=runtime_status)
                context_service = PlanContextService(store=store)
                selection = shared.selection_state
                context = PlanContextSnapshot(
                    contexts=tuple(context_service._context_from_radio(shared, radio) for radio in shared.radio_profiles),
                    selected_radio_id=selection.settings_radio_id or selection.primary_runtime_radio_id,
                    primary_runtime_radio_id=selection.primary_runtime_radio_id,
                    active_runtime_radio_ids=selection.active_runtime_radio_ids,
                    startup_mode=shared.startup_mode,
                    warnings=shared.warnings,
                )
                devices = tuple(store.list_device_profiles())
                choices = {
                    "plans": tuple((str(row.get("name") or f"Frequency Plan #{row['id']}")
                                     + (" (draft)" if str(row.get("status") or "saved").lower() == "draft" else ""), int(row["id"]))
                                    for row in plans if str(row.get("category") or "").lower()
                                    not in {HF_DAILY_SOURCE_CATEGORY, HF_NET_SOURCE_CATEGORY}),
                    "sop": (("Active SOP Builder Layers", "__live__"),)
                           + tuple((str(row.get("name") or f"SOP Schedule Plan #{row['id']}"), str(row["id"]))
                                   for row in plans if str(row.get("category") or "").lower() == "sop_schedule"),
                }
                for key, label in ((HF_DAILY_SOURCE_SETS_KEY, "Active Daily Schedule"),
                                   (HF_NET_SOURCE_SETS_KEY, "Active Net Schedule")):
                    choices[key] = ((label, "__live__"),) + (((NO_NET_SOURCE_SET_LABEL, NO_NET_SOURCE_SET_ID),) if key == HF_NET_SOURCE_SETS_KEY else ()) + tuple(
                        (str(row.get("name") or row["id"]), str(row["id"])) for row in sources[key])
                choice_indices = {key: {identity: index for index, (_, identity) in enumerate(rows)}
                                  for key, rows in choices.items()}
                self._base = PlannerCatalogModel(
                    catalog=catalog, plans=tuple(plans), devices=devices,
                    assignments=assignments, context=context,
                    sources={key: tuple(value) for key, value in sources.items()},
                    live=tuple(tuple(dict(row) for row in rows) for rows in (hf, net, sop, policies)),
                    resources=tuple(source_reader._load_net_resources_from_db() or ()),
                    usage={}, validations={},
                    plan_index={int(row["id"]): row for row in plans},
                    device_index={int(row["id"]): row for row in devices},
                    source_index={(key, str(row["id"])): row for key, rows in sources.items() for row in rows}, guard_reviews={},
                    choices=choices, choice_indices=choice_indices,
                )
                self._store = store
                self._identity = identity
            except Exception:
                self.close()
                raise
        if source_ids is None:
            source_ids = {HF_DAILY_SOURCE_SETS_KEY: catalog.settings.get(SELECTED_HF_DAILY_SOURCE_SET_KEY, "__live__"),
                          HF_NET_SOURCE_SETS_KEY: catalog.settings.get(SELECTED_HF_NET_SOURCE_SET_KEY, "__live__")}
        usage = {}
        for key, category, label in ((HF_DAILY_SOURCE_SETS_KEY, HF_DAILY_SOURCE_CATEGORY, "Active Daily Schedule"),
                                     (HF_NET_SOURCE_SETS_KEY, HF_NET_SOURCE_CATEGORY, "Active Net Schedule")):
            selected = (source_ids or {}).get(key, "__live__")
            usage[(key, selected)] = plan_source_usage_summary(self._store, category=category, set_id=selected, live_label=label)
        validations = {}
        for radio_id, payload in validation_requests:
            clean = {key: value for key, value in payload.items() if not key.startswith("_catalog_")}
            validations[validation_key(radio_id, clean)] = self._store.validate_frequency_plan_for_device(radio_id, clean)
        from types import SimpleNamespace
        from freqinout.core.planner_guard_rules import PlannerGuardRules, guard_key
        context_radio, guided_id = guard_context or (self._base.context.selected_radio_id, 0)
        context_radio = context_radio or self._base.context.selected_radio_id or self._base.context.primary_runtime_radio_id
        rules = PlannerGuardRules()
        rules.settings = DetachedSettings(catalog.settings, catalog.config_dir)
        rules._guided_plan_handoff_device_profile_id = int(guided_id or 0)
        rules.plan_context_service = SimpleNamespace(
            store=self._store,
            context_for_tab=lambda *args, **kwargs: self._base.context.context_for_radio(context_radio))
        reviews = {}
        for payload in guard_payloads:
            if payload is None:
                continue
            validation = rules._rf_guard_preflight_for_plan(payload)
            validation = rules._merge_frequency_variant_validation(validation, payload)
            for field in ("messages", "warnings", "blocked"):
                if field in validation:
                    validation[field] = tuple(str(item).strip() for item in validation[field] or () if str(item or "").strip())
            reviews[guard_key(payload, context_radio, guided_id)] = validation
        # Every mutable value delivered to Qt is detached from this worker's
        # reusable base and private databases.
        return copy.deepcopy(replace(self._base, catalog=catalog, usage=usage, validations=validations, guard_reviews=reviews))
