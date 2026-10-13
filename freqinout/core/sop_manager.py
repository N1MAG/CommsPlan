from __future__ import annotations

import datetime as dt
import json
import math
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

from freqinout.core.config_paths import get_config_dir
from freqinout.core.local_ops_store import get_all_operators
from freqinout.core.logger import log
from freqinout.core.operating_group_frequency import (
    format_frequency_mhz,
    normalize_frequency_hz,
    reattach_sop_frequency_key,
    sop_frequency_reference_signature,
)
from freqinout.core.propagation_outcome_ingest import STATE_TO_FEMA_REGION
from freqinout.core.schedule_targeting import normalize_schedule_target_fields
from freqinout.core.settings_manager import SettingsManager


CONFIG_DIR = get_config_dir() / "config"
NETS_DB = CONFIG_DIR / "freqinout_nets.db"
SOP_FREQUENCY_VARIANT_REFERENCES_KEY = "sop_frequency_variant_references"


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _parse_iso_utc(value: str | None) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except Exception:
        return None


def _parse_hhmm(value: str, default_hhmm: str = "00:00") -> Tuple[int, int]:
    text = (value or default_hhmm).strip()
    try:
        hh, mm = text.split(":")
        h = max(0, min(23, int(hh)))
        m = max(0, min(59, int(mm)))
        return h, m
    except Exception:
        return 0, 0


def _day_name_from_utc(ts: dt.datetime) -> str:
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    return days[ts.weekday()]


def _table_columns(conn: sqlite3.Connection, table_name: str) -> Set[str]:
    try:
        cur = conn.execute(f"PRAGMA table_info({table_name})")
        return {str(row[1] or "").strip().lower() for row in cur.fetchall() if row and len(row) > 1}
    except Exception:
        return set()


def _norm_state(value: Any) -> str:
    txt = str(value or "").strip().upper()
    return txt[:2] if txt else ""


def _norm_region(value: Any) -> str:
    txt = str(value or "").strip().upper()
    if not txt:
        return ""
    if txt.startswith("R") and len(txt) == 2 and txt[1].isdigit():
        txt = f"R0{txt[1]}"
    if txt.startswith("R") and len(txt) == 3 and txt[1:].isdigit():
        txt = f"R{txt[1:].zfill(2)}"
    if txt in {f"R{idx:02d}" for idx in range(1, 11)}:
        return txt
    return ""


def _norm_sitrep(raw: Any) -> str:
    txt = str(raw or "").strip().upper()
    if txt in {"GREEN", "YELLOW", "RED"}:
        return txt
    return "UNKNOWN"


def _norm_group(value: Any) -> str:
    txt = str(value or "").strip().upper()
    if not txt:
        return ""
    for ch in "\"'[]{}()":
        txt = txt.replace(ch, "")
    txt = " ".join(txt.split())
    return txt.strip()


def _norm_trusted_filter(value: Any) -> str:
    txt = str(value or "").strip().upper()
    if txt in {"TRUSTED", "UNTRUSTED"}:
        return txt
    return ""


def _is_enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return int(value) != 0
    txt = str(value).strip().lower()
    if not txt:
        return False
    return txt in {"1", "true", "yes", "y", "on", "enabled"}


def _trusted_label(value: Any) -> str:
    try:
        return "TRUSTED" if int(value or 0) > 0 else "UNTRUSTED"
    except Exception:
        txt = str(value or "").strip().lower()
        return "TRUSTED" if txt in {"true", "yes", "y", "trusted"} else "UNTRUSTED"


def _group_tokens_from_json(raw: Any) -> Set[str]:
    out: Set[str] = set()
    if raw is None:
        return out
    text = str(raw).strip()
    if not text:
        return out

    def _add(val: Any) -> None:
        token = _norm_group(val)
        if token:
            out.add(token)

    try:
        parsed = json.loads(text)
    except Exception:
        parsed = None

    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, str):
                _add(item)
            elif isinstance(item, dict):
                for k, v in item.items():
                    if isinstance(v, str):
                        _add(v)
                    if bool(v):
                        _add(k)
    elif isinstance(parsed, dict):
        for k, v in parsed.items():
            if isinstance(v, str):
                _add(v)
            if bool(v):
                _add(k)
    elif isinstance(parsed, str):
        inner = parsed.strip()
        if inner:
            try:
                nested = json.loads(inner)
            except Exception:
                nested = None
            if isinstance(nested, list):
                for item in nested:
                    if isinstance(item, str):
                        _add(item)
                    elif isinstance(item, dict):
                        for k, v in item.items():
                            if isinstance(v, str):
                                _add(v)
                            if bool(v):
                                _add(k)
            elif isinstance(nested, dict):
                for k, v in nested.items():
                    if isinstance(v, str):
                        _add(v)
                    if bool(v):
                        _add(k)
            else:
                text = inner

    for chunk in str(text).replace(";", ",").replace("|", ",").split(","):
        _add(chunk)
    return out


def _collect_group_tokens(*values: Any) -> Set[str]:
    out: Set[str] = set()
    if len(values) >= 4:
        raw_json = values[3]
    else:
        raw_json = ""
    for raw in values[:3]:
        token = _norm_group(raw)
        if token:
            out.add(token)
    out.update(_group_tokens_from_json(raw_json))
    return out


def _passes_geo_filter(state: str, *, state_filter: str, region_filter: str) -> bool:
    st = _norm_state(state)
    sf = _norm_state(state_filter)
    rf = _norm_region(region_filter)
    if sf and st != sf:
        return False
    if rf:
        if STATE_TO_FEMA_REGION.get(st, "") != rf:
            return False
    return True


