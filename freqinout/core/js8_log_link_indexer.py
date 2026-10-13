from __future__ import annotations

import datetime
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from freqinout.core.checkins_db import ensure_operator_checkins_schema
from freqinout.core.logger import log
from freqinout.core.operating_group_frequency import normalize_frequency_hz, resolve_operating_group_frequency
from freqinout.core.operator_activity import ensure_js8_callsign_stats, record_js8_activity_batch
from freqinout.core.settings_manager import SettingsManager


# JS8 log ingestion for SettingsTab "Load JS8 Traffic"
class JS8LogLinkIndexer:
    """
    Parses JS8Call DIRECTED.TXT and ALL.TXT to populate js8_links table.
    Only ALL.TXT lines containing "Transmitting" are ingested.
    """

    def __init__(self, settings: SettingsManager, db_path: Path):
        self.settings = settings
        self.db_path = db_path
        self._operator_schema_ready = False

    @staticmethod
    def _utc_now_iso() -> str:
        return datetime.datetime.now(datetime.timezone.utc).isoformat()

    def _open_operator_db(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.execute("PRAGMA busy_timeout=5000")
        if not self._operator_schema_ready:
            ensure_operator_checkins_schema(conn)
            self._operator_schema_ready = True
        return conn

    @staticmethod
    def _base_callsign(cs: str) -> str:
        """
        Strip common portable/mobile suffixes so variants map to the base callsign.
        Examples: KG5RKW/P -> KG5RKW, K0ABC/M -> K0ABC
        """
        cs_norm = (cs or "").strip().upper()
        if not cs_norm:
            return ""
        # remove trailing /<suffix> where suffix is letters/numbers up to 4 chars
        import re

        return re.sub(r"/(P|M|MM|QRP|SOTA|ROVER|[A-Z0-9]{1,4})$", "", cs_norm)

    def _freq_to_band(self, freq_hz: Optional[float]) -> Optional[str]:
        if freq_hz is None:
            return None
        try:
            mhz = float(freq_hz) / 1_000_000.0
        except Exception:
            return None
        bands = [
            ("160M", 1.8, 2.0),
            ("80M", 3.5, 4.0),
            ("60M", 5.0, 5.5),
            ("40M", 7.0, 7.3),
            ("30M", 10.1, 10.15),
            ("20M", 14.0, 14.35),
            ("17M", 18.068, 18.168),
            ("15M", 21.0, 21.45),
            ("12M", 24.89, 24.99),
            ("10M", 28.0, 29.7),
            ("6M", 50.0, 54.0),
            ("2M", 144.0, 148.0),
        ]
        for name, lo, hi in bands:
            if lo <= mhz <= hi:
                return name
        return None

    def _lookup_operating_group(self, freq_hz: Optional[float]) -> str:
        """
        Map a frequency to a group only when logical attribution is unique.
        """
        try:
            ops = self.settings.get("operating_groups", []) or []
        except Exception:
            return ""
        if not freq_hz:
            return ""
        try:
            mhz = float(freq_hz) / 1_000_000.0
        except (TypeError, ValueError, OverflowError):
            return ""
        band = self._freq_to_band(freq_hz) or ""
        groups = {str(row.get("group") or "").strip().upper() for row in ops if isinstance(row, dict)}
        matched: set[str] = set()
        wanted_hz = normalize_frequency_hz(mhz)
        for group in groups:
            if not group:
                continue
            resolution = resolve_operating_group_frequency(ops, group=group, band=band, frequency=mhz)
            exact_candidates = [
                row for row in resolution.candidates
                if wanted_hz is not None and normalize_frequency_hz(row.get("frequency")) == wanted_hz
            ]
            if resolution.resolved or exact_candidates:
                matched.add(group)
        return next(iter(matched)) if len(matched) == 1 else ""

    # -------- parsing helpers -------- #
    def _parse_directed_line(self, line: str) -> Optional[tuple]:
        """
        DIRECTED.TXT format (tab separated):
        2025-12-09 03:30:55\t3.588000\t1950\t+05\tKE7CIU: KJ5CRF HEARTBEAT SNR -12
        """
        parts = [p for p in line.strip().split("\t") if p]
        if len(parts) < 5:
            return None
        dt_str, freq_txt, _shift, snr_txt, msg = parts[0], parts[1], parts[2], parts[3], parts[4]
        if self._is_heartbeat_traffic(msg):
            return None
        origin, dest, relay_via = self._extract_origin_dest_relay(msg)
        if not origin or not dest:
            return None
        try:
            ts = datetime.datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=datetime.timezone.utc).timestamp()
        except Exception:
            return None
        try:
            freq_hz = float(freq_txt) * 1_000_000.0
        except Exception:
            freq_hz = None
        try:
            snr = float(snr_txt)
        except Exception:
            snr = None
        return (ts, origin, dest, snr, freq_hz, bool(relay_via), relay_via)

    def _maybe_capture_group_grid(self, line: str) -> None:
        """
        Capture group/grid info from group messages ending with '%} ♢' and upsert into operator_checkins.
        Example:
        N7SHM: @AMRRON  ,DN28HH,5,...,{F%} ♢
        """
        if "GRID?" in line.upper() or "..." in line:
            return
        parts = line.split("\t")
        if len(parts) < 5:
            return
        try:
            ts_str = parts[0][:19]
            ts = datetime.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=datetime.timezone.utc)
        except Exception:
            ts = datetime.datetime.now(datetime.timezone.utc)
        msg = parts[4]
        if ":" not in msg or "GRID" not in msg.upper():
            return
        origin, rest = msg.split(":", 1)
        origin = origin.strip().upper()
        # tokens for parsing
        tokens = rest.strip().replace(",", " ").split()
        if not tokens:
            return
        try:
            idx = [t.upper() for t in tokens].index("GRID")
        except ValueError:
            return
        if idx + 1 >= len(tokens):
            return
        grid = tokens[idx + 1].strip().upper()
        if not grid or "?" in grid or not self._valid_grid(grid):
            return
        grp = ""
        for t in tokens:
            if t.startswith("@"):
                grp = t.lstrip("@").upper()
                break
        freq_hz = None
        try:
            freq_hz = float(parts[1]) * 1_000_000.0
        except Exception:
            freq_hz = None
        groups = []
        if grp and self._is_allowed_group(grp):
            groups.append(grp)
        op_grp = self._lookup_operating_group(freq_hz)
        if op_grp:
            groups.append(op_grp)
        if not groups:
            return
        self._upsert_operator_info(origin, grid, groups, ts)

    def _is_allowed_group(self, grp: str) -> bool:
        g = (grp or "").strip().upper()
        if not g:
            return False
        try:
            prim = [x.strip().upper() for x in (self.settings.get("primary_js8_groups", []) or []) if x]
        except Exception:
            prim = []
        try:
            ops = [str(row.get("group", "")).strip().upper() for row in (self.settings.get("operating_groups", []) or []) if row]
        except Exception:
            ops = []
        return g in prim or g in ops

    def _valid_grid(self, grid: str) -> bool:
        import re
        # Maidenhead: 4-char (LLDD) or 6-char (LLDDLL)
        return bool(re.match(r"^[A-R]{2}[0-9]{2}([A-X]{2})?$", grid.upper()))

    def _parse_commstat_grid(self, text: str) -> str:
        match = re.search(r",\s*([A-R]{2}[0-9]{2}(?:[A-X]{2})?)\s*,", text or "")
        return match.group(1) if match else ""

    def _parse_commstat_state(self, text: str) -> str:
        match = re.search(
            r",\s*[A-R]{2}[0-9]{2}(?:[A-X]{2})?\s*,[^,]*,[^,]*,[^,]*,\s*([A-Z]{2})\b",
            text or "",
        )
        return match.group(1) if match else ""

    def _extract_group_name(self, msg: str, freq_hz: Optional[float]) -> str:
        upper = (msg or "").upper()
        match = re.search(r"@([A-Z0-9]{1,15})", upper)
        if match:
            grp = match.group(1).strip().upper()
            if grp and self._is_allowed_group(grp):
                return grp
        op_grp = self._lookup_operating_group(freq_hz)
        return op_grp or ""

    def _maybe_capture_geo_tokens(self, callsign: str, msg: str, freq_hz: Optional[float]) -> None:
        cs = self._base_callsign(callsign)
        text = (msg or "").strip()
        if not cs or not text:
            return
        upper = text.upper()
        if "*DE*" in upper:
            return
        if any(code in upper for code in ("F!107", "F!305", "F!307", "F!308", "F!701")):
            return
        if "GR[" not in upper and "ST[" not in upper and "," not in text:
            return
        state = ""
        grid = ""
        match = re.search(r"GR\[([A-R]{2}[0-9]{2}(?:[A-X]{2})?)\]", upper)
        if match:
            grid = match.group(1)
        match = re.search(r"ST\[([A-Z]{2})\]", upper)
        if match:
            state = match.group(1)
        if "," in text:
            if not grid:
                grid = self._parse_commstat_grid(upper)
            if not state:
                state = self._parse_commstat_state(upper)
        if grid and not self._valid_grid(grid):
            grid = ""
        if state and not re.match(r"^[A-Z]{2}$", state):
            state = ""
        if not grid and not state:
            return
        group_name = self._extract_group_name(text, freq_hz)
        self._update_operator_geo(cs, state, grid, group_name)

    def _update_operator_geo(self, callsign: str, state: str, grid: str, group_name: str) -> None:
        cs = self._base_callsign(callsign)
        state = (state or "").strip().upper()
        grid = (grid or "").strip().upper()
        if not cs or (not state and not grid):
            return
        if state and not re.match(r"^[A-Z]{2}$", state):
            state = ""
        if grid and not self._valid_grid(grid):
            grid = ""
        if not state and not grid:
            return
        conn = None
        try:
            conn = self._open_operator_db()
            cur = conn.cursor()
            cur.execute(
                "SELECT state, grid, group1, group2, group3, groups_json, trusted FROM operator_checkins WHERE callsign=?",
                (cs,),
            )
            row = cur.fetchone()
            now_iso = self._utc_now_iso()
            group_name = (group_name or "").strip().upper()
            if row is None:
                groups = [g for g in [group_name] if g]
                cur.execute(
                    """
                    INSERT INTO operator_checkins (
                        callsign, name, state, grid, group1, group2, group3, group_role,
                        first_seen_utc, last_seen_utc, last_net, last_role, checkin_count, groups_json, trusted
                    ) VALUES (?, '', ?, ?, ?, ?, ?, '', ?, ?, '', '', 0, ?, 0)
                    """,
                    (
                        cs,
                        state or "",
                        grid or "",
                        groups[0] if len(groups) > 0 else "",
                        groups[1] if len(groups) > 1 else "",
                        groups[2] if len(groups) > 2 else "",
                        now_iso,
                        now_iso,
                        json.dumps(groups) if groups else None,
                    ),
                )
            else:
                old_state, old_grid, g1, g2, g3, gj, trusted = row
                new_state = (old_state or "").strip().upper()
                old_grid_norm = (old_grid or "").strip().upper()
                has_group = bool((g1 or "").strip() or (g2 or "").strip() or (g3 or "").strip())
                if not has_group and gj:
                    try:
                        parsed = json.loads(gj)
                        if isinstance(parsed, list) and any(str(x).strip() for x in parsed):
                            has_group = True
                    except Exception:
                        pass
                if not (has_group and (new_state or old_grid_norm)) and state and state != new_state:
                    new_state = state
                new_grid = old_grid_norm
                if grid:
                    if not new_grid:
                        new_grid = grid
                    elif len(new_grid) == 4 and len(grid) == 6 and new_grid == grid[:4]:
                        new_grid = grid
                    elif len(new_grid) == 6 and len(grid) == 6 and grid != new_grid:
                        new_grid = grid
                slots = [g1 or "", g2 or "", g3 or ""]
                if group_name and group_name not in slots:
                    for idx, val in enumerate(slots):
                        if not val:
                            slots[idx] = group_name
                            break
                extra_json = []
                if gj:
                    try:
                        prev = json.loads(gj)
                        if isinstance(prev, list):
                            extra_json.extend([str(x).upper() for x in prev])
                    except Exception:
                        pass
                if group_name and group_name not in extra_json:
                    extra_json.append(group_name)
                cur.execute(
                    """
                    UPDATE operator_checkins
                    SET
                        state=?,
                        grid=?,
                        group1=?,
                        group2=?,
                        group3=?,
                        groups_json=?,
                        last_seen_utc=?
                    WHERE callsign=?
                    """,
                    (
                        new_state or "",
                        new_grid or "",
                        slots[0],
                        slots[1],
                        slots[2],
                        json.dumps([g for g in extra_json if g]) if extra_json else gj,
                        now_iso,
                        cs,
                    ),
                )
            conn.commit()
        except Exception as e:
            self._operator_schema_ready = False
            log.debug("JS8LogLinkIndexer: failed to update operator geo %s: %s", callsign, e)
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass

    def _upsert_operator_info(self, callsign: str, grid: str, group_val: str, ts: datetime.datetime) -> None:
        # Reuse js8call tab helpers not available here; implement lightweight upsert
        cs = self._base_callsign(callsign)
        if not cs:
            return
        ts_str = ts.astimezone(datetime.timezone.utc).isoformat()
        conn = None
        try:
            conn = self._open_operator_db()
            cur = conn.cursor()
            cur.execute(
                "SELECT grid, group1, group2, group3, groups_json, trusted FROM operator_checkins WHERE callsign=?",
                (cs,),
            )
            row = cur.fetchone()
            groups = [group_val] if isinstance(group_val, str) else list(group_val or [])
            groups = [g.strip().upper() for g in groups if g]
            if row is None:
                cur.execute(
                    """
                    INSERT INTO operator_checkins (
                        callsign, name, state, grid, group1, group2, group3, group_role,
                        first_seen_utc, last_seen_utc, last_net, last_role, checkin_count, groups_json, trusted
                    ) VALUES (?, '', '', ?, ?, ?, ?, '', ?, ?, '', '', 0, ?, 0)
                    """,
                    (
                        cs,
                        grid,
                        groups[0] if len(groups) > 0 else "",
                        groups[1] if len(groups) > 1 else "",
                        groups[2] if len(groups) > 2 else "",
                        ts_str,
                        ts_str,
                        json.dumps(groups) if groups else None,
                    ),
                )
            else:
                existing_grid, g1, g2, g3, gj, trusted = row
                # Keep existing grid if already set; do not replace with new reports
                final_grid = existing_grid.strip().upper() if existing_grid else grid
                slots = [g1 or "", g2 or "", g3 or ""]
                slot_set = {s.strip().upper() for s in slots if s}
                merged = slot_set.copy()
                merged.update(groups)
                slots_filled = [s.strip().upper() for s in slots if s.strip()]
                for g in groups:
                    if len(slots_filled) < 3 and g not in slots_filled:
                        slots_filled.append(g)
                while len(slots_filled) < 3:
                    slots_filled.append("")
                extra = merged - set(slots_filled) if merged else set()
                extra_json = []
                if gj:
                    try:
                        prev = json.loads(gj)
                        if isinstance(prev, list):
                            extra_json.extend([str(x).upper() for x in prev])
                    except Exception:
                        pass
                for g in extra:
                    if g and g not in extra_json:
                        extra_json.append(g)
                cur.execute(
                    """
                    UPDATE operator_checkins
                    SET
                        grid=?,
                        group1=?,
                        group2=?,
                        group3=?,
                        groups_json=?,
                        last_seen_utc=?
                    WHERE callsign=?
                    """,
                    (
                        final_grid,
                        slots_filled[0],
                        slots_filled[1],
                        slots_filled[2],
                        json.dumps(extra_json) if extra_json else gj,
                        ts_str,
                        cs,
                    ),
                )
            conn.commit()
        except Exception as e:
            self._operator_schema_ready = False
            log.debug("JS8LogLinkIndexer: failed to upsert operator info %s: %s", callsign, e)
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass

    def _parse_all_line(self, line: str) -> Optional[tuple]:
        """
        ALL.TXT lines of interest contain "Transmitting":
        2025-12-06 20:17:15  Transmitting 14.11 MHz  JS8:  N1MAG: W3BFO SNR -01
        """
        if "Transmitting" not in line:
            return None
        try:
            dt_str = line[:19]
            ts = datetime.datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=datetime.timezone.utc).timestamp()
        except Exception:
            return None
        freq_hz = None
        try:
            mhz_part = line.split("Transmitting", 1)[1]
            mhz_tok = [tok for tok in mhz_part.split() if tok.replace(".", "", 1).isdigit()]
            if mhz_tok:
                freq_hz = float(mhz_tok[0]) * 1_000_000.0
        except Exception:
            freq_hz = None
        # Extract JS8 payload (origin: destination ...) after "JS8:" if present
        msg_part = ""
        if "JS8:" in line:
            msg_part = line.split("JS8:", 1)[1]
        elif ":" in line:
            msg_part = line.split(":", 1)[1]
        # Trim leading colon/space
        msg_part = msg_part.lstrip(": ").strip()
        if self._is_heartbeat_traffic(msg_part):
            return None
        origin, dest, relay_via = self._extract_origin_dest_relay(msg_part)
        if not origin or not dest:
            return None
        snr = None
        for tok in reversed(msg_part.split()):
            try:
                snr = float(tok)
                break
            except Exception:
                continue
        return (ts, origin, dest, snr, freq_hz, bool(relay_via), relay_via)

    def _extract_origin_dest(self, msg: str) -> tuple[str, str]:
        origin, dest, _relay_via = self._extract_origin_dest_relay(msg)
        return origin, dest

    def _extract_origin_call(self, msg: str) -> str:
        if ":" not in msg:
            return ""
        origin, _rest = msg.split(":", 1)
        origin = self._clean_route_token(origin)
        return origin if self._looks_like_station_call(origin) else ""

    @staticmethod
    def _clean_route_token(token: str) -> str:
        text = (token or "").strip().upper()
        text = text.strip(" ,;[](){}<>")
        if text.startswith("@"):
            return ""
        text = re.sub(r"[^A-Z0-9/]", "", text)
        return text

    @staticmethod
    def _looks_like_station_call(token: str) -> bool:
        text = (token or "").strip().upper()
        if not text or len(text) < 3 or len(text) > 14:
            return False
        if not any(ch.isdigit() for ch in text) or not any(ch.isalpha() for ch in text):
            return False
        if text in {"ALLCALL", "ALL", "CQ", "HEARTBEAT", "HB"}:
            return False
        return bool(re.match(r"^[A-Z0-9]+(?:/[A-Z0-9]{1,4})?$", text))

    @staticmethod
    def _is_heartbeat_traffic(msg: str) -> bool:
        text = (msg or "").upper()
        return bool(re.search(r"\b(?:HEARTBEAT|HB)\b", text))

    def _extract_origin_dest_relay(self, msg: str) -> tuple[str, str, str]:
        if ":" not in msg:
            return "", "", ""
        origin, rest = msg.split(":", 1)
        origin = self._clean_route_token(origin)
        first = (rest.strip().split() or [""])[0]
        first = first.strip().strip(",").strip().upper()
        relay_via = ""
        if ">" in first:
            parts = [self._clean_route_token(part) for part in first.split(">")]
            parts = [part for part in parts if part]
            if len(parts) >= 2:
                relay_via = ">".join(parts[:-1])
                dest = parts[-1]
            else:
                dest = parts[0] if parts else ""
        else:
            dest = self._clean_route_token(first)
        if not self._looks_like_station_call(origin) or not self._looks_like_station_call(dest):
            return "", "", ""
        return origin, dest, relay_via

    # -------- DB helpers -------- #
    def _ensure_table(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS js8_links (
                ts REAL,
                origin TEXT,
                destination TEXT,
                snr REAL,
                band TEXT,
                freq_hz REAL,
                source_id TEXT,
                app_instance_id TEXT,
                source_radio_id TEXT,
                is_relay INTEGER DEFAULT 0,
                relay_via TEXT,
                is_spotter INTEGER DEFAULT 0,
                last_seen_utc TEXT
            )
            """
        )
        # Add last_seen_utc if created earlier without it
        try:
            cur = conn.execute("PRAGMA table_info(js8_links)")
            cols = {row[1] for row in cur.fetchall()}
            if "last_seen_utc" not in cols:
                conn.execute("ALTER TABLE js8_links ADD COLUMN last_seen_utc TEXT")
            if "source_id" not in cols:
                conn.execute("ALTER TABLE js8_links ADD COLUMN source_id TEXT")
            if "app_instance_id" not in cols:
                conn.execute("ALTER TABLE js8_links ADD COLUMN app_instance_id TEXT")
            if "source_radio_id" not in cols:
                conn.execute("ALTER TABLE js8_links ADD COLUMN source_radio_id TEXT")
            if "direction_known" not in cols:
                conn.execute("ALTER TABLE js8_links ADD COLUMN direction_known INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_js8_links_ts ON js8_links(ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_js8_links_origin_ts ON js8_links(origin, ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_js8_links_destination_ts ON js8_links(destination, ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_js8_links_origin_dest ON js8_links(origin, destination)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_js8_links_band ON js8_links(band)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_js8_links_source ON js8_links(source_id, ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_js8_links_app_instance ON js8_links(app_instance_id, ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_js8_links_radio ON js8_links(source_radio_id, ts)")
        except Exception:
            pass
        ensure_js8_callsign_stats(conn, rebuild_if_empty=True)
        conn.commit()

    def _clear_table(self, conn: sqlite3.Connection) -> None:
        conn.execute("DELETE FROM js8_links")
        conn.execute("DELETE FROM js8_callsign_stats")
        conn.commit()

    def _ensure_latest_ts(self, last_default: float = 0.0) -> float:
        """
        Return the latest timestamp from js8_links or provided default.
        """
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.execute("SELECT MAX(ts) FROM js8_links")
            row = cur.fetchone()
            conn.close()
            if row and row[0]:
                return float(row[0])
        except Exception:
            pass
        return float(last_default or 0.0)

    def link_count(self) -> int:
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.execute("SELECT COUNT(*) FROM js8_links")
            row = cur.fetchone()
            conn.close()
            return int(row[0] or 0) if row else 0
        except Exception:
            return 0

    # -------- public API -------- #
    def update(self, since_ts: Optional[float] = None, *, force_rebuild: bool = False) -> int:
        """
        Rebuild js8_links from DIRECTED.TXT and ALL.TXT.
        Returns number of rows inserted.
        """
        directed_path = self._resolve_directed_path()
        return self.update_from_directed_path(
            directed_path,
            since_ts=since_ts,
            directed_offset_key="js8_links_directed_offset",
            all_offset_key="js8_links_all_offset",
            force_rebuild=force_rebuild,
        )

    def update_from_directed_path(
        self,
        directed_path: Optional[Path],
        *,
        since_ts: Optional[float] = None,
        directed_offset_key: str = "",
        all_offset_key: str = "",
        source_id: str = "",
        app_instance_id: str = "",
        source_radio_id: str = "",
        force_rebuild: bool = False,
    ) -> int:
        """
        Incrementally index one JS8Call log source.

        Multi-rig setups can run one JS8Call instance per radio. Offsets are
        scoped to a source path/instance so one log tail cannot advance another
        instance's checkpoint.
        """
        all_path = directed_path.parent / "ALL.TXT" if directed_path else None
        directed_offset = 0
        all_offset = 0
        effective_since = since_ts
        if effective_since is None:
            effective_since = self._ensure_latest_ts(last_default=0.0)
        directed_offset_key = directed_offset_key or self._offset_key_for_path("js8_links_directed_offset", directed_path)
        all_offset_key = all_offset_key or self._offset_key_for_path("js8_links_all_offset", all_path)
        source_id_txt = str(source_id or "").strip()
        app_instance_id_txt = str(app_instance_id or "").strip()
        source_radio_id_txt = str(source_radio_id or "").strip()
        using_legacy_offsets = (
            directed_offset_key == "js8_links_directed_offset"
            and all_offset_key == "js8_links_all_offset"
        )
        try:
            directed_offset = int(self.settings.get(directed_offset_key, 0) or 0)
        except Exception:
            directed_offset = 0
        try:
            all_offset = int(self.settings.get(all_offset_key, 0) or 0)
        except Exception:
            all_offset = 0
        if force_rebuild:
            directed_offset = 0
            all_offset = 0
            effective_since = None
        elif not using_legacy_offsets and directed_offset <= 0 and all_offset <= 0:
            effective_since = None

        # De-duplicate by ordered sender/recipient + band; reciprocal traffic stays separate.
        last_seen: Dict[str, float] = {}
        agg: Dict[tuple, Dict] = {}

        def handle_parsed(parsed: Optional[tuple]) -> None:
            if not parsed:
                return
            if len(parsed) >= 7:
                ts, origin, dest, snr, freq_hz, is_relay, relay_via = parsed[:7]
            else:
                ts, origin, dest, snr, freq_hz = parsed[:5]
                is_relay, relay_via = False, ""
            if effective_since and (not ts or ts < effective_since):
                return
            a = (origin or "").strip().upper()
            b = (dest or "").strip().upper()
            if not a or not b:
                return
            try:
                if ts:
                    if ts > last_seen.get(a, 0):
                        last_seen[a] = ts
                    if ts > last_seen.get(b, 0):
                        last_seen[b] = ts
            except Exception:
                pass
            band = self._freq_to_band(freq_hz)
            key = ((a, b), band)
            entry = agg.setdefault(
                key,
                {
                    "last_ts": ts,
                    "snr_sum": 0.0,
                    "snr_count": 0,
                    "freq_hz": freq_hz,
                    "is_relay": bool(is_relay),
                    "relay_via": str(relay_via or "").strip().upper(),
                },
            )
            if ts and (entry["last_ts"] is None or ts > entry["last_ts"]):
                entry["last_ts"] = ts
                if freq_hz is not None:
                    entry["freq_hz"] = freq_hz
                entry["is_relay"] = bool(is_relay)
                entry["relay_via"] = str(relay_via or "").strip().upper()
            try:
                if snr is not None:
                    entry["snr_sum"] += float(snr)
                    entry["snr_count"] += 1
            except Exception:
                pass
            if entry["freq_hz"] is None and freq_hz is not None:
                entry["freq_hz"] = freq_hz

        if directed_path and directed_path.exists():
            try:
                size_now = directed_path.stat().st_size
                if directed_offset < 0 or directed_offset > size_now:
                    directed_offset = 0
                with directed_path.open("r", encoding="utf-8", errors="ignore") as fh:
                    if directed_offset > 0:
                        fh.seek(directed_offset)
                    last_pos = fh.tell()
                    while True:
                        line = fh.readline()
                        if not line:
                            break
                        last_pos = fh.tell()
                        parts = line.split("\t", 4)
                        msg = parts[4] if len(parts) >= 5 else ""
                        if self._is_heartbeat_traffic(msg):
                            continue
                        origin = self._extract_origin_call(msg)
                        freq_hz = None
                        try:
                            freq_hz = float(parts[1]) * 1_000_000.0 if len(parts) >= 2 else None
                        except Exception:
                            freq_hz = None
                        if origin and msg:
                            self._maybe_capture_geo_tokens(origin, msg, freq_hz)
                        self._maybe_capture_group_grid(line)
                        handle_parsed(self._parse_directed_line(line))
                    try:
                        self.settings.set(directed_offset_key, int(last_pos))
                    except Exception:
                        pass
            except Exception as e:
                log.debug("JS8LogLinkIndexer: failed reading DIRECTED.TXT: %s", e)

        if all_path and all_path.exists():
            try:
                size_now = all_path.stat().st_size
                if all_offset < 0 or all_offset > size_now:
                    all_offset = 0
                with all_path.open("r", encoding="utf-8", errors="ignore") as fh:
                    if all_offset > 0:
                        fh.seek(all_offset)
                    last_pos = fh.tell()
                    while True:
                        line = fh.readline()
                        if not line:
                            break
                        last_pos = fh.tell()
                        if "Transmitting" in line:
                            msg_part = ""
                            if "JS8:" in line:
                                msg_part = line.split("JS8:", 1)[1]
                            elif ":" in line:
                                msg_part = line.split(":", 1)[1]
                            msg_part = msg_part.lstrip(": ").strip()
                            if self._is_heartbeat_traffic(msg_part):
                                continue
                            origin = self._extract_origin_call(msg_part)
                            freq_hz = None
                            try:
                                mhz_part = line.split("Transmitting", 1)[1]
                                mhz_tok = [tok for tok in mhz_part.split() if tok.replace(".", "", 1).isdigit()]
                                if mhz_tok:
                                    freq_hz = float(mhz_tok[0]) * 1_000_000.0
                            except Exception:
                                freq_hz = None
                            if origin and msg_part:
                                self._maybe_capture_geo_tokens(origin, msg_part, freq_hz)
                        handle_parsed(self._parse_all_line(line))
                    try:
                        self.settings.set(all_offset_key, int(last_pos))
                    except Exception:
                        pass
            except Exception as e:
                log.debug("JS8LogLinkIndexer: failed reading ALL.TXT: %s", e)

        if not agg:
            return 0

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        try:
            self._ensure_table(conn)
            payload = []
            activity_rows = []
            for key, entry in agg.items():
                pair, band = key
                origin, dest = pair
                avg_snr = entry["snr_sum"] / entry["snr_count"] if entry["snr_count"] else None
                activity_rows.append(
                    (origin, entry["last_ts"], band, entry.get("freq_hz"), source_id_txt, app_instance_id_txt, source_radio_id_txt)
                )
                activity_rows.append(
                    (dest, entry["last_ts"], band, entry.get("freq_hz"), source_id_txt, app_instance_id_txt, source_radio_id_txt)
                )
                payload.append(
                    (
                        entry["last_ts"],
                        origin,
                        dest,
                        avg_snr,
                        band,
                        entry.get("freq_hz"),
                        source_id_txt,
                        app_instance_id_txt,
                        source_radio_id_txt,
                        1 if entry.get("is_relay") else 0,
                        entry.get("relay_via") or None,
                        0,
                    )
                )
            # delete any existing rows for same pair+band to avoid duplicates
            for key, _ in agg.items():
                pair, band = key
                origin, dest = pair
                if source_id_txt:
                    conn.execute(
                        "DELETE FROM js8_links WHERE origin=? AND destination=? AND direction_known=1 AND IFNULL(band,'')=IFNULL(?,IFNULL(band,'')) AND IFNULL(source_id,'')=?",
                        (origin, dest, band, source_id_txt),
                    )
                else:
                    conn.execute(
                        "DELETE FROM js8_links WHERE origin=? AND destination=? AND direction_known=1 AND IFNULL(band,'')=IFNULL(?,IFNULL(band,'')) AND IFNULL(source_id,'')=''",
                        (origin, dest, band),
                    )
            conn.executemany(
                """
                INSERT INTO js8_links
                    (ts, origin, destination, snr, band, freq_hz, source_id, app_instance_id, source_radio_id, is_relay, relay_via, is_spotter, direction_known)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                payload,
            )
            record_js8_activity_batch(conn, activity_rows)
            # Update last_seen_utc per callsign using most recent ts from source logs
            try:
                for cs, ts_val in last_seen.items():
                    if not cs or not ts_val:
                        continue
                    iso = datetime.datetime.utcfromtimestamp(ts_val).strftime("%Y-%m-%d %H:%M:%S")
                    conn.execute(
                        "UPDATE js8_links SET last_seen_utc=? WHERE origin=? OR destination=?",
                        (iso, cs, cs),
                    )
            except Exception as e:
                log.debug("JS8LogLinkIndexer: failed to stamp last_seen_utc: %s", e)
            conn.commit()
            return len(payload)
        finally:
            conn.close()

    def update_from_ingest_sources(
        self,
        sources: Iterable[object],
        *,
        since_ts: Optional[float] = None,
        force_rebuild: bool = False,
    ) -> Dict[str, int]:
        """
        Incrementally index JS8Call log sources described by the ingest inventory.

        The inventory exposes DIRECTED.TXT and ALL.TXT as separate file sources so
        health/status UI can reason about each path. The link indexer consumes
        them as one JS8Call log pair per app instance to keep offsets independent
        per radio while avoiding duplicate scans.
        """
        grouped: Dict[str, Dict[str, object]] = {}
        for source in sources or ():
            family = str(getattr(source, "family", "") or "").strip().lower()
            source_type = str(getattr(source, "source_type", "") or "").strip().lower()
            if family != "js8call" or source_type != "file":
                continue
            role = str((getattr(source, "metadata", {}) or {}).get("role", "") or "").strip().lower()
            if role not in {"directed", "all"}:
                continue
            app_key = str(getattr(source, "app_instance_id", "") or getattr(source, "radio_id", "") or "").strip()
            if not app_key:
                app_key = str(Path(str(getattr(source, "path", "") or "")).expanduser().parent)
            bucket = grouped.setdefault(app_key, {})
            bucket[role] = source

        counts: Dict[str, int] = {}
        for app_key, bucket in grouped.items():
            directed_source = bucket.get("directed")
            all_source = bucket.get("all")
            if directed_source is not None:
                directed_path = Path(str(getattr(directed_source, "path", "") or "")).expanduser()
                directed_key = str(getattr(directed_source, "checkpoint_key", "") or "")
                all_key = str(getattr(all_source, "checkpoint_key", "") or "") if all_source is not None else ""
                source_id = str(getattr(directed_source, "source_id", "") or app_key)
            elif all_source is not None:
                all_path = Path(str(getattr(all_source, "path", "") or "")).expanduser()
                directed_path = all_path.with_name("DIRECTED.TXT")
                directed_key = self._offset_key_for_path("js8_links_directed_offset", directed_path)
                all_key = str(getattr(all_source, "checkpoint_key", "") or "")
                source_id = str(getattr(all_source, "source_id", "") or app_key)
            else:
                continue
            counts[source_id] = self.update_from_directed_path(
                directed_path,
                since_ts=since_ts,
                directed_offset_key=directed_key,
                all_offset_key=all_key,
                source_id=source_id,
                app_instance_id=str(getattr(directed_source or all_source, "app_instance_id", "") or ""),
                source_radio_id=str(getattr(directed_source or all_source, "radio_id", "") or ""),
                force_rebuild=force_rebuild,
            )
        return counts

    def query_links(self, *args, **kwargs):
        return []

    def ingest_live(
        self,
        ts: float,
        origin: str,
        destination: str,
        snr: Optional[float] = None,
        freq_hz: Optional[float] = None,
        is_spotter: int = 0,
        source_id: str = "",
        app_instance_id: str = "",
        source_radio_id: str = "",
    ) -> None:
        """
        Incrementally upsert a single observation from js8net without rebuilding entire table.
        """
        origin = (origin or "").strip().upper()
        destination = (destination or "").strip().upper()
        if not origin or not destination:
            return
        band = self._freq_to_band(freq_hz)
        ts_val = float(ts or time.time())
        iso = datetime.datetime.utcfromtimestamp(ts_val).strftime("%Y-%m-%d %H:%M:%S")
        source_id_txt = str(source_id or "").strip()
        app_instance_id_txt = str(app_instance_id or "").strip()
        source_radio_id_txt = str(source_radio_id or "").strip()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        try:
            self._ensure_table(conn)
            if source_id_txt:
                conn.execute(
                    """
                    DELETE FROM js8_links
                     WHERE origin=? AND destination=? AND direction_known=1
                       AND IFNULL(band,'')=IFNULL(?,IFNULL(band,''))
                       AND COALESCE(source_id, '')=?
                    """,
                    (origin, destination, band, source_id_txt),
                )
            else:
                conn.execute(
                    """
                    DELETE FROM js8_links
                     WHERE origin=? AND destination=? AND direction_known=1
                       AND IFNULL(band,'')=IFNULL(?,IFNULL(band,''))
                       AND COALESCE(source_id, '')=''
                    """,
                    (origin, destination, band),
                )
            conn.execute(
                """
                INSERT INTO js8_links (
                    ts, origin, destination, snr, band, freq_hz, is_relay, relay_via,
                    is_spotter, last_seen_utc, source_id, app_instance_id, source_radio_id, direction_known
                )
                VALUES (?, ?, ?, ?, ?, ?, 0, NULL, ?, ?, ?, ?, ?, 1)
                """,
                (
                    ts_val,
                    origin,
                    destination,
                    snr,
                    band,
                    freq_hz,
                    int(bool(is_spotter)),
                    iso,
                    source_id_txt,
                    app_instance_id_txt,
                    source_radio_id_txt,
                ),
            )
            record_js8_activity_batch(
                conn,
                (
                    (origin, ts_val, band, freq_hz, source_id_txt, app_instance_id_txt, source_radio_id_txt),
                    (destination, ts_val, band, freq_hz, source_id_txt, app_instance_id_txt, source_radio_id_txt),
                ),
            )
            try:
                conn.execute("UPDATE js8_links SET last_seen_utc=? WHERE origin=? OR destination=?", (iso, origin, origin))
                conn.execute("UPDATE js8_links SET last_seen_utc=? WHERE origin=? OR destination=?", (iso, destination, destination))
            except Exception:
                pass
            conn.commit()
        finally:
            conn.close()

    def backfill_geo_from_logs(self) -> int:
        """
        Scan DIRECTED.TXT and ALL.TXT for grid/state tokens and update operator_checkins.
        Returns number of lines scanned.
        """
        directed_path = self._resolve_directed_path()
        all_path = directed_path.parent / "ALL.TXT" if directed_path else None
        if not directed_path or not directed_path.exists():
            return 0
        scanned = 0
        try:
            with directed_path.open("r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    scanned += 1
                    parts = line.split("\t", 4)
                    msg = parts[4] if len(parts) >= 5 else ""
                    origin = self._extract_origin_call(msg)
                    freq_hz = None
                    try:
                        freq_hz = float(parts[1]) * 1_000_000.0 if len(parts) >= 2 else None
                    except Exception:
                        freq_hz = None
                    if origin and msg:
                        self._maybe_capture_geo_tokens(origin, msg, freq_hz)
        except Exception as e:
            log.debug("JS8LogLinkIndexer: geo backfill failed on DIRECTED.TXT: %s", e)
        if all_path and all_path.exists():
            try:
                with all_path.open("r", encoding="utf-8", errors="ignore") as fh:
                    for line in fh:
                        scanned += 1
                        if "Transmitting" not in line:
                            continue
                        msg_part = ""
                        if "JS8:" in line:
                            msg_part = line.split("JS8:", 1)[1]
                        elif ":" in line:
                            msg_part = line.split(":", 1)[1]
                        msg_part = msg_part.lstrip(": ").strip()
                        origin = self._extract_origin_call(msg_part)
                        freq_hz = None
                        try:
                            mhz_part = line.split("Transmitting", 1)[1]
                            mhz_tok = [tok for tok in mhz_part.split() if tok.replace(".", "", 1).isdigit()]
                            if mhz_tok:
                                freq_hz = float(mhz_tok[0]) * 1_000_000.0
                        except Exception:
                            freq_hz = None
                        if origin and msg_part:
                            self._maybe_capture_geo_tokens(origin, msg_part, freq_hz)
            except Exception as e:
                log.debug("JS8LogLinkIndexer: geo backfill failed on ALL.TXT: %s", e)
        return scanned

    def ingest_live_batch(self, observations: List[tuple]) -> None:
        """
        Upsert multiple live observations in a single transaction.
        Each observation: (ts, origin, destination, snr, freq_hz, is_spotter)
        or (ts, origin, destination, snr, freq_hz, is_spotter, source_id, app_instance_id, source_radio_id).
        """
        if not observations:
            return
        rows = []
        activity_rows = []
        last_seen: Dict[str, float] = {}
        for observation in observations:
            try:
                ts, origin, destination, snr, freq_hz, is_spotter = observation[:6]
                source_id, app_instance_id, source_radio_id = (
                    observation[6],
                    observation[7],
                    observation[8],
                ) if len(observation) >= 9 else ("", "", "")
            except Exception:
                continue
            origin = (origin or "").strip().upper()
            destination = (destination or "").strip().upper()
            if not origin or not destination:
                continue
            band = self._freq_to_band(freq_hz)
            ts_val = float(ts or time.time())
            iso = datetime.datetime.utcfromtimestamp(ts_val).strftime("%Y-%m-%d %H:%M:%S")
            source_id_txt = str(source_id or "").strip()
            app_instance_id_txt = str(app_instance_id or "").strip()
            source_radio_id_txt = str(source_radio_id or "").strip()
            rows.append(
                (
                    ts_val,
                    origin,
                    destination,
                    snr,
                    band,
                    freq_hz,
                    int(bool(is_spotter)),
                    iso,
                    source_id_txt,
                    app_instance_id_txt,
                    source_radio_id_txt,
                )
            )
            activity_rows.append((origin, ts_val, band, freq_hz, source_id_txt, app_instance_id_txt, source_radio_id_txt))
            activity_rows.append((destination, ts_val, band, freq_hz, source_id_txt, app_instance_id_txt, source_radio_id_txt))
            if ts_val:
                last_seen[origin] = max(last_seen.get(origin, 0), ts_val)
                last_seen[destination] = max(last_seen.get(destination, 0), ts_val)
        if not rows:
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        try:
            self._ensure_table(conn)
            for (
                ts_val,
                origin,
                destination,
                snr,
                band,
                freq_hz,
                is_spotter,
                iso,
                source_id_txt,
                app_instance_id_txt,
                source_radio_id_txt,
            ) in rows:
                if source_id_txt:
                    conn.execute(
                        """
                        DELETE FROM js8_links
                         WHERE origin=? AND destination=? AND direction_known=1
                           AND IFNULL(band,'')=IFNULL(?,IFNULL(band,''))
                           AND COALESCE(source_id, '')=?
                        """,
                        (origin, destination, band, source_id_txt),
                    )
                else:
                    conn.execute(
                        """
                        DELETE FROM js8_links
                         WHERE origin=? AND destination=? AND direction_known=1
                           AND IFNULL(band,'')=IFNULL(?,IFNULL(band,''))
                           AND COALESCE(source_id, '')=''
                        """,
                        (origin, destination, band),
                    )
                conn.execute(
                    """
                    INSERT INTO js8_links (
                        ts, origin, destination, snr, band, freq_hz, is_relay, relay_via,
                        is_spotter, last_seen_utc, source_id, app_instance_id, source_radio_id, direction_known
                    )
                    VALUES (?, ?, ?, ?, ?, ?, 0, NULL, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        ts_val,
                        origin,
                        destination,
                        snr,
                        band,
                        freq_hz,
                        int(bool(is_spotter)),
                        iso,
                        source_id_txt,
                        app_instance_id_txt,
                        source_radio_id_txt,
                    ),
                )
            record_js8_activity_batch(conn, activity_rows)
            try:
                for cs, ts_val in last_seen.items():
                    iso = datetime.datetime.utcfromtimestamp(ts_val).strftime("%Y-%m-%d %H:%M:%S")
                    conn.execute("UPDATE js8_links SET last_seen_utc=? WHERE origin=? OR destination=?", (iso, cs, cs))
            except Exception:
                pass
            conn.commit()
        finally:
            conn.close()

    def _resolve_directed_path(self) -> Optional[Path]:
        path_txt = (self.settings.get("js8_directed_path", "") or "").strip()
        if not path_txt:
            return None
        p = Path(path_txt)
        return p if p.exists() else None

    @staticmethod
    def _offset_key_for_path(prefix: str, path: Optional[Path]) -> str:
        if path is None:
            return prefix
        try:
            import hashlib

            resolved = str(path.expanduser().resolve())
            digest = hashlib.sha1(resolved.encode("utf-8", errors="ignore")).hexdigest()[:16]
            return f"{prefix}_{digest}"
        except Exception:
            return prefix
