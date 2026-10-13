from __future__ import annotations

import datetime as dt
import json
import math
import sqlite3
from freqinout.core.sqlite_utils import connect_sqlite_readonly
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


class PropagationService:
    """
    Shared propagation scoring helper used by GUI surfaces.

    The service intentionally supports compatibility modes so callers can
    preserve existing behavior while sharing one implementation.
    """

    def __init__(
        self,
        *,
        default_profiles: Mapping[str, Mapping[str, float]],
        profiles_path: Optional[Path] = None,
        climatology_db_path: Optional[Path] = None,
        outcome_db_path: Optional[Path] = None,
        db_index_mode: str = "floor5",
        climatology_month: Optional[int] = None,
    ) -> None:
        self._default_profiles: Dict[str, Dict[str, float]] = {
            str(k).strip().upper(): dict(v) for k, v in dict(default_profiles).items()
        }
        self._profiles_path = Path(profiles_path) if profiles_path else None
        self._climatology_db_path = Path(climatology_db_path) if climatology_db_path else None
        self._outcome_db_path = Path(outcome_db_path) if outcome_db_path else None
        self._db_index_mode = str(db_index_mode).strip().lower()
        self._climatology_month = climatology_month

        self._profiles_cache: Optional[Dict[str, Dict[str, float]]] = None
        self._db_cache: Dict[Tuple[int, str, int, int], float] = {}
        self._db_loaded = False
        self._db_available = False
        self._outcome_table_checked = False
        self._outcome_table_available = False
        self._empirical_cache: Dict[Tuple[Any, ...], Tuple[float, Dict[str, Any]]] = {}
        self._empirical_cache_ttl_sec = 60.0
        self._empirical_cache_max_entries = 4000
        self._empirical_cache_prune_interval_sec = 15.0
        self._empirical_cache_last_prune_mono = 0.0

    def _prune_empirical_cache(self, now_mono: float) -> None:
        # Skip frequent pruning in hot paths.
        if (now_mono - float(self._empirical_cache_last_prune_mono or 0.0)) < self._empirical_cache_prune_interval_sec:
            return
        self._empirical_cache_last_prune_mono = float(now_mono)

        if not self._empirical_cache:
            return

        ttl = max(1.0, float(self._empirical_cache_ttl_sec))
        stale_keys: List[Tuple[Any, ...]] = []
        for key, item in self._empirical_cache.items():
            ts = float(item[0]) if isinstance(item, tuple) and item else 0.0
            if (now_mono - ts) > ttl:
                stale_keys.append(key)
        for key in stale_keys:
            self._empirical_cache.pop(key, None)

        if len(self._empirical_cache) <= self._empirical_cache_max_entries:
            return
        # Trim oldest survivors if still over cap.
        overflow = len(self._empirical_cache) - int(self._empirical_cache_max_entries)
        try:
            oldest = sorted(self._empirical_cache.items(), key=lambda kv: float(kv[1][0]))[:overflow]
            for key, _ in oldest:
                self._empirical_cache.pop(key, None)
        except Exception:
            # Safe fallback if ordering fails for any reason.
            while len(self._empirical_cache) > self._empirical_cache_max_entries:
                try:
                    self._empirical_cache.pop(next(iter(self._empirical_cache)))
                except Exception:
                    break

    def load_profiles(self) -> Dict[str, Dict[str, float]]:
        if self._profiles_cache is not None:
            return self._profiles_cache
        profiles = {k: dict(v) for k, v in self._default_profiles.items()}
        path = self._profiles_path
        if path and path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    for key, value in raw.items():
                        band = str(key).strip().upper()
                        if band in profiles and isinstance(value, dict):
                            profiles[band].update(value)
            except Exception:
                # Keep defaults on malformed profile files.
                pass
        self._profiles_cache = profiles
        return profiles

    def load_climatology_cache(self) -> None:
        if self._db_loaded:
            return
        self._db_loaded = True
        path = self._climatology_db_path
        if not path or not path.exists():
            self._db_available = False
            return
        try:
            conn = connect_sqlite_readonly(path)
            cur = conn.cursor()
            sql = "SELECT month, band, lat_idx, lon_idx, muf_score FROM muf_grid"
            if self._climatology_month is None:
                cur.execute(sql)
            else:
                # A map snapshot needs only the current month, never the full archive.
                cur.execute(sql + " WHERE month=? LIMIT 20000", (self._climatology_month,))
            for month, band, lat_idx, lon_idx, score in cur.fetchall():
                key = (int(month), str(band).upper(), int(lat_idx), int(lon_idx))
                self._db_cache[key] = float(score)
            conn.close()
            self._db_available = bool(self._db_cache)
        except Exception:
            self._db_available = False

    def lookup_db_score(self, band: str, lat: float, lon: float, month: int) -> Optional[float]:
        self.load_climatology_cache()
        if not self._db_available:
            return None
        idx = self._coords_to_indices(lat, lon)
        if idx is None:
            return None
        lat_idx, lon_idx = idx
        key = (int(month), str(band).strip().upper(), int(lat_idx), int(lon_idx))
        return self._db_cache.get(key)

    def band_score_db(self, band: str, lat: float, lon: float, month: int) -> Optional[float]:
        score = self.lookup_db_score(band, lat, lon, month)
        if score is None:
            return None
        if score <= 1.0:
            return max(0.0, min(100.0, score * 100.0))
        return max(0.0, min(100.0, score))

    def band_score(self, band: str, distance_km: float, hour_utc: int) -> float:
        profiles = self.load_profiles()
        prof = profiles.get(str(band).strip().upper(), {})
        ideal = float(prof.get("ideal_km", 2000))
        spread = float(prof.get("spread_km", 2000))
        day_factor = float(prof.get("day", 0.8))
        night_factor = float(prof.get("night", 0.8))
        is_day = 6 <= int(hour_utc) < 18
        factor = day_factor if is_day else night_factor
        if spread <= 0:
            spread = 1.0
        dist_pen = max(0.0, 1.0 - abs(float(distance_km) - ideal) / spread)
        score = 100.0 * factor * dist_pen
        return max(0.0, min(100.0, score))

    def diurnal_weight(self, band: str, hour_local: int) -> float:
        profiles = self.load_profiles()
        prof = profiles.get(str(band).strip().upper(), {})
        day_factor = float(prof.get("day", 0.8))
        night_factor = float(prof.get("night", 0.8))
        is_day = 6 <= int(hour_local) < 18
        return day_factor if is_day else night_factor

    @staticmethod
    def local_hour_from_lon(utc_dt: dt.datetime, lon: float) -> int:
        try:
            offset = float(lon) / 15.0
        except Exception:
            offset = 0.0
        hour = (int(utc_dt.hour) + offset) % 24
        return int(hour)

    @staticmethod
    def path_band_weight(band: str, distance_km: float, hour_local: int) -> float:
        band_key = (band or "").strip().upper()
        is_day = 6 <= int(hour_local) < 18
        if distance_km < 300:
            if is_day:
                weights = {"80M": 1.0, "40M": 1.2, "30M": 0.8, "20M": 0.4, "15M": 0.2, "10M": 0.1}
            else:
                weights = {"80M": 1.3, "40M": 1.1, "30M": 0.6, "20M": 0.3, "15M": 0.15, "10M": 0.1}
        elif distance_km < 900:
            if is_day:
                weights = {"80M": 0.6, "40M": 1.0, "30M": 1.0, "20M": 0.8, "15M": 0.5, "10M": 0.3}
            else:
                weights = {"80M": 0.9, "40M": 1.1, "30M": 0.9, "20M": 0.5, "15M": 0.2, "10M": 0.1}
        else:
            if is_day:
                weights = {"80M": 0.2, "40M": 0.6, "30M": 0.9, "20M": 1.2, "15M": 1.0, "10M": 0.7}
            else:
                weights = {"80M": 0.4, "40M": 1.2, "30M": 1.0, "20M": 0.7, "15M": 0.3, "10M": 0.2}
        return float(weights.get(band_key, 0.5))

    @staticmethod
    def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        r = 6371.0
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlon / 2.0) ** 2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return r * c

    def modeled_band_score(
        self,
        *,
        band: str,
        user_ll: Tuple[float, float],
        dest_lat: float,
        dest_lon: float,
        now_utc: dt.datetime,
        distance_km: float,
    ) -> float:
        mid_lat = (user_ll[0] + dest_lat) / 2.0
        mid_lon = (user_ll[1] + dest_lon) / 2.0
        hour_local = self.local_hour_from_lon(now_utc, mid_lon)
        base = self.band_score_db(band, mid_lat, mid_lon, int(now_utc.month))
        if base is None:
            base = self.band_score(band, float(distance_km), int(now_utc.hour))
        diurnal = self.diurnal_weight(band, hour_local)
        path_weight = self.path_band_weight(band, float(distance_km), hour_local)
        score = float(base) * float(diurnal) * float(path_weight)
        return max(0.0, min(100.0, score))

    @staticmethod
    def _clamp(value: float, lo: float, hi: float) -> float:
        return max(float(lo), min(float(hi), float(value)))

    @staticmethod
    def _safe_float(value: object, default: float) -> float:
        try:
            return float(value)
        except Exception:
            return float(default)

    @staticmethod
    def _safe_int(value: object, default: int) -> int:
        try:
            return int(float(value))
        except Exception:
            return int(default)

    def _normalize_blend_settings(self, blend_settings: Optional[Mapping[str, object]]) -> Dict[str, float]:
        raw = dict(blend_settings or {})
        enabled_raw = raw.get("prop_blend_enabled", raw.get("blend_enabled", 1))
        enabled = 1.0 if str(enabled_raw).strip().lower() not in {"0", "false", "off", "no"} else 0.0
        alpha = self._clamp(self._safe_float(raw.get("prop_empirical_alpha", 2.0), 2.0), 0.1, 20.0)
        beta = self._clamp(self._safe_float(raw.get("prop_empirical_beta", 3.0), 3.0), 0.1, 20.0)
        half_life = self._clamp(
            self._safe_float(raw.get("prop_decay_half_life_days", 75.0), 75.0),
            5.0,
            365.0,
        )
        gate_attempt = self._clamp(
            self._safe_float(raw.get("prop_blend_gate_attempt_min", 8.0), 8.0),
            0.5,
            200.0,
        )
        gate_days = self._safe_int(raw.get("prop_blend_gate_unique_days_min", 3), 3)
        gate_days = max(1, min(60, gate_days))
        max_blend = self._clamp(
            self._safe_float(raw.get("prop_blend_max_weight", 0.85), 0.85),
            0.05,
            0.95,
        )
        recent_window = self._safe_int(raw.get("prop_blend_recent_window_days", 30), 30)
        recent_window = max(1, min(180, recent_window))
        history_cap = self._safe_int(raw.get("prop_blend_history_cap_days", 365), 365)
        history_cap = max(7, min(730, history_cap))
        return {
            "enabled": enabled,
            "alpha": alpha,
            "beta": beta,
            "half_life_days": half_life,
            "gate_attempt_min": gate_attempt,
            "gate_unique_days_min": float(gate_days),
            "max_blend_weight": max_blend,
            "recent_window_days": float(recent_window),
            "history_cap_days": float(history_cap),
        }

    def _outcome_table_exists(self) -> bool:
        if self._outcome_table_checked:
            return self._outcome_table_available
        self._outcome_table_checked = True
        path = self._outcome_db_path
        if not path or not path.exists():
            self._outcome_table_available = False
            return False
        try:
            conn = connect_sqlite_readonly(path)
            cur = conn.cursor()
            cur.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='prop_contact_events'"
            )
            self._outcome_table_available = cur.fetchone() is not None
            conn.close()
        except Exception:
            self._outcome_table_available = False
        return self._outcome_table_available

    @staticmethod
    def _parse_ts_utc(ts_utc: str) -> Optional[dt.datetime]:
        txt = (ts_utc or "").strip()
        if not txt:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                parsed = dt.datetime.strptime(txt[:19], fmt)
                return parsed.replace(tzinfo=dt.timezone.utc)
            except Exception:
                continue
        return None

    def _weighted_history(
        self,
        *,
        now_utc: dt.datetime,
        origin_grid6: str,
        target_type: str,
        target_id: Optional[str],
        band: str,
        half_life_days: float,
        recent_window_days: float,
        history_cap_days: float,
        row_limit: int = 1000,
    ) -> Dict[str, float]:
        if not self._outcome_table_exists():
            return {
                "weighted_attempt": 0.0,
                "weighted_success": 0.0,
                "weighted_attempt_recent": 0.0,
                "unique_days_recent": 0.0,
                "recency_factor": 0.0,
            }
        path = self._outcome_db_path
        if not path:
            return {
                "weighted_attempt": 0.0,
                "weighted_success": 0.0,
                "weighted_attempt_recent": 0.0,
                "unique_days_recent": 0.0,
                "recency_factor": 0.0,
            }
        origin_grid6 = (origin_grid6 or "").strip().upper()
        target_type = (target_type or "").strip().upper()
        band = (band or "").strip().upper()
        if not (origin_grid6 and target_type and band):
            return {
                "weighted_attempt": 0.0,
                "weighted_success": 0.0,
                "weighted_attempt_recent": 0.0,
                "unique_days_recent": 0.0,
                "recency_factor": 0.0,
            }
        params: List[object]
        sql = (
            "SELECT ts_utc, outcome FROM prop_contact_events "
            "WHERE origin_grid6=? AND target_type=? AND band=? AND ts_utc>=? "
        )
        cutoff = now_utc - dt.timedelta(days=max(1.0, float(history_cap_days)))
        params = [
            origin_grid6,
            target_type,
            band,
            cutoff.astimezone(dt.timezone.utc).strftime("%Y-%m-%d"),
        ]
        if target_id is not None:
            sql += "AND target_id=? "
            params.append((target_id or "").strip().upper())
        sql += "ORDER BY ts_utc DESC LIMIT ?"
        params.append(int(max(100, row_limit)))
        try:
            conn = connect_sqlite_readonly(path)
            cur = conn.cursor()
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
            conn.close()
        except Exception:
            return {
                "weighted_attempt": 0.0,
                "weighted_success": 0.0,
                "weighted_attempt_recent": 0.0,
                "unique_days_recent": 0.0,
                "recency_factor": 0.0,
            }

        weighted_attempt = 0.0
        weighted_success = 0.0
        weighted_attempt_recent = 0.0
        unique_days_recent: set[str] = set()
        min_age = float("inf")
        half_life = max(1.0, float(half_life_days))
        for ts_utc, outcome in rows:
            ts = self._parse_ts_utc(str(ts_utc or ""))
            if not ts:
                continue
            age_days = (now_utc - ts).total_seconds() / 86400.0
            if age_days < 0.0:
                continue
            if age_days > float(history_cap_days):
                continue
            min_age = min(min_age, age_days)
            weight = math.pow(0.5, age_days / half_life)
            weighted_attempt += weight
            ok = 0.0 if str(outcome or "").strip().upper() == "FAILED" else 1.0
            weighted_success += weight * ok
            if age_days <= float(recent_window_days):
                weighted_attempt_recent += weight
                unique_days_recent.add(ts.strftime("%Y-%m-%d"))
        recency_factor = 0.0 if not math.isfinite(min_age) else math.pow(0.5, min_age / half_life)
        return {
            "weighted_attempt": weighted_attempt,
            "weighted_success": weighted_success,
            "weighted_attempt_recent": weighted_attempt_recent,
            "unique_days_recent": float(len(unique_days_recent)),
            "recency_factor": recency_factor,
        }

    def blend_modeled_score(
        self,
        *,
        modeled_score: float,
        now_utc: dt.datetime,
        origin_grid6: str,
        target_type: str,
        target_id: str,
        band: str,
        blend_settings: Optional[Mapping[str, object]] = None,
    ) -> Tuple[float, Dict[str, Any]]:
        cfg = self._normalize_blend_settings(blend_settings)
        modeled = self._clamp(float(modeled_score), 0.0, 100.0)
        origin = (origin_grid6 or "").strip().upper()
        ttype = (target_type or "").strip().upper()
        tid = (target_id or "").strip().upper()
        bnd = (band or "").strip().upper()
        if cfg["enabled"] <= 0.0:
            return modeled, {
                "blend_weight": 0.0,
                "empirical_rate": 0.0,
                "confidence": "LOW",
                "reason_code": "BLEND_DISABLED",
                "pooled": False,
            }
        if not (origin and ttype and tid and bnd):
            return modeled, {
                "blend_weight": 0.0,
                "empirical_rate": 0.0,
                "confidence": "LOW",
                "reason_code": "NO_TARGET",
                "pooled": False,
            }

        # Historical evidence changes on ingest, not meaningfully between the
        # morning/day/night model points rendered in one refresh. A UTC-day
        # cache key avoids repeating the same bounded SQLite history scan for
        # each display window.
        day_bucket = int(now_utc.timestamp() // 86_400)
        sig = (
            round(cfg["alpha"], 3),
            round(cfg["beta"], 3),
            round(cfg["half_life_days"], 3),
            round(cfg["gate_attempt_min"], 3),
            int(cfg["gate_unique_days_min"]),
            round(cfg["max_blend_weight"], 3),
            int(cfg["recent_window_days"]),
            int(cfg["history_cap_days"]),
        )
        cache_key = ("blend", day_bucket, origin, ttype, tid, bnd, sig)
        now_mono = time.monotonic()
        self._prune_empirical_cache(now_mono)
        cached = self._empirical_cache.get(cache_key)
        if cached and (now_mono - cached[0]) < self._empirical_cache_ttl_sec:
            info = dict(cached[1])
            return float(info.get("final_score", modeled)), info

        primary = self._weighted_history(
            now_utc=now_utc,
            origin_grid6=origin,
            target_type=ttype,
            target_id=tid,
            band=bnd,
            half_life_days=cfg["half_life_days"],
            recent_window_days=cfg["recent_window_days"],
            history_cap_days=cfg["history_cap_days"],
        )
        gate_attempt = cfg["gate_attempt_min"]
        gate_days = int(cfg["gate_unique_days_min"])
        p_gate = (
            primary["weighted_attempt_recent"] >= gate_attempt
            and int(primary["unique_days_recent"]) >= gate_days
        )
        chosen = dict(primary)
        pooled = False
        max_blend = cfg["max_blend_weight"]
        if not p_gate:
            pooled_hist = self._weighted_history(
                now_utc=now_utc,
                origin_grid6=origin,
                target_type=ttype,
                target_id=None,
                band=bnd,
                half_life_days=cfg["half_life_days"],
                recent_window_days=cfg["recent_window_days"],
                history_cap_days=cfg["history_cap_days"],
            )
            q_gate = (
                pooled_hist["weighted_attempt_recent"] >= gate_attempt
                and int(pooled_hist["unique_days_recent"]) >= gate_days
            )
            if q_gate:
                chosen = pooled_hist
                pooled = True
                max_blend *= 0.8

        weighted_attempt = chosen["weighted_attempt"]
        weighted_success = chosen["weighted_success"]
        weighted_recent = chosen["weighted_attempt_recent"]
        unique_days_recent = int(chosen["unique_days_recent"])
        recency_factor = chosen["recency_factor"]

        empirical_rate = (
            (weighted_success + cfg["alpha"])
            / (weighted_attempt + cfg["alpha"] + cfg["beta"])
            if (weighted_attempt + cfg["alpha"] + cfg["beta"]) > 0.0
            else modeled / 100.0
        )
        gate = weighted_recent >= gate_attempt and unique_days_recent >= gate_days
        blend_weight = 0.0
        if gate:
            sample_factor = self._clamp((weighted_recent - gate_attempt) / 24.0, 0.0, 1.0)
            blend_weight = self._clamp(sample_factor * recency_factor, 0.0, max_blend)
        final = (modeled * (1.0 - blend_weight)) + ((empirical_rate * 100.0) * blend_weight)
        delta = abs(modeled - (empirical_rate * 100.0))
        confidence = "LOW"
        if blend_weight > 0.0:
            if weighted_recent >= 30.0 and recency_factor >= 0.70 and delta <= 20.0:
                confidence = "HIGH"
            elif weighted_recent >= gate_attempt and recency_factor >= 0.40:
                confidence = "MED"
        else:
            if weighted_recent >= (gate_attempt * 0.5) and unique_days_recent >= 1:
                confidence = "MED"
        if blend_weight <= 0.0 and confidence == "HIGH":
            confidence = "MED"
        reason = "PRIMARY_HISTORY" if gate and not pooled else "POOLED_HISTORY" if gate and pooled else "SPARSE_HISTORY"
        info = {
            "final_score": self._clamp(final, 0.0, 100.0),
            "blend_weight": float(blend_weight),
            "empirical_rate": float(empirical_rate),
            "weighted_attempt_recent": float(weighted_recent),
            "unique_days_recent": int(unique_days_recent),
            "recency_factor": float(recency_factor),
            "confidence": confidence,
            "reason_code": reason,
            "pooled": bool(pooled),
        }
        self._empirical_cache[cache_key] = (now_mono, dict(info))
        if len(self._empirical_cache) > self._empirical_cache_max_entries:
            self._prune_empirical_cache(now_mono + self._empirical_cache_prune_interval_sec)
        return float(info["final_score"]), info

    def top_bands_modeled(
        self,
        *,
        bands: Sequence[str],
        mid_utc: dt.datetime,
        user_ll: Tuple[float, float],
        points: Sequence[Tuple[float, float]],
        origin_grid6: str = "",
        target_type: str = "",
        target_id: str = "",
        blend_settings: Optional[Mapping[str, object]] = None,
        limit: int = 2,
    ) -> List[Tuple[str, float]]:
        if not points:
            return []
        out: List[Tuple[str, float]] = []
        for band in bands:
            vals: List[float] = []
            for lat, lon in points:
                dist = self.haversine_km(user_ll[0], user_ll[1], lat, lon)
                vals.append(
                    self.modeled_band_score(
                        band=str(band),
                        user_ll=user_ll,
                        dest_lat=float(lat),
                        dest_lon=float(lon),
                        now_utc=mid_utc,
                        distance_km=dist,
                    )
                )
            if vals:
                modeled_avg = sum(vals) / max(1, len(vals))
                final_score = modeled_avg
                if (
                    blend_settings is not None
                    and origin_grid6
                    and target_type
                    and target_id
                ):
                    final_score, _ = self.blend_modeled_score(
                        modeled_score=modeled_avg,
                        now_utc=mid_utc,
                        origin_grid6=origin_grid6,
                        target_type=target_type,
                        target_id=target_id,
                        band=str(band),
                        blend_settings=blend_settings,
                    )
                out.append((str(band).upper(), final_score))
        out.sort(key=lambda x: x[1], reverse=True)
        return out[: max(1, int(limit))]

    def _coords_to_indices(self, lat: float, lon: float) -> Optional[Tuple[int, int]]:
        mode = self._db_index_mode
        try:
            if mode == "round_halfdeg":
                lat_idx = int(round((float(lat) + 90.0) * 2))
                lon_idx = int(round((float(lon) + 180.0) * 2))
                return lat_idx, lon_idx
            lat_idx = int((float(lat) + 90.0) // 5)
            lon_idx = int((float(lon) + 180.0) // 5)
            lat_idx = max(0, min(35, lat_idx))
            lon_idx = max(0, min(71, lon_idx))
            return lat_idx, lon_idx
        except Exception:
            return None