class SOPManager:
    """
    Persistence and reminder logic for SOP profiles/actions.
    """

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or NETS_DB
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings = SettingsManager()
        self._active_hf_conflicts_cache: Optional[List[Dict[str, Any]]] = None
        self._active_hf_conflicts_cache_monotonic: float = 0.0
        self._active_hf_conflicts_cache_ttl_seconds: float = 3.0
        self.ensure_tables()
        try:
            self.enforce_single_profile_per_category()
        except Exception as e:
            log.debug("SOP: enforce_single_profile_per_category skipped: %s", e)

    CATEGORY_HF = "HF"
    CATEGORY_LOCAL = "LOCAL"
    CONFLICT_POLICY_SOP = "SOP_ALL"
    CONFLICT_POLICY_NET = "NET_PRIORITY"
    CONFLICT_POLICY_DAILY = "DAILY_PRIORITY"
    NET_SOP_POLICY_SOP = "SOP_PRIORITY"
    NET_SOP_POLICY_NET = "NET_PRIORITY"

    def _invalidate_active_hf_conflicts_cache(self) -> None:
        self._active_hf_conflicts_cache = None
        self._active_hf_conflicts_cache_monotonic = 0.0

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _settings_db_path(self) -> Path:
        cfg_path = getattr(self.settings, "_config_path", None)
        if cfg_path:
            try:
                return Path(cfg_path)
            except Exception:
                pass
        return get_config_dir() / "config" / "freqinout.db"

    @staticmethod
    def _frequency_reference_signature(row: Mapping[str, Any]) -> Dict[str, Any]:
        return sop_frequency_reference_signature(row)

    @staticmethod
    def _frequency_reference_entry(row_id: int, row: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "signature": SOPManager._frequency_reference_signature(row),
            "operating_group_frequency_key": str(row.get("operating_group_frequency_key") or "").strip(),
            "deleted": False,
            "id": int(row_id),
        }

    @staticmethod
    def _frequency_reference_row(
        row: Mapping[str, Any], *, group: Any, band: Any, mode: Any, frequency: Any
    ) -> Dict[str, Any]:
        reference = dict(row)
        reference.update(
            {
                "group_name": str(group or "").strip().upper(),
                "band": str(band or "").strip().upper(),
                "mode": str(mode or "").strip().upper(),
                "frequency": SOPManager._normalize_frequency(frequency),
            }
        )
        return reference

    def _write_frequency_variant_sidecar(
        self,
        profile_id: int,
        *,
        actions: Mapping[int, Mapping[str, Any]] | None = None,
        layers: Mapping[int, Mapping[str, Any]] | None = None,
        deleted_actions: Set[int] = frozenset(),
        deleted_layers: Set[int] = frozenset(),
    ) -> None:
        """Persist variant references in Settings JSON without changing the SOP DB schema."""
        if not actions and not layers and not deleted_actions and not deleted_layers:
            return
        old_value = self.settings.get(SOP_FREQUENCY_VARIANT_REFERENCES_KEY, {})
        if not old_value and not deleted_actions and not deleted_layers:
            incoming = list((actions or {}).values()) + list((layers or {}).values())
            if not any(str(row.get("operating_group_frequency_key") or "").strip() for row in incoming):
                # Keep legacy imports and lightweight Settings fakes read-only.
                return
        sidecar = dict(old_value) if isinstance(old_value, dict) else {}
        profile = dict(sidecar.get(str(int(profile_id))) or {})
        action_refs = dict(profile.get("actions") or {})
        layer_refs = dict(profile.get("layers") or {})
        for row_id in deleted_actions:
            old = dict(action_refs.get(str(row_id)) or {})
            old["deleted"] = True
            action_refs[str(row_id)] = old
        for row_id in deleted_layers:
            old = dict(layer_refs.get(str(row_id)) or {})
            old["deleted"] = True
            layer_refs[str(row_id)] = old
        for row_id, row in (actions or {}).items():
            key = str(int(row_id))
            entry = self._frequency_reference_entry(int(row_id), row)
            old = action_refs.get(key)
            if not entry["operating_group_frequency_key"] and isinstance(old, dict) and not old.get("deleted"):
                if old.get("signature") == entry["signature"]:
                    entry["operating_group_frequency_key"] = str(old.get("operating_group_frequency_key") or "")
                elif old.get("operating_group_frequency_key"):
                    entry["operating_group_frequency_key"] = "unresolved_mirror_reference"
            action_refs[key] = entry
        for row_id, row in (layers or {}).items():
            key = str(int(row_id))
            entry = self._frequency_reference_entry(int(row_id), row)
            old = layer_refs.get(key)
            if not entry["operating_group_frequency_key"] and isinstance(old, dict) and not old.get("deleted"):
                if old.get("signature") == entry["signature"]:
                    entry["operating_group_frequency_key"] = str(old.get("operating_group_frequency_key") or "")
                elif old.get("operating_group_frequency_key"):
                    entry["operating_group_frequency_key"] = "unresolved_mirror_reference"
            layer_refs[key] = entry
        profile.update({"actions": action_refs, "layers": layer_refs})
        sidecar[str(int(profile_id))] = profile
        try:
            self.settings.set(SOP_FREQUENCY_VARIANT_REFERENCES_KEY, sidecar)
            persisted = self.settings._conn.execute(
                "SELECT value FROM kv WHERE key=?", (SOP_FREQUENCY_VARIANT_REFERENCES_KEY,)
            ).fetchone()
            if not persisted or json.loads(str(persisted[0])) != sidecar:
                raise RuntimeError("SOP frequency variant references were not persisted.")
        except Exception:
            try:
                self.settings._data[SOP_FREQUENCY_VARIANT_REFERENCES_KEY] = old_value
            except Exception:
                pass
            raise

    def _reattach_frequency_variant_keys(self, profile_id: int, actions: List[Dict[str, Any]], layers: List[Dict[str, Any]]) -> None:
        sidecar = self.settings.get(SOP_FREQUENCY_VARIANT_REFERENCES_KEY, {})
        for kind, rows in (("actions", actions), ("layers", layers)):
            for index, row in enumerate(rows):
                rows[index] = reattach_sop_frequency_key(row, sidecar, profile_id, kind=kind)

    def _prune_frequency_variant_sidecar(self, profile_id: int, *, remove_profile: bool = False) -> None:
        """Best-effort post-commit cleanup; failures retain safe stale references."""
        old_value = self.settings.get(SOP_FREQUENCY_VARIANT_REFERENCES_KEY, {})
        if not isinstance(old_value, dict) or str(int(profile_id)) not in old_value:
            return
        sidecar = dict(old_value)
        if remove_profile:
            sidecar.pop(str(int(profile_id)), None)
        else:
            try:
                conn = self._connect()
                try:
                    actions = {str(int(row[0])) for row in conn.execute("SELECT id FROM sop_actions WHERE profile_id=?", (int(profile_id),))}
                    layers = {str(int(row[0])) for row in conn.execute("SELECT id FROM sop_schedule_layer WHERE profile_id=?", (int(profile_id),))}
                finally:
                    conn.close()
            except Exception as exc:
                log.debug("SOP: retaining stale frequency-reference metadata after row lookup failure: %s", exc)
                return
            profile = dict(sidecar.get(str(int(profile_id))) or {})
            profile["actions"] = {key: value for key, value in dict(profile.get("actions") or {}).items() if key in actions}
            profile["layers"] = {key: value for key, value in dict(profile.get("layers") or {}).items() if key in layers}
            sidecar[str(int(profile_id))] = profile
        try:
            self.settings.set(SOP_FREQUENCY_VARIANT_REFERENCES_KEY, sidecar)
            persisted = self.settings._conn.execute(
                "SELECT value FROM kv WHERE key=?", (SOP_FREQUENCY_VARIANT_REFERENCES_KEY,)
            ).fetchone()
            if not persisted or json.loads(str(persisted[0])) != sidecar:
                raise RuntimeError("SOP frequency variant reference cleanup was not persisted.")
        except Exception as exc:
            log.debug("SOP: retaining stale frequency-reference metadata after cleanup failure: %s", exc)

    @staticmethod
    def _normalize_day_utc(value: Any) -> str:
        raw = str(value or "").strip()
        if not raw:
            return "ALL"
        upper = raw.upper()
        if upper in {"ALL", "DAILY"}:
            return "ALL"
        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        for day in days:
            if upper.startswith(day[:3].upper()):
                return day
        return "ALL"

    @staticmethod
    def _normalize_recurrence(value: Any) -> str:
        raw = str(value or "Weekly").strip().upper()
        if raw == "MONTHLY":
            raw = "PERIODIC"
        if raw in {"DAILY", "PERIODIC", "BI-WEEKLY", "WEEKLY"}:
            return "Bi-Weekly" if raw == "BI-WEEKLY" else raw.title()
        return "Weekly"

    @staticmethod
    def _normalize_month_weeks(value: Any) -> str:
        weeks: List[int] = []
        for token in str(value or "").split(","):
            token = token.strip()
            if not token:
                continue
            try:
                week = int(token)
            except Exception:
                continue
            if 1 <= week <= 5:
                weeks.append(week)
        return ",".join(str(w) for w in sorted(set(weeks)))

    @staticmethod
    def _normalize_hhmm(value: Any) -> str:
        h, m = _parse_hhmm(str(value or ""), default_hhmm="00:00")
        return f"{h:02d}:{m:02d}"

    @staticmethod
    def _normalize_frequency(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if normalize_frequency_hz(text) is not None:
            return format_frequency_mhz(text)
        try:
            return f"{float(text):.3f}"
        except Exception:
            return text

    @classmethod
    def _normalize_category(cls, value: Any) -> str:
        raw = str(value or "").strip().upper()
        if raw in {"LOCAL", "LOCAL NET", "LOCAL COMMS", "LOCAL_COMMS"}:
            return cls.CATEGORY_LOCAL
        return cls.CATEGORY_HF

    @classmethod
    def _normalize_conflict_policy(cls, value: Any) -> str:
        raw = str(value or "").strip().upper()
        if raw in {cls.CONFLICT_POLICY_NET, "NET", "NET_PRIORITY"}:
            return cls.CONFLICT_POLICY_NET
        if raw in {cls.CONFLICT_POLICY_DAILY, "DAILY", "DAILY_PRIORITY"}:
            return cls.CONFLICT_POLICY_DAILY
        return cls.CONFLICT_POLICY_SOP

    @classmethod
    def _normalize_net_sop_policy(cls, value: Any) -> str:
        raw = str(value or "").strip().upper()
        if raw in {"SOP", "SOP_PRIORITY"}:
            return cls.NET_SOP_POLICY_SOP
        return cls.NET_SOP_POLICY_NET

    @classmethod
    def _net_row_signature(cls, row: Dict[str, Any]) -> str:
        data = normalize_schedule_target_fields(row)
        day = cls._normalize_day_utc(data.get("day_utc"))
        recurrence = cls._normalize_recurrence(data.get("recurrence"))
        biweekly = int(data.get("biweekly_offset_weeks") or 0)
        weeks = cls._normalize_month_weeks(data.get("month_weeks"))
        band = str(data.get("band") or "").strip().upper()
        freq = cls._normalize_frequency(data.get("frequency"))
        start = cls._normalize_hhmm(data.get("start_utc") or "00:00")
        end = cls._normalize_hhmm(data.get("end_utc") or "23:59")
        group_name = str(data.get("group_name") or "").strip().upper()
        net_name = str(data.get("name") or data.get("net_name") or "").strip().upper()
        target_scope = str(data.get("target_scope") or "station").strip().lower() or "station"
        target_device_profile_id = int(data.get("target_device_profile_id") or 0)
        target_operating_profile_id = int(data.get("target_operating_profile_id") or 0)
        return (
            f"NET|{group_name}|{band}|{freq}|{day}|{recurrence}|{biweekly}|"
            f"{weeks}|{start}|{end}|{net_name}|{target_scope}|"
            f"{target_device_profile_id}|{target_operating_profile_id}"
        )

    @classmethod
    def _sop_row_signature(cls, row: Dict[str, Any]) -> str:
        day = cls._normalize_day_utc(row.get("day_utc"))
        recurrence = cls._normalize_recurrence(row.get("recurrence"))
        biweekly = int(row.get("biweekly_offset_weeks") or 0)
        weeks = cls._normalize_month_weeks(row.get("month_weeks"))
        band = str(row.get("band") or "").strip().upper()
        freq = cls._normalize_frequency(row.get("frequency"))
        start = cls._normalize_hhmm(row.get("start_utc") or "00:00")
        end = cls._normalize_hhmm(row.get("end_utc") or "23:59")
        group_name = str(row.get("group_name") or "").strip().upper()
        profile_id = int(row.get("sop_profile_id") or 0)
        layer_id = int(row.get("sop_layer_id") or row.get("id") or 0)
        return (
            f"SOP|{profile_id}|{layer_id}|{group_name}|{band}|{freq}|{day}|"
            f"{recurrence}|{biweekly}|{weeks}|{start}|{end}"
        )

    @staticmethod
    def _policy_conflict_key(
        net_row_signature: str,
        sop_row_signature: str,
        window_start_utc: str,
        window_end_utc: str,
    ) -> str:
        return f"{net_row_signature}|{sop_row_signature}|{window_start_utc}|{window_end_utc}"

    @staticmethod
    def _normalize_condition_levels(value: Any) -> str:
        raw = str(value or "").strip().upper()
        if not raw or raw == "ALL":
            return "ALL"
        out: List[int] = []
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
                out.append(lvl)
        if not out:
            return "ALL"
        return ",".join(str(v) for v in sorted(set(out)))

    def ensure_tables(self) -> None:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS sop_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    category TEXT DEFAULT 'HF',
                    operating_group TEXT NOT NULL,
                    secondary_group TEXT,
                    frequency TEXT NOT NULL,
                    sop_start_utc TEXT NOT NULL,
                    priority INTEGER DEFAULT 100,
                    active INTEGER DEFAULT 0,
                    window_hours INTEGER DEFAULT 12,
                    created_utc TEXT,
                    updated_utc TEXT
                )
                """
            )
            cur.execute("PRAGMA table_info(sop_profiles)")
            profile_cols = {str(row[1] or "").strip().lower() for row in cur.fetchall()}
            if "category" not in profile_cols:
                cur.execute("ALTER TABLE sop_profiles ADD COLUMN category TEXT DEFAULT 'HF'")
            if "priority" not in profile_cols:
                cur.execute("ALTER TABLE sop_profiles ADD COLUMN priority INTEGER DEFAULT 100")
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_sop_profiles_name ON sop_profiles(name)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_sop_profiles_active ON sop_profiles(active)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_sop_profiles_category ON sop_profiles(category)")

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS sop_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile_id INTEGER NOT NULL,
                    group_name TEXT,
                    condition_levels TEXT DEFAULT 'ALL',
                    band TEXT,
                    frequency TEXT,
                    software TEXT NOT NULL,
                    mode TEXT,
                    action_key TEXT NOT NULL,
                    action_label TEXT NOT NULL,
                    enabled INTEGER DEFAULT 1,
                    daily_start_utc TEXT,
                    daily_end_utc TEXT,
                    duration_minutes INTEGER DEFAULT 60,
                    interval_hours INTEGER DEFAULT 3,
                    interval_minutes INTEGER,
                    interval_phase_minutes INTEGER DEFAULT 0,
                    conflict_policy TEXT DEFAULT 'SOP_ALL',
                    daily_conflict_summary TEXT,
                    net_conflict_summary TEXT,
                    schedule_applied INTEGER DEFAULT 1,
                    description TEXT,
                    contact_rule TEXT DEFAULT 'none',
                    contact_target TEXT,
                    sort_order INTEGER DEFAULT 0
                )
                """
            )
            cur.execute("PRAGMA table_info(sop_actions)")
            action_cols = {row[1] for row in cur.fetchall()}
            if "band" not in action_cols:
                cur.execute("ALTER TABLE sop_actions ADD COLUMN band TEXT")
                action_cols.add("band")
            if "group_name" not in action_cols:
                cur.execute("ALTER TABLE sop_actions ADD COLUMN group_name TEXT")
                action_cols.add("group_name")
            if "condition_levels" not in action_cols:
                cur.execute("ALTER TABLE sop_actions ADD COLUMN condition_levels TEXT DEFAULT 'ALL'")
                action_cols.add("condition_levels")
            if "frequency" not in action_cols:
                cur.execute("ALTER TABLE sop_actions ADD COLUMN frequency TEXT")
                action_cols.add("frequency")
            if "mode" not in action_cols:
                cur.execute("ALTER TABLE sop_actions ADD COLUMN mode TEXT")
                action_cols.add("mode")
            if "daily_start_utc" not in action_cols:
                cur.execute("ALTER TABLE sop_actions ADD COLUMN daily_start_utc TEXT")
                action_cols.add("daily_start_utc")
            if "daily_end_utc" not in action_cols:
                cur.execute("ALTER TABLE sop_actions ADD COLUMN daily_end_utc TEXT")
                action_cols.add("daily_end_utc")
            if "duration_minutes" not in action_cols:
                cur.execute("ALTER TABLE sop_actions ADD COLUMN duration_minutes INTEGER DEFAULT 60")
                action_cols.add("duration_minutes")
            if "contact_rule" not in action_cols:
                cur.execute("ALTER TABLE sop_actions ADD COLUMN contact_rule TEXT DEFAULT 'none'")
                action_cols.add("contact_rule")
            if "contact_target" not in action_cols:
                cur.execute("ALTER TABLE sop_actions ADD COLUMN contact_target TEXT")
                action_cols.add("contact_target")
            if "interval_minutes" not in action_cols:
                cur.execute("ALTER TABLE sop_actions ADD COLUMN interval_minutes INTEGER")
                action_cols.add("interval_minutes")
            if "interval_phase_minutes" not in action_cols:
                cur.execute("ALTER TABLE sop_actions ADD COLUMN interval_phase_minutes INTEGER DEFAULT 0")
                action_cols.add("interval_phase_minutes")
            if "conflict_policy" not in action_cols:
                cur.execute("ALTER TABLE sop_actions ADD COLUMN conflict_policy TEXT DEFAULT 'SOP_ALL'")
                action_cols.add("conflict_policy")
            if "daily_conflict_summary" not in action_cols:
                cur.execute("ALTER TABLE sop_actions ADD COLUMN daily_conflict_summary TEXT")
                action_cols.add("daily_conflict_summary")
            if "net_conflict_summary" not in action_cols:
                cur.execute("ALTER TABLE sop_actions ADD COLUMN net_conflict_summary TEXT")
                action_cols.add("net_conflict_summary")
            if "schedule_applied" not in action_cols:
                cur.execute("ALTER TABLE sop_actions ADD COLUMN schedule_applied INTEGER DEFAULT 1")
                action_cols.add("schedule_applied")
            if "sort_order" not in action_cols:
                cur.execute("ALTER TABLE sop_actions ADD COLUMN sort_order INTEGER DEFAULT 0")
                action_cols.add("sort_order")
            try:
                cur.execute(
                    """
                    UPDATE sop_actions
                    SET
                        group_name = COALESCE(NULLIF(TRIM(group_name), ''), ''),
                        condition_levels = COALESCE(NULLIF(TRIM(condition_levels), ''), 'ALL'),
                        mode = COALESCE(mode, ''),
                        daily_start_utc = COALESCE(NULLIF(TRIM(daily_start_utc), ''), '00:00'),
                        daily_end_utc = COALESCE(NULLIF(TRIM(daily_end_utc), ''), '23:59'),
                        duration_minutes = CASE
                            WHEN COALESCE(duration_minutes, 0) <= 0 THEN 60
                            ELSE duration_minutes
                        END,
                        conflict_policy = CASE
                            WHEN UPPER(COALESCE(TRIM(conflict_policy), '')) IN ('NET', 'NET_PRIORITY') THEN 'NET_PRIORITY'
                            WHEN UPPER(COALESCE(TRIM(conflict_policy), '')) IN ('DAILY', 'DAILY_PRIORITY') THEN 'DAILY_PRIORITY'
                            ELSE 'SOP_ALL'
                        END,
                        schedule_applied = CASE
                            WHEN schedule_applied IS NULL THEN 1
                            ELSE schedule_applied
                        END
                    """
                )
            except Exception as e:
                log.debug("SOP: normalize action planning defaults skipped: %s", e)
            if "interval_hours" in action_cols and "interval_minutes" in action_cols and "interval_phase_minutes" in action_cols:
                # Legacy Local Net rows used fractional-hour intervals (e.g., 3:30) to express
                # stagger intent. Convert common quarter-hour offsets to interval+phase.
                try:
                    cur.execute(
                        """
                        UPDATE sop_actions
                        SET
                            interval_phase_minutes = (interval_minutes % 60),
                            interval_minutes = CAST(interval_minutes / 60 AS INTEGER) * 60,
                            interval_hours = CASE
                                WHEN CAST(interval_minutes / 60 AS INTEGER) < 1 THEN 1
                                ELSE CAST(interval_minutes / 60 AS INTEGER)
                            END
                        WHERE lower(trim(COALESCE(software, ''))) = 'local net'
                          AND (
                                lower(trim(COALESCE(contact_rule, ''))) IN ('local_profile', 'local_group')
                                OR lower(trim(COALESCE(action_key, ''))) LIKE 'local_%'
                              )
                          AND COALESCE(interval_phase_minutes, 0) = 0
                          AND COALESCE(interval_minutes, 0) >= 120
                          AND (COALESCE(interval_minutes, 0) % 60) IN (15, 30, 45)
                        """
                    )
                    migrated = int(cur.rowcount or 0)
                    if migrated > 0:
                        log.info("SOP: migrated %d legacy local interval rows to interval+phase", migrated)
                except Exception as e:
                    log.debug("SOP: local interval migration skipped: %s", e)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_sop_actions_profile ON sop_actions(profile_id)")

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS sop_action_state (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile_id INTEGER NOT NULL,
                    action_id INTEGER NOT NULL,
                    last_completed_utc TEXT,
                    updated_utc TEXT
                )
                """
            )
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_sop_state_action ON sop_action_state(profile_id, action_id)"
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS sop_schedule_layer (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile_id INTEGER NOT NULL,
                    day_utc TEXT NOT NULL,
                    recurrence TEXT DEFAULT 'Weekly',
                    biweekly_offset_weeks INTEGER DEFAULT 0,
                    month_weeks TEXT,
                    condition_levels TEXT DEFAULT 'ALL',
                    group_name TEXT,
                    band TEXT,
                    mode TEXT,
                    vfo TEXT,
                    frequency TEXT NOT NULL,
                    start_utc TEXT NOT NULL,
                    end_utc TEXT NOT NULL,
                    enabled INTEGER DEFAULT 1,
                    sort_order INTEGER DEFAULT 0,
                    updated_utc TEXT
                )
                """
            )
            cur.execute("PRAGMA table_info(sop_schedule_layer)")
            layer_cols = {str(row[1] or "").strip().lower() for row in cur.fetchall()}
            if "recurrence" not in layer_cols:
                cur.execute("ALTER TABLE sop_schedule_layer ADD COLUMN recurrence TEXT DEFAULT 'Weekly'")
            if "biweekly_offset_weeks" not in layer_cols:
                cur.execute("ALTER TABLE sop_schedule_layer ADD COLUMN biweekly_offset_weeks INTEGER DEFAULT 0")
            if "month_weeks" not in layer_cols:
                cur.execute("ALTER TABLE sop_schedule_layer ADD COLUMN month_weeks TEXT")
            if "condition_levels" not in layer_cols:
                cur.execute("ALTER TABLE sop_schedule_layer ADD COLUMN condition_levels TEXT DEFAULT 'ALL'")
                layer_cols.add("condition_levels")
            if "group_name" not in layer_cols:
                cur.execute("ALTER TABLE sop_schedule_layer ADD COLUMN group_name TEXT")
                layer_cols.add("group_name")
            if "band" not in layer_cols:
                cur.execute("ALTER TABLE sop_schedule_layer ADD COLUMN band TEXT")
                layer_cols.add("band")
            if "mode" not in layer_cols:
                cur.execute("ALTER TABLE sop_schedule_layer ADD COLUMN mode TEXT")
                layer_cols.add("mode")
            if "vfo" not in layer_cols:
                cur.execute("ALTER TABLE sop_schedule_layer ADD COLUMN vfo TEXT")
                layer_cols.add("vfo")
            if "enabled" not in layer_cols:
                cur.execute("ALTER TABLE sop_schedule_layer ADD COLUMN enabled INTEGER DEFAULT 1")
                layer_cols.add("enabled")
            if "sort_order" not in layer_cols:
                cur.execute("ALTER TABLE sop_schedule_layer ADD COLUMN sort_order INTEGER DEFAULT 0")
                layer_cols.add("sort_order")
            if "updated_utc" not in layer_cols:
                cur.execute("ALTER TABLE sop_schedule_layer ADD COLUMN updated_utc TEXT")
                layer_cols.add("updated_utc")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_sop_layer_profile ON sop_schedule_layer(profile_id)")
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_sop_layer_profile_day ON sop_schedule_layer(profile_id, day_utc, start_utc)"
            )
            try:
                # SOP HF workflows are daily-only. Normalize any legacy HF layer rows
                # so scheduler/UI consistently treat them as ALL-day recurring windows.
                cur.execute(
                    """
                    UPDATE sop_schedule_layer
                    SET
                        day_utc = 'ALL',
                        recurrence = 'Daily',
                        biweekly_offset_weeks = 0,
                        month_weeks = '',
                        updated_utc = COALESCE(updated_utc, ?)
                    WHERE profile_id IN (
                        SELECT id
                        FROM sop_profiles
                        WHERE UPPER(COALESCE(category, 'HF')) = 'HF'
                    )
                    AND (
                        UPPER(COALESCE(day_utc, '')) <> 'ALL'
                        OR UPPER(COALESCE(recurrence, 'WEEKLY')) <> 'DAILY'
                        OR COALESCE(biweekly_offset_weeks, 0) <> 0
                        OR COALESCE(TRIM(month_weeks), '') <> ''
                    )
                    """,
                    (_utc_now_iso(),),
                )
                # Backfill per-row layer group labels from matching SOP actions when possible.
                if "group_name" in layer_cols:
                    cur.execute(
                        """
                        UPDATE sop_schedule_layer
                        SET
                            group_name = (
                                SELECT UPPER(TRIM(a.group_name))
                                FROM sop_actions a
                                WHERE a.profile_id = sop_schedule_layer.profile_id
                                  AND UPPER(COALESCE(a.band, '')) = UPPER(COALESCE(sop_schedule_layer.band, ''))
                                  AND TRIM(COALESCE(a.frequency, '')) = TRIM(COALESCE(sop_schedule_layer.frequency, ''))
                                  AND TRIM(COALESCE(a.daily_start_utc, '')) = TRIM(COALESCE(sop_schedule_layer.start_utc, ''))
                                  AND TRIM(COALESCE(a.daily_end_utc, '')) = TRIM(COALESCE(sop_schedule_layer.end_utc, ''))
                                  AND COALESCE(a.enabled, 1) = 1
                                  AND TRIM(COALESCE(a.group_name, '')) <> ''
                                ORDER BY a.sort_order, a.id
                                LIMIT 1
                            ),
                            updated_utc = COALESCE(updated_utc, ?)
                        WHERE TRIM(COALESCE(group_name, '')) = ''
                        """
                    , (_utc_now_iso(),))
                    # Final fallback to profile operating group when row-level action group is unavailable.
                    cur.execute(
                        """
                        UPDATE sop_schedule_layer
                        SET
                            group_name = UPPER(TRIM(COALESCE(
                                (SELECT p.operating_group FROM sop_profiles p WHERE p.id = sop_schedule_layer.profile_id),
                                ''
                            ))),
                            updated_utc = COALESCE(updated_utc, ?)
                        WHERE TRIM(COALESCE(group_name, '')) = ''
                        """
                    , (_utc_now_iso(),))
            except Exception as e:
                log.debug("SOP: normalize legacy HF layer cadence skipped: %s", e)

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS sop_net_conflict_policy (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sop_profile_id INTEGER NOT NULL,
                    sop_layer_id INTEGER,
                    net_row_signature TEXT NOT NULL,
                    sop_row_signature TEXT NOT NULL,
                    policy TEXT NOT NULL,
                    window_start_utc TEXT NOT NULL,
                    window_end_utc TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    resolution_note TEXT,
                    updated_utc TEXT
                )
                """
            )
            cur.execute("PRAGMA table_info(sop_net_conflict_policy)")
            policy_cols = {str(row[1] or "").strip().lower() for row in cur.fetchall()}
            if "sop_profile_id" not in policy_cols:
                cur.execute("ALTER TABLE sop_net_conflict_policy ADD COLUMN sop_profile_id INTEGER NOT NULL DEFAULT 0")
            if "sop_layer_id" not in policy_cols:
                cur.execute("ALTER TABLE sop_net_conflict_policy ADD COLUMN sop_layer_id INTEGER")
            if "net_row_signature" not in policy_cols:
                cur.execute("ALTER TABLE sop_net_conflict_policy ADD COLUMN net_row_signature TEXT NOT NULL DEFAULT ''")
            if "sop_row_signature" not in policy_cols:
                cur.execute("ALTER TABLE sop_net_conflict_policy ADD COLUMN sop_row_signature TEXT NOT NULL DEFAULT ''")
            if "policy" not in policy_cols:
                cur.execute("ALTER TABLE sop_net_conflict_policy ADD COLUMN policy TEXT NOT NULL DEFAULT 'NET_PRIORITY'")
            if "window_start_utc" not in policy_cols:
                cur.execute("ALTER TABLE sop_net_conflict_policy ADD COLUMN window_start_utc TEXT NOT NULL DEFAULT ''")
            if "window_end_utc" not in policy_cols:
                cur.execute("ALTER TABLE sop_net_conflict_policy ADD COLUMN window_end_utc TEXT NOT NULL DEFAULT ''")
            if "active" not in policy_cols:
                cur.execute("ALTER TABLE sop_net_conflict_policy ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
            if "resolution_note" not in policy_cols:
                cur.execute("ALTER TABLE sop_net_conflict_policy ADD COLUMN resolution_note TEXT")
            if "updated_utc" not in policy_cols:
                cur.execute("ALTER TABLE sop_net_conflict_policy ADD COLUMN updated_utc TEXT")
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_sop_net_conflict_unique ON sop_net_conflict_policy(net_row_signature, sop_row_signature, window_start_utc, window_end_utc)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_sop_net_conflict_profile_active ON sop_net_conflict_policy(sop_profile_id, active)"
            )
            try:
                cur.execute(
                    """
                    UPDATE sop_net_conflict_policy
                    SET policy = CASE
                        WHEN UPPER(COALESCE(TRIM(policy), '')) IN ('SOP', 'SOP_PRIORITY') THEN 'SOP_PRIORITY'
                        ELSE 'NET_PRIORITY'
                    END
                    """
                )
            except Exception as e:
                log.debug("SOP: normalize net conflict policy skipped: %s", e)

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS sop_profile_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT NOT NULL,
                    label TEXT NOT NULL,
                    note TEXT,
                    snapshot_json TEXT NOT NULL,
                    created_utc TEXT NOT NULL,
                    updated_utc TEXT NOT NULL
                )
                """
            )
            cur.execute("PRAGMA table_info(sop_profile_versions)")
            version_cols = {str(row[1] or "").strip().lower() for row in cur.fetchall()}
            if "category" not in version_cols:
                cur.execute("ALTER TABLE sop_profile_versions ADD COLUMN category TEXT NOT NULL DEFAULT 'HF'")
            if "label" not in version_cols:
                cur.execute("ALTER TABLE sop_profile_versions ADD COLUMN label TEXT NOT NULL DEFAULT ''")
            if "note" not in version_cols:
                cur.execute("ALTER TABLE sop_profile_versions ADD COLUMN note TEXT")
            if "snapshot_json" not in version_cols:
                cur.execute("ALTER TABLE sop_profile_versions ADD COLUMN snapshot_json TEXT NOT NULL DEFAULT '{}'")
            if "created_utc" not in version_cols:
                cur.execute("ALTER TABLE sop_profile_versions ADD COLUMN created_utc TEXT NOT NULL DEFAULT ''")
            if "updated_utc" not in version_cols:
                cur.execute("ALTER TABLE sop_profile_versions ADD COLUMN updated_utc TEXT NOT NULL DEFAULT ''")
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_sop_profile_versions_cat_created ON sop_profile_versions(category, created_utc)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_sop_profile_versions_created ON sop_profile_versions(created_utc)"
            )
            conn.commit()
        finally:
            conn.close()

    def list_profiles(self) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            cur = conn.execute(
                """
                SELECT id, name, category, operating_group, secondary_group, frequency, sop_start_utc,
                       priority, active, window_hours, created_utc, updated_utc
                FROM sop_profiles
                ORDER BY name COLLATE NOCASE
                """
            )
            rows = cur.fetchall()
            return [
                {
                    "id": r[0],
                    "name": r[1],
                    "category": self._normalize_category(r[2]),
                    "operating_group": r[3] or "",
                    "secondary_group": r[4] or "",
                    "frequency": r[5] or "",
                    "sop_start_utc": r[6] or "00:00",
                    "priority": int(r[7] or 100),
                    "active": bool(r[8]),
                    "window_hours": int(r[9] or 12),
                    "created_utc": r[10] or "",
                    "updated_utc": r[11] or "",
                }
                for r in rows
            ]
        finally:
            conn.close()

    @classmethod
    def default_profile_version_label(cls, category: str) -> str:
        cat = cls._normalize_category(category)
        prefix = "HF SOP" if cat == cls.CATEGORY_HF else "Local Comms SOP"
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        return f"{prefix} {stamp}"

    def save_profile_version(
        self,
        *,
        category: str,
        snapshot: Dict[str, Any],
        label: str = "",
        note: str = "",
    ) -> int:
        if not isinstance(snapshot, dict):
            raise ValueError("Version snapshot must be a JSON object.")
        cat = self._normalize_category(category)
        profile_obj = snapshot.get("profile")
        if not isinstance(profile_obj, dict):
            raise ValueError("Version snapshot must include a profile object.")
        profile_obj["category"] = self._normalize_category(profile_obj.get("category") or cat)
        cat = self._normalize_category(profile_obj.get("category") or cat)
        label_txt = str(label or "").strip() or self.default_profile_version_label(cat)
        note_txt = str(note or "").strip()
        now_iso = _utc_now_iso()
        payload = json.dumps(snapshot, sort_keys=True, default=str)
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO sop_profile_versions
                        (category, label, note, snapshot_json, created_utc, updated_utc)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (cat, label_txt, note_txt, payload, now_iso, now_iso),
                )
                row = conn.execute("SELECT last_insert_rowid()").fetchone()
                return int(row[0] or 0) if row else 0
        finally:
            conn.close()

    def list_profile_versions(self, *, category: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        try:
            lim = max(1, min(1000, int(limit or 200)))
        except Exception:
            lim = 200
        cat = self._normalize_category(category) if str(category or "").strip() else ""
        conn = self._connect()
        try:
            if cat:
                rows = conn.execute(
                    """
                    SELECT id, category, label, note, snapshot_json, created_utc, updated_utc
                    FROM sop_profile_versions
                    WHERE UPPER(COALESCE(category, 'HF')) = ?
                    ORDER BY COALESCE(created_utc, updated_utc, '') DESC, id DESC
                    LIMIT ?
                    """,
                    (cat, lim),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, category, label, note, snapshot_json, created_utc, updated_utc
                    FROM sop_profile_versions
                    ORDER BY COALESCE(created_utc, updated_utc, '') DESC, id DESC
                    LIMIT ?
                    """,
                    (lim,),
                ).fetchall()
            out: List[Dict[str, Any]] = []
            for row in rows:
                try:
                    parsed = json.loads(str(row[4] or "{}"))
                except Exception:
                    parsed = {}
                actions = parsed.get("actions") if isinstance(parsed, dict) else []
                action_count = len(actions) if isinstance(actions, list) else 0
                out.append(
                    {
                        "id": int(row[0] or 0),
                        "category": self._normalize_category(row[1]),
                        "label": str(row[2] or "").strip(),
                        "note": str(row[3] or "").strip(),
                        "created_utc": str(row[5] or "").strip(),
                        "updated_utc": str(row[6] or "").strip(),
                        "action_count": int(action_count),
                    }
                )
            return out
        finally:
            conn.close()

    def get_profile_version(self, version_id: int) -> Optional[Dict[str, Any]]:
        vid = int(version_id or 0)
        if vid <= 0:
            return None
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT id, category, label, note, snapshot_json, created_utc, updated_utc
                FROM sop_profile_versions
                WHERE id = ?
                """,
                (vid,),
            ).fetchone()
            if not row:
                return None
            try:
                snapshot = json.loads(str(row[4] or "{}"))
            except Exception:
                snapshot = {}
            return {
                "id": int(row[0] or 0),
                "category": self._normalize_category(row[1]),
                "label": str(row[2] or "").strip(),
                "note": str(row[3] or "").strip(),
                "snapshot": snapshot if isinstance(snapshot, dict) else {},
                "created_utc": str(row[5] or "").strip(),
                "updated_utc": str(row[6] or "").strip(),
            }
        finally:
            conn.close()

    def delete_profile_version(self, version_id: int) -> bool:
        vid = int(version_id or 0)
        if vid <= 0:
            return False
        conn = self._connect()
        try:
            with conn:
                cur = conn.execute("DELETE FROM sop_profile_versions WHERE id = ?", (vid,))
            return int(cur.rowcount or 0) > 0
        finally:
            conn.close()

    def list_profiles_with_category(self) -> List[Dict[str, Any]]:
        """
        Return SOP profiles with an inferred SOP category summary:
          - HF
          - Local Net
          - HF + Local Net
        """
        conn = self._connect()
        try:
            cur = conn.execute(
                """
                SELECT
                    p.id,
                    p.name,
                    p.category,
                    p.operating_group,
                    p.secondary_group,
                    p.priority,
                    p.active,
                    p.window_hours,
                    p.created_utc,
                    p.updated_utc,
                    SUM(
                        CASE
                            WHEN UPPER(TRIM(COALESCE(a.software, ''))) = 'LOCAL NET' THEN 1
                            ELSE 0
                        END
                    ) AS local_count,
                    SUM(
                        CASE
                            WHEN TRIM(COALESCE(a.software, '')) <> ''
                                 AND UPPER(TRIM(COALESCE(a.software, ''))) <> 'LOCAL NET' THEN 1
                            ELSE 0
                        END
                    ) AS hf_count
                FROM sop_profiles p
                LEFT JOIN sop_actions a
                  ON a.profile_id = p.id
                GROUP BY
                    p.id,
                    p.name,
                    p.category,
                    p.operating_group,
                    p.secondary_group,
                    p.priority,
                    p.active,
                    p.window_hours,
                    p.created_utc,
                    p.updated_utc
                ORDER BY p.name COLLATE NOCASE
                """
            )
            out: List[Dict[str, Any]] = []
            for row in cur.fetchall():
                category_raw = self._normalize_category(row[2])
                local_count = int(row[10] or 0)
                hf_count = int(row[11] or 0)
                if category_raw == self.CATEGORY_LOCAL:
                    category = "Local Net"
                elif local_count > 0 and hf_count > 0:
                    category = "HF + Local Net"
                elif local_count > 0 and hf_count <= 0:
                    category = "Local Net"
                else:
                    category = "HF"
                out.append(
                    {
                        "id": int(row[0] or 0),
                        "name": row[1] or "",
                        "category": category_raw,
                        "operating_group": row[3] or "",
                        "secondary_group": row[4] or "",
                        "priority": int(row[5] or 100),
                        "active": bool(row[6]),
                        "window_hours": int(row[7] or 12),
                        "created_utc": row[8] or "",
                        "updated_utc": row[9] or "",
                        "sop_category": category,
                    }
                )
            return out
        finally:
            conn.close()

    def set_profile_active(self, profile_id: int, active: bool) -> bool:
        """
        Toggle SOP profile active state without mutating action/layer rows.
        Returns True when a profile row was updated.
        """
        pid = int(profile_id or 0)
        if pid <= 0:
            return False
        now_iso = _utc_now_iso()
        conn = self._connect()
        try:
            with conn:
                cur = conn.execute(
                    """
                    UPDATE sop_profiles
                    SET active = ?, updated_utc = ?
                    WHERE id = ?
                    """,
                    (1 if active else 0, now_iso, pid),
                )
            try:
                updated = int(cur.rowcount or 0) > 0
            except Exception:
                updated = True
            if updated:
                self._invalidate_active_hf_conflicts_cache()
            return updated
        finally:
            conn.close()

    def get_profile_by_category(self, category: str) -> Optional[Dict[str, Any]]:
        cat = self._normalize_category(category)
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT id
                FROM sop_profiles
                WHERE UPPER(COALESCE(category, 'HF')) = ?
                ORDER BY COALESCE(updated_utc, created_utc, '') DESC, id ASC
                LIMIT 1
                """,
                (cat,),
            ).fetchone()
            if not row:
                return None
            return self.get_profile(int(row[0] or 0))
        finally:
            conn.close()

    def ensure_category_profile(self, category: str) -> Dict[str, Any]:
        cat = self._normalize_category(category)
        existing = self.get_profile_by_category(cat)
        if existing:
            return existing
        default_name = "HF SOP" if cat == self.CATEGORY_HF else "Local Comms SOP"
        payload = {
            "name": default_name,
            "category": cat,
            "operating_group": "",
            "secondary_group": "",
            "frequency": "",
            "sop_start_utc": "00:00",
            "priority": 100,
            "active": False,
            "window_hours": 24,
        }
        profile_id = self.save_profile(payload, actions=[], schedule_layer=[])
        created = self.get_profile(profile_id)
        if not created:
            raise RuntimeError(f"Failed to create SOP profile for category {cat}")
        return created

    def set_category_active(self, category: str, active: bool) -> bool:
        profile = self.get_profile_by_category(category)
        if not profile:
            profile = self.ensure_category_profile(category)
        return self.set_profile_active(int(profile.get("id") or 0), bool(active))

    def enforce_single_profile_per_category(self) -> Dict[str, int]:
        """
        Keep at most one active profile per category for runtime safety.
        Returns the canonical profile id per category (0 when missing).
        """
        out: Dict[str, int] = {self.CATEGORY_HF: 0, self.CATEGORY_LOCAL: 0}
        conn = self._connect()
        try:
            now_iso = _utc_now_iso()
            with conn:
                for category in (self.CATEGORY_HF, self.CATEGORY_LOCAL):
                    rows = conn.execute(
                        """
                        SELECT id
                        FROM sop_profiles
                        WHERE UPPER(COALESCE(category, 'HF')) = ?
                        ORDER BY COALESCE(updated_utc, created_utc, '') DESC, id ASC
                        """,
                        (category,),
                    ).fetchall()
                    ids = [int(r[0] or 0) for r in rows if int(r[0] or 0) > 0]
                    if not ids:
                        continue
                    keep_id = ids[0]
                    out[category] = keep_id
                    for stale_id in ids[1:]:
                        conn.execute(
                            """
                            UPDATE sop_profiles
                            SET active = 0, updated_utc = ?
                            WHERE id = ?
                            """,
                            (now_iso, stale_id),
                        )
            self._invalidate_active_hf_conflicts_cache()
            return out
        finally:
            conn.close()

    def get_profile(self, profile_id: int) -> Optional[Dict[str, Any]]:
        conn = self._connect()
        try:
            cur = conn.execute(
                """
                SELECT id, name, category, operating_group, secondary_group, frequency, sop_start_utc,
                       priority, active, window_hours, created_utc, updated_utc
                FROM sop_profiles WHERE id = ?
                """,
                (profile_id,),
            )
            row = cur.fetchone()
            if not row:
                return None

            actions_cur = conn.execute(
                """
                SELECT a.id, a.group_name, a.condition_levels, a.band, a.frequency, a.software, a.mode,
                       a.action_key, a.action_label, a.enabled,
                       a.daily_start_utc, a.daily_end_utc, a.duration_minutes,
                       a.interval_hours, a.interval_minutes, a.interval_phase_minutes,
                       a.conflict_policy, a.daily_conflict_summary, a.net_conflict_summary, a.schedule_applied,
                       a.description, a.contact_rule, a.contact_target, a.sort_order,
                       s.last_completed_utc
                FROM sop_actions a
                LEFT JOIN sop_action_state s
                  ON s.profile_id = a.profile_id AND s.action_id = a.id
                WHERE a.profile_id = ?
                ORDER BY a.software, a.sort_order, a.id
                """,
                (profile_id,),
            )
            actions = []
            for ar in actions_cur.fetchall():
                daily_start_utc = self._normalize_hhmm(ar[10] or "00:00")
                daily_end_utc = self._normalize_hhmm(ar[11] or "23:59")
                try:
                    duration_minutes = int(ar[12] or 60)
                except Exception:
                    duration_minutes = 60
                if duration_minutes not in {30, 60}:
                    duration_minutes = 30 if duration_minutes < 45 else 60
                actions.append(
                    {
                        "id": ar[0],
                        "group_name": str(ar[1] or "").strip().upper(),
                        "condition_levels": self._normalize_condition_levels(ar[2]),
                        "band": ar[3] or "",
                        "frequency": ar[4] or "",
                        "software": ar[5],
                        "mode": str(ar[6] or "").strip().upper(),
                        "action_key": ar[7],
                        "action_label": ar[8],
                        "enabled": bool(ar[9]),
                        "daily_start_utc": daily_start_utc,
                        "daily_end_utc": daily_end_utc,
                        "duration_minutes": duration_minutes,
                        "interval_hours": int(ar[13] or 3),
                        "interval_minutes": int(ar[14] or (int(ar[13] or 3) * 60)),
                        "interval_phase_minutes": int(ar[15] or 0),
                        "conflict_policy": self._normalize_conflict_policy(ar[16]),
                        "daily_conflict_summary": str(ar[17] or "").strip(),
                        "net_conflict_summary": str(ar[18] or "").strip(),
                        "schedule_applied": bool(ar[19]) if ar[19] is not None else True,
                        "description": ar[20] or "",
                        "contact_rule": ar[21] or "none",
                        "contact_target": ar[22] or "",
                        "sort_order": int(ar[23] or 0),
                        "last_completed_utc": ar[24] or "",
                    }
                )

            layer_cur = conn.execute(
                """
                SELECT
                    id,
                    day_utc,
                    recurrence,
                    biweekly_offset_weeks,
                    month_weeks,
                    condition_levels,
                    group_name,
                    band,
                    mode,
                    vfo,
                    frequency,
                    start_utc,
                    end_utc,
                    enabled,
                    sort_order,
                    updated_utc
                FROM sop_schedule_layer
                WHERE profile_id = ?
                ORDER BY sort_order, id
                """,
                (profile_id,),
            )
            schedule_layer = []
            for lr in layer_cur.fetchall():
                schedule_layer.append(
                    {
                        "id": int(lr[0] or 0),
                        "day_utc": str(lr[1] or "ALL"),
                        "recurrence": self._normalize_recurrence(lr[2]),
                        "biweekly_offset_weeks": int(lr[3] or 0),
                        "month_weeks": self._normalize_month_weeks(lr[4]),
                        "condition_levels": self._normalize_condition_levels(lr[5]),
                        "group_name": str(lr[6] or "").strip().upper(),
                        "band": str(lr[7] or "").strip().upper(),
                        "mode": str(lr[8] or "").strip().upper(),
                        "vfo": str(lr[9] or "").strip().upper(),
                        "frequency": str(lr[10] or "").strip(),
                        "start_utc": str(lr[11] or ""),
                        "end_utc": str(lr[12] or ""),
                        "enabled": bool(lr[13]),
                        "sort_order": int(lr[14] or 0),
                        "updated_utc": str(lr[15] or ""),
                    }
                )

            self._reattach_frequency_variant_keys(int(profile_id), actions, schedule_layer)

            return {
                "id": row[0],
                "name": row[1],
                "category": self._normalize_category(row[2]),
                "operating_group": row[3] or "",
                "secondary_group": row[4] or "",
                "frequency": row[5] or "",
                "sop_start_utc": row[6] or "00:00",
                "priority": int(row[7] or 100),
                "active": bool(row[8]),
                "window_hours": int(row[9] or 12),
                "created_utc": row[10] or "",
                "updated_utc": row[11] or "",
                "actions": actions,
                "schedule_layer": schedule_layer,
            }
        finally:
            conn.close()

    def save_profile(
        self,
        payload: Dict[str, Any],
        actions: List[Dict[str, Any]],
        schedule_layer: Optional[List[Dict[str, Any]]] = None,
    ) -> int:
        now_iso = _utc_now_iso()
        conn = self._connect()
        rebuild_from_actions = False
        try:
            with conn:
                profile_id = int(payload.get("id") or 0)
                profile_category = self._normalize_category(payload.get("category"))
                row = (
                    (payload.get("name") or "").strip(),
                    profile_category,
                    (payload.get("operating_group") or "").strip(),
                    (payload.get("secondary_group") or "").strip(),
                    (payload.get("frequency") or "").strip(),
                    (payload.get("sop_start_utc") or "00:00").strip(),
                    int(payload.get("priority") or 100),
                    1 if payload.get("active") else 0,
                    int(payload.get("window_hours") or 12),
                )
                if profile_id > 0:
                    conn.execute(
                        """
                        UPDATE sop_profiles
                        SET name=?, category=?, operating_group=?, secondary_group=?, frequency=?, sop_start_utc=?,
                            priority=?, active=?, window_hours=?, updated_utc=?
                        WHERE id=?
                        """,
                        (*row, now_iso, profile_id),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO sop_profiles
                            (name, category, operating_group, secondary_group, frequency, sop_start_utc,
                             priority, active, window_hours, created_utc, updated_utc)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (*row, now_iso, now_iso),
                    )
                    profile_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

                existing = {
                    int(r[0])
                    for r in conn.execute("SELECT id FROM sop_actions WHERE profile_id = ?", (profile_id,)).fetchall()
                }
                kept: set[int] = set()
                saved_action_refs: Dict[int, Mapping[str, Any]] = {}

                for idx, action in enumerate(actions):
                    action_id = int(action.get("id") or 0)
                    interval_minutes = int(action.get("interval_minutes") or 0)
                    if interval_minutes <= 0:
                        interval_minutes = int(action.get("interval_hours") or 3) * 60
                    interval_phase_minutes = max(0, int(action.get("interval_phase_minutes") or 0))
                    interval_phase_minutes = interval_phase_minutes % max(1, interval_minutes)
                    interval_hours_legacy = max(1, int((interval_minutes + 59) // 60))
                    duration_minutes = int(action.get("duration_minutes") or 60)
                    if duration_minutes not in {30, 60}:
                        duration_minutes = 30 if duration_minutes < 45 else 60
                    vals = (
                        profile_id,
                        (action.get("group_name") or "").strip().upper(),
                        self._normalize_condition_levels(action.get("condition_levels")),
                        (action.get("band") or "").strip().upper(),
                        (action.get("frequency") or "").strip(),
                        (action.get("software") or "").strip(),
                        (action.get("mode") or "").strip().upper(),
                        (action.get("action_key") or "").strip(),
                        (action.get("action_label") or "").strip(),
                        1 if action.get("enabled", True) else 0,
                        self._normalize_hhmm(action.get("daily_start_utc") or "00:00"),
                        self._normalize_hhmm(action.get("daily_end_utc") or "23:59"),
                        duration_minutes,
                        interval_hours_legacy,
                        interval_minutes,
                        interval_phase_minutes,
                        self._normalize_conflict_policy(action.get("conflict_policy")),
                        str(action.get("daily_conflict_summary") or "").strip(),
                        str(action.get("net_conflict_summary") or "").strip(),
                        1 if action.get("schedule_applied", True) else 0,
                        (action.get("description") or "").strip(),
                        (action.get("contact_rule") or "none").strip(),
                        (action.get("contact_target") or "").strip().upper(),
                        int(action.get("sort_order") if action.get("sort_order") is not None else idx),
                    )
                    if action_id > 0:
                        updated_action = conn.execute(
                            """
                            UPDATE sop_actions
                            SET group_name=?, condition_levels=?, band=?, frequency=?, software=?, mode=?,
                                action_key=?, action_label=?, enabled=?,
                                daily_start_utc=?, daily_end_utc=?, duration_minutes=?,
                                interval_hours=?, interval_minutes=?, interval_phase_minutes=?,
                                conflict_policy=?, daily_conflict_summary=?, net_conflict_summary=?, schedule_applied=?,
                                description=?, contact_rule=?, contact_target=?, sort_order=?
                            WHERE id=? AND profile_id=?
                            """,
                            (
                                vals[1],
                                vals[2],
                                vals[3],
                                vals[4],
                                vals[5],
                                vals[6],
                                vals[7],
                                vals[8],
                                vals[9],
                                vals[10],
                                vals[11],
                                vals[12],
                                vals[13],
                                vals[14],
                                vals[15],
                                vals[16],
                                vals[17],
                                vals[18],
                                vals[19],
                                vals[20],
                                vals[21],
                                vals[22],
                                vals[23],
                                action_id,
                                profile_id,
                            ),
                        )
                        if int(updated_action.rowcount or 0) <= 0:
                            raise ValueError(f"SOP action {action_id} no longer exists; reload before saving.")
                        kept.add(action_id)
                        saved_action_refs[action_id] = self._frequency_reference_row(
                            action, group=vals[1], band=vals[3], mode=vals[6], frequency=vals[4]
                        )
                    else:
                        conn.execute(
                            """
                            INSERT INTO sop_actions
                                (profile_id, group_name, condition_levels, band, frequency, software, mode,
                                 action_key, action_label, enabled,
                                 daily_start_utc, daily_end_utc, duration_minutes,
                                 interval_hours, interval_minutes, interval_phase_minutes,
                                 conflict_policy, daily_conflict_summary, net_conflict_summary, schedule_applied,
                                 description, contact_rule, contact_target, sort_order)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            vals,
                        )
                        new_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
                        kept.add(new_id)
                        saved_action_refs[new_id] = self._frequency_reference_row(
                            action, group=vals[1], band=vals[3], mode=vals[6], frequency=vals[4]
                        )

                to_delete = existing - kept
                for aid in to_delete:
                    conn.execute("DELETE FROM sop_action_state WHERE profile_id=? AND action_id=?", (profile_id, aid))
                    conn.execute("DELETE FROM sop_actions WHERE id=? AND profile_id=?", (aid, profile_id))

                if schedule_layer is not None:
                    existing_layer = {
                        int(r[0])
                        for r in conn.execute(
                            "SELECT id FROM sop_schedule_layer WHERE profile_id = ?",
                            (profile_id,),
                        ).fetchall()
                    }
                    kept_layer: set[int] = set()
                    saved_layer_refs: Dict[int, Mapping[str, Any]] = {}
                    for idx, layer in enumerate(schedule_layer):
                        if not isinstance(layer, dict):
                            continue
                        layer_id = int(layer.get("id") or 0)
                        recurrence = self._normalize_recurrence(layer.get("recurrence"))
                        day_utc = self._normalize_day_utc(layer.get("day_utc"))
                        if recurrence == "Daily":
                            day_utc = "ALL"
                        row_group_name = str(
                            layer.get("group_name") or payload.get("operating_group") or ""
                        ).strip().upper()
                        vals = (
                            profile_id,
                            day_utc,
                            recurrence,
                            int(layer.get("biweekly_offset_weeks") or 0),
                            self._normalize_month_weeks(layer.get("month_weeks")),
                            self._normalize_condition_levels(layer.get("condition_levels")),
                            row_group_name,
                            str(layer.get("band") or "").strip().upper(),
                            str(layer.get("mode") or "").strip().upper(),
                            str(layer.get("vfo") or "").strip().upper(),
                            self._normalize_frequency(layer.get("frequency")),
                            self._normalize_hhmm(layer.get("start_utc")),
                            self._normalize_hhmm(layer.get("end_utc")),
                            1 if layer.get("enabled", True) else 0,
                            int(layer.get("sort_order") if layer.get("sort_order") is not None else idx),
                            now_iso,
                        )
                        if not vals[10]:
                            continue
                        if layer_id > 0:
                            cur = conn.execute(
                                """
                                UPDATE sop_schedule_layer
                                SET day_utc=?, recurrence=?, biweekly_offset_weeks=?, month_weeks=?,
                                    condition_levels=?, group_name=?, band=?, mode=?, vfo=?, frequency=?, start_utc=?, end_utc=?,
                                    enabled=?, sort_order=?, updated_utc=?
                                WHERE id=? AND profile_id=?
                                """,
                                (
                                    vals[1],
                                    vals[2],
                                    vals[3],
                                    vals[4],
                                    vals[5],
                                    vals[6],
                                    vals[7],
                                    vals[8],
                                    vals[9],
                                    vals[10],
                                    vals[11],
                                    vals[12],
                                    vals[13],
                                    vals[14],
                                    vals[15],
                                    layer_id,
                                    profile_id,
                                ),
                            )
                            if int(cur.rowcount or 0) > 0:
                                kept_layer.add(layer_id)
                                saved_layer_refs[layer_id] = self._frequency_reference_row(
                                    layer, group=vals[6], band=vals[7], mode=vals[8], frequency=vals[10]
                                )
                                continue
                        conn.execute(
                            """
                            INSERT INTO sop_schedule_layer
                                (profile_id, day_utc, recurrence, biweekly_offset_weeks, month_weeks,
                                 condition_levels, group_name, band, mode, vfo, frequency, start_utc, end_utc,
                                 enabled, sort_order, updated_utc)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            vals,
                        )
                        new_layer_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
                        kept_layer.add(new_layer_id)
                        saved_layer_refs[new_layer_id] = self._frequency_reference_row(
                            layer, group=vals[6], band=vals[7], mode=vals[8], frequency=vals[10]
                        )

                    to_delete_layer = existing_layer - kept_layer
                    for lid in to_delete_layer:
                        conn.execute("DELETE FROM sop_schedule_layer WHERE id=? AND profile_id=?", (lid, profile_id))
                    self._write_frequency_variant_sidecar(
                        profile_id,
                        actions=saved_action_refs,
                        layers=saved_layer_refs,
                        deleted_actions=to_delete,
                        deleted_layers=to_delete_layer,
                    )
                elif profile_category == self.CATEGORY_HF:
                    rebuild_from_actions = True
                    self._write_frequency_variant_sidecar(
                        profile_id,
                        actions=saved_action_refs,
                        deleted_actions=to_delete,
                    )
                else:
                    self._write_frequency_variant_sidecar(
                        profile_id,
                        actions=saved_action_refs,
                        deleted_actions=to_delete,
                    )
            if rebuild_from_actions:
                try:
                    self.rebuild_schedule_layer_from_actions(profile_id)
                except Exception as e:
                    log.debug("SOP: rebuild_schedule_layer_from_actions failed for profile %s: %s", profile_id, e)
            if profile_category == self.CATEGORY_HF:
                try:
                    sync_stats = self.sync_profile_net_sop_conflict_policies(profile_id, horizon_days=35)
                    log.debug(
                        "SOP: synced Net/SOP conflict policies for profile %s (saved=%s cleared=%s desired=%s)",
                        profile_id,
                        int(sync_stats.get("saved") or 0),
                        int(sync_stats.get("cleared") or 0),
                        int(sync_stats.get("desired") or 0),
                    )
                except Exception as e:
                    log.debug("SOP: Net/SOP conflict policy sync failed for profile %s: %s", profile_id, e)
            self._prune_frequency_variant_sidecar(profile_id)
            self._invalidate_active_hf_conflicts_cache()
            return profile_id
        finally:
            conn.close()

    def upsert_schedule_layer_rows(self, profile_id: int, layer_rows: List[Dict[str, Any]]) -> int:
        """
        Upsert schedule-layer rows for one profile.
        Returns number of rows inserted or updated.
        """
        pid = int(profile_id or 0)
        rows = [r for r in (layer_rows or []) if isinstance(r, dict)]
        if pid <= 0 or not rows:
            return 0

        conn = self._connect()
        changed = 0
        saved_layer_refs: Dict[int, Mapping[str, Any]] = {}
        try:
            now_iso = _utc_now_iso()
            with conn:
                cur = conn.execute(
                    "SELECT COALESCE(MAX(sort_order), -1) FROM sop_schedule_layer WHERE profile_id = ?",
                    (pid,),
                )
                fetched = cur.fetchone()
                next_sort = int((fetched[0] if fetched else -1) or -1) + 1

                for idx, layer in enumerate(rows):
                    layer_id = int(layer.get("id") or 0)
                    recurrence = self._normalize_recurrence(layer.get("recurrence"))
                    day_utc = self._normalize_day_utc(layer.get("day_utc"))
                    if recurrence == "Daily":
                        day_utc = "ALL"
                    biweekly = int(layer.get("biweekly_offset_weeks") or 0)
                    month_weeks = self._normalize_month_weeks(layer.get("month_weeks"))
                    condition_levels = self._normalize_condition_levels(layer.get("condition_levels"))
                    group_name = str(layer.get("group_name") or "").strip().upper()
                    band = str(layer.get("band") or "").strip().upper()
                    mode = str(layer.get("mode") or "").strip().upper()
                    vfo = str(layer.get("vfo") or "A").strip().upper() or "A"
                    freq = self._normalize_frequency(layer.get("frequency"))
                    start_utc = self._normalize_hhmm(layer.get("start_utc"))
                    end_utc = self._normalize_hhmm(layer.get("end_utc"))
                    enabled = 1 if layer.get("enabled", True) else 0
                    sort_order = layer.get("sort_order")
                    if sort_order is None:
                        sort_order = next_sort + idx
                    try:
                        sort_val = int(sort_order)
                    except Exception:
                        sort_val = next_sort + idx

                    if not (band and mode and freq and start_utc and end_utc):
                        continue

                    if layer_id > 0:
                        updated = conn.execute(
                            """
                            UPDATE sop_schedule_layer
                            SET day_utc=?, recurrence=?, biweekly_offset_weeks=?, month_weeks=?,
                                condition_levels=?, group_name=?, band=?, mode=?, vfo=?, frequency=?, start_utc=?, end_utc=?,
                                enabled=?, sort_order=?, updated_utc=?
                            WHERE id=? AND profile_id=?
                            """,
                            (
                                day_utc,
                                recurrence,
                                biweekly,
                                month_weeks,
                                condition_levels,
                                group_name,
                                band,
                                mode,
                                vfo,
                                freq,
                                start_utc,
                                end_utc,
                                enabled,
                                sort_val,
                                now_iso,
                                layer_id,
                                pid,
                            ),
                        )
                        if int(updated.rowcount or 0) > 0:
                            changed += 1
                            saved_layer_refs[layer_id] = self._frequency_reference_row(
                                layer, group=group_name, band=band, mode=mode, frequency=freq
                            )
                            continue

                    existing = conn.execute(
                        """
                        SELECT id
                        FROM sop_schedule_layer
                        WHERE profile_id=? AND day_utc=? AND recurrence=? AND biweekly_offset_weeks=? AND month_weeks=?
                          AND condition_levels=? AND group_name=? AND band=? AND mode=? AND vfo=? AND frequency=? AND start_utc=? AND end_utc=?
                        LIMIT 1
                        """,
                        (
                            pid,
                            day_utc,
                            recurrence,
                            biweekly,
                            month_weeks,
                            condition_levels,
                            group_name,
                            band,
                            mode,
                            vfo,
                            freq,
                            start_utc,
                            end_utc,
                        ),
                    ).fetchone()
                    if existing:
                        conn.execute(
                            """
                            UPDATE sop_schedule_layer
                            SET enabled=?, updated_utc=?
                            WHERE id=? AND profile_id=?
                            """,
                            (enabled, now_iso, int(existing[0] or 0), pid),
                        )
                        changed += 1
                        saved_layer_refs[int(existing[0] or 0)] = self._frequency_reference_row(
                            layer, group=group_name, band=band, mode=mode, frequency=freq
                        )
                        continue

                    conn.execute(
                        """
                        INSERT INTO sop_schedule_layer
                            (profile_id, day_utc, recurrence, biweekly_offset_weeks, month_weeks,
                             condition_levels, group_name, band, mode, vfo, frequency, start_utc, end_utc,
                             enabled, sort_order, updated_utc)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            pid,
                            day_utc,
                            recurrence,
                            biweekly,
                            month_weeks,
                            condition_levels,
                            group_name,
                            band,
                            mode,
                            vfo,
                            freq,
                            start_utc,
                            end_utc,
                            enabled,
                            sort_val,
                            now_iso,
                        ),
                    )
                    saved_layer_refs[int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])] = self._frequency_reference_row(
                        layer, group=group_name, band=band, mode=mode, frequency=freq
                    )
                    changed += 1
                if saved_layer_refs:
                    self._write_frequency_variant_sidecar(pid, layers=saved_layer_refs)
            if changed > 0:
                self._prune_frequency_variant_sidecar(pid)
                self._invalidate_active_hf_conflicts_cache()
            return changed
        finally:
            conn.close()

    def delete_profile(self, profile_id: int) -> None:
        conn = self._connect()
        try:
            with conn:
                action_ids = {int(row[0]) for row in conn.execute("SELECT id FROM sop_actions WHERE profile_id=?", (profile_id,))}
                layer_ids = {int(row[0]) for row in conn.execute("SELECT id FROM sop_schedule_layer WHERE profile_id=?", (profile_id,))}
                self._write_frequency_variant_sidecar(
                    int(profile_id), deleted_actions=action_ids, deleted_layers=layer_ids
                )
                conn.execute("DELETE FROM sop_action_state WHERE profile_id=?", (profile_id,))
                conn.execute("DELETE FROM sop_actions WHERE profile_id=?", (profile_id,))
                conn.execute("DELETE FROM sop_schedule_layer WHERE profile_id=?", (profile_id,))
                conn.execute("DELETE FROM sop_profiles WHERE id=?", (profile_id,))
            self._prune_frequency_variant_sidecar(int(profile_id), remove_profile=True)
            self._invalidate_active_hf_conflicts_cache()
        finally:
            conn.close()

    def mark_action_complete(self, profile_id: int, action_id: int, completed_utc: Optional[str] = None) -> None:
        stamp = completed_utc or _utc_now_iso()
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO sop_action_state (profile_id, action_id, last_completed_utc, updated_utc)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(profile_id, action_id)
                    DO UPDATE SET last_completed_utc=excluded.last_completed_utc,
                                  updated_utc=excluded.updated_utc
                    """,
                    (profile_id, action_id, stamp, stamp),
                )
        finally:
            conn.close()

    def compute_next_due(
        self,
        start_hhmm: str,
        interval_minutes: int,
        now_utc: Optional[dt.datetime] = None,
        last_completed_utc: str | None = None,
        phase_minutes: int = 0,
    ) -> dt.datetime:
        now = now_utc or dt.datetime.now(dt.timezone.utc)
        interval_m = max(1, int(interval_minutes))
        interval = interval_m * 60
        phase_m = max(0, int(phase_minutes or 0)) % interval_m
        h, m = _parse_hhmm(start_hhmm, default_hhmm="00:00")
        anchor = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if anchor > now:
            anchor -= dt.timedelta(days=1)
        if phase_m:
            anchor += dt.timedelta(minutes=phase_m)
            if anchor > now:
                anchor -= dt.timedelta(days=1)

        ref = now
        last_completed = _parse_iso_utc(last_completed_utc)
        if last_completed and last_completed > ref:
            ref = last_completed
        if last_completed and last_completed <= ref:
            ref = last_completed + dt.timedelta(seconds=1)

        delta = (ref - anchor).total_seconds()
        if delta <= 0:
            return anchor
        step_count = int(delta // interval)
        if step_count * interval < delta:
            step_count += 1
        return anchor + dt.timedelta(seconds=step_count * interval)

    @staticmethod
    def _ensure_utc(value: dt.datetime) -> dt.datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc)

    def _condition_level_map(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        try:
            rows = self.settings.get("operating_groups", []) or []
        except Exception:
            rows = []
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
                lvl = int(row.get("condition_level", 0) or 0)
            except Exception:
                lvl = 0
            if not (1 <= lvl <= 5):
                continue
            prev = out.get(group)
            if prev is None or lvl < prev:
                out[group] = lvl
        return out

    @staticmethod
    def _action_condition_match(condition_levels: str, group_level: Optional[int]) -> bool:
        normalized = SOPManager._normalize_condition_levels(condition_levels)
        if normalized == "ALL":
            return True
        if group_level is None:
            return True
        levels: Set[int] = set()
        for token in normalized.split(","):
            token = token.strip()
            if not token:
                continue
            try:
                lvl = int(token)
            except Exception:
                continue
            if 1 <= lvl <= 5:
                levels.add(lvl)
        if not levels:
            return True
        return int(group_level) in levels

    @staticmethod
    def _is_local_action(action: Dict[str, Any]) -> bool:
        software = str(action.get("software") or "").strip().upper()
        contact_rule = str(action.get("contact_rule") or "").strip().lower()
        action_key = str(action.get("action_key") or "").strip().lower()
        if software == "LOCAL NET":
            return True
        if contact_rule in {"local_group", "local_profile"}:
            return True
        return action_key.startswith("local_")

    def build_action_occurrences_in_window(
        self,
        action: Dict[str, Any],
        *,
        window_start_utc: dt.datetime,
        window_end_utc: dt.datetime,
    ) -> List[Tuple[dt.datetime, dt.datetime]]:
        start_utc = self._ensure_utc(window_start_utc)
        end_utc = self._ensure_utc(window_end_utc)
        if end_utc < start_utc:
            return []
        start_hhmm = self._normalize_hhmm(action.get("daily_start_utc") or "00:00")
        end_hhmm = self._normalize_hhmm(action.get("daily_end_utc") or "23:59")
        interval_minutes = max(1, int(action.get("interval_minutes") or 60))
        phase_minutes = max(0, int(action.get("interval_phase_minutes") or 0)) % interval_minutes
        duration_minutes = int(action.get("duration_minutes") or 60)
        if duration_minutes not in {30, 60}:
            duration_minutes = 30 if duration_minutes < 45 else 60
        sh, sm = _parse_hhmm(start_hhmm, default_hhmm="00:00")
        eh, em = _parse_hhmm(end_hhmm, default_hhmm="23:59")
        day_start = start_utc.date() - dt.timedelta(days=1)
        day_end = end_utc.date() + dt.timedelta(days=1)
        out: List[Tuple[dt.datetime, dt.datetime]] = []
        day = day_start
        while day <= day_end:
            anchor = dt.datetime(day.year, day.month, day.day, sh, sm, tzinfo=dt.timezone.utc)
            window_end = dt.datetime(day.year, day.month, day.day, eh, em, tzinfo=dt.timezone.utc)
            if window_end <= anchor:
                window_end += dt.timedelta(days=1)
            due = anchor + dt.timedelta(minutes=phase_minutes)
            if due < anchor:
                due = anchor
            guard = 0
            while due <= window_end and guard < 240:
                due_end = due + dt.timedelta(minutes=duration_minutes)
                if due_end > window_end:
                    due_end = window_end
                if due_end > start_utc and due < end_utc:
                    out.append((due, due_end))
                due += dt.timedelta(minutes=interval_minutes)
                guard += 1
            day += dt.timedelta(days=1)
        out.sort(key=lambda p: p[0])
        return out

    @staticmethod
    def _day_matches_name(day_value: str, weekday_name: str) -> bool:
        day_norm = SOPManager._normalize_day_utc(day_value).upper()
        if day_norm == "ALL":
            return True
        return weekday_name.upper().startswith(day_norm[:3].upper())

    def _schedule_row_applies_on_date(self, row: Dict[str, Any], date_val: dt.date) -> bool:
        recurrence = self._normalize_recurrence(row.get("recurrence"))
        day_utc = self._normalize_day_utc(row.get("day_utc"))
        weekday_name = _day_name_from_utc(
            dt.datetime(date_val.year, date_val.month, date_val.day, 12, 0, tzinfo=dt.timezone.utc)
        )
        if recurrence == "Daily":
            return True
        if not self._day_matches_name(day_utc, weekday_name):
            return False
        if recurrence == "Periodic":
            weeks_txt = self._normalize_month_weeks(row.get("month_weeks"))
            weeks = [int(tok) for tok in weeks_txt.split(",") if tok.strip().isdigit()]
            if not weeks:
                weeks = [1]
            return self._month_week_index(date_val) in weeks
        if recurrence == "Bi-Weekly":
            try:
                offset = int(row.get("biweekly_offset_weeks") or 0)
            except Exception:
                offset = 0
            week_idx = int(date_val.isocalendar()[1])
            return ((week_idx - offset) % 2) == 0
        return True

    def _load_group_schedule_rows(
        self,
        *,
        operating_group: str,
        source: str,
    ) -> List[Dict[str, Any]]:
        group = (operating_group or "").strip().upper()
        if not group:
            return []
        table_name = "daily_schedule_tab" if source == "daily" else "net_schedule_tab"
        if source == "daily":
            conn = sqlite3.connect(self._settings_db_path())
        else:
            conn = self._connect()
        out: List[Dict[str, Any]] = []
        try:
            cols = _table_columns(conn, table_name)
            if not cols:
                return []
            group_col = "group_name" if "group_name" in cols else ("group" if "group" in cols else "")
            if not group_col:
                return []
            if not {"day_utc", "start_utc", "end_utc"}.issubset(cols):
                return []
            name_expr = (
                "COALESCE(net_name, '')"
                if source == "net" and "net_name" in cols
                else "COALESCE(group_name, '')"
                if "group_name" in cols
                else "''"
            )
            recurrence_expr = "COALESCE(recurrence, 'Weekly')" if "recurrence" in cols else "'Weekly'"
            weeks_expr = "COALESCE(month_weeks, '')" if "month_weeks" in cols else "''"
            biweek_expr = "COALESCE(biweekly_offset_weeks, 0)" if "biweekly_offset_weeks" in cols else "0"
            band_expr = "COALESCE(band, '')" if "band" in cols else "''"
            freq_expr = "COALESCE(frequency, '')" if "frequency" in cols else "''"
            sql = f"""
                SELECT day_utc, start_utc, end_utc,
                       {recurrence_expr},
                       {weeks_expr},
                       {biweek_expr},
                       {band_expr},
                       {freq_expr},
                       {name_expr}
                FROM {table_name}
                WHERE UPPER(COALESCE({group_col}, '')) = ?
            """
            for row in conn.execute(sql, (group,)).fetchall():
                out.append(
                    {
                        "day_utc": str(row[0] or "ALL"),
                        "start_utc": self._normalize_hhmm(row[1] or "00:00"),
                        "end_utc": self._normalize_hhmm(row[2] or "23:59"),
                        "recurrence": self._normalize_recurrence(row[3]),
                        "month_weeks": self._normalize_month_weeks(row[4]),
                        "biweekly_offset_weeks": int(row[5] or 0),
                        "band": str(row[6] or "").strip().upper(),
                        "frequency": self._normalize_frequency(row[7]),
                        "name": str(row[8] or "").strip(),
                        "source": source,
                    }
                )
            return out
        except Exception as e:
            log.debug("SOP: load_group_schedule_rows failed (%s): %s", source, e)
            return []
        finally:
            conn.close()

    def _load_all_schedule_rows(self, *, source: str) -> List[Dict[str, Any]]:
        table_name = "daily_schedule_tab" if source == "daily" else "net_schedule_tab"
        if source == "daily":
            conn = sqlite3.connect(self._settings_db_path())
        else:
            conn = self._connect()
        out: List[Dict[str, Any]] = []
        try:
            cols = _table_columns(conn, table_name)
            if not cols:
                return []
            if not {"day_utc", "start_utc", "end_utc"}.issubset(cols):
                return []
            group_col = "group_name" if "group_name" in cols else ("group" if "group" in cols else "")
            name_expr = (
                "COALESCE(net_name, '')"
                if source == "net" and "net_name" in cols
                else "COALESCE(group_name, '')"
                if "group_name" in cols
                else "''"
            )
            recurrence_expr = "COALESCE(recurrence, 'Weekly')" if "recurrence" in cols else "'Weekly'"
            weeks_expr = "COALESCE(month_weeks, '')" if "month_weeks" in cols else "''"
            biweek_expr = "COALESCE(biweekly_offset_weeks, 0)" if "biweekly_offset_weeks" in cols else "0"
            band_expr = "COALESCE(band, '')" if "band" in cols else "''"
            freq_expr = "COALESCE(frequency, '')" if "frequency" in cols else "''"
            group_expr = f"COALESCE({group_col}, '')" if group_col else "''"
            sql = f"""
                SELECT day_utc, start_utc, end_utc,
                       {recurrence_expr},
                       {weeks_expr},
                       {biweek_expr},
                       {band_expr},
                       {freq_expr},
                       {name_expr},
                       {group_expr}
                FROM {table_name}
            """
            for row in conn.execute(sql).fetchall():
                out.append(
                    {
                        "day_utc": str(row[0] or "ALL"),
                        "start_utc": self._normalize_hhmm(row[1] or "00:00"),
                        "end_utc": self._normalize_hhmm(row[2] or "23:59"),
                        "recurrence": self._normalize_recurrence(row[3]),
                        "month_weeks": self._normalize_month_weeks(row[4]),
                        "biweekly_offset_weeks": int(row[5] or 0),
                        "band": str(row[6] or "").strip().upper(),
                        "frequency": self._normalize_frequency(row[7]),
                        "name": str(row[8] or "").strip(),
                        "group_name": str(row[9] or "").strip().upper(),
                        "source": source,
                    }
                )
            return out
        except Exception as e:
            log.debug("SOP: load_all_schedule_rows failed (%s): %s", source, e)
            return []
        finally:
            conn.close()

    def _expand_schedule_rows_windows(
        self,
        rows: List[Dict[str, Any]],
        *,
        window_start_utc: dt.datetime,
        window_end_utc: dt.datetime,
    ) -> List[Dict[str, Any]]:
        start_utc = self._ensure_utc(window_start_utc)
        end_utc = self._ensure_utc(window_end_utc)
        out: List[Dict[str, Any]] = []
        day = start_utc.date() - dt.timedelta(days=1)
        day_end = end_utc.date() + dt.timedelta(days=1)
        while day <= day_end:
            for row in rows:
                if not self._schedule_row_applies_on_date(row, day):
                    continue
                sh, sm = _parse_hhmm(str(row.get("start_utc") or ""), default_hhmm="00:00")
                eh, em = _parse_hhmm(str(row.get("end_utc") or ""), default_hhmm="23:59")
                begin = dt.datetime(day.year, day.month, day.day, sh, sm, tzinfo=dt.timezone.utc)
                finish = dt.datetime(day.year, day.month, day.day, eh, em, tzinfo=dt.timezone.utc)
                if finish <= begin:
                    finish += dt.timedelta(days=1)
                if finish <= start_utc or begin >= end_utc:
                    continue
                copy_row = dict(row)
                copy_row["start_dt_utc"] = begin
                copy_row["end_dt_utc"] = finish
                out.append(copy_row)
            day += dt.timedelta(days=1)
        return out

    @staticmethod
    def _ranges_overlap(a_start: dt.datetime, a_end: dt.datetime, b_start: dt.datetime, b_end: dt.datetime) -> bool:
        return a_start < b_end and b_start < a_end

    @staticmethod
    def _day_abbrev_for_dt(value: dt.datetime) -> str:
        try:
            return value.astimezone(dt.timezone.utc).strftime("%a")
        except Exception:
            return ""

    def detect_action_conflicts(
        self,
        *,
        action: Dict[str, Any],
        operating_group: str,
        horizon_days: int = 7,
        check_all_groups: bool = False,
        peer_actions: Optional[List[Dict[str, Any]]] = None,
        include_details: bool = False,
    ) -> Dict[str, Any]:
        now_utc = dt.datetime.now(dt.timezone.utc)
        window_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        window_end = window_start + dt.timedelta(days=max(1, int(horizon_days)))
        occurrences = self.build_action_occurrences_in_window(
            action,
            window_start_utc=window_start,
            window_end_utc=window_end,
        )
        if check_all_groups:
            daily_rows = self._load_all_schedule_rows(source="daily")
            net_rows = self._load_all_schedule_rows(source="net")
        else:
            daily_rows = self._load_group_schedule_rows(operating_group=operating_group, source="daily")
            net_rows = self._load_group_schedule_rows(operating_group=operating_group, source="net")
        daily_windows = self._expand_schedule_rows_windows(
            daily_rows,
            window_start_utc=window_start,
            window_end_utc=window_end,
        )
        net_windows = self._expand_schedule_rows_windows(
            net_rows,
            window_start_utc=window_start,
            window_end_utc=window_end,
        )
        action_freq = self._normalize_frequency(action.get("frequency"))
        action_band = str(action.get("band") or "").strip().upper()
        daily_conflicts: Set[str] = set()
        net_conflicts: Set[str] = set()
        sop_conflicts: Set[str] = set()
        daily_details: List[Dict[str, Any]] = []
        net_details: List[Dict[str, Any]] = []
        sop_details: List[Dict[str, Any]] = []
        daily_detail_seen: Set[Tuple[str, str, str, str, str, str]] = set()
        net_detail_seen: Set[Tuple[str, str, str, str, str, str]] = set()
        sop_detail_seen: Set[Tuple[str, str, str, str, str, str]] = set()
        detail_limit_per_source = 80
        detail_overflow = {"daily": 0, "net": 0, "sop": 0}
        first_conflict = False
        first_occurrence_checked = False

        def _append_detail(
            *,
            bucket_key: str,
            target: List[Dict[str, Any]],
            seen: Set[Tuple[str, str, str, str, str, str]],
            occ_start: dt.datetime,
            occ_end: dt.datetime,
            other_start: dt.datetime,
            other_end: dt.datetime,
            other_label: str,
            other_group: str,
            other_band: str,
            other_freq: str,
        ) -> None:
            if not include_details:
                return
            overlap_start = max(self._ensure_utc(occ_start), self._ensure_utc(other_start))
            overlap_end = min(self._ensure_utc(occ_end), self._ensure_utc(other_end))
            if overlap_end <= overlap_start:
                return
            overlap_start_iso = overlap_start.replace(microsecond=0).isoformat()
            overlap_end_iso = overlap_end.replace(microsecond=0).isoformat()
            key = (
                overlap_start_iso,
                overlap_end_iso,
                str(other_label or "").strip(),
                str(other_group or "").strip().upper(),
                str(other_band or "").strip().upper(),
                self._normalize_frequency(other_freq),
            )
            if key in seen:
                return
            if len(target) >= detail_limit_per_source:
                detail_overflow[bucket_key] = int(detail_overflow.get(bucket_key) or 0) + 1
                return
            seen.add(key)
            target.append(
                {
                    "overlap_start_utc": overlap_start_iso,
                    "overlap_end_utc": overlap_end_iso,
                    "other_label": str(other_label or "").strip(),
                    "other_group": str(other_group or "").strip().upper(),
                    "other_band": str(other_band or "").strip().upper(),
                    "other_frequency": self._normalize_frequency(other_freq),
                    "action_band": action_band,
                    "action_frequency": action_freq,
                    "reason": "Time overlap on different frequency",
                }
            )

        peer_windows: List[Dict[str, Any]] = []
        for peer in peer_actions or []:
            if not isinstance(peer, dict):
                continue
            if peer is action:
                continue
            if not _is_enabled(peer.get("enabled", True)):
                continue
            if self._is_local_action(peer):
                continue
            peer_band = str(peer.get("band") or "").strip().upper()
            peer_freq = self._normalize_frequency(peer.get("frequency"))
            # Same band/frequency is not a radio-frequency conflict.
            if peer_band and action_band and peer_band == action_band and peer_freq and action_freq and peer_freq == action_freq:
                continue
            peer_label = str(peer.get("action_label") or peer.get("action_key") or "SOP Action").strip()
            peer_group = str(peer.get("group_name") or "").strip().upper()
            peer_desc = peer_label
            if peer_group:
                peer_desc = f"{peer_group}: {peer_desc}"
            peer_occurrences = self.build_action_occurrences_in_window(
                peer,
                window_start_utc=window_start,
                window_end_utc=window_end,
            )
            for p_start, p_end in peer_occurrences:
                peer_windows.append(
                    {
                        "start_dt_utc": p_start,
                        "end_dt_utc": p_end,
                        "desc": peer_desc,
                        "label": peer_label,
                        "group": peer_group,
                        "band": peer_band,
                        "frequency": peer_freq,
                    }
                )

        for occ_start, occ_end in occurrences:
            occ_daily = False
            occ_net = False
            occ_sop = False
            for row in daily_windows:
                if not self._ranges_overlap(
                    occ_start,
                    occ_end,
                    self._ensure_utc(row["start_dt_utc"]),
                    self._ensure_utc(row["end_dt_utc"]),
                ):
                    continue
                row_freq = self._normalize_frequency(row.get("frequency"))
                if row_freq and action_freq and row_freq == action_freq:
                    continue
                name = str(row.get("name") or operating_group).strip().upper() or operating_group
                daily_conflicts.add(name)
                _append_detail(
                    bucket_key="daily",
                    target=daily_details,
                    seen=daily_detail_seen,
                    occ_start=occ_start,
                    occ_end=occ_end,
                    other_start=self._ensure_utc(row["start_dt_utc"]),
                    other_end=self._ensure_utc(row["end_dt_utc"]),
                    other_label=name,
                    other_group=str(row.get("group_name") or operating_group),
                    other_band=str(row.get("band") or "").strip().upper(),
                    other_freq=row.get("frequency") or "",
                )
                occ_daily = True
            for row in net_windows:
                if not self._ranges_overlap(
                    occ_start,
                    occ_end,
                    self._ensure_utc(row["start_dt_utc"]),
                    self._ensure_utc(row["end_dt_utc"]),
                ):
                    continue
                row_freq = self._normalize_frequency(row.get("frequency"))
                if row_freq and action_freq and row_freq == action_freq:
                    continue
                name = str(row.get("name") or "Net").strip() or "Net"
                day_txt = self._day_abbrev_for_dt(self._ensure_utc(row["start_dt_utc"]))
                net_conflicts.add(f"{name} ({day_txt})" if day_txt else name)
                _append_detail(
                    bucket_key="net",
                    target=net_details,
                    seen=net_detail_seen,
                    occ_start=occ_start,
                    occ_end=occ_end,
                    other_start=self._ensure_utc(row["start_dt_utc"]),
                    other_end=self._ensure_utc(row["end_dt_utc"]),
                    other_label=name,
                    other_group=str(row.get("group_name") or ""),
                    other_band=str(row.get("band") or "").strip().upper(),
                    other_freq=row.get("frequency") or "",
                )
                occ_net = True
            for peer_row in peer_windows:
                p_start = self._ensure_utc(peer_row.get("start_dt_utc"))
                p_end = self._ensure_utc(peer_row.get("end_dt_utc"))
                if not self._ranges_overlap(occ_start, occ_end, p_start, p_end):
                    continue
                p_desc = str(peer_row.get("desc") or "").strip()
                sop_conflicts.add(p_desc)
                _append_detail(
                    bucket_key="sop",
                    target=sop_details,
                    seen=sop_detail_seen,
                    occ_start=occ_start,
                    occ_end=occ_end,
                    other_start=p_start,
                    other_end=p_end,
                    other_label=str(peer_row.get("label") or p_desc or "SOP Action"),
                    other_group=str(peer_row.get("group") or ""),
                    other_band=str(peer_row.get("band") or "").strip().upper(),
                    other_freq=peer_row.get("frequency") or "",
                )
                occ_sop = True
            if not first_occurrence_checked:
                first_conflict = bool(occ_daily or occ_net or occ_sop)
                first_occurrence_checked = True
        daily_summary = ", ".join(sorted(daily_conflicts))
        net_summary = ", ".join(sorted(net_conflicts))
        sop_summary = ", ".join(sorted(sop_conflicts))

        def _detail_sort_key(row: Dict[str, Any]) -> Tuple[str, str, str, str]:
            return (
                str(row.get("overlap_start_utc") or ""),
                str(row.get("other_label") or "").upper(),
                str(row.get("other_band") or "").upper(),
                str(row.get("other_frequency") or ""),
            )

        if daily_details:
            daily_details.sort(key=_detail_sort_key)
        if net_details:
            net_details.sort(key=_detail_sort_key)
        if sop_details:
            sop_details.sort(key=_detail_sort_key)
        return {
            "daily_conflicts": sorted(daily_conflicts),
            "net_conflicts": sorted(net_conflicts),
            "sop_conflicts": sorted(sop_conflicts),
            "daily_summary": daily_summary,
            "net_summary": net_summary,
            "sop_summary": sop_summary,
            "daily_details": daily_details,
            "net_details": net_details,
            "sop_details": sop_details,
            "daily_detail_overflow": int(detail_overflow.get("daily") or 0),
            "net_detail_overflow": int(detail_overflow.get("net") or 0),
            "sop_detail_overflow": int(detail_overflow.get("sop") or 0),
            "has_conflict": bool(daily_conflicts or net_conflicts or sop_conflicts),
            "first_occurrence_conflict": bool(first_conflict),
        }

    def suggest_non_conflicting_start(
        self,
        *,
        action: Dict[str, Any],
        operating_group: str,
        step_minutes: int = 15,
        check_all_groups: bool = False,
        peer_actions: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        base = dict(action or {})
        end_hhmm = self._normalize_hhmm(base.get("daily_end_utc") or "23:59")
        interval = max(1, int(base.get("interval_minutes") or 60))
        step = max(5, int(step_minutes))
        for minute in range(0, 24 * 60, step):
            hh = minute // 60
            mm = minute % 60
            candidate = f"{hh:02d}:{mm:02d}"
            base["daily_start_utc"] = candidate
            # Keep end at least one duration after start when possible.
            dur = int(base.get("duration_minutes") or 60)
            end_minute = minute + max(30, min(60, dur))
            if end_minute < 24 * 60:
                base["daily_end_utc"] = f"{end_minute // 60:02d}:{end_minute % 60:02d}"
            else:
                base["daily_end_utc"] = end_hhmm
            diag = self.detect_action_conflicts(
                action=base,
                operating_group=operating_group,
                horizon_days=7,
                check_all_groups=check_all_groups,
                peer_actions=peer_actions,
            )
            if not bool(diag.get("first_occurrence_conflict")):
                return candidate
        return self._normalize_hhmm(action.get("daily_start_utc") or "00:00")

    def rebuild_schedule_layer_from_actions(self, profile_id: int) -> Dict[str, Any]:
        profile = self.get_profile(int(profile_id or 0))
        if not profile:
            return {"profile_id": int(profile_id or 0), "updated_rows": 0, "skipped_rows": 0}
        if self._normalize_category(profile.get("category")) == self.CATEGORY_LOCAL:
            return {"profile_id": int(profile.get("id") or 0), "updated_rows": 0, "skipped_rows": 0}
        actions = [a for a in (profile.get("actions") or []) if isinstance(a, dict)]
        if not actions:
            self.upsert_schedule_layer_rows(int(profile.get("id") or 0), [])
            return {"profile_id": int(profile.get("id") or 0), "updated_rows": 0, "skipped_rows": 0}

        layer_rows: List[Dict[str, Any]] = []
        skipped_rows = 0
        action_updates: List[Tuple[str, str, int]] = []
        for action in actions:
            if not _is_enabled(action.get("enabled", True)):
                continue
            if self._is_local_action(action):
                continue
            if not bool(action.get("schedule_applied", True)):
                continue
            band = str(action.get("band") or "").strip().upper()
            freq = self._normalize_frequency(action.get("frequency"))
            if not band or not freq:
                skipped_rows += 1
                continue
            mode = str(action.get("mode") or "").strip().upper() or "DIGI"
            action_group = str(action.get("group_name") or profile.get("operating_group") or "").strip().upper()
            diag = self.detect_action_conflicts(
                action=action,
                operating_group=action_group,
                horizon_days=7,
                check_all_groups=True,
                peer_actions=actions,
            )
            start_utc = self._normalize_hhmm(action.get("daily_start_utc") or "00:00")
            end_utc = self._normalize_hhmm(action.get("daily_end_utc") or "23:59")
            layer_rows.append(
                {
                    "day_utc": "ALL",
                    "recurrence": "Daily",
                    "biweekly_offset_weeks": 0,
                    "month_weeks": "",
                    "condition_levels": self._normalize_condition_levels(action.get("condition_levels")),
                    "group_name": action_group,
                    "band": band,
                    "mode": mode,
                    "vfo": "A",
                    "frequency": freq,
                    "operating_group_frequency_key": str(action.get("operating_group_frequency_key") or ""),
                    "start_utc": start_utc,
                    "end_utc": end_utc,
                    "enabled": True,
                }
            )
            daily_summary = str(diag.get("daily_summary") or "").strip()
            net_summary = str(diag.get("net_summary") or "").strip()
            action_updates.append((daily_summary, net_summary, int(action.get("id") or 0)))

        dedup: Dict[Tuple[str, str, str, str, str, str, str, str], Dict[str, Any]] = {}
        for row in layer_rows:
            key = (
                str(row.get("day_utc") or "").upper(),
                str(row.get("band") or "").upper(),
                str(row.get("mode") or "").upper(),
                str(row.get("frequency") or ""),
                str(row.get("start_utc") or ""),
                str(row.get("end_utc") or ""),
                str(row.get("group_name") or "").upper(),
                self._normalize_condition_levels(row.get("condition_levels")),
            )
            dedup[key] = row
        final_rows = list(dedup.values())
        final_rows.sort(
            key=lambda r: (
                str(r.get("day_utc") or ""),
                str(r.get("start_utc") or ""),
                str(r.get("band") or ""),
                str(r.get("frequency") or ""),
            )
        )
        for idx, row in enumerate(final_rows):
            row["sort_order"] = idx

        conn = self._connect()
        try:
            with conn:
                conn.execute("DELETE FROM sop_schedule_layer WHERE profile_id = ?", (int(profile.get("id") or 0),))
            self.upsert_schedule_layer_rows(int(profile.get("id") or 0), final_rows)
            self._prune_frequency_variant_sidecar(int(profile.get("id") or 0))
            with conn:
                for daily_summary, net_summary, action_id in action_updates:
                    if action_id <= 0:
                        continue
                    conn.execute(
                        """
                        UPDATE sop_actions
                        SET daily_conflict_summary = ?, net_conflict_summary = ?
                        WHERE id = ? AND profile_id = ?
                        """,
                        (daily_summary, net_summary, action_id, int(profile.get("id") or 0)),
                    )
        finally:
            conn.close()

        return {
            "profile_id": int(profile.get("id") or 0),
            "updated_rows": len(final_rows),
            "skipped_rows": int(skipped_rows),
        }

    def collect_profile_conflicts(
        self,
        profile_id: int,
        *,
        include_details: bool = False,
        include_suggestions: bool = True,
    ) -> List[Dict[str, Any]]:
        profile = self.get_profile(int(profile_id or 0))
        if not profile:
            return []
        operating_group = str(profile.get("operating_group") or "").strip().upper()
        peer_actions: List[Dict[str, Any]] = [
            dict(a)
            for a in (profile.get("actions", []) or [])
            if isinstance(a, dict) and _is_enabled(a.get("enabled", True)) and not self._is_local_action(a)
        ]
        out: List[Dict[str, Any]] = []
        for action in peer_actions:
            diag = self.detect_action_conflicts(
                action=action,
                operating_group=str(action.get("group_name") or operating_group).strip().upper(),
                horizon_days=7,
                check_all_groups=True,
                peer_actions=peer_actions,
                include_details=include_details,
            )
            out.append(
                {
                    "action_id": int(action.get("id") or 0),
                    "action_label": str(action.get("action_label") or ""),
                    "resource": str(action.get("software") or ""),
                    "band": str(action.get("band") or ""),
                    "frequency": str(action.get("frequency") or ""),
                    "daily_conflicts": list(diag.get("daily_conflicts") or []),
                    "net_conflicts": list(diag.get("net_conflicts") or []),
                    "sop_conflicts": list(diag.get("sop_conflicts") or []),
                    "daily_summary": str(diag.get("daily_summary") or ""),
                    "net_summary": str(diag.get("net_summary") or ""),
                    "sop_summary": str(diag.get("sop_summary") or ""),
                    "has_conflict": bool(diag.get("has_conflict")),
                    "first_occurrence_conflict": bool(diag.get("first_occurrence_conflict")),
                    "suggested_start_utc": (
                        self.suggest_non_conflicting_start(
                            action=action,
                            operating_group=str(action.get("group_name") or operating_group).strip().upper(),
                            check_all_groups=True,
                            peer_actions=peer_actions,
                        )
                        if include_suggestions
                        else ""
                    ),
                    "conflict_policy": self._normalize_conflict_policy(action.get("conflict_policy")),
                    "daily_details": list(diag.get("daily_details") or []),
                    "net_details": list(diag.get("net_details") or []),
                    "sop_details": list(diag.get("sop_details") or []),
                }
            )
        return out

    def collect_active_hf_conflicts(
        self,
        *,
        force_refresh: bool = False,
        include_details: bool = False,
        include_suggestions: bool = True,
    ) -> List[Dict[str, Any]]:
        if not include_details and not force_refresh and self._active_hf_conflicts_cache is not None:
            age_seconds = time.monotonic() - float(self._active_hf_conflicts_cache_monotonic or 0.0)
            if 0.0 <= age_seconds <= float(self._active_hf_conflicts_cache_ttl_seconds or 0.0):
                return [dict(row) for row in self._active_hf_conflicts_cache]

        out: List[Dict[str, Any]] = []
        for profile in self.list_profiles():
            if not bool(profile.get("active")):
                continue
            if self._normalize_category(profile.get("category")) != self.CATEGORY_HF:
                continue
            profile_id = int(profile.get("id") or 0)
            if profile_id <= 0:
                continue
            conflicts = self.collect_profile_conflicts(
                profile_id,
                include_details=include_details,
                include_suggestions=include_suggestions,
            )
            for item in conflicts:
                if bool(item.get("has_conflict")):
                    out.append(
                        {
                            "profile_id": profile_id,
                            "profile_name": str(profile.get("name") or ""),
                            **item,
                        }
                    )
        if not include_details:
            self._active_hf_conflicts_cache = [dict(row) for row in out]
            self._active_hf_conflicts_cache_monotonic = time.monotonic()
        return [dict(row) for row in out]

    def list_net_sop_conflict_policies(self, *, active_only: bool = True) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            if not _table_columns(conn, "sop_net_conflict_policy"):
                return []
            sql = """
                SELECT
                    id,
                    sop_profile_id,
                    sop_layer_id,
                    net_row_signature,
                    sop_row_signature,
                    policy,
                    window_start_utc,
                    window_end_utc,
                    active,
                    resolution_note,
                    updated_utc
                FROM sop_net_conflict_policy
            """
            params: List[Any] = []
            if active_only:
                sql += " WHERE COALESCE(active, 1) = 1"
            sql += " ORDER BY COALESCE(updated_utc, '') DESC, id DESC"
            out: List[Dict[str, Any]] = []
            for row in conn.execute(sql, tuple(params)).fetchall():
                out.append(
                    {
                        "id": int(row[0] or 0),
                        "sop_profile_id": int(row[1] or 0),
                        "sop_layer_id": int(row[2] or 0),
                        "net_row_signature": str(row[3] or "").strip(),
                        "sop_row_signature": str(row[4] or "").strip(),
                        "policy": self._normalize_net_sop_policy(row[5]),
                        "window_start_utc": str(row[6] or "").strip(),
                        "window_end_utc": str(row[7] or "").strip(),
                        "active": bool(row[8]) if row[8] is not None else True,
                        "resolution_note": str(row[9] or "").strip(),
                        "updated_utc": str(row[10] or "").strip(),
                    }
                )
            return out
        except Exception as e:
            log.debug("SOP: list_net_sop_conflict_policies failed: %s", e)
            return []
        finally:
            conn.close()

    @staticmethod
    def _parse_net_row_signature(signature: str) -> Dict[str, str]:
        parts = str(signature or "").split("|")
        if len(parts) < 14:
            return {}
        return {
            "group_name": str(parts[1] or "").strip().upper(),
            "band": str(parts[2] or "").strip().upper(),
            "frequency": str(parts[3] or "").strip(),
            "day_utc": str(parts[4] or "").strip().upper(),
            "recurrence": str(parts[5] or "").strip(),
            "start_utc": str(parts[8] or "").strip(),
            "end_utc": str(parts[9] or "").strip(),
            "name": str(parts[10] or "").strip(),
            "target_scope": str(parts[11] or "").strip().lower(),
            "target_device_profile_id": str(parts[12] or "").strip(),
            "target_operating_profile_id": str(parts[13] or "").strip(),
        }

    @staticmethod
    def _parse_sop_row_signature(signature: str) -> Dict[str, str]:
        parts = str(signature or "").split("|")
        if len(parts) < 12:
            return {}
        return {
            "sop_profile_id": str(parts[1] or "").strip(),
            "sop_layer_id": str(parts[2] or "").strip(),
            "group_name": str(parts[3] or "").strip().upper(),
            "band": str(parts[4] or "").strip().upper(),
            "frequency": str(parts[5] or "").strip(),
            "day_utc": str(parts[6] or "").strip().upper(),
            "recurrence": str(parts[7] or "").strip(),
            "start_utc": str(parts[10] or "").strip(),
            "end_utc": str(parts[11] or "").strip(),
        }

    def list_net_sop_policy_review_rows(self, *, horizon_days: int = 7) -> List[Dict[str, Any]]:
        policies = self.list_net_sop_conflict_policies(active_only=True)
        if not policies:
            return []
        current_conflicts = self.collect_active_net_sop_conflicts(horizon_days=horizon_days)
        current_by_key: Dict[str, Dict[str, Any]] = {}
        for row in current_conflicts:
            key = self._policy_conflict_key(
                str(row.get("net_row_signature") or ""),
                str(row.get("sop_row_signature") or ""),
                str(row.get("window_start_utc") or ""),
                str(row.get("window_end_utc") or ""),
            )
            if key and key not in current_by_key:
                current_by_key[key] = row

        out: List[Dict[str, Any]] = []
        for row in policies:
            net_sig = str(row.get("net_row_signature") or "")
            sop_sig = str(row.get("sop_row_signature") or "")
            start_utc = str(row.get("window_start_utc") or "")
            end_utc = str(row.get("window_end_utc") or "")
            key = self._policy_conflict_key(net_sig, sop_sig, start_utc, end_utc)
            current = current_by_key.get(key) or {}
            net_meta = self._parse_net_row_signature(net_sig)
            sop_meta = self._parse_sop_row_signature(sop_sig)
            net_summary = str(current.get("net_summary") or "").strip()
            if not net_summary:
                net_name = str(net_meta.get("name") or net_meta.get("group_name") or "Net").strip()
                net_summary = (
                    f"{net_name} "
                    f"{str(net_meta.get('band') or '').strip()} "
                    f"{str(net_meta.get('frequency') or '').strip()} "
                    f"{str(net_meta.get('day_utc') or '').strip()} "
                    f"{str(net_meta.get('start_utc') or '').strip()}-{str(net_meta.get('end_utc') or '').strip()}"
                ).strip()
            sop_summary = str(current.get("sop_summary") or "").strip()
            if not sop_summary:
                sop_group = str(sop_meta.get("group_name") or "SOP").strip()
                sop_summary = (
                    f"{sop_group} "
                    f"{str(sop_meta.get('band') or '').strip()} "
                    f"{str(sop_meta.get('frequency') or '').strip()} "
                    f"{str(sop_meta.get('day_utc') or '').strip()} "
                    f"{str(sop_meta.get('start_utc') or '').strip()}-{str(sop_meta.get('end_utc') or '').strip()}"
                ).strip()
            out.append(
                {
                    **row,
                    "state": "Current" if bool(current) else "Stale",
                    "net_summary": net_summary,
                    "sop_summary": sop_summary,
                }
            )
        out.sort(
            key=lambda r: (
                str(r.get("window_start_utc") or ""),
                str(r.get("sop_summary") or ""),
                str(r.get("net_summary") or ""),
            )
        )
        return out

    def save_net_sop_conflict_policies(self, decisions: List[Dict[str, Any]]) -> int:
        if not decisions:
            return 0
        conn = self._connect()
        saved = 0
        try:
            with conn:
                for row in decisions:
                    if not isinstance(row, dict):
                        continue
                    net_sig = str(row.get("net_row_signature") or "").strip()
                    sop_sig = str(row.get("sop_row_signature") or "").strip()
                    start_utc = str(row.get("window_start_utc") or "").strip()
                    end_utc = str(row.get("window_end_utc") or "").strip()
                    if not net_sig or not sop_sig or not start_utc or not end_utc:
                        continue
                    policy = self._normalize_net_sop_policy(row.get("policy"))
                    updated_utc = _utc_now_iso()
                    conn.execute(
                        """
                        INSERT INTO sop_net_conflict_policy (
                            sop_profile_id,
                            sop_layer_id,
                            net_row_signature,
                            sop_row_signature,
                            policy,
                            window_start_utc,
                            window_end_utc,
                            active,
                            resolution_note,
                            updated_utc
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                        ON CONFLICT(net_row_signature, sop_row_signature, window_start_utc, window_end_utc)
                        DO UPDATE SET
                            sop_profile_id = excluded.sop_profile_id,
                            sop_layer_id = excluded.sop_layer_id,
                            policy = excluded.policy,
                            active = 1,
                            resolution_note = excluded.resolution_note,
                            updated_utc = excluded.updated_utc
                        """,
                        (
                            int(row.get("sop_profile_id") or 0),
                            int(row.get("sop_layer_id") or 0),
                            net_sig,
                            sop_sig,
                            policy,
                            start_utc,
                            end_utc,
                            str(row.get("resolution_note") or "").strip(),
                            updated_utc,
                        ),
                    )
                    saved += 1
            if saved > 0:
                self._invalidate_active_hf_conflicts_cache()
            return saved
        except Exception as e:
            log.debug("SOP: save_net_sop_conflict_policies failed: %s", e)
            return saved
        finally:
            conn.close()

    def update_net_sop_conflict_policy(self, policy_id: int, policy: Any) -> bool:
        pid = int(policy_id or 0)
        if pid <= 0:
            return False
        norm_policy = self._normalize_net_sop_policy(policy)
        now_iso = _utc_now_iso()
        conn = self._connect()
        try:
            with conn:
                cur = conn.execute(
                    """
                    UPDATE sop_net_conflict_policy
                    SET policy = ?, updated_utc = ?, active = 1
                    WHERE id = ?
                    """,
                    (norm_policy, now_iso, pid),
                )
            try:
                updated = int(cur.rowcount or 0) > 0
            except Exception:
                updated = True
            if updated:
                self._invalidate_active_hf_conflicts_cache()
            return updated
        except Exception as e:
            log.debug("SOP: update_net_sop_conflict_policy failed: %s", e)
            return False
        finally:
            conn.close()

    def clear_net_sop_conflict_policies(self, policy_ids: Optional[List[int]] = None) -> int:
        ids = sorted({int(v) for v in (policy_ids or []) if int(v) > 0})
        conn = self._connect()
        try:
            with conn:
                if ids:
                    marks = ",".join("?" for _ in ids)
                    cur = conn.execute(
                        f"""
                        UPDATE sop_net_conflict_policy
                        SET active = 0, updated_utc = ?
                        WHERE id IN ({marks})
                        """,
                        (_utc_now_iso(), *ids),
                    )
                else:
                    cur = conn.execute(
                        """
                        UPDATE sop_net_conflict_policy
                        SET active = 0, updated_utc = ?
                        WHERE COALESCE(active, 1) = 1
                        """,
                        (_utc_now_iso(),),
                    )
            try:
                cleared = int(cur.rowcount or 0)
            except Exception:
                cleared = 0
            if cleared > 0:
                self._invalidate_active_hf_conflicts_cache()
            return cleared
        except Exception as e:
            log.debug("SOP: clear_net_sop_conflict_policies failed: %s", e)
            return 0
        finally:
            conn.close()

    def _load_active_hf_sop_layer_rows(
        self,
        *,
        include_profile_ids: Optional[Set[int]] = None,
    ) -> List[Dict[str, Any]]:
        include_ids = sorted({int(v) for v in (include_profile_ids or set()) if int(v) > 0})
        conn = self._connect()
        try:
            cols_layer = _table_columns(conn, "sop_schedule_layer")
            cols_profiles = _table_columns(conn, "sop_profiles")
            if not cols_layer or not cols_profiles:
                return []
            category_expr = "UPPER(COALESCE(p.category, 'HF'))"
            group_expr = (
                "COALESCE(NULLIF(TRIM(l.group_name), ''), COALESCE(p.operating_group, ''))"
                if "group_name" in cols_layer
                else "COALESCE(p.operating_group, '')"
            )
            sql = """
                SELECT
                    l.id,
                    l.profile_id,
                    COALESCE(p.name, ''),
                    {group_expr},
                    COALESCE(l.day_utc, 'ALL'),
                    COALESCE(l.recurrence, 'Weekly'),
                    COALESCE(l.biweekly_offset_weeks, 0),
                    COALESCE(l.month_weeks, ''),
                    COALESCE(l.band, ''),
                    COALESCE(l.frequency, ''),
                    COALESCE(l.start_utc, '00:00'),
                    COALESCE(l.end_utc, '23:59'),
                    COALESCE(l.enabled, 1),
                    COALESCE(l.condition_levels, 'ALL')
                FROM sop_schedule_layer l
                JOIN sop_profiles p ON p.id = l.profile_id
                WHERE COALESCE(l.enabled, 1) = 1
                  AND {category_expr} = 'HF'
            """.replace("{category_expr}", category_expr).replace("{group_expr}", group_expr)
            params: List[Any] = []
            if include_ids:
                marks = ",".join("?" for _ in include_ids)
                sql += f" AND (COALESCE(p.active, 0) = 1 OR p.id IN ({marks}))"
                params.extend(include_ids)
            else:
                sql += " AND COALESCE(p.active, 0) = 1"
            out: List[Dict[str, Any]] = []
            condition_levels = self._condition_level_map()
            for (
                layer_id,
                profile_id,
                profile_name,
                group_name,
                day_utc,
                recurrence,
                biweekly_offset_weeks,
                month_weeks,
                band,
                frequency,
                start_utc,
                end_utc,
                _enabled,
                cond_levels,
            ) in conn.execute(sql, tuple(params)).fetchall():
                group_txt = str(group_name or "").strip().upper()
                group_level = condition_levels.get(group_txt)
                if not self._action_condition_match(str(cond_levels or "ALL"), group_level):
                    continue
                out.append(
                    {
                        "id": int(layer_id or 0),
                        "sop_layer_id": int(layer_id or 0),
                        "sop_profile_id": int(profile_id or 0),
                        "sop_profile_name": str(profile_name or "").strip(),
                        "group_name": group_txt,
                        "day_utc": self._normalize_day_utc(day_utc),
                        "recurrence": self._normalize_recurrence(recurrence),
                        "biweekly_offset_weeks": int(biweekly_offset_weeks or 0),
                        "month_weeks": self._normalize_month_weeks(month_weeks),
                        "band": str(band or "").strip().upper(),
                        "frequency": self._normalize_frequency(frequency),
                        "start_utc": self._normalize_hhmm(start_utc or "00:00"),
                        "end_utc": self._normalize_hhmm(end_utc or "23:59"),
                        "source": "sop_layer",
                    }
                )
            return out
        except Exception as e:
            log.debug("SOP: _load_active_hf_sop_layer_rows failed: %s", e)
            return []
        finally:
            conn.close()

    def _normalize_net_rows_for_conflict_scan(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            start_utc = self._normalize_hhmm(row.get("start_utc") or "")
            end_utc = self._normalize_hhmm(row.get("end_utc") or "")
            if not start_utc or not end_utc:
                continue
            out.append(
                {
                    "day_utc": self._normalize_day_utc(row.get("day_utc") or "ALL"),
                    "start_utc": start_utc,
                    "end_utc": end_utc,
                    "recurrence": self._normalize_recurrence(row.get("recurrence") or "Weekly"),
                    "month_weeks": self._normalize_month_weeks(row.get("month_weeks") or ""),
                    "biweekly_offset_weeks": int(row.get("biweekly_offset_weeks") or 0),
                    "band": str(row.get("band") or "").strip().upper(),
                    "frequency": self._normalize_frequency(row.get("frequency")),
                    "name": str(row.get("name") or row.get("net_name") or "").strip(),
                    "group_name": str(row.get("group_name") or "").strip().upper(),
                    "source": "net",
                }
            )
        return out

    def collect_active_net_sop_conflicts(
        self,
        *,
        horizon_days: int = 7,
        include_profile_ids: Optional[Set[int]] = None,
        net_rows_override: Optional[List[Dict[str, Any]]] = None,
        include_same_frequency: bool = False,
        lookback_days: int = 0,
    ) -> List[Dict[str, Any]]:
        now_utc = dt.datetime.now(dt.timezone.utc)
        base_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        back_days = max(0, int(lookback_days))
        window_start = base_start - dt.timedelta(days=back_days)
        window_end = base_start + dt.timedelta(days=max(1, int(horizon_days)))
        if isinstance(net_rows_override, list):
            net_rows = self._normalize_net_rows_for_conflict_scan(net_rows_override)
        else:
            net_rows = self._load_all_schedule_rows(source="net")
        sop_rows = self._load_active_hf_sop_layer_rows(include_profile_ids=include_profile_ids)
        if not net_rows or not sop_rows:
            return []

        net_windows = self._expand_schedule_rows_windows(
            net_rows,
            window_start_utc=window_start,
            window_end_utc=window_end,
        )
        sop_windows = self._expand_schedule_rows_windows(
            sop_rows,
            window_start_utc=window_start,
            window_end_utc=window_end,
        )
        if not net_windows or not sop_windows:
            return []

        policy_map: Dict[str, Dict[str, Any]] = {}
        for row in self.list_net_sop_conflict_policies(active_only=True):
            key = self._policy_conflict_key(
                str(row.get("net_row_signature") or ""),
                str(row.get("sop_row_signature") or ""),
                str(row.get("window_start_utc") or ""),
                str(row.get("window_end_utc") or ""),
            )
            if key and key not in policy_map:
                policy_map[key] = row

        out: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        for net_row in net_windows:
            net_start = self._ensure_utc(net_row.get("start_dt_utc"))
            net_end = self._ensure_utc(net_row.get("end_dt_utc"))
            net_group = str(net_row.get("group_name") or "").strip().upper()
            net_band = str(net_row.get("band") or "").strip().upper()
            net_freq = self._normalize_frequency(net_row.get("frequency"))
            net_name = str(net_row.get("name") or net_row.get("net_name") or "").strip()
            for sop_row in sop_windows:
                sop_start = self._ensure_utc(sop_row.get("start_dt_utc"))
                sop_end = self._ensure_utc(sop_row.get("end_dt_utc"))
                if not self._ranges_overlap(net_start, net_end, sop_start, sop_end):
                    continue
                sop_group = str(sop_row.get("group_name") or "").strip().upper()
                if net_group == sop_group:
                    continue
                if not (net_group or sop_group):
                    continue
                sop_band = str(sop_row.get("band") or "").strip().upper()
                sop_freq = self._normalize_frequency(sop_row.get("frequency"))
                same_freq = bool(net_freq and sop_freq and net_freq == sop_freq)
                if same_freq and not include_same_frequency:
                    continue

                overlap_start = max(net_start, sop_start)
                overlap_end = min(net_end, sop_end)
                if overlap_end <= overlap_start:
                    continue
                net_sig = self._net_row_signature(net_row)
                sop_sig = self._sop_row_signature(sop_row)
                overlap_start_iso = overlap_start.replace(microsecond=0).isoformat()
                overlap_end_iso = overlap_end.replace(microsecond=0).isoformat()
                conflict_key = self._policy_conflict_key(net_sig, sop_sig, overlap_start_iso, overlap_end_iso)
                if conflict_key in seen:
                    continue
                seen.add(conflict_key)
                existing = policy_map.get(conflict_key) or {}
                policy = self._normalize_net_sop_policy(existing.get("policy"))
                profile_name = str(sop_row.get("sop_profile_name") or "").strip() or "HF SOP"
                net_label = net_name or (net_group or "Net")
                net_summary = (
                    f"{net_label} {net_band} {net_freq} "
                    f"{overlap_start.strftime('%a %H:%M')}-{overlap_end.strftime('%H:%M')}"
                ).strip()
                sop_summary = (
                    f"{profile_name} {sop_group} {sop_band} {sop_freq} "
                    f"{overlap_start.strftime('%a %H:%M')}-{overlap_end.strftime('%H:%M')}"
                ).strip()
                out.append(
                    {
                        "sop_profile_id": int(sop_row.get("sop_profile_id") or 0),
                        "sop_profile_name": profile_name,
                        "sop_layer_id": int(sop_row.get("sop_layer_id") or 0),
                        "sop_group_name": sop_group,
                        "sop_band": sop_band,
                        "sop_frequency": sop_freq,
                        "sop_day_utc": str(sop_row.get("day_utc") or "ALL"),
                        "sop_start_utc": self._normalize_hhmm(sop_row.get("start_utc") or "00:00"),
                        "sop_end_utc": self._normalize_hhmm(sop_row.get("end_utc") or "23:59"),
                        "net_group_name": net_group,
                        "net_name": net_name,
                        "net_band": net_band,
                        "net_frequency": net_freq,
                        "net_day_utc": str(net_row.get("day_utc") or "ALL"),
                        "net_start_utc": self._normalize_hhmm(net_row.get("start_utc") or "00:00"),
                        "net_end_utc": self._normalize_hhmm(net_row.get("end_utc") or "23:59"),
                        "window_start_utc": overlap_start_iso,
                        "window_end_utc": overlap_end_iso,
                        "net_row_signature": net_sig,
                        "sop_row_signature": sop_sig,
                        "net_summary": net_summary,
                        "sop_summary": sop_summary,
                        "conflict_summary": f"{sop_summary} conflicts with {net_summary}",
                        "resolved_policy": policy if existing else "",
                        "has_policy": bool(existing),
                    }
                )
        out.sort(
            key=lambda r: (
                str(r.get("window_start_utc") or ""),
                str(r.get("sop_profile_name") or ""),
                str(r.get("net_name") or ""),
                str(r.get("net_group_name") or ""),
            )
        )
        return out

    def _net_policy_from_action_conflict_policy(self, value: Any) -> str:
        policy = self._normalize_conflict_policy(value)
        if policy == self.CONFLICT_POLICY_SOP:
            return self.NET_SOP_POLICY_SOP
        if policy == self.CONFLICT_POLICY_NET:
            return self.NET_SOP_POLICY_NET
        return ""

    def _layer_policy_map_for_profile(self, profile: Dict[str, Any]) -> Dict[int, str]:
        """
        Resolve one Net/SOP policy per HF layer row from matching SOP action rows.
        When multiple actions collapse into one layer row, NET priority wins ties.
        """
        layer_rows = [r for r in (profile.get("schedule_layer") or []) if isinstance(r, dict)]
        actions = [a for a in (profile.get("actions") or []) if isinstance(a, dict)]
        if not layer_rows or not actions:
            return {}

        def _row_full_key(group_name: str, band: str, frequency: str, start_utc: str, end_utc: str, mode: str, cond: str) -> Tuple[str, str, str, str, str, str, str]:
            return (
                str(group_name or "").strip().upper(),
                str(band or "").strip().upper(),
                self._normalize_frequency(frequency),
                self._normalize_hhmm(start_utc or "00:00"),
                self._normalize_hhmm(end_utc or "23:59"),
                str(mode or "").strip().upper(),
                self._normalize_condition_levels(cond),
            )

        def _row_loose_key(group_name: str, band: str, frequency: str, start_utc: str, end_utc: str) -> Tuple[str, str, str, str, str]:
            return (
                str(group_name or "").strip().upper(),
                str(band or "").strip().upper(),
                self._normalize_frequency(frequency),
                self._normalize_hhmm(start_utc or "00:00"),
                self._normalize_hhmm(end_utc or "23:59"),
            )

        layer_by_full: Dict[Tuple[str, str, str, str, str, str, str], List[int]] = {}
        layer_by_loose: Dict[Tuple[str, str, str, str, str], List[int]] = {}
        for layer in layer_rows:
            if not _is_enabled(layer.get("enabled", True)):
                continue
            layer_id = int(layer.get("id") or layer.get("sop_layer_id") or 0)
            if layer_id <= 0:
                continue
            full_key = _row_full_key(
                layer.get("group_name"),
                layer.get("band"),
                layer.get("frequency"),
                layer.get("start_utc"),
                layer.get("end_utc"),
                layer.get("mode"),
                layer.get("condition_levels"),
            )
            loose_key = _row_loose_key(
                layer.get("group_name"),
                layer.get("band"),
                layer.get("frequency"),
                layer.get("start_utc"),
                layer.get("end_utc"),
            )
            layer_by_full.setdefault(full_key, []).append(layer_id)
            layer_by_loose.setdefault(loose_key, []).append(layer_id)

        layer_policy: Dict[int, str] = {}
        sorted_actions = sorted(
            actions,
            key=lambda a: int(a.get("sort_order") if a.get("sort_order") is not None else 0),
        )
        fallback_group = str(profile.get("operating_group") or "").strip().upper()
        for action in sorted_actions:
            if not _is_enabled(action.get("enabled", True)):
                continue
            if self._is_local_action(action):
                continue
            if not bool(action.get("schedule_applied", True)):
                continue
            net_policy = self._net_policy_from_action_conflict_policy(action.get("conflict_policy"))
            if net_policy not in {self.NET_SOP_POLICY_SOP, self.NET_SOP_POLICY_NET}:
                continue
            group_name = str(action.get("group_name") or fallback_group).strip().upper()
            mode = str(action.get("mode") or "").strip().upper() or "DIGI"
            condition_levels = self._normalize_condition_levels(action.get("condition_levels"))
            full_key = _row_full_key(
                group_name,
                action.get("band"),
                action.get("frequency"),
                action.get("daily_start_utc"),
                action.get("daily_end_utc"),
                mode,
                condition_levels,
            )
            loose_key = _row_loose_key(
                group_name,
                action.get("band"),
                action.get("frequency"),
                action.get("daily_start_utc"),
                action.get("daily_end_utc"),
            )
            layer_ids = list(layer_by_full.get(full_key) or layer_by_loose.get(loose_key) or [])
            for layer_id in layer_ids:
                existing = layer_policy.get(layer_id, "")
                if not existing:
                    layer_policy[layer_id] = net_policy
                    continue
                if existing == self.NET_SOP_POLICY_NET:
                    continue
                if net_policy == self.NET_SOP_POLICY_NET:
                    layer_policy[layer_id] = self.NET_SOP_POLICY_NET
        return layer_policy

    def sync_profile_net_sop_conflict_policies(
        self,
        profile_id: int,
        *,
        horizon_days: int = 35,
    ) -> Dict[str, int]:
        """
        Synchronize Net/SOP arbitration rows from SOP action conflict policies.
        This keeps runtime/FreqPlanner precedence aligned with SOP Builder decisions.
        """
        pid = int(profile_id or 0)
        if pid <= 0:
            return {"saved": 0, "cleared": 0, "desired": 0}
        profile = self.get_profile(pid) or {}
        if self._normalize_category(profile.get("category")) != self.CATEGORY_HF:
            return {"saved": 0, "cleared": 0, "desired": 0}

        layer_policy = self._layer_policy_map_for_profile(profile)
        if not layer_policy:
            active_rows = [
                r
                for r in self.list_net_sop_conflict_policies(active_only=True)
                if int(r.get("sop_profile_id") or 0) == pid
            ]
            stale_ids = [int(r.get("id") or 0) for r in active_rows if int(r.get("id") or 0) > 0]
            cleared = int(self.clear_net_sop_conflict_policies(stale_ids) or 0) if stale_ids else 0
            return {"saved": 0, "cleared": cleared, "desired": 0}

        conflicts = self.collect_active_net_sop_conflicts(
            horizon_days=max(1, int(horizon_days)),
            include_profile_ids={pid},
            include_same_frequency=True,
            lookback_days=7,
        )
        decisions: List[Dict[str, Any]] = []
        desired_keys: Set[str] = set()
        for row in conflicts:
            if int(row.get("sop_profile_id") or 0) != pid:
                continue
            layer_id = int(row.get("sop_layer_id") or 0)
            policy = layer_policy.get(layer_id, "")
            if policy not in {self.NET_SOP_POLICY_SOP, self.NET_SOP_POLICY_NET}:
                continue
            net_sig = str(row.get("net_row_signature") or "").strip()
            sop_sig = str(row.get("sop_row_signature") or "").strip()
            start_utc = str(row.get("window_start_utc") or "").strip()
            end_utc = str(row.get("window_end_utc") or "").strip()
            if not net_sig or not sop_sig or not start_utc or not end_utc:
                continue
            conflict_key = self._policy_conflict_key(net_sig, sop_sig, start_utc, end_utc)
            desired_keys.add(conflict_key)
            decisions.append(
                {
                    "sop_profile_id": pid,
                    "sop_layer_id": layer_id,
                    "net_row_signature": net_sig,
                    "sop_row_signature": sop_sig,
                    "window_start_utc": start_utc,
                    "window_end_utc": end_utc,
                    "policy": policy,
                    "resolution_note": "SOP action conflict policy sync",
                }
            )

        active_rows = [
            r
            for r in self.list_net_sop_conflict_policies(active_only=True)
            if int(r.get("sop_profile_id") or 0) == pid
        ]
        stale_ids: List[int] = []
        for row in active_rows:
            key = self._policy_conflict_key(
                str(row.get("net_row_signature") or ""),
                str(row.get("sop_row_signature") or ""),
                str(row.get("window_start_utc") or ""),
                str(row.get("window_end_utc") or ""),
            )
            if key not in desired_keys:
                row_id = int(row.get("id") or 0)
                if row_id > 0:
                    stale_ids.append(row_id)
        cleared = int(self.clear_net_sop_conflict_policies(stale_ids) or 0) if stale_ids else 0
        saved = int(self.save_net_sop_conflict_policies(decisions) or 0) if decisions else 0
        return {"saved": saved, "cleared": cleared, "desired": len(desired_keys)}

    def load_secondary_groups(self) -> List[str]:
        conn = self._connect()
        values: set[str] = set()
        try:
            cur = conn.execute("SELECT group1, group2, group3 FROM operator_checkins")
            for g1, g2, g3 in cur.fetchall():
                for raw in (g1, g2, g3):
                    val = (raw or "").strip().upper()
                    if val:
                        values.add(val)
        except Exception as e:
            log.debug("SOP: load_secondary_groups failed: %s", e)
        finally:
            conn.close()
        return sorted(values)

    def resolve_primary_contacts(self, operating_group: str, secondary_group: str = "") -> Dict[str, List[str]]:
        group = (operating_group or "").strip().upper()
        subgroup = (secondary_group or "").strip().upper()
        hub_primary: List[str] = []
        hub_alt: List[str] = []
        ncs_primary: List[str] = []
        ncs_alt: List[str] = []
        peer: List[str] = []
        if not group:
            return {"hub": [], "ncs": [], "peer": []}

        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT callsign, group1, group2, group3, group_role FROM operator_checkins"
            )
            for callsign, g1, g2, g3, role in cur.fetchall():
                groups = [((g or "").strip().upper()) for g in (g1, g2, g3)]
                primary_match = group in groups
                secondary_match = (not subgroup) or (subgroup in groups)
                if not (primary_match and secondary_match):
                    continue
                cs = (callsign or "").strip().upper()
                r = (role or "").strip().upper()
                if not cs:
                    continue
                if r == "HUB":
                    if cs not in hub_primary:
                        hub_primary.append(cs)
                elif r == "HUB-ALT":
                    if cs not in hub_alt:
                        hub_alt.append(cs)
                elif r == "NCS":
                    if cs not in ncs_primary:
                        ncs_primary.append(cs)
                elif r == "ANCS":
                    if cs not in ncs_alt:
                        ncs_alt.append(cs)
                elif r == "PEER":
                    if cs not in peer:
                        peer.append(cs)
        except Exception as e:
            log.debug("SOP: resolve_primary_contacts failed: %s", e)
        finally:
            conn.close()
        hub = sorted(hub_primary) + sorted(hub_alt)
        ncs = sorted(ncs_primary) + sorted(ncs_alt)
        return {"hub": hub, "ncs": ncs, "peer": sorted(peer)}

    def resolve_group_callsigns(self, operating_group: str, secondary_group: str = "") -> List[str]:
        group = (operating_group or "").strip().upper()
        subgroup = (secondary_group or "").strip().upper()
        if not group:
            return []
        calls: List[str] = []
        conn = self._connect()
        try:
            cur = conn.execute("SELECT callsign, group1, group2, group3 FROM operator_checkins")
            for callsign, g1, g2, g3 in cur.fetchall():
                groups = [((g or "").strip().upper()) for g in (g1, g2, g3)]
                primary_match = group in groups
                secondary_match = (not subgroup) or (subgroup in groups)
                if not (primary_match and secondary_match):
                    continue
                cs = (callsign or "").strip().upper()
                if cs and cs not in calls:
                    calls.append(cs)
        except Exception as e:
            log.debug("SOP: resolve_group_callsigns failed: %s", e)
        finally:
            conn.close()
        return sorted(calls)

    def _load_schedule_windows(self, operating_group: str, band: str, frequency: str) -> List[Dict[str, Any]]:
        group = (operating_group or "").strip().upper()
        band_uc = (band or "").strip().upper()
        freq = self._normalize_frequency(frequency)
        if not group:
            return []

        windows: List[Dict[str, Any]] = []
        try:
            for source in ("daily", "net"):
                rows = self._load_group_schedule_rows(operating_group=group, source=source)
                for row in rows:
                    row_band = str(row.get("band") or "").strip().upper()
                    row_freq = self._normalize_frequency(row.get("frequency"))
                    if band_uc and row_band and row_band != band_uc:
                        continue
                    if freq and row_freq and row_freq != freq:
                        continue
                    day = self._normalize_day_utc(row.get("day_utc") or "ALL")
                    start = self._normalize_hhmm(row.get("start_utc") or "00:00")
                    end = self._normalize_hhmm(row.get("end_utc") or "23:59")
                    if not day or not start or not end:
                        continue
                    windows.append(
                        {
                            "day_utc": day,
                            "start_utc": start,
                            "end_utc": end,
                            "recurrence": self._normalize_recurrence(row.get("recurrence") or "Weekly"),
                            "month_weeks": self._normalize_month_weeks(row.get("month_weeks") or ""),
                            "biweekly_offset_weeks": int(row.get("biweekly_offset_weeks") or 0),
                        }
                    )
        except Exception as e:
            log.debug("SOP: load schedule windows failed: %s", e)
        return windows

    def _load_sop_layer_windows(
        self,
        *,
        profile_id: Optional[int],
        band: str,
        frequency: str,
    ) -> List[Dict[str, Any]]:
        band_uc = (band or "").strip().upper()
        freq = self._normalize_frequency(frequency)
        out: List[Dict[str, Any]] = []
        conn = self._connect()
        try:
            cols_layer = _table_columns(conn, "sop_schedule_layer")
            group_expr = (
                "COALESCE(NULLIF(TRIM(l.group_name), ''), COALESCE(p.operating_group, ''))"
                if "group_name" in cols_layer
                else "COALESCE(p.operating_group, '')"
            )
            sql = """
                SELECT
                    l.day_utc,
                    l.start_utc,
                    l.end_utc,
                    COALESCE(l.recurrence, 'Weekly') AS recurrence,
                    COALESCE(l.month_weeks, '') AS month_weeks,
                    COALESCE(l.biweekly_offset_weeks, 0) AS biweekly_offset_weeks,
                    COALESCE(l.condition_levels, 'ALL') AS condition_levels,
                    COALESCE(l.band, '') AS band,
                    COALESCE(l.frequency, '') AS frequency,
                    {group_expr} AS operating_group
                FROM sop_schedule_layer l
                JOIN sop_profiles p ON p.id = l.profile_id
                WHERE l.enabled = 1
                  AND p.active = 1
            """.replace("{group_expr}", group_expr)
            params: List[Any] = []
            if profile_id and int(profile_id) > 0:
                sql += " AND l.profile_id = ?"
                params.append(int(profile_id))
            condition_levels = self._condition_level_map()
            for row in conn.execute(sql, tuple(params)).fetchall():
                row_band = str(row[7] or "").strip().upper()
                row_freq = self._normalize_frequency(row[8])
                if band_uc and row_band and row_band != band_uc:
                    continue
                if freq and row_freq and row_freq != freq:
                    continue
                cond_levels = self._normalize_condition_levels(row[6])
                group_name = str(row[9] or "").strip().upper()
                group_level = condition_levels.get(group_name)
                if not self._action_condition_match(cond_levels, group_level):
                    continue
                day = str(row[0] or "").strip()
                start = str(row[1] or "").strip()
                end = str(row[2] or "").strip()
                if not day or not start or not end:
                    continue
                out.append(
                    {
                        "day_utc": day,
                        "start_utc": start,
                        "end_utc": end,
                        "recurrence": self._normalize_recurrence(row[3]),
                        "month_weeks": self._normalize_month_weeks(row[4]),
                        "biweekly_offset_weeks": int(row[5] or 0),
                        "condition_levels": cond_levels,
                    }
                )
            return out
        except Exception as e:
            log.debug("SOP: load sop layer windows failed: %s", e)
            return []
        finally:
            conn.close()

    @staticmethod
    def _month_week_index(date_val: dt.date) -> int:
        return 1 + ((date_val.day - 1) // 7)

    def _window_matches_due(self, window: Dict[str, Any], due_utc: dt.datetime) -> bool:
        day_raw = self._normalize_day_utc(window.get("day_utc"))
        recurrence = self._normalize_recurrence(window.get("recurrence"))
        start_h, start_m = _parse_hhmm(str(window.get("start_utc") or ""), default_hhmm="00:00")
        end_h, end_m = _parse_hhmm(str(window.get("end_utc") or ""), default_hhmm="00:00")
        smin = start_h * 60 + start_m
        emin = end_h * 60 + end_m
        due_min = due_utc.hour * 60 + due_utc.minute
        day_name = _day_name_from_utc(due_utc).upper()
        prev_day = _day_name_from_utc(due_utc - dt.timedelta(days=1)).upper()
        day = day_raw.upper()
        overnight = smin > emin

        if recurrence == "Daily":
            day = "ALL"
        if day == "ALL":
            day = day_name

        def _periodic_match(use_prev_day: bool) -> bool:
            if recurrence != "Periodic":
                return True
            weeks_txt = self._normalize_month_weeks(window.get("month_weeks"))
            weeks = [int(tok) for tok in weeks_txt.split(",") if tok.strip().isdigit()]
            if not weeks:
                weeks = [1]
            ref_date = due_utc.date() - dt.timedelta(days=1) if use_prev_day else due_utc.date()
            return self._month_week_index(ref_date) in weeks

        if day == day_name:
            if not _periodic_match(False):
                return False
            if not overnight:
                return smin <= due_min < emin
            return due_min >= smin or due_min < emin
        if overnight and day == prev_day:
            if not _periodic_match(True):
                return False
            return due_min < emin
        return False

    def is_due_aligned_with_schedule(
        self,
        operating_group: str,
        band: str,
        frequency: str,
        due_utc: dt.datetime,
        *,
        profile_id: Optional[int] = None,
    ) -> bool:
        windows = self._load_schedule_windows(operating_group, band, frequency)
        windows.extend(
            self._load_sop_layer_windows(
                profile_id=profile_id,
                band=band,
                frequency=frequency,
            )
        )
        if not windows:
            return True
        for window in windows:
            if self._window_matches_due(window, due_utc):
                return True
        return False

    def diagnose_due_alignment(
        self,
        operating_group: str,
        band: str,
        frequency: str,
        due_utc: dt.datetime,
        *,
        profile_id: Optional[int] = None,
        treat_no_windows_as_aligned: bool = False,
    ) -> Dict[str, Any]:
        """
        Return alignment diagnostics for a due time against Daily/Net/SOP windows.

        reason:
          - in_window
          - outside_window
          - no_windows
        """
        windows = self._load_schedule_windows(operating_group, band, frequency)
        windows.extend(
            self._load_sop_layer_windows(
                profile_id=profile_id,
                band=band,
                frequency=frequency,
            )
        )
        total_windows = len(windows)
        if total_windows <= 0:
            return {
                "aligned": bool(treat_no_windows_as_aligned),
                "reason": "no_windows",
                "has_windows": False,
                "matching_windows": 0,
                "total_windows": 0,
            }
        matches = 0
        for window in windows:
            if self._window_matches_due(window, due_utc):
                matches += 1
        return {
            "aligned": matches > 0,
            "reason": "in_window" if matches > 0 else "outside_window",
            "has_windows": True,
            "matching_windows": matches,
            "total_windows": total_windows,
        }

    def build_schedule_layer_candidates(
        self,
        *,
        operating_group: str,
        action_rows: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        group = (operating_group or "").strip().upper()
        if not group:
            return {"rows": [], "unmatched": ["Operating Group is required."], "matched_actions": 0}
        targets: List[Tuple[str, str, str]] = []
        target_labels: Dict[Tuple[str, str], str] = {}
        unmatched: List[str] = []
        for idx, row in enumerate(action_rows):
            if not isinstance(row, dict):
                continue
            software = str(row.get("software") or "").strip()
            action_label = str(row.get("action_label") or "").strip()
            if software == "Local Net":
                continue
            band = str(row.get("band") or "").strip().upper()
            freq_raw = str(row.get("frequency") or "").strip()
            freq = self._normalize_frequency(freq_raw)
            label = f"Row {idx + 1}"
            if action_label:
                label += f" ({action_label})"
            if not band or not freq:
                unmatched.append(f"{label}: missing band/frequency.")
                continue
            key = (band, freq)
            if key not in target_labels:
                target_labels[key] = label
            targets.append((band, freq, label))
        if not targets:
            return {"rows": [], "unmatched": unmatched or ["No eligible non-local action rows found."], "matched_actions": 0}

        conn = self._connect()
        try:
            cols_daily = _table_columns(conn, "daily_schedule_tab")
            cols_net = _table_columns(conn, "net_schedule_tab")
            rows: List[Dict[str, Any]] = []
            matched_targets: Set[Tuple[str, str]] = set()

            def _fetch_daily_rows(band: str, freq: str) -> None:
                if not cols_daily:
                    return
                group_col = "group_name" if "group_name" in cols_daily else ("group" if "group" in cols_daily else "")
                if not group_col:
                    return
                if not {"day_utc", "start_utc", "end_utc", "frequency", "band"}.issubset(cols_daily):
                    return
                mode_expr = "COALESCE(mode, '')" if "mode" in cols_daily else "''"
                vfo_expr = "COALESCE(vfo, 'A')" if "vfo" in cols_daily else "'A'"
                sql = (
                    f"""
                    SELECT
                        day_utc,
                        start_utc,
                        end_utc,
                        COALESCE(band, ''),
                        COALESCE(frequency, ''),
                        {mode_expr},
                        {vfo_expr}
                    FROM daily_schedule_tab
                    WHERE UPPER(COALESCE({group_col}, '')) = ?
                      AND UPPER(COALESCE(band, '')) = ?
                    """
                )
                params = (group, band)
                for d, s, e, b, f, m, v in conn.execute(sql, params).fetchall():
                    if normalize_frequency_hz(f) != normalize_frequency_hz(freq):
                        continue
                    rows.append(
                        {
                            "day_utc": "ALL",
                            "recurrence": "Daily",
                            "biweekly_offset_weeks": 0,
                            "month_weeks": "",
                            "band": str(b or "").strip().upper(),
                            "mode": str(m or "").strip().upper(),
                            "vfo": "",
                            "frequency": self._normalize_frequency(f),
                            "start_utc": self._normalize_hhmm(s),
                            "end_utc": self._normalize_hhmm(e),
                            "enabled": True,
                        }
                    )

            def _fetch_net_rows(band: str, freq: str) -> None:
                if not cols_net:
                    return
                group_col = "group_name" if "group_name" in cols_net else ("group" if "group" in cols_net else "")
                if not group_col:
                    return
                if not {"day_utc", "start_utc", "end_utc", "frequency", "band"}.issubset(cols_net):
                    return
                rec_expr = "COALESCE(recurrence, 'Weekly')" if "recurrence" in cols_net else "'Weekly'"
                biweek_expr = "COALESCE(biweekly_offset_weeks, 0)" if "biweekly_offset_weeks" in cols_net else "0"
                weeks_expr = "COALESCE(month_weeks, '')" if "month_weeks" in cols_net else "''"
                mode_expr = "COALESCE(mode, '')" if "mode" in cols_net else "''"
                vfo_expr = "COALESCE(vfo, 'A')" if "vfo" in cols_net else "'A'"
                sql = (
                    f"""
                    SELECT
                        day_utc,
                        start_utc,
                        end_utc,
                        {rec_expr},
                        {biweek_expr},
                        {weeks_expr},
                        COALESCE(band, ''),
                        COALESCE(frequency, ''),
                        {mode_expr},
                        {vfo_expr}
                    FROM net_schedule_tab
                    WHERE UPPER(COALESCE({group_col}, '')) = ?
                      AND UPPER(COALESCE(band, '')) = ?
                    """
                )
                params = (group, band)
                for d, s, e, rec, biw, weeks, b, f, m, v in conn.execute(sql, params).fetchall():
                    if normalize_frequency_hz(f) != normalize_frequency_hz(freq):
                        continue
                    rows.append(
                        {
                            "day_utc": "ALL",
                            "recurrence": "Daily",
                            "biweekly_offset_weeks": 0,
                            "month_weeks": "",
                            "band": str(b or "").strip().upper(),
                            "mode": str(m or "").strip().upper(),
                            "vfo": "",
                            "frequency": self._normalize_frequency(f),
                            "start_utc": self._normalize_hhmm(s),
                            "end_utc": self._normalize_hhmm(e),
                            "enabled": True,
                        }
                    )

            for band, freq, _label in targets:
                before = len(rows)
                _fetch_daily_rows(band, freq)
                _fetch_net_rows(band, freq)
                if len(rows) > before:
                    matched_targets.add((band, freq))

            dedup: Dict[Tuple[str, str, int, str, str, str, str, str, str, str], Dict[str, Any]] = {}
            for row in rows:
                key = (
                    str(row.get("day_utc") or "ALL"),
                    str(row.get("recurrence") or "Weekly"),
                    int(row.get("biweekly_offset_weeks") or 0),
                    str(row.get("month_weeks") or ""),
                    str(row.get("band") or "").upper(),
                    str(row.get("mode") or "").upper(),
                    str(row.get("vfo") or "A").upper(),
                    str(row.get("frequency") or ""),
                    str(row.get("start_utc") or ""),
                    str(row.get("end_utc") or ""),
                )
                dedup[key] = row
            out_rows = list(dedup.values())
            out_rows.sort(
                key=lambda x: (
                    str(x.get("day_utc") or "ALL"),
                    str(x.get("start_utc") or ""),
                    str(x.get("frequency") or ""),
                    str(x.get("recurrence") or ""),
                )
            )
            for idx, row in enumerate(out_rows):
                row["sort_order"] = idx
            for band, freq, _label in targets:
                if (band, freq) not in matched_targets:
                    unmatched.append(f"{target_labels.get((band, freq), 'Action')}: no matching HF/Net schedule windows found.")
            return {
                "rows": out_rows,
                "unmatched": unmatched,
                "matched_actions": len(matched_targets),
            }
        except Exception as e:
            log.debug("SOP: build schedule layer candidates failed: %s", e)
            return {"rows": [], "unmatched": [f"Failed to build candidates: {e}"], "matched_actions": 0}
        finally:
            conn.close()

    def build_upcoming_actions(
        self,
        horizon_hours: int = 12,
        only_active: bool = True,
        now_utc: Optional[dt.datetime] = None,
    ) -> List[Dict[str, Any]]:
        now = now_utc or dt.datetime.now(dt.timezone.utc)
        now = self._ensure_utc(now)
        horizon = now + dt.timedelta(hours=max(1, int(horizon_hours)))
        overdue_warn_end = dt.timedelta(minutes=30)
        condition_levels = self._condition_level_map()
        rows: List[Dict[str, Any]] = []
        for profile in self.list_profiles():
            if only_active and not profile.get("active"):
                continue
            full = self.get_profile(int(profile["id"]))
            if not full:
                continue
            profile_group = str(full.get("operating_group") or "").strip().upper()
            contacts = self.resolve_primary_contacts(
                full.get("operating_group", ""),
                full.get("secondary_group", ""),
            )
            for action in full.get("actions", []):
                if not isinstance(action, dict):
                    continue
                if not _is_enabled(action.get("enabled", True)):
                    continue
                action_group = str(action.get("group_name") or "").strip().upper() or profile_group
                group_level = condition_levels.get(action_group)
                if not self._action_condition_match(str(action.get("condition_levels") or "ALL"), group_level):
                    continue

                interval_m = int(action.get("interval_minutes") or 0)
                if interval_m <= 0:
                    interval_m = max(1, int(action.get("interval_hours") or 3)) * 60
                interval_phase_m = max(0, int(action.get("interval_phase_minutes") or 0)) % max(1, interval_m)
                action_for_occurrence = dict(action)
                if not str(action_for_occurrence.get("daily_start_utc") or "").strip():
                    action_for_occurrence["daily_start_utc"] = str(full.get("sop_start_utc") or "00:00")
                occurrence_rows = self.build_action_occurrences_in_window(
                    action_for_occurrence,
                    window_start_utc=now - overdue_warn_end,
                    window_end_utc=horizon + dt.timedelta(minutes=1),
                )
                if not occurrence_rows:
                    continue

                due: Optional[dt.datetime] = None
                status = "Upcoming"
                for occ_start, _occ_end in occurrence_rows:
                    if occ_start <= now <= (occ_start + overdue_warn_end):
                        due = occ_start
                        if now <= (occ_start + dt.timedelta(minutes=20)):
                            status = "Due Now"
                        else:
                            status = "Overdue"
                        break
                    if occ_start >= now:
                        due = occ_start
                        status = "Upcoming"
                        break
                if due is None:
                    continue

                interval_td = dt.timedelta(minutes=interval_m)
                current_due = due
                last_completed = _parse_iso_utc(action.get("last_completed_utc"))
                completed_current = bool(last_completed and current_due <= last_completed < (current_due + interval_td))
                if completed_current and status in {"Due Now", "Overdue"}:
                    status = "Completed"

                action_band = (action.get("band") or "").strip().upper()
                action_freq = self._normalize_frequency((action.get("frequency") or "").strip() or full.get("frequency", ""))
                rule = (action.get("contact_rule") or "none").strip()
                aligned = True
                alignment_reason = "not_evaluated"
                has_schedule_windows = False
                if rule not in {"local_profile", "local_group"}:
                    diag = self.diagnose_due_alignment(
                        action_group or full.get("operating_group", ""),
                        action_band,
                        action_freq,
                        due,
                        profile_id=int(full.get("id") or 0),
                        treat_no_windows_as_aligned=True,
                    )
                    aligned = bool(diag.get("aligned", True))
                    alignment_reason = str(diag.get("reason") or "not_evaluated").strip()
                    has_schedule_windows = bool(diag.get("has_windows"))
                selected_target = (action.get("contact_target") or "").strip().upper()
                targets: List[str] = []
                if rule == "hub_or_hub_alt":
                    targets = [selected_target] if selected_target and selected_target != "__ANY_ROLE__" else ["Any (Role Match)"]
                elif rule == "ncs_or_ancs":
                    targets = [selected_target] if selected_target and selected_target != "__ANY_ROLE__" else ["Any (Role Match)"]
                elif rule in {"callsign", "peer", "local_profile", "local_group"}:
                    targets = [selected_target] if selected_target else []
                elif rule == "group":
                    targets = [selected_target] if selected_target else ["Any (Group Match)"]

                rows.append(
                    {
                        "profile_id": int(full["id"]),
                        "profile_name": full.get("name", ""),
                        "category": self._normalize_category(full.get("category")),
                        "operating_group": action_group or full.get("operating_group", ""),
                        "band": action_band,
                        "frequency": action_freq,
                        "action_id": int(action.get("id") or 0),
                        "software": action.get("software", ""),
                        "mode": str(action.get("mode") or "").strip().upper(),
                        "action_key": action.get("action_key", ""),
                        "action_label": action.get("action_label", ""),
                        "description": action.get("description", ""),
                        "contact_rule": rule,
                        "contact_target": (action.get("contact_target") or "").strip().upper(),
                        "contact_targets": targets,
                        "condition_levels": self._normalize_condition_levels(action.get("condition_levels")),
                        "interval_minutes": interval_m,
                        "interval_phase_minutes": interval_phase_m,
                        "daily_start_utc": self._normalize_hhmm(action_for_occurrence.get("daily_start_utc") or "00:00"),
                        "daily_end_utc": self._normalize_hhmm(action_for_occurrence.get("daily_end_utc") or "23:59"),
                        "duration_minutes": int(action.get("duration_minutes") or 60),
                        "conflict_policy": self._normalize_conflict_policy(action.get("conflict_policy")),
                        "next_due_utc": due,
                        "aligned": aligned,
                        "alignment_reason": alignment_reason,
                        "has_schedule_windows": has_schedule_windows,
                        "status": status,
                        "is_completed": completed_current and status == "Completed",
                        **{key: value for key, value in normalize_schedule_target_fields(
                            action if action.get("target_scope") else full
                        ).items() if key in {"target_scope", "target_device_profile_id", "target_operating_profile_id"}},
                    }
                )
        rows.sort(key=lambda x: x["next_due_utc"])
        return rows

    def list_export_regions(self) -> List[str]:
        return sorted({str(v).strip().upper() for v in STATE_TO_FEMA_REGION.values() if str(v).strip()})

    def list_hf_groups_for_export(self) -> List[str]:
        conn = self._connect()
        out: Set[str] = set()
        try:
            cols = _table_columns(conn, "operator_checkins")
            if not cols or "callsign" not in cols:
                return []
            group1_expr = "IFNULL(group1, '')" if "group1" in cols else "''"
            group2_expr = "IFNULL(group2, '')" if "group2" in cols else "''"
            group3_expr = "IFNULL(group3, '')" if "group3" in cols else "''"
            groups_json_expr = "IFNULL(groups_json, '')" if "groups_json" in cols else "''"
            cur = conn.execute(
                f"""
                SELECT {group1_expr}, {group2_expr}, {group3_expr}, {groups_json_expr}
                FROM operator_checkins
                """
            )
            for g1, g2, g3, groups_json in cur.fetchall():
                out.update(_collect_group_tokens(g1, g2, g3, groups_json))
            return sorted(out)
        except Exception as e:
            log.debug("SOP: list_hf_groups_for_export failed: %s", e)
            return []
        finally:
            conn.close()

    def list_local_categories_for_export(self) -> List[str]:
        out: Set[str] = set()
        try:
            for row in get_all_operators():
                category = str(row.get("category") or "").strip()
                if category:
                    out.add(category)
            return sorted(out, key=lambda x: x.upper())
        except Exception as e:
            log.debug("SOP: list_local_categories_for_export failed: %s", e)
            return []

    def _contact_display_for_export(
        self,
        *,
        rule: str,
        target: str,
        contacts: Dict[str, List[str]],
    ) -> str:
        rule_val = (rule or "none").strip().lower()
        tgt = (target or "").strip().upper()
        any_role = "__ANY_ROLE__"
        if rule_val == "hub_or_hub_alt":
            if tgt and tgt != any_role:
                return tgt
            vals = contacts.get("hub", []) or []
            return " OR ".join(vals[:4]) if vals else "Any (Role Match)"
        if rule_val == "ncs_or_ancs":
            if tgt and tgt != any_role:
                return tgt
            vals = contacts.get("ncs", []) or []
            return " OR ".join(vals[:4]) if vals else "Any (Role Match)"
        if rule_val in {"callsign", "peer", "local_profile", "local_group"}:
            return tgt or "--"
        if rule_val == "group":
            return tgt or "Any (Group Match)"
        return "--"

    def build_profile_export_rows(
        self,
        profile_id: int,
        *,
        now_utc: Optional[dt.datetime] = None,
    ) -> List[Dict[str, Any]]:
        full = self.get_profile(profile_id)
        if not full:
            return []
        now = now_utc or dt.datetime.now(dt.timezone.utc)
        contacts = self.resolve_primary_contacts(
            full.get("operating_group", ""),
            full.get("secondary_group", ""),
        )
        rows: List[Dict[str, Any]] = []
        for action in full.get("actions", []):
            if not bool(action.get("enabled", True)):
                continue
            interval_m = int(action.get("interval_minutes") or 0)
            if interval_m <= 0:
                interval_m = max(1, int(action.get("interval_hours") or 3)) * 60
            interval_phase_m = max(0, int(action.get("interval_phase_minutes") or 0)) % max(1, interval_m)
            action_for_occurrence = dict(action)
            if not str(action_for_occurrence.get("daily_start_utc") or "").strip():
                action_for_occurrence["daily_start_utc"] = str(full.get("sop_start_utc") or "00:00")
            if not str(action_for_occurrence.get("daily_end_utc") or "").strip():
                action_for_occurrence["daily_end_utc"] = "23:59"
            occurrences = self.build_action_occurrences_in_window(
                action_for_occurrence,
                window_start_utc=now - dt.timedelta(minutes=30),
                window_end_utc=now + dt.timedelta(hours=24),
            )
            if not occurrences:
                continue
            next_due = occurrences[0][0]
            for occ_start, _occ_end in occurrences:
                if occ_start >= now:
                    next_due = occ_start
                    break
            interval_td = dt.timedelta(minutes=interval_m)
            current_due = next_due
            status = "Upcoming"
            if current_due <= now < (current_due + dt.timedelta(minutes=30)):
                late = now - current_due
                if late <= dt.timedelta(minutes=20):
                    status = "Due Now"
                else:
                    status = "Overdue"
            last_completed = _parse_iso_utc(action.get("last_completed_utc"))
            if last_completed and current_due <= last_completed < (current_due + interval_td):
                status = "Completed"
            rows.append(
                {
                    "profile_id": int(full.get("id") or 0),
                    "profile_name": str(full.get("name") or ""),
                    "operating_group": str(full.get("operating_group") or ""),
                    "secondary_group": str(full.get("secondary_group") or ""),
                    "sop_start_utc": str(action_for_occurrence.get("daily_start_utc") or "00:00"),
                    "action_id": int(action.get("id") or 0),
                    "software": str(action.get("software") or ""),
                    "action_label": str(action.get("action_label") or ""),
                    "band": str(action.get("band") or "").strip().upper(),
                    "frequency": str(action.get("frequency") or "").strip() or str(full.get("frequency") or ""),
                    "description": str(action.get("description") or ""),
                    "interval_minutes": interval_m,
                    "interval_phase_minutes": interval_phase_m,
                    "status": status,
                    "next_due_utc": next_due,
                    "contact_display": self._contact_display_for_export(
                        rule=str(action.get("contact_rule") or "none"),
                        target=str(action.get("contact_target") or ""),
                        contacts=contacts,
                    ),
                }
            )
        rows.sort(key=lambda x: x["next_due_utc"])
        return rows

    def build_profile_daily_plan_rows(
        self,
        profile_id: int,
        *,
        day_start_utc: dt.datetime,
        day_end_utc: dt.datetime,
    ) -> List[Dict[str, Any]]:
        full = self.get_profile(profile_id)
        if not full:
            return []
        start = day_start_utc if isinstance(day_start_utc, dt.datetime) else dt.datetime.now(dt.timezone.utc)
        end = day_end_utc if isinstance(day_end_utc, dt.datetime) else start
        if start.tzinfo is None:
            start = start.replace(tzinfo=dt.timezone.utc)
        else:
            start = start.astimezone(dt.timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=dt.timezone.utc)
        else:
            end = end.astimezone(dt.timezone.utc)
        if end < start:
            return []

        contacts = self.resolve_primary_contacts(
            full.get("operating_group", ""),
            full.get("secondary_group", ""),
        )
        profile_group = str(full.get("operating_group") or "").strip().upper()
        condition_levels = self._condition_level_map()
        rows: List[Dict[str, Any]] = []
        total_window_minutes = max(1, int((end - start).total_seconds() // 60))
        for action in full.get("actions", []):
            if not _is_enabled(action.get("enabled", True)):
                continue
            action_group = str(action.get("group_name") or "").strip().upper() or profile_group
            group_level = condition_levels.get(action_group)
            if not self._action_condition_match(str(action.get("condition_levels") or "ALL"), group_level):
                continue
            interval_m = int(action.get("interval_minutes") or 0)
            if interval_m <= 0:
                interval_m = max(1, int(action.get("interval_hours") or 3)) * 60
            interval_m = max(1, interval_m)
            phase_m = max(0, int(action.get("interval_phase_minutes") or 0)) % max(1, interval_m)
            action_for_occurrence = dict(action)
            action_for_occurrence["interval_minutes"] = interval_m
            action_for_occurrence["interval_phase_minutes"] = phase_m
            if not str(action_for_occurrence.get("daily_start_utc") or "").strip():
                action_for_occurrence["daily_start_utc"] = str(full.get("sop_start_utc") or "00:00")
            if not str(action_for_occurrence.get("daily_end_utc") or "").strip():
                action_for_occurrence["daily_end_utc"] = "23:59"
            occurrences = self.build_action_occurrences_in_window(
                action_for_occurrence,
                window_start_utc=start,
                window_end_utc=end + dt.timedelta(seconds=1),
            )
            max_iters = min(240, int(math.ceil(total_window_minutes / max(1, interval_m))) + 3)
            iters = 0
            for due, _due_end in occurrences:
                if due > end:
                    continue
                if due < start:
                    continue
                if iters >= max_iters:
                    break
                band = str(action.get("band") or "").strip().upper()
                freq = str(action.get("frequency") or "").strip() or str(full.get("frequency") or "")
                rows.append(
                    {
                        "profile_id": int(full.get("id") or 0),
                        "profile_name": str(full.get("name") or ""),
                        "due_utc": due,
                        "resource": str(action.get("software") or ""),
                        "action_label": str(action.get("action_label") or ""),
                        "band": band,
                        "frequency": freq,
                        "band_freq": f"{band} {freq}".strip() if (band or freq) else "--",
                        "contact_display": self._contact_display_for_export(
                            rule=str(action.get("contact_rule") or "none"),
                            target=str(action.get("contact_target") or ""),
                            contacts=contacts,
                        ),
                        "description": str(action.get("description") or ""),
                    }
                )
                iters += 1
        rows.sort(
            key=lambda x: (
                x.get("due_utc") if isinstance(x.get("due_utc"), dt.datetime) else dt.datetime.max.replace(tzinfo=dt.timezone.utc),
                str(x.get("resource") or ""),
                str(x.get("action_label") or ""),
                str(x.get("band_freq") or ""),
            )
        )
        return rows

    def build_profile_periodic_action_rows(self, profile_id: int) -> List[Dict[str, Any]]:
        full = self.get_profile(profile_id)
        if not full:
            return []
        contacts = self.resolve_primary_contacts(
            full.get("operating_group", ""),
            full.get("secondary_group", ""),
        )
        profile_group = str(full.get("operating_group") or "").strip().upper()
        condition_levels = self._condition_level_map()
        periodic_layers = [
            row
            for row in (full.get("schedule_layer") or [])
            if bool(row.get("enabled", True))
            and str(row.get("recurrence") or "").strip().lower() == "periodic"
            and str(row.get("month_weeks") or "").strip()
        ]
        if not periodic_layers:
            return []

        out: List[Dict[str, Any]] = []
        seen: Set[Tuple[str, str, str, str, str, str, str]] = set()
        for layer in periodic_layers:
            weeks = self._normalize_month_weeks(layer.get("month_weeks"))
            if not weeks:
                continue
            layer_group = str(layer.get("group_name") or "").strip().upper() or profile_group
            layer_group_level = condition_levels.get(layer_group)
            if not self._action_condition_match(str(layer.get("condition_levels") or "ALL"), layer_group_level):
                continue
            day = self._normalize_day_utc(layer.get("day_utc"))
            layer_band = str(layer.get("band") or "").strip().upper()
            layer_freq = str(layer.get("frequency") or "").strip()
            for action in full.get("actions", []):
                if not _is_enabled(action.get("enabled", True)):
                    continue
                action_group = str(action.get("group_name") or "").strip().upper() or profile_group
                action_group_level = condition_levels.get(action_group)
                if not self._action_condition_match(str(action.get("condition_levels") or "ALL"), action_group_level):
                    continue
                band = str(action.get("band") or "").strip().upper() or layer_band
                freq = str(action.get("frequency") or "").strip() or layer_freq or str(full.get("frequency") or "")
                row = {
                    "profile_id": int(full.get("id") or 0),
                    "profile_name": str(full.get("name") or ""),
                    "weeks_of_month": weeks,
                    "day_of_week": day,
                    "resource": str(action.get("software") or ""),
                    "action_label": str(action.get("action_label") or ""),
                    "band_freq": f"{band} {freq}".strip() if (band or freq) else "--",
                    "contact_display": self._contact_display_for_export(
                        rule=str(action.get("contact_rule") or "none"),
                        target=str(action.get("contact_target") or ""),
                        contacts=contacts,
                    ),
                    "description": str(action.get("description") or ""),
                }
                key = (
                    row["weeks_of_month"],
                    row["day_of_week"],
                    row["resource"],
                    row["action_label"],
                    row["band_freq"],
                    row["contact_display"],
                    row["description"],
                )
                if key in seen:
                    continue
                seen.add(key)
                out.append(row)
        out.sort(
            key=lambda x: (
                str(x.get("weeks_of_month") or ""),
                str(x.get("day_of_week") or ""),
                str(x.get("resource") or ""),
                str(x.get("action_label") or ""),
            )
        )
        return out

    def list_hf_operators_for_export(
        self,
        *,
        state_filter: str = "",
        region_filter: str = "",
        group_filters: Optional[List[str]] = None,
        trusted_filters: Optional[List[str]] = None,
    ) -> List[Dict[str, str]]:
        conn = self._connect()
        out: List[Dict[str, str]] = []
        try:
            cols = _table_columns(conn, "operator_checkins")
            if not cols or "callsign" not in cols:
                return []
            name_expr = "IFNULL(name, '')" if "name" in cols else "''"
            state_expr = "IFNULL(state, '')" if "state" in cols else "''"
            notes_expr = "IFNULL(notes, '')" if "notes" in cols else "''"
            group1_expr = "IFNULL(group1, '')" if "group1" in cols else "''"
            group2_expr = "IFNULL(group2, '')" if "group2" in cols else "''"
            group3_expr = "IFNULL(group3, '')" if "group3" in cols else "''"
            groups_json_expr = "IFNULL(groups_json, '')" if "groups_json" in cols else "''"
            trusted_expr = "trusted" if "trusted" in cols else "0"
            selected_groups = {_norm_group(v) for v in (group_filters or []) if _norm_group(v)}
            selected_trusted = {_norm_trusted_filter(v) for v in (trusted_filters or []) if _norm_trusted_filter(v)}
            sitrep_map: Dict[str, str] = {}
            try:
                cur = conn.execute("SELECT callsign, effective_status FROM sitrep_latest_by_callsign")
                for callsign, status in cur.fetchall():
                    cs = str(callsign or "").strip().upper()
                    if cs:
                        sitrep_map[cs] = _norm_sitrep(status)
            except Exception:
                if "sitrep_status" in cols:
                    try:
                        cur = conn.execute("SELECT callsign, sitrep_status FROM operator_checkins")
                        for callsign, status in cur.fetchall():
                            cs = str(callsign or "").strip().upper()
                            if cs:
                                sitrep_map[cs] = _norm_sitrep(status)
                    except Exception:
                        pass
            cur = conn.execute(
                f"""
                SELECT
                    IFNULL(callsign, ''),
                    {name_expr},
                    {state_expr},
                    {notes_expr},
                    {group1_expr},
                    {group2_expr},
                    {group3_expr},
                    {groups_json_expr},
                    {trusted_expr}
                FROM operator_checkins
                ORDER BY callsign COLLATE NOCASE
                """
            )
            for callsign, name, state, notes, g1, g2, g3, groups_json, trusted in cur.fetchall():
                cs = str(callsign or "").strip().upper()
                st = _norm_state(state)
                if not cs:
                    continue
                if not _passes_geo_filter(st, state_filter=state_filter, region_filter=region_filter):
                    continue
                row_groups = _collect_group_tokens(g1, g2, g3, groups_json)
                if selected_groups and not (row_groups & selected_groups):
                    continue
                row_trusted = _trusted_label(trusted)
                if selected_trusted and row_trusted not in selected_trusted:
                    continue
                out.append(
                    {
                        "callsign": cs,
                        "name": str(name or "").strip(),
                        "state": st,
                        "sitrep": sitrep_map.get(cs, "UNKNOWN"),
                        "notes": str(notes or "").strip(),
                    }
                )
            return out
        except Exception as e:
            log.debug("SOP: list_hf_operators_for_export failed: %s", e)
            return []
        finally:
            conn.close()

    def list_local_operators_for_export(
        self,
        *,
        state_filter: str = "",
        region_filter: str = "",
        category_filters: Optional[List[str]] = None,
    ) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        selected_categories = {_norm_group(v) for v in (category_filters or []) if _norm_group(v)}
        try:
            for row in get_all_operators():
                cs = str(row.get("callsign") or "").strip().upper()
                st = _norm_state(row.get("state"))
                category = str(row.get("category") or "").strip()
                if not cs:
                    continue
                if not _passes_geo_filter(st, state_filter=state_filter, region_filter=region_filter):
                    continue
                if selected_categories and _norm_group(category) not in selected_categories:
                    continue
                out.append(
                    {
                        "callsign": cs,
                        "first_name": str(row.get("first_name") or "").strip(),
                        "last_name": str(row.get("last_name") or "").strip(),
                        "city": str(row.get("city") or "").strip(),
                        "state": st,
                        "category": category,
                        "sitrep": _norm_sitrep(row.get("sitrep_status")),
                        "notes": str(row.get("notes") or "").strip(),
                    }
                )
            return out
        except Exception as e:
            log.debug("SOP: list_local_operators_for_export failed: %s", e)
            return []

    def export_profile_json(self, profile_id: int) -> Dict[str, Any]:
        profile = self.get_profile(profile_id)
        if not profile:
            raise ValueError("Profile not found")
        return {
            "schema_version": 2,
            "exported_utc": _utc_now_iso(),
            "profile": profile,
        }

    def import_profile_json(self, payload: Dict[str, Any]) -> int:
        profile = payload.get("profile")
        if not isinstance(profile, dict):
            raise ValueError("Invalid SOP import: missing profile object")

        imported = dict(profile)
        imported.pop("id", None)
        imported["category"] = self._normalize_category(imported.get("category"))
        actions = imported.pop("actions", [])
        schedule_layer = imported.pop("schedule_layer", [])
        name = (imported.get("name") or "Imported SOP").strip()

        existing_names = {p.get("name", "").lower() for p in self.list_profiles()}
        base = name
        suffix = 1
        while name.lower() in existing_names:
            suffix += 1
            name = f"{base} ({suffix})"
        imported["name"] = name
        try:
            imported["priority"] = int(imported.get("priority") or 100)
        except Exception:
            imported["priority"] = 100

        normalized_actions: List[Dict[str, Any]] = []
        if isinstance(actions, list):
            for idx, a in enumerate(actions):
                if not isinstance(a, dict):
                    continue
                normalized_actions.append(
                    {
                        "group_name": (a.get("group_name") or "").strip().upper(),
                        "condition_levels": self._normalize_condition_levels(a.get("condition_levels")),
                        "band": (a.get("band") or "").strip().upper(),
                        "frequency": (a.get("frequency") or "").strip(),
                        "operating_group_frequency_key": str(a.get("operating_group_frequency_key") or "").strip(),
                        "software": (a.get("software") or "").strip(),
                        "mode": str(a.get("mode") or "").strip().upper(),
                        "action_key": (a.get("action_key") or "").strip(),
                        "action_label": (a.get("action_label") or "").strip(),
                        "enabled": bool(a.get("enabled", True)),
                        "daily_start_utc": self._normalize_hhmm(a.get("daily_start_utc") or "00:00"),
                        "daily_end_utc": self._normalize_hhmm(a.get("daily_end_utc") or "23:59"),
                        "duration_minutes": int(a.get("duration_minutes") or 60),
                        "interval_hours": int(a.get("interval_hours") or 3),
                        "interval_minutes": int(a.get("interval_minutes") or (int(a.get("interval_hours") or 3) * 60)),
                        "interval_phase_minutes": int(a.get("interval_phase_minutes") or 0),
                        "conflict_policy": self._normalize_conflict_policy(a.get("conflict_policy")),
                        "daily_conflict_summary": str(a.get("daily_conflict_summary") or "").strip(),
                        "net_conflict_summary": str(a.get("net_conflict_summary") or "").strip(),
                        "schedule_applied": bool(a.get("schedule_applied", True)),
                        "description": (a.get("description") or "").strip(),
                        "contact_rule": (a.get("contact_rule") or "none").strip(),
                        "contact_target": (a.get("contact_target") or "").strip().upper(),
                        "sort_order": int(a.get("sort_order") if a.get("sort_order") is not None else idx),
                    }
                )
        normalized_layers: List[Dict[str, Any]] = []
        if isinstance(schedule_layer, list):
            for idx, row in enumerate(schedule_layer):
                if not isinstance(row, dict):
                    continue
                normalized_layers.append(
                    {
                        "day_utc": self._normalize_day_utc(row.get("day_utc")),
                        "recurrence": self._normalize_recurrence(row.get("recurrence")),
                        "biweekly_offset_weeks": int(row.get("biweekly_offset_weeks") or 0),
                        "month_weeks": self._normalize_month_weeks(row.get("month_weeks")),
                        "condition_levels": self._normalize_condition_levels(row.get("condition_levels")),
                        "group_name": str(row.get("group_name") or "").strip().upper(),
                        "band": str(row.get("band") or "").strip().upper(),
                        "mode": str(row.get("mode") or "").strip().upper(),
                        "vfo": str(row.get("vfo") or "").strip().upper(),
                        "frequency": self._normalize_frequency(row.get("frequency")),
                        "operating_group_frequency_key": str(row.get("operating_group_frequency_key") or "").strip(),
                        "start_utc": self._normalize_hhmm(row.get("start_utc")),
                        "end_utc": self._normalize_hhmm(row.get("end_utc")),
                        "enabled": bool(row.get("enabled", True)),
                        "sort_order": int(row.get("sort_order") if row.get("sort_order") is not None else idx),
                    }
                )
        return self.save_profile(imported, normalized_actions, normalized_layers)

    @staticmethod
    def dumps_json(payload: Dict[str, Any]) -> str:
        def _default(o: Any) -> Any:
            if isinstance(o, dt.datetime):
                return o.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()
            raise TypeError(f"Unsupported type: {type(o)!r}")

        return json.dumps(payload, indent=2, sort_keys=True, default=_default)
