from __future__ import annotations

import datetime
import hashlib
import json
import re
import sqlite3
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from freqinout.core.config_paths import get_config_dir
from freqinout.core.js8_expect_dispatcher import dispatch_expect_auto_reply, record_expect_dispatch_hold
from freqinout.core.group_utils import normalize_group_name
from freqinout.core.operator_identity import callsigns_for_operator, canonical_callsign, ensure_operator_identity_schema, resolve_operator_identity
from freqinout.core.js8_spotter_forms import (
    FORM_ID_PATTERN,
    FORM_TOKEN_RE,
    MAPPER_SETTINGS_KEY,
    form_id_enabled,
    form_codes_enabled_for,
    forms_enabled_for,
    normalize_form_code,
    resolve_spotter_forms_dir,
)
from freqinout.core.js8_spotter_decode import decode_spotter_form_text, split_spotter_form_text
from freqinout.core.js8_spotter_codec import unwrap_native_js8_form_payload
from freqinout.core.js8_spotter_status import (
    classify_mcf304_status,
    classify_spotter_status,
    spotter_status_label,
)
from freqinout.core.js8_expect_store import (
    ExpectEvaluationResult,
    ExpectRequestClaimResult,
    claim_expect_request,
    complete_expect_request_claim,
    evaluate_dynamic_flamp_request,
    evaluate_expect_request,
)
from freqinout.core.js8_message_policy import classify_js8_payload, directed_js8_payload
from freqinout.core.logger import log
from freqinout.core.condition_alert_ingest import condition_alert_observations_for_message_intelligence
from freqinout.core.condition_alerts import CONDITION_ALERT_RULES_SETTING_KEY
from freqinout.core.message_intelligence import analyze_spotter_text
from freqinout.core.message_projection_queue import ensure_source_dirty_triggers
from freqinout.core.js8_message_schema import ensure_js8_message_cache_schema
from freqinout.core.js8_receipt_context import api_receive_frequency, finite_number, receive_context
from freqinout.core.js8_pin_review import received_pin_observation
from freqinout.core.observation_projection import observation_from_message_intelligence
from freqinout.core.observation_store import upsert_observation_conn
from freqinout.core.settings_manager import SettingsManager
from freqinout.core.sqlite_utils import connect_sqlite_readonly
from freqinout.core.traffic_actionability import configured_group_names, load_operator_traffic_context
from freqinout.core.varac_bbs_vault import (
    flamp_transfer_index_status,
    index_flamp_transfer_state,
    lookup_flamp_transfer_state,
    parse_dynamic_flamp_query,
)


JS8_MAX_AGE_SECONDS = 30 * 24 * 60 * 60  # 30 days
FLAMP_TRANSFER_INDEX_MAX_AGE_SECONDS = 10 * 60
FLAMP_PARTIAL_SNAPSHOT_MAX_AGE_SECONDS = 10 * 60
DYNAMIC_EXPECT_REQUEST_MAX_AGE_SECONDS = 30 * 60
SPOTTER_STATUS_FORM_ID = "304"  # Kept for compatibility with older tests/callers.
SPOTTER_STATUS_FORMS = {"104", "301", "304", "701B", "701C"}
SPOTTER_PROMPT_RE = re.compile(r"([A-Z0-9]{2})\[(.*?)\]\s*", re.IGNORECASE)
SPOTTER_TOKEN_RE = re.compile(r"\s*#[A-Z0-9]{3,}\s*", re.IGNORECASE)
FIXED_EXPECT_QUERY_RE = re.compile(
    rf"(?:^|\s)E\?\s+(?P<expect_key>F!{FORM_ID_PATTERN})"
    r"(?P<provenance>(?:\s+\*DE\*\s+[A-Z0-9/]+)*)\s*$",
    re.IGNORECASE,
)
FIXED_EXPECT_ADDRESS_PREFIX_RE = re.compile(
    r"^(?:[A-Z0-9/]+\s*:\s*)+(?P<target>@?[A-Z0-9/]+)\s*[>:]?\s*$",
    re.IGNORECASE,
)


def parse_js8_api_utc(value: object) -> tuple[str, float]:
    """Normalize a native JS8 API UTC value to text and epoch seconds.

    Subspace emits epoch milliseconds, while older JS8 API/event sources have
    emitted epoch seconds or ``YYYY-MM-DD HH:MM:SS`` text.  This helper is
    intentionally used only for API events; on-disk JS8 inbox timestamps keep
    their existing file-ingest semantics.
    """

    if value is None or isinstance(value, bool):
        return "", 0.0

    if isinstance(value, (int, float)):
        try:
            numeric = float(value)
        except Exception:
            numeric = 0.0
    else:
        text = str(value or "").strip()
        if not text:
            return "", 0.0
        if re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", text):
            try:
                numeric = float(text)
            except Exception:
                numeric = 0.0
        else:
            try:
                dt_value = datetime.datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=datetime.timezone.utc
                )
            except Exception:
                return "", 0.0
            return dt_value.strftime("%Y-%m-%d %H:%M:%S"), dt_value.timestamp()

    if numeric <= 0:
        return "", 0.0
    timestamp = numeric / 1000.0 if numeric >= 100_000_000_000 else numeric
    try:
        dt_value = datetime.datetime.fromtimestamp(timestamp, tz=datetime.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return "", 0.0
    return dt_value.strftime("%Y-%m-%d %H:%M:%S"), timestamp


class JS8FormDecoder:
    def __init__(self, settings: SettingsManager):
        self.settings = settings
        self._form_cache: Dict[str, List[Dict]] = {}
        self._form_cache_max_entries = 256

    def _prune_form_cache(self) -> None:
        while len(self._form_cache) > self._form_cache_max_entries:
            try:
                self._form_cache.pop(next(iter(self._form_cache)))
            except Exception:
                break

    def decode_form(self, form_id: str, responses: str, comment: str, raw: str = "") -> str:
        form_id = (form_id or "").strip()
        if not form_id:
            return raw or responses
        form = self._load_form_definition(form_id)
        if not form:
            return raw or responses
        prompt_values = {
            key.upper(): value.strip()
            for key, value in SPOTTER_PROMPT_RE.findall(str(comment or ""))
        }
        remaining_comment = SPOTTER_PROMPT_RE.sub("", str(comment or ""))
        remaining_comment = SPOTTER_TOKEN_RE.sub(" ", remaining_comment).strip()
        out_lines: List[str] = []
        resp_idx = 0
        for q in form:
            question = (q.get("q", "") or "").strip()
            prompt_key = str(q.get("prompt_key", "") or "").strip().upper()
            if prompt_key:
                out_lines.append(question)
                out_lines.append(prompt_values.get(prompt_key, "(no response)"))
                out_lines.append("")
                continue
            answers = q.get("ans", {}) or {}
            out_lines.append(question)
            if resp_idx < len(responses):
                code = responses[resp_idx]
                ans = answers.get(code, f"(unknown: {code})")
                out_lines.append(ans)
            else:
                out_lines.append("(no response)")
            resp_idx += 1
            out_lines.append("")
        if remaining_comment:
            out_lines.append("Comment:")
            out_lines.append(remaining_comment)
        return "\n".join(out_lines).strip() or (raw or responses)

    def _load_form_definition(self, form_id: str) -> List[Dict]:
        if form_id in self._form_cache:
            return self._form_cache[form_id]
        forms_dir = resolve_spotter_forms_dir(self.settings.get("js8_forms_path", ""))
        path = forms_dir / f"MCF{form_id}.txt"
        if not path.exists():
            return []
        questions: List[Dict] = []
        current_q = None
        try:
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("."):
                    continue
                if line.startswith("?"):
                    if current_q:
                        questions.append(current_q)
                    current_q = {"q": line[1:].strip(), "ans": {}}
                elif line.startswith("[") and "]" in line:
                    if current_q:
                        questions.append(current_q)
                        current_q = None
                    prompt_key = line[1 : line.find("]")].strip().upper()
                    prompt_text = line[line.find("]") + 1 :].strip()
                    if prompt_key:
                        questions.append(
                            {
                                "q": prompt_text or prompt_key,
                                "prompt_key": prompt_key,
                                "ans": {},
                            }
                        )
                elif line.startswith("@") and current_q:
                    try:
                        key, text = line[1], line[2:].strip()
                        current_q["ans"][key] = text
                    except Exception:
                        continue
            if current_q:
                questions.append(current_q)
        except Exception as e:
            log.debug("MessageIngest: failed to parse form %s: %s", form_id, e)
            questions = []
        self._form_cache[form_id] = questions
        self._prune_form_cache()
        return questions


ExpectDispatchClientFactory = Callable[[str, str], Any]


class MessageIngestor:
    def __init__(
        self,
        settings: SettingsManager,
        *,
        expect_dispatch_client_factory: Optional[ExpectDispatchClientFactory] = None,
        expect_auto_reply_enabled: Optional[bool] = None,
    ):
        self.settings = settings
        self._decoder = JS8FormDecoder(settings)
        self._expect_dispatch_client_factory = expect_dispatch_client_factory
        self._expect_auto_reply_enabled_override = expect_auto_reply_enabled
        self._projection_trigger_tables_ready: set[str] = set()
        # Settings is a stable worker snapshot for one ingest run. Discovering
        # MCF forms is filesystem work, so resolve the enabled status-form set
        # once per ingestor instead of once per traffic row/backfill upsert.
        self._mapped_status_form_ids_cache: frozenset[str] | None = None

    def _ensure_projection_triggers_once(
        self, conn: sqlite3.Connection, table_name: str
    ) -> None:
        table = str(table_name or "").strip()
        if not table or table in self._projection_trigger_tables_ready:
            return
        installed = ensure_source_dirty_triggers(conn)
        if any(f"trg_mip_dirty_{table}_" in name for name in installed):
            self._projection_trigger_tables_ready.add(table)

    def ingest_js8_messages(
        self,
        *,
        inbox_path: Optional[Path] = None,
        source_radio_id: object = "",
        js8_instance_id: object = "",
        source_key: str = "",
    ) -> None:
        inbox_path = inbox_path or self._inbox_path()
        if not inbox_path or not inbox_path.exists():
            return
        self._ensure_local_js8_tables()
        effective_source_key = self._js8_source_key(source_key=source_key, source_radio_id=source_radio_id, js8_instance_id=js8_instance_id)
        max_local_id = max(
            self._local_max_js8_id(source_key=effective_source_key),
            self._js8_ingest_checkpoint(source_key=effective_source_key),
        )
        try:
            with connect_sqlite_readonly(inbox_path, timeout=0.10, busy_timeout_ms=100) as conn:
                cur = conn.cursor()
                queries = [
                    ("inbox_v1", "id, json, type, value"),
                    ("inbox_v1", "rowid as id, json, type, value"),
                    ("inbox_v1", "id, message, type, value"),
                    ("inbox_v1", "id, blob"),
                    ("inbox", "id, json, type, value"),
                    ("inbox", "rowid as id, json, type, value"),
                    ("inbox", "id, message, type, value"),
                ]
                rows = []
                for table, cols in queries:
                    try:
                        cur.execute(f"SELECT {cols} FROM {table} WHERE id > ? ORDER BY 1", (max_local_id,))
                        rows = cur.fetchall()
                        break
                    except Exception:
                        rows = []
        except Exception as e:
            log.debug("MessageIngest: JS8 ingest read failed: %s", e)
            rows = []

        state_map = self._load_js8_state_map(source_key=effective_source_key)
        message_form_codes = self._form_codes_for_flag("messages")
        directed_callsigns, directed_groups = self._directed_js8_recipients()
        now_ts = time.time()
        highest_seen_id = max_local_id
        for row in rows:
            rid = row[0] if len(row) > 0 else 0
            try:
                highest_seen_id = max(highest_seen_id, int(rid or 0))
            except Exception:
                pass
            if rid <= max_local_id:
                continue
            blob = row[1] if len(row) > 1 else ""
            state = row[2] if len(row) > 2 else ""
            js = blob
            try:
                parsed = json.loads(js or "{}")
                if "params" not in parsed and len(row) >= 4:
                    parsed = {
                        "params": parsed,
                        "type": row[2] if len(row) > 2 else "",
                        "value": row[3] if len(row) > 3 else "",
                    }
                params = parsed.get("params", {}) or {}
                if not state:
                    state = parsed.get("type", "") or parsed.get("TYPE", "")
            except Exception:
                params = {}
            text = (params.get("TEXT") or "").strip()
            from_call = (params.get("FROM") or "").strip().upper()
            to_call = (params.get("TO") or "").strip()
            parsed_sender, parsed_dest, _parsed_payload = self._split_directed_js8_text(text)
            if not from_call:
                from_call = parsed_sender
            if not to_call:
                to_call = parsed_dest
            if to_call and (directed_callsigns or directed_groups) and not self._directed_js8_target_matches(
                to_call, directed_callsigns, directed_groups
            ):
                continue
            payload = directed_js8_payload(text)
            try:
                self._enqueue_next_msg_id(
                    from_call,
                    payload,
                    source_key=effective_source_key,
                    source_radio_id=source_radio_id,
                    js8_instance_id=js8_instance_id,
                    source_path=str(inbox_path),
                )
            except Exception:
                pass
            decision = classify_js8_payload(payload)
            if not decision.inbox_visible:
                continue
            text = decision.canonical_text
            form_payload = unwrap_native_js8_form_payload(text)
            if form_payload:
                text = form_payload
            utc_str = (params.get("UTC") or "").strip()
            try:
                utc_ts = datetime.datetime.strptime(utc_str, "%Y-%m-%d %H:%M:%S").timestamp()
            except Exception:
                utc_ts = 0.0
            if utc_ts and (now_ts - utc_ts) > JS8_MAX_AGE_SECONDS:
                continue
            msg_type = "MSG"
            decoded = text
            if normalize_form_code((text.split() or [""])[0]):
                form_part, resp, comment = self._parse_form_parts(text)
                msg_type = f"F!{form_part}" if form_part else "MSG"
                if form_part and not form_id_enabled(form_part, message_form_codes):
                    continue
                decoded = self._decoder.decode_form(form_part, resp, comment, raw=text)
            saved_state = state_map.get(rid)
            if saved_state:
                eff_state = saved_state[0]
                read_ts = saved_state[1]
            else:
                eff_state = (state or "").upper() or "UNREAD"
                read_ts = 0.0
            self._insert_js8_local(
                rid,
                from_call,
                to_call,
                msg_type,
                utc_str,
                utc_ts,
                text,
                decoded,
                eff_state,
                read_ts,
                source_key=effective_source_key,
                source_id=rid,
                source_radio_id=source_radio_id,
                js8_instance_id=js8_instance_id,
                source_path=str(inbox_path),
            )
        self._set_js8_ingest_checkpoint(
            source_key=effective_source_key,
            last_source_id=highest_seen_id,
        )

    def ingest_spotter_from_directed(
        self,
        *,
        directed_path: Optional[Path] = None,
        source_radio_id: object = "",
        js8_instance_id: object = "",
        source_key: object = "",
        offset_key: str = "",
        evaluate_expect: bool = True,
        force_rebuild: bool = False,
    ) -> int:
        directed_path = directed_path or self._resolve_directed_path()
        if not directed_path or not directed_path.exists():
            return 0
        self._ensure_spotter_table()
        self._ensure_local_js8_tables()
        imported = 0
        try:
            offset = int(self.settings.get(offset_key or self._spotter_offset_key(directed_path, source_radio_id), 0) or 0)
        except Exception:
            offset = 0
        if force_rebuild:
            offset = 0
        try:
            size_now = directed_path.stat().st_size
            if offset < 0 or offset > size_now:
                offset = 0
            directed_callsigns, directed_groups = self._directed_js8_recipients()
            with directed_path.open("r", encoding="utf-8", errors="ignore") as fh:
                if offset:
                    fh.seek(offset)
                last_pos = fh.tell()
                while True:
                    line = fh.readline()
                    if not line:
                        break
                    last_pos = fh.tell()
                    fixed_expect = self._parse_fixed_expect_directed_line(line)
                    if fixed_expect:
                        target = str(fixed_expect.get("to_call") or "").strip().upper()
                        if not (directed_callsigns or directed_groups) or self._directed_js8_target_matches(
                            target, directed_callsigns, directed_groups
                        ):
                            if evaluate_expect:
                                self._handle_fixed_expect_query(
                                    fixed_expect,
                                    source_radio_id=source_radio_id,
                                    js8_instance_id=js8_instance_id,
                                )
                        continue
                    dynamic = self._parse_dynamic_directed_line(line)
                    if dynamic:
                        if evaluate_expect:
                            self._handle_dynamic_flamp_query(
                                dynamic,
                                source_radio_id=source_radio_id,
                                js8_instance_id=js8_instance_id,
                                source_key=source_key,
                                source_path=directed_path,
                            )
                        continue
                    parsed = self._parse_directed_spotter_line(line)
                    if parsed:
                        form_id = str(parsed.get("form_id") or "").strip()
                        raw_form = str(parsed.get("raw_form") or "").strip()
                        if not form_id or not raw_form:
                            continue
                        from_call = str(parsed.get("from_call") or "").strip().upper()
                        token = str(parsed.get("spotter_token") or "").strip().upper()
                        if not from_call:
                            continue
                        if self._spotter_exists(
                            from_call,
                            form_id,
                            token,
                            raw_form,
                            source_radio_id=source_radio_id,
                            js8_instance_id=js8_instance_id,
                            source_key=source_key,
                        ):
                            continue
                        form_part, resp, comment = self._parse_form_parts(raw_form)
                        decoded = self._decoder.decode_form(form_part, resp, comment, raw=raw_form)
                        if not decoded or decoded == raw_form:
                            decoded = decode_spotter_form_text(raw_form)
                        db_path = self._db_path()
                        if not db_path:
                            continue
                        conn = sqlite3.connect(db_path)
                        cur = conn.cursor()
                        ingested_ts = float(time.time())
                        cur.execute(
                            """
                            INSERT INTO spotter_traffic
                                (utc_ts, utc_str, from_call, to_call, form_id, spotter_token,
                                 raw_text, decoded_text, state, read_ts, relay_via,
                                 source_radio_id, js8_instance_id, source_key, ingested_ts)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'UNREAD', 0, ?, ?, ?, ?, ?)
                            """,
                            (
                                float(parsed.get("utc_ts") or 0.0),
                                str(parsed.get("utc_str") or ""),
                                from_call,
                                str(parsed.get("to_call") or "").strip().upper(),
                                form_id,
                                token,
                                raw_form,
                                decoded or raw_form,
                                str(parsed.get("relay_via") or "").strip().upper(),
                                str(source_radio_id or ""),
                                str(js8_instance_id or ""),
                                str(source_key or ""),
                                ingested_ts,
                            ),
                        )
                        imported_id = int(cur.lastrowid or 0)
                        self._upsert_spotter_station_status(
                            cur,
                            from_call=from_call,
                            form_id=form_id,
                            response_code=resp,
                            raw_form=raw_form,
                            utc_ts=float(parsed.get("utc_ts") or 0.0),
                            utc_str=str(parsed.get("utc_str") or ""),
                            ingested_ts=ingested_ts,
                        )
                        self._mirror_spotter_observation(
                            conn,
                            imported_id=imported_id,
                            raw_form=raw_form,
                            form_id=form_id,
                            from_call=from_call,
                            to_call=str(parsed.get("to_call") or "").strip().upper(),
                            utc_str=str(parsed.get("utc_str") or ""),
                            source_radio_id=source_radio_id,
                            js8_instance_id=js8_instance_id,
                            source_kind="directed",
                        )
                        conn.commit()
                        conn.close()
                        imported += 1
                        continue
                    message_row = self._parse_directed_js8_message_line(
                        line,
                        directed_callsigns=directed_callsigns,
                        directed_groups=directed_groups,
                        source_radio_id=source_radio_id,
                        js8_instance_id=js8_instance_id,
                        source_key=source_key,
                        source_path=directed_path,
                    )
                    if not message_row:
                        continue
                    if self._store_directed_js8_message(message_row):
                        imported += 1
                self.settings.set(offset_key or self._spotter_offset_key(directed_path, source_radio_id), int(last_pos))
                if hasattr(self.settings, "save"):
                    self.settings.save()
        except Exception as e:
            log.debug("MessageIngest: spotter ingest failed reading DIRECTED.TXT: %s", e)
        return imported

    def ingest_dynamic_flamp_from_directed(
        self,
        *,
        directed_path: Path,
        source_radio_id: object,
        js8_instance_id: object,
        source_key: object = "",
        offset_key: str,
        fallback_offset_key: str = "",
    ) -> int:
        """Tail only dynamic FLAMP requests from a directed-message file.

        This path deliberately avoids general message projection and schema
        work so unattended queries can be noticed at a short cadence without
        making the rest of message ingestion expensive.
        """
        path = Path(directed_path).expanduser()
        if not path.exists():
            return 0
        raw_offset = self.settings.get(offset_key, None)
        if raw_offset is None and fallback_offset_key:
            raw_offset = self.settings.get(fallback_offset_key, 0)
        try:
            offset = int(raw_offset or 0)
        except Exception:
            offset = 0
        try:
            size_now = path.stat().st_size
            if offset < 0 or offset > size_now:
                offset = 0
            recognized = 0
            with path.open("r", encoding="utf-8", errors="ignore") as fh:
                if offset:
                    fh.seek(offset)
                last_pos = fh.tell()
                while True:
                    line = fh.readline()
                    if not line:
                        break
                    # Do not checkpoint an in-progress append. JS8Call records
                    # are newline-delimited; the next poll can read it whole.
                    if not line.endswith(("\n", "\r")) and fh.tell() >= size_now:
                        break
                    last_pos = fh.tell()
                    parsed = self._parse_dynamic_directed_line(line)
                    if parsed is None:
                        continue
                    recognized += 1
                    self._handle_dynamic_flamp_query(
                        parsed,
                        source_radio_id=source_radio_id,
                        js8_instance_id=js8_instance_id,
                        source_key=source_key,
                        source_path=path,
                    )
            self.settings.set(offset_key, last_pos)
            if hasattr(self.settings, "save"):
                self.settings.save()
            return recognized
        except Exception as exc:
            log.warning(
                "MessageIngest: dynamic FLAMP directed tail failed source=%s path=%s: %s",
                str(source_key or js8_instance_id or source_radio_id or "unknown"),
                path,
                exc,
            )
            return 0

    def ingest_spotter_from_js8_events(
        self,
        messages: Iterable[Dict[str, Any]],
        *,
        source_radio_id: object = "",
        js8_instance_id: object = "",
        source_key: object = "",
        evaluate_expect: bool = True,
    ) -> int:
        self._ensure_spotter_table()
        self._ensure_local_js8_tables()
        imported = 0
        directed_callsigns, directed_groups = self._directed_js8_recipients()
        for event in list(messages or []):
            fixed_expect = self._parse_fixed_expect_js8_event(event)
            if fixed_expect:
                target = str(fixed_expect.get("to_call") or "").strip().upper()
                if not (directed_callsigns or directed_groups) or self._directed_js8_target_matches(
                    target, directed_callsigns, directed_groups
                ):
                    if evaluate_expect:
                        self._handle_fixed_expect_query(
                            fixed_expect,
                            source_radio_id=source_radio_id,
                            js8_instance_id=js8_instance_id,
                        )
                continue
            dynamic = self._parse_dynamic_js8_event(event)
            if dynamic:
                if evaluate_expect:
                    self._handle_dynamic_flamp_query(
                        dynamic,
                        source_radio_id=source_radio_id,
                        js8_instance_id=js8_instance_id,
                        source_key=source_key,
                        source_path=None,
                    )
                continue
            parsed = self._parse_js8_spotter_event(event)
            if parsed:
                form_id = str(parsed.get("form_id") or "").strip()
                raw_form = str(parsed.get("raw_form") or "").strip()
                from_call = str(parsed.get("from_call") or "").strip().upper()
                token = str(parsed.get("spotter_token") or "").strip().upper()
                if not form_id or not raw_form or not from_call:
                    continue
                if self._spotter_exists(
                    from_call,
                    form_id,
                    token,
                    raw_form,
                    source_radio_id=source_radio_id,
                    js8_instance_id=js8_instance_id,
                    source_key=source_key,
                ):
                    continue
                form_part, resp, comment = self._parse_form_parts(raw_form)
                decoded = self._decoder.decode_form(form_part, resp, comment, raw=raw_form)
                if not decoded or decoded == raw_form:
                    decoded = decode_spotter_form_text(raw_form)
                db_path = self._db_path()
                if not db_path:
                    continue
                ingested_ts = float(time.time())
                try:
                    conn = sqlite3.connect(db_path)
                    cur = conn.cursor()
                    cur.execute(
                        """
                        INSERT INTO spotter_traffic
                            (utc_ts, utc_str, from_call, to_call, form_id, spotter_token,
                             raw_text, decoded_text, state, read_ts, relay_via,
                             source_radio_id, js8_instance_id, source_key, ingested_ts)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'UNREAD', 0, ?, ?, ?, ?, ?)
                        """,
                        (
                            float(parsed.get("utc_ts") or 0.0),
                            str(parsed.get("utc_str") or ""),
                            from_call,
                            str(parsed.get("to_call") or "").strip().upper(),
                            form_id,
                            token,
                            raw_form,
                            decoded or raw_form,
                            str(parsed.get("relay_via") or "").strip().upper(),
                            str(source_radio_id or ""),
                            str(js8_instance_id or ""),
                            str(source_key or ""),
                            ingested_ts,
                        ),
                    )
                    imported_id = int(cur.lastrowid or 0)
                    self._upsert_spotter_station_status(
                        cur,
                        from_call=from_call,
                        form_id=form_id,
                        response_code=resp,
                        raw_form=raw_form,
                        utc_ts=float(parsed.get("utc_ts") or 0.0),
                        utc_str=str(parsed.get("utc_str") or ""),
                        ingested_ts=ingested_ts,
                    )
                    self._mirror_spotter_observation(
                        conn,
                        imported_id=imported_id,
                        raw_form=raw_form,
                        form_id=form_id,
                        from_call=from_call,
                        to_call=str(parsed.get("to_call") or "").strip().upper(),
                        utc_str=str(parsed.get("utc_str") or ""),
                        source_radio_id=source_radio_id,
                        js8_instance_id=js8_instance_id,
                        source_kind="js8-api",
                    )
                    conn.commit()
                    conn.close()
                except Exception as exc:
                    log.debug("MessageIngest: JS8 event Spotter insert failed: %s", exc)
                    continue
                imported += 1
                continue
            message_row = self._parse_directed_js8_message_event(
                event,
                directed_callsigns=directed_callsigns,
                directed_groups=directed_groups,
                source_radio_id=source_radio_id,
                js8_instance_id=js8_instance_id,
                source_key=source_key,
            )
            if not message_row:
                continue
            if self._store_directed_js8_message(message_row):
                imported += 1
        return imported

    def _expect_auto_reply_runtime_enabled(self) -> bool:
        if self._expect_auto_reply_enabled_override is not None:
            enabled = bool(self._expect_auto_reply_enabled_override)
        else:
            try:
                enabled = bool(self.settings.get("js8_expect_unattended_auto_reply_enabled", False))
            except Exception:
                enabled = False
        try:
            paused = bool(self.settings.get("js8_expect_unattended_auto_reply_paused", False))
        except Exception:
            paused = False
        return bool(enabled and not paused)

    def _mirror_spotter_observation(
        self,
        conn: sqlite3.Connection,
        *,
        imported_id: int,
        raw_form: str,
        form_id: str,
        from_call: str,
        to_call: str,
        utc_str: str,
        source_radio_id: object,
        js8_instance_id: object,
        source_kind: str,
    ) -> None:
        try:
            info = analyze_spotter_text(
                raw_form,
                form_name=f"MCF{str(form_id or '').strip()}",
                from_call=from_call,
                to_call=to_call,
            )
            observation = observation_from_message_intelligence(
                info,
                source_ref=f"spotter_traffic:{int(imported_id or 0)}",
                source_family="spotter",
                source_radio_id=self._int_or_none(source_radio_id),
                source_app=str(js8_instance_id or "").strip(),
                received_utc=utc_str,
                event_utc=utc_str,
                status="UNREAD",
                extra_provenance={
                    "ingest_source": source_kind,
                    "js8_instance_id": str(js8_instance_id or "").strip(),
                },
            )
            upsert_observation_conn(conn, observation)
            for alert_observation in condition_alert_observations_for_message_intelligence(
                info,
                self.settings.get(CONDITION_ALERT_RULES_SETTING_KEY, None),
                source_ref=f"spotter_traffic:{int(imported_id or 0)}",
                source_family="JS8Spotter",
                source_radio_id=self._int_or_none(source_radio_id),
                source_app=str(js8_instance_id or "").strip(),
                received_utc=utc_str,
            ):
                upsert_observation_conn(conn, alert_observation)
        except Exception as exc:
            log.debug("MessageIngest: observation projection mirror failed: %s", exc)

    @staticmethod
    def _int_or_none(value: object) -> int | None:
        try:
            return int(value) if str(value or "").strip() else None
        except Exception:
            return None

    def _maybe_dispatch_expect_auto_reply(
        self,
        evaluation: ExpectEvaluationResult,
        *,
        event_id: str,
        source_radio_id: object = "",
        source_js8_instance_id: object = "",
        requesting_callsign: object = "",
        target_group: object = "",
        db_path: Optional[Path] = None,
        claim_event_key: str = "",
        claim_q_id: str = "",
        relay_path: object = "",
    ) -> None:
        if evaluation.decision != "reply-ready":
            return
        if not self._expect_auto_reply_runtime_enabled():
            return
        claim: Optional[ExpectRequestClaimResult] = None
        if claim_event_key:
            claim = claim_expect_request(
                event_key=claim_event_key,
                expect_entry_id=int(evaluation.expect_entry_id or 0),
                q_id=str(claim_q_id or evaluation.q_id or evaluation.expect_key or ""),
                source_radio_id=source_radio_id,
                source_js8_instance_id=source_js8_instance_id,
                requesting_callsign=requesting_callsign,
                target_group=target_group,
                max_replies=int(evaluation.max_replies or 1),
                cooldown_seconds=int(evaluation.cooldown_seconds or 0),
                db_path=db_path,
            )
            if not claim.acquired:
                record_expect_dispatch_hold(
                    evaluation=evaluation,
                    reason=claim.reason,
                    event_id=event_id,
                    source_radio_id=source_radio_id,
                    source_js8_instance_id=source_js8_instance_id,
                    requesting_callsign=requesting_callsign,
                    target_group=target_group,
                    db_path=db_path,
                )
                return
        client_factory = self._expect_dispatch_client_factory
        if client_factory is None:
            if claim is not None:
                complete_expect_request_claim(
                    event_key=claim.event_key,
                    status="held",
                    reason="No JS8 client factory is configured for Expect auto-reply.",
                    db_path=db_path,
                )
            record_expect_dispatch_hold(
                evaluation=evaluation,
                reason="No JS8 client factory is configured for Expect auto-reply.",
                event_id=event_id,
                source_radio_id=source_radio_id,
                source_js8_instance_id=source_js8_instance_id,
                requesting_callsign=requesting_callsign,
                target_group=target_group,
                db_path=db_path,
            )
            log.debug("MessageIngest: Expect auto-reply runtime enabled, but no JS8 client factory is configured.")
            return
        reply_radio_id = str(evaluation.reply_radio_id or source_radio_id or "")
        reply_js8_instance_id = str(evaluation.reply_js8_instance_id or source_js8_instance_id or "")
        try:
            client = client_factory(reply_radio_id, reply_js8_instance_id)
            if client is None:
                reason = "No JS8 client is available for this Expect auto-reply source."
                source_owner = getattr(client_factory, "__self__", None)
                status = getattr(source_owner, "last_status", None)
                status_reason = str(getattr(status, "reason", "") or "").strip()
                if status_reason:
                    reason = status_reason
                if claim is not None:
                    complete_expect_request_claim(
                        event_key=claim.event_key,
                        status="held",
                        reason=reason,
                        db_path=db_path,
                    )
                record_expect_dispatch_hold(
                    evaluation=evaluation,
                    reason=reason,
                    event_id=event_id,
                    source_radio_id=source_radio_id,
                    source_js8_instance_id=source_js8_instance_id,
                    requesting_callsign=requesting_callsign,
                    target_group=target_group,
                    db_path=db_path,
                )
                log.debug("MessageIngest: Expect auto-reply client factory returned no client for radio=%s js8=%s.", reply_radio_id, reply_js8_instance_id)
                return
            dispatch_result = dispatch_expect_auto_reply(
                evaluation=evaluation,
                client=client,
                runtime_unattended_enabled=True,
                event_id=event_id,
                source_radio_id=source_radio_id,
                source_js8_instance_id=source_js8_instance_id,
                requesting_callsign=requesting_callsign,
                target_group=target_group,
                db_path=db_path,
                claim_event_key=claim_event_key,
                claim_q_id=claim_q_id,
                claim_already_acquired=claim is not None,
                relay_path=relay_path,
            )
            log.info(
                "FIO Spotter Expect: dispatch decision=%s key=%s from=%s radio=%s js8=%s reason=%s",
                dispatch_result.decision,
                str(evaluation.expect_key or ""),
                str(requesting_callsign or ""),
                reply_radio_id,
                reply_js8_instance_id,
                dispatch_result.reason,
            )
        except Exception as exc:
            if claim is not None:
                complete_expect_request_claim(
                    event_key=claim.event_key,
                    status="failed",
                    reason=str(exc),
                    db_path=db_path,
                    retry_after_seconds=30,
                )
            log.warning("MessageIngest: Expect auto-reply dispatch failed for %s: %s", event_id, exc)

    def _db_path(self) -> Path | None:
        try:
            return get_config_dir() / "config" / "freqinout_nets.db"
        except Exception as e:
            log.debug("MessageIngest: failed to resolve DB path: %s", e)
            return None

    def _backlog_db_path(self) -> Path | None:
        return self._db_path()

    def _ensure_backlog_table(self) -> None:
        db_path = self._backlog_db_path()
        if not db_path:
            return
        conn = None
        try:
            resolved = str(Path(db_path).resolve())
            try:
                stat = Path(db_path).stat()
                path_key = (resolved, stat.st_dev, stat.st_ino)
            except FileNotFoundError:
                path_key = (resolved, None, None)
            if path_key[1] is not None and getattr(self, "_backlog_schema_ready_path", None) == path_key:
                return
            conn = sqlite3.connect(db_path)
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(autoquery_backlog)")}
            if not columns:
                conn.execute("""CREATE TABLE IF NOT EXISTS autoquery_backlog (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, callsign TEXT NOT NULL,
                    msg_id TEXT, kind TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING',
                    attempts INTEGER DEFAULT 0, last_attempt_ts REAL, created_ts REAL
                )""")
            for column in ("source_key", "source_radio_id", "js8_instance_id", "source_path"):
                if column not in columns:
                    conn.execute(f"ALTER TABLE autoquery_backlog ADD COLUMN {column} TEXT")
            conn.commit()
            stat = Path(db_path).stat()
            after_key = (resolved, stat.st_dev, stat.st_ino)
            # Never stamp the new path's inode with assurance of an old open
            # connection. Fresh creation also gets one confirming next probe.
            self._backlog_schema_ready_path = after_key if after_key == path_key else None
        except Exception as e:
            log.debug("MessageIngest: failed to ensure backlog table: %s", e)
        finally:
            if conn is not None:
                conn.close()

    def _enqueue_next_msg_id(
        self,
        from_call: str,
        text: str,
        *,
        source_key: object = "",
        source_radio_id: object = "",
        js8_instance_id: object = "",
        source_path: object = "",
    ) -> None:
        call = (from_call or "").strip().upper()
        if not call or not text:
            return
        m = re.search(r"NEXT\s+MSG\s+ID\s+(\d+)", text.upper())
        if not m:
            return
        next_id = m.group(1)
        if not next_id:
            return
        self._ensure_backlog_table()
        db_path = self._backlog_db_path()
        if not db_path:
            return
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute(
                """
                SELECT 1 FROM autoquery_backlog
                WHERE callsign=? AND COALESCE(msg_id,'')=COALESCE(?, '') AND kind='MSG'
                  AND COALESCE(source_key, '')=COALESCE(?, '')
                LIMIT 1
                """,
                (call, next_id, str(source_key or "").strip()),
            )
            if cur.fetchone():
                conn.close()
                return
            now_ts = time.time()
            cur.execute(
                """
                INSERT INTO autoquery_backlog
                    (callsign, msg_id, kind, status, attempts, last_attempt_ts, created_ts,
                     source_key, source_radio_id, js8_instance_id, source_path)
                VALUES (?, ?, 'MSG', 'PENDING', 0, ?, ?, ?, ?, ?, ?)
                """,
                (
                    call,
                    next_id,
                    now_ts,
                    now_ts,
                    str(source_key or "").strip(),
                    str(source_radio_id or "").strip(),
                    str(js8_instance_id or "").strip(),
                    str(source_path or "").strip(),
                ),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            log.debug("MessageIngest: failed to enqueue NEXT MSG ID: %s", e)

    def _spotter_offset_key(self, directed_path: Optional[Path] = None, source_radio_id: object = "") -> str:
        source = str(source_radio_id or "").strip()
        if source:
            return f"spotter_directed_offset_radio_{source}"
        if directed_path is not None:
            try:
                key_src = str(directed_path.expanduser().resolve())
            except Exception:
                key_src = str(directed_path)
            digest = hashlib.sha1(key_src.encode("utf-8", errors="replace")).hexdigest()[:16]
            return f"spotter_directed_offset_path_{digest}"
        return "spotter_directed_offset"

    def _resolve_directed_path(self) -> Optional[Path]:
        directed = (self.settings.get("js8_directed_path", "") or "").strip()
        if not directed:
            return None
        return Path(directed)

    def _spotter_exists(
        self,
        from_call: str,
        form_id: str,
        token: str,
        raw_text: str,
        *,
        source_radio_id: object = "",
        js8_instance_id: object = "",
        source_key: object = "",
    ) -> bool:
        db_path = self._db_path()
        if not db_path:
            return False
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            source_radio = str(source_radio_id or "").strip()
            source_js8 = str(js8_instance_id or "").strip()
            source_identity = str(source_key or "").strip()
            source_clause = ""
            source_params: tuple[object, ...] = ()
            if source_identity:
                source_clause = " AND COALESCE(source_key, '')=?"
                source_params = (source_identity,)
            elif source_radio or source_js8:
                source_clause = " AND COALESCE(source_radio_id, '')=? AND LOWER(COALESCE(js8_instance_id, ''))=LOWER(?)"
                source_params = (source_radio, source_js8)
            if token:
                cur.execute(
                    f"""
                    SELECT 1 FROM spotter_traffic
                    WHERE from_call=? AND form_id=? AND spotter_token=?
                    {source_clause}
                    LIMIT 1
                    """,
                    (from_call, form_id, token) + source_params,
                )
            else:
                cur.execute(
                    f"""
                    SELECT 1 FROM spotter_traffic
                    WHERE from_call=? AND form_id=? AND raw_text=?
                    {source_clause}
                    LIMIT 1
                    """,
                    (from_call, form_id, raw_text) + source_params,
                )
            exists = cur.fetchone() is not None
            conn.close()
            return exists
        except Exception:
            return False

    @staticmethod
    def _fixed_expect_match(text: object) -> Optional[re.Match[str]]:
        cleaned = str(text or "").strip()
        if cleaned.endswith("\u2662"):
            cleaned = cleaned[:-1].rstrip()
        return FIXED_EXPECT_QUERY_RE.search(cleaned)

    @staticmethod
    def _expect_address_prefix(text: str, end: int) -> tuple[str, str]:
        prefix = str(text or "")[:end].strip()
        match = FIXED_EXPECT_ADDRESS_PREFIX_RE.fullmatch(prefix)
        if match is None:
            return "", ""
        sender_match = re.match(r"\s*([A-Z0-9/]+)\s*:", prefix, flags=re.IGNORECASE)
        sender = sender_match.group(1).upper() if sender_match else ""
        return sender, match.group("target").upper()

    @staticmethod
    def _fixed_expect_identity(
        *, immediate_sender: object, provenance_text: object
    ) -> tuple[str, str]:
        immediate = str(immediate_sender or "").strip().upper().lstrip("@")
        provenance = [
            value.upper()
            for value in re.findall(
                r"\*DE\*\s*([A-Z0-9/]+)",
                str(provenance_text or ""),
                re.IGNORECASE,
            )
        ]
        origin = provenance[0] if provenance else immediate
        hops: list[str] = []
        if provenance and immediate and immediate != origin:
            hops.append(immediate)
        for hop in reversed(provenance[1:]):
            if hop != origin and hop not in hops:
                hops.append(hop)
        return origin, ">".join(hops)

    def _parse_fixed_expect_directed_line(self, line: str) -> Optional[Dict[str, Any]]:
        if not line or not line.rstrip().endswith("\u2662"):
            return None
        parts = [part for part in line.strip().split("\t") if part]
        if len(parts) < 5:
            parts = re.split(r"\s+", line.strip(), maxsplit=4)
        if len(parts) < 5:
            return None
        try:
            timestamp = datetime.datetime.strptime(str(parts[0])[:19], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=datetime.timezone.utc
            )
        except Exception:
            return None
        message = str(parts[4] or "").strip()
        match = self._fixed_expect_match(message)
        if match is None:
            return None
        immediate, target = self._expect_address_prefix(message, match.start())
        origin, relay_path = self._fixed_expect_identity(
            immediate_sender=immediate,
            provenance_text=match.group("provenance"),
        )
        if not origin or not target:
            return None
        return {
            "expect_key": normalize_form_code(match.group("expect_key")),
            "from_call": origin,
            "to_call": target,
            "relay_path": relay_path,
            "utc_ts": timestamp.timestamp(),
            "event_seed": message,
        }

    def _parse_fixed_expect_js8_event(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not isinstance(event, dict):
            return None
        event_type = str(event.get("type") or "").strip().upper()
        if event_type and event_type != "RX.DIRECTED":
            return None
        params = event.get("params") if isinstance(event.get("params"), dict) else {}
        text = ""
        match: Optional[re.Match[str]] = None
        for candidate in (params.get("TEXT"), event.get("value")):
            candidate_text = str(candidate or "").strip()
            candidate_match = self._fixed_expect_match(candidate_text)
            if candidate_match is not None:
                text = candidate_text
                match = candidate_match
                break
        if match is None:
            return None
        immediate = str(params.get("FROM") or "").strip().upper()
        target = str(params.get("TO") or "").strip().upper()
        prefix = text[: match.start()].strip()
        if prefix:
            prefix_sender, prefix_target = self._expect_address_prefix(text, match.start())
            if not prefix_target:
                bare_target = re.fullmatch(r"(@?[A-Z0-9/]+)\s*[>:]?", prefix, flags=re.IGNORECASE)
                prefix_target = bare_target.group(1).upper() if bare_target else ""
            if not prefix_target or (target and prefix_target != target):
                return None
            if not immediate:
                immediate = prefix_sender
            if not target:
                target = prefix_target
        origin, relay_path = self._fixed_expect_identity(
            immediate_sender=immediate,
            provenance_text=match.group("provenance"),
        )
        if not origin or not target:
            return None
        _utc_text, utc_ts = parse_js8_api_utc(params.get("UTC") or event.get("time"))
        if utc_ts <= 0:
            utc_ts = time.time()
        stable = str(params.get("ID") or params.get("MSG_ID") or event.get("id") or "").strip()
        return {
            "expect_key": normalize_form_code(match.group("expect_key")),
            "from_call": origin,
            "to_call": target,
            "relay_path": relay_path,
            "utc_ts": utc_ts,
            "event_seed": stable or text,
        }

    def _fixed_expect_hold(
        self,
        *,
        expect_key: str,
        reason: str,
        event_id: str,
        source_radio_id: object,
        js8_instance_id: object,
        from_call: str,
        target: str,
        db_path: Optional[Path],
    ) -> None:
        record_expect_dispatch_hold(
            evaluation=ExpectEvaluationResult(decision="held", reason=reason, expect_key=expect_key),
            reason=reason,
            event_id=event_id,
            source_radio_id=source_radio_id,
            source_js8_instance_id=js8_instance_id,
            requesting_callsign=from_call,
            target_group=target if target.startswith("@") else "",
            db_path=db_path,
        )

    def _handle_fixed_expect_query(
        self,
        parsed: Mapping[str, Any],
        *,
        source_radio_id: object,
        js8_instance_id: object,
    ) -> None:
        expect_key = str(parsed.get("expect_key") or "").strip().upper()
        from_call = str(parsed.get("from_call") or "").strip().upper()
        target = str(parsed.get("to_call") or "").strip().upper()
        relay_path = str(parsed.get("relay_path") or "").strip().upper()
        received_ts = float(parsed.get("utc_ts") or time.time())
        event_id = (
            "fixed-expect:"
            f"{str(source_radio_id or '').strip()}:{str(js8_instance_id or '').strip().lower()}:"
            f"{int(received_ts)}:{from_call}:{target}:{expect_key}"
        )
        db_path = self._db_path()
        if not expect_key or not from_call or not target:
            return
        request_age = time.time() - received_ts
        if request_age > DYNAMIC_EXPECT_REQUEST_MAX_AGE_SECONDS or request_age < -120:
            self._fixed_expect_hold(
                expect_key=expect_key,
                reason="Fixed Expect request is outside the safe live-request window.",
                event_id=event_id,
                source_radio_id=source_radio_id,
                js8_instance_id=js8_instance_id,
                from_call=from_call,
                target=target,
                db_path=db_path,
            )
            return
        if not str(source_radio_id or "").strip() or not str(js8_instance_id or "").strip():
            self._fixed_expect_hold(
                expect_key=expect_key,
                reason="Fixed Expect request is missing a concrete source radio/JS8 instance.",
                event_id=event_id,
                source_radio_id=source_radio_id,
                js8_instance_id=js8_instance_id,
                from_call=from_call,
                target=target,
                db_path=db_path,
            )
            return
        evaluation = evaluate_expect_request(
            expect_key=expect_key,
            requesting_callsign=from_call,
            target_group=target if target.startswith("@") else "",
            source_radio_id=source_radio_id,
            js8_instance_id=js8_instance_id,
            event_id=event_id,
            db_path=db_path,
        )
        self._maybe_dispatch_expect_auto_reply(
            evaluation,
            event_id=event_id,
            source_radio_id=source_radio_id,
            source_js8_instance_id=js8_instance_id,
            requesting_callsign=from_call,
            target_group=target if target.startswith("@") else "",
            db_path=db_path,
            claim_event_key=event_id,
            claim_q_id=expect_key,
            relay_path=relay_path,
        )

    def _parse_dynamic_directed_line(self, line: str) -> Optional[Dict[str, Any]]:
        if not line or not line.rstrip().endswith("\u2662"):
            return None
        parts = [part for part in line.strip().split("\t") if part]
        if len(parts) < 5:
            parts = re.split(r"\s+", line.strip(), maxsplit=4)
        if len(parts) < 5 or ":" not in parts[4]:
            return None
        try:
            timestamp = datetime.datetime.strptime(str(parts[0])[:19], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=datetime.timezone.utc
            )
        except Exception:
            return None
        relay_via, rest = str(parts[4]).split(":", 1)
        rest = rest.strip()
        words = rest.split(None, 1)
        if len(words) != 2:
            return None
        target = words[0].split(">", 1)[0].strip(" ,").upper()
        query = parse_dynamic_flamp_query(words[1].rstrip("\u2662").strip())
        if not target or query is None:
            return None
        text_upper = str(parts[4]).upper()
        relayed = bool(re.search(r"\*DE\*\s*[A-Z0-9/]+", text_upper))
        de_match = re.search(r"\*DE\*\s*([A-Z0-9/]+)", text_upper)
        sender = (de_match.group(1) if de_match else relay_via).strip().upper()
        event_id = "directed-q:" + hashlib.sha256(line.encode("utf-8", errors="replace")).hexdigest()
        return {
            "q_id": query.q_id,
            "confidence": query.confidence,
            "from_call": sender,
            "to_call": target,
            "relayed": relayed,
            "event_id": event_id,
            "utc_ts": timestamp.timestamp(),
        }

    def _parse_dynamic_js8_event(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not isinstance(event, dict):
            return None
        params = event.get("params") if isinstance(event.get("params"), dict) else {}
        text = str(params.get("TEXT") or event.get("value") or "").strip()
        query = parse_dynamic_flamp_query(text)
        sender = str(params.get("FROM") or "").strip().upper()
        target = str(params.get("TO") or "").strip().upper()
        if not sender or not target:
            return None
        # JS8Call builds differ: some RX.DIRECTED events expose only the
        # payload, while others retain the addressed prefix in TEXT.  Accept
        # the latter only when that prefix exactly matches API TO.
        if query is None:
            target_token = target
            prefixed = re.fullmatch(
                rf"\s*{re.escape(target_token)}\s*[>:]?\s+(E\?\s+Q\s*[0-9A-F]{{4}})\s*",
                text,
                flags=re.IGNORECASE,
            )
            query = parse_dynamic_flamp_query(prefixed.group(1) if prefixed else "")
        if query is None:
            return None
        relayed = bool(re.search(r"\*DE\*\s*[A-Z0-9/]+", text.upper()))
        stable = str(params.get("ID") or params.get("MSG_ID") or event.get("id") or "").strip()
        if not stable:
            stable = hashlib.sha256(
                json.dumps(
                    {"from": sender, "to": target, "text": text, "utc": params.get("UTC") or event.get("time")},
                    sort_keys=True,
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
        _utc_str, utc_ts = parse_js8_api_utc(params.get("UTC") or event.get("time"))
        if utc_ts <= 0:
            utc_ts = time.time()
        return {
            "q_id": query.q_id,
            "confidence": query.confidence,
            "from_call": sender,
            "to_call": target,
            "relayed": relayed,
            "event_id": f"js8-api-q:{stable}",
            "utc_ts": utc_ts,
        }

    def _flamp_dynamic_enabled(self) -> bool:
        try:
            # The existing unattended-Expect runtime flag remains the safety
            # gate.  The dynamic service is a separate, explicit opt-in so an
            # upgraded station cannot begin answering Q traffic silently.
            return bool(self.settings.get("js8_expect_dynamic_flamp_enabled", False))
        except Exception:
            return False

    def _flamp_relay_dir_for_source(self, source_radio_id: object, js8_instance_id: object) -> str:
        try:
            raw = self.settings.get("varac_bbs_vault_flamp_relay_dir", "")
        except Exception:
            raw = ""
        if isinstance(raw, dict):
            keys = (
                str(source_radio_id or "").strip(),
                str(js8_instance_id or "").strip(),
                f"{str(source_radio_id or '').strip()}:{str(js8_instance_id or '').strip()}",
                "default",
            )
            for key in keys:
                if key and str(raw.get(key, "") or "").strip():
                    return str(raw[key]).strip()
            return ""
        return str(raw or "").strip()

    def _flamp_receive_dir_for_source(self) -> str:
        """Return this radio profile's configured FLAMP completed-RX root."""

        try:
            paths = self.settings.get("message_paths", {}) or {}
        except Exception:
            paths = {}
        if not isinstance(paths, Mapping):
            return ""
        raw = str(paths.get("flamp", "") or "").strip()
        if not raw:
            return ""
        path = Path(raw).expanduser()
        if path.is_file():
            path = path.parent
        if path.name.lower() == "flamp" and (path / "rx").is_dir():
            path = path / "rx"
        return str(path)

    def refresh_dynamic_flamp_state(
        self, *, source_radio_id: object, js8_instance_id: object
    ) -> int:
        """Refresh the source-scoped FLAMP projection outside the RF request path."""

        db_path = self._db_path()
        if db_path is None:
            return 0
        return index_flamp_transfer_state(
            self._flamp_relay_dir_for_source(source_radio_id, js8_instance_id),
            db_path=db_path,
            source_radio_id=source_radio_id,
            source_js8_instance_id=js8_instance_id,
            receive_dir=self._flamp_receive_dir_for_source(),
        )

    def _dynamic_flamp_hold(
        self,
        *,
        q_id: str,
        reason: str,
        event_id: str,
        source_radio_id: object,
        js8_instance_id: object,
        from_call: str,
        target: str,
        db_path: Optional[Path],
    ) -> None:
        from freqinout.core.js8_expect_dispatcher import record_expect_dispatch_hold

        evaluation = ExpectEvaluationResult(
            decision="held",
            reason=reason,
            expect_key=f"Q {q_id}",
            q_id=q_id,
        )
        record_expect_dispatch_hold(
            evaluation=evaluation,
            reason=reason,
            event_id=event_id,
            source_radio_id=source_radio_id,
            source_js8_instance_id=js8_instance_id,
            requesting_callsign=from_call,
            target_group=target,
            db_path=db_path,
        )
        log.info(
            "FIO Spotter Expect: held dynamic FLAMP request q=%s from=%s target=%s radio=%s js8=%s reason=%s",
            q_id,
            from_call,
            target,
            str(source_radio_id or ""),
            str(js8_instance_id or ""),
            reason,
        )

    def _handle_dynamic_flamp_query(
        self,
        parsed: Mapping[str, Any],
        *,
        source_radio_id: object,
        js8_instance_id: object,
        source_key: object,
        source_path: Optional[Path],
    ) -> None:
        q_id = str(parsed.get("q_id") or "").strip().upper()
        event_id = str(parsed.get("event_id") or "").strip()
        from_call = str(parsed.get("from_call") or "").strip().upper()
        target = str(parsed.get("to_call") or "").strip().upper()
        db_path = self._db_path()
        if not q_id or not event_id or not from_call or not target:
            return
        log.info(
            "FIO Spotter Expect: received dynamic FLAMP request q=%s from=%s target=%s radio=%s js8=%s",
            q_id,
            from_call,
            target,
            str(source_radio_id or ""),
            str(js8_instance_id or ""),
        )
        event_id = f"{event_id}|radio={str(source_radio_id or '').strip()}|js8={str(js8_instance_id or '').strip()}|from={from_call}|to={target}|q={q_id}"
        received_ts = float(parsed.get("utc_ts") or time.time())
        request_age = time.time() - received_ts
        if request_age > DYNAMIC_EXPECT_REQUEST_MAX_AGE_SECONDS or request_age < -120:
            self._dynamic_flamp_hold(
                q_id=q_id,
                reason="Dynamic Q request is outside the safe live-request window.",
                event_id=event_id,
                source_radio_id=source_radio_id,
                js8_instance_id=js8_instance_id,
                from_call=from_call,
                target=target,
                db_path=db_path,
            )
            return
        if not str(source_radio_id or "").strip() or not str(js8_instance_id or "").strip():
            self._dynamic_flamp_hold(
                q_id=q_id,
                reason="Dynamic Q request is missing a concrete source radio/JS8 instance.",
                event_id=event_id,
                source_radio_id=source_radio_id,
                js8_instance_id=js8_instance_id,
                from_call=from_call,
                target=target,
                db_path=db_path,
            )
            return
        if bool(parsed.get("relayed")):
            self._dynamic_flamp_hold(
                q_id=q_id,
                reason="Relayed dynamic Q requests require an explicit trusted-relay policy.",
                event_id=event_id,
                source_radio_id=source_radio_id,
                js8_instance_id=js8_instance_id,
                from_call=from_call,
                target=target,
                db_path=db_path,
            )
            return
        if float(parsed.get("confidence") or 0.0) < 1.0:
            self._dynamic_flamp_hold(
                q_id=q_id,
                reason="Dynamic Q request did not meet the high-confidence directed-message gate.",
                event_id=event_id,
                source_radio_id=source_radio_id,
                js8_instance_id=js8_instance_id,
                from_call=from_call,
                target=target,
                db_path=db_path,
            )
            return
        if not self._flamp_dynamic_enabled():
            self._dynamic_flamp_hold(
                q_id=q_id,
                reason="Dynamic FLAMP Expect service is disabled.",
                event_id=event_id,
                source_radio_id=source_radio_id,
                js8_instance_id=js8_instance_id,
                from_call=from_call,
                target=target,
                db_path=db_path,
            )
            return
        if not self._expect_auto_reply_runtime_enabled():
            self._dynamic_flamp_hold(
                q_id=q_id,
                reason="Runtime unattended Expect auto-reply is disabled or paused.",
                event_id=event_id,
                source_radio_id=source_radio_id,
                js8_instance_id=js8_instance_id,
                from_call=from_call,
                target=target,
                db_path=db_path,
            )
            return
        evaluation = evaluate_dynamic_flamp_request(
            q_id=q_id,
            requesting_callsign=from_call,
            target_group=target if target.startswith("@") else "",
            source_radio_id=source_radio_id,
            js8_instance_id=js8_instance_id,
            event_id=event_id,
            db_path=db_path,
        )
        if evaluation.decision != "reply-ready":
            self._dynamic_flamp_hold(
                q_id=q_id,
                reason=evaluation.reason,
                event_id=event_id,
                source_radio_id=source_radio_id,
                js8_instance_id=js8_instance_id,
                from_call=from_call,
                target=target,
                db_path=db_path,
            )
            return
        index_status = flamp_transfer_index_status(
            db_path=db_path,
            source_radio_id=source_radio_id,
            source_js8_instance_id=js8_instance_id,
        ) if db_path else None
        index_age = time.time() - float((index_status or {}).get("scanned_ts") or 0.0)
        if (
            not index_status
            or not bool(index_status.get("scan_success"))
            or index_age > FLAMP_TRANSFER_INDEX_MAX_AGE_SECONDS
        ):
            detail = str((index_status or {}).get("error_text") or "").strip()
            reason = "FLAMP transfer index has not completed a recent successful background scan."
            if detail:
                reason += f" {detail}"
            self._dynamic_flamp_hold(
                q_id=q_id,
                reason=reason,
                event_id=event_id,
                source_radio_id=source_radio_id,
                js8_instance_id=js8_instance_id,
                from_call=from_call,
                target=target,
                db_path=db_path,
            )
            return
        state = lookup_flamp_transfer_state(
            q_id,
            db_path=db_path,
            source_radio_id=source_radio_id,
            source_js8_instance_id=js8_instance_id,
        ) if db_path else None
        if state is not None and float(state.get("validated_scan_ts") or 0.0) < float(
            index_status.get("scanned_ts") or 0.0
        ):
            self._dynamic_flamp_hold(
                q_id=q_id,
                reason="FLAMP transfer was not validated by the latest successful source scan.",
                event_id=event_id,
                source_radio_id=source_radio_id,
                js8_instance_id=js8_instance_id,
                from_call=from_call,
                target=target,
                db_path=db_path,
            )
            return
        if state is None:
            payload = f"Q {q_id} NO"
        else:
            state_name = str(state.get("state") or "unavailable").lower()
            if state_name == "unavailable" and int(state.get("source_mtime_ns") or 0) > 0:
                self._dynamic_flamp_hold(
                    q_id=q_id,
                    reason="FLAMP transfer exists but its total/block set is not authoritative.",
                    event_id=event_id,
                    source_radio_id=source_radio_id,
                    js8_instance_id=js8_instance_id,
                    from_call=from_call,
                    target=target,
                    db_path=db_path,
                )
                return
            if state_name == "complete" and state.get("total_blocks") and not state.get("missing_blocks"):
                payload = f"Q {q_id} YES"
            elif state_name == "partial" and state.get("total_blocks") and float(state.get("parser_confidence") or 0.0) >= 1.0:
                snapshot_age = time.time() - float(state.get("observed_ts") or 0.0)
                if snapshot_age > FLAMP_PARTIAL_SNAPSHOT_MAX_AGE_SECONDS or snapshot_age < -120:
                    self._dynamic_flamp_hold(
                        q_id=q_id,
                        reason="Saved FLAMP relay snapshot is too old to prove the current missing-block list.",
                        event_id=event_id,
                        source_radio_id=source_radio_id,
                        js8_instance_id=js8_instance_id,
                        from_call=from_call,
                        target=target,
                        db_path=db_path,
                    )
                    return
                missing = sorted({int(item) for item in (state.get("missing_blocks") or [])})
                body = ",".join(str(item) for item in missing)
                if not missing or len(f"Q {q_id} {body}") > 180:
                    self._dynamic_flamp_hold(
                        q_id=q_id,
                        reason="Authoritative missing-block list is empty or exceeds the JS8 response budget.",
                        event_id=event_id,
                        source_radio_id=source_radio_id,
                        js8_instance_id=js8_instance_id,
                        from_call=from_call,
                        target=target,
                        db_path=db_path,
                    )
                    return
                payload = f"Q {q_id} {body}"
            elif state_name == "unavailable":
                payload = f"Q {q_id} NO"
            else:
                self._dynamic_flamp_hold(
                    q_id=q_id,
                    reason="FLAMP transfer state is not authoritative enough to answer.",
                    event_id=event_id,
                    source_radio_id=source_radio_id,
                    js8_instance_id=js8_instance_id,
                    from_call=from_call,
                    target=target,
                    db_path=db_path,
                )
                return
        # Dynamic replies always use the receiving source endpoint.  A broad
        # ``all`` policy must never redirect a reply through a primary radio.
        evaluation = replace(
            evaluation,
            response_text=payload,
            expect_key=f"Q {q_id}",
            q_id=q_id,
            reply_radio_id=str(source_radio_id or "").strip(),
            reply_js8_instance_id=str(js8_instance_id or "").strip(),
        )
        self._maybe_dispatch_expect_auto_reply(
            evaluation,
            event_id=event_id,
            source_radio_id=source_radio_id,
            source_js8_instance_id=js8_instance_id,
            requesting_callsign=from_call,
            target_group=target if target.startswith("@") else "",
            db_path=db_path,
            claim_event_key=event_id,
            claim_q_id=q_id,
        )

    def _parse_directed_spotter_line(self, line: str) -> Optional[Dict[str, str | float]]:
        if not line:
            return None
        if not line.rstrip().endswith("\u2662"):
            return None
        parts = [p for p in line.strip().split("\t") if p]
        if len(parts) < 5:
            parts = re.split(r"\s+", line.strip(), maxsplit=4)
        if len(parts) < 5:
            return None
        dt_str, _freq_txt, _shift, _snr_txt, msg = parts[0], parts[1], parts[2], parts[3], parts[4]
        if ":" not in msg:
            return None
        msg_upper = msg.upper()
        if re.search(r"(?:^|\s)E\?\s+F!", msg_upper):
            return None
        if re.search(r"\.\.\.\s*(?:\u2662)?$", msg):
            return None
        form_match = FORM_TOKEN_RE.search(msg_upper)
        if not form_match:
            return None
        try:
            ts = datetime.datetime.strptime(dt_str[:19], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=datetime.timezone.utc
            )
        except Exception:
            return None
        relay_via, rest = msg.split(":", 1)
        relay_via = relay_via.strip().upper()
        rest = rest.strip()
        dest_token = (rest.split() or [""])[0]
        dest = dest_token.split(">")[0].strip().strip(",").upper()
        if not dest:
            return None
        de_match = re.search(r"\*DE\*\s*([A-Z0-9/]+)", msg_upper)
        from_call = de_match.group(1) if de_match else relay_via
        raw_form = unwrap_native_js8_form_payload(msg)
        raw_form = re.split(r"\*DE\*", raw_form, 1, flags=re.IGNORECASE)[0].strip()
        if raw_form.endswith("\u2662"):
            raw_form = raw_form[:-1].rstrip()
        token_match = re.search(r"(#[A-Z0-9]{3,})", raw_form.upper())
        token = token_match.group(1) if token_match else ""
        form_id = normalize_form_code(form_match.group(0))[2:]
        return {
            "utc_ts": ts.timestamp(),
            "utc_str": ts.strftime("%Y-%m-%d %H:%M:%S"),
            "from_call": from_call.strip().upper(),
            "to_call": dest.strip().upper(),
            "form_id": form_id,
            "spotter_token": token,
            "raw_form": raw_form,
            "relay_via": relay_via,
        }

    def _parse_js8_spotter_event(self, event: Dict[str, Any]) -> Optional[Dict[str, str | float]]:
        if not isinstance(event, dict):
            return None
        params = event.get("params") if isinstance(event.get("params"), dict) else {}
        text = str(params.get("TEXT") or event.get("value") or "").strip()
        if not text:
            return None
        text_upper = text.upper()
        if re.search(r"(?:^|\s)E\?\s+F!", text_upper) or re.search(
            r"\.\.\.\s*(?:\u2662)?$", text
        ):
            return None
        form_match = FORM_TOKEN_RE.search(text_upper)
        if not form_match:
            return None
        form_start = form_match.start()
        raw_form = unwrap_native_js8_form_payload(text)
        raw_form = re.split(r"\*DE\*", raw_form, 1, flags=re.IGNORECASE)[0].strip()
        if raw_form.endswith("\u2662"):
            raw_form = raw_form[:-1].rstrip()
        token_match = re.search(r"(#[A-Z0-9]{3,})", raw_form.upper())
        utc_str, utc_ts = parse_js8_api_utc(params.get("UTC") or event.get("time"))
        if utc_ts <= 0:
            utc_ts = time.time()
            utc_str = datetime.datetime.fromtimestamp(utc_ts, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        from_call = str(params.get("FROM") or "").strip().upper()
        to_call = str(params.get("TO") or "").strip().upper()
        de_match = re.search(r"\*DE\*\s*([A-Z0-9/]+)", text_upper)
        if de_match:
            from_call = de_match.group(1).strip().upper()
        if not from_call:
            return None
        if not to_call:
            leading = text[:form_start].strip()
            leading_parts = leading.split()
            if leading_parts and leading_parts[-1].upper() == "MSG":
                leading_parts.pop()
            to_call = (leading_parts[-1] if leading_parts else "").strip().strip(",").upper()
        if not to_call:
            to_call = str(params.get("CMD") or "").strip().upper()
        return {
            "utc_ts": float(utc_ts),
            "utc_str": utc_str,
            "from_call": from_call,
            "to_call": to_call,
            "form_id": normalize_form_code(form_match.group(0))[2:],
            "spotter_token": token_match.group(1) if token_match else "",
            "raw_form": raw_form,
            "relay_via": str(params.get("FROM") or "").strip().upper(),
        }

    @staticmethod
    def _parse_form_parts(text: str) -> tuple[str, str, str]:
        form_code, response, remainder, _token = split_spotter_form_text(
            unwrap_native_js8_form_payload(text)
        )
        return (form_code[2:] if form_code.startswith("F!") else "", response, remainder)

    @staticmethod
    def _classify_mcf304_status(response_code: str) -> tuple[str, str]:
        return classify_mcf304_status(response_code)

    @staticmethod
    def _status_label(status_key: str) -> str:
        return spotter_status_label(status_key)

    @classmethod
    def _classify_spotter_status(
        cls, form_id: str, response_code: str
    ) -> tuple[str, str, str]:
        return classify_spotter_status(form_id, response_code)

    def _mapped_status_form_ids(self) -> set[str]:
        cached = getattr(self, "_mapped_status_form_ids_cache", None)
        if cached is not None:
            return set(cached)
        try:
            raw = self.settings.get(MAPPER_SETTINGS_KEY, [])
        except Exception:
            raw = []
        mapped = {
            code[2:]
            for code in forms_enabled_for(self.settings, flag="status")
            if code.startswith("F!") and code[2:] in SPOTTER_STATUS_FORMS
        }
        resolved = mapped if isinstance(raw, list) and raw else (mapped or set(SPOTTER_STATUS_FORMS))
        self._mapped_status_form_ids_cache = frozenset(resolved)
        return set(resolved)

    def _form_codes_for_flag(self, flag: str) -> set[str] | None:
        return form_codes_enabled_for(self.settings, flag=flag)

    def _upsert_spotter_station_status(
        self,
        cur: sqlite3.Cursor,
        *,
        from_call: str,
        form_id: str,
        response_code: str,
        raw_form: str,
        utc_ts: float,
        utc_str: str,
        ingested_ts: float,
        status_source: str = "",
        mapped_status_form_ids: set[str] | None = None,
    ) -> None:
        fid = (form_id or "").strip()
        enabled_forms = (
            mapped_status_form_ids
            if mapped_status_form_ids is not None
            else self._mapped_status_form_ids()
        )
        if fid not in enabled_forms:
            return
        call = (from_call or "").strip().upper()
        if not call:
            return
        status_key, status_label, source_detail = self._classify_spotter_status(fid, response_code)
        source = (status_source or "").strip().upper() or f"F!{fid}"
        cur.execute(
            """
            INSERT INTO spotter_station_status
                (from_call, form_id, status_key, status_label, response_code, updated_utc_ts, updated_utc_str,
                 raw_text, updated_ingested_ts, status_source, status_source_detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(from_call) DO UPDATE SET
                form_id=excluded.form_id,
                status_key=excluded.status_key,
                status_label=excluded.status_label,
                response_code=excluded.response_code,
                updated_utc_ts=excluded.updated_utc_ts,
                updated_utc_str=excluded.updated_utc_str,
                raw_text=excluded.raw_text,
                updated_ingested_ts=excluded.updated_ingested_ts,
                status_source=excluded.status_source,
                status_source_detail=excluded.status_source_detail
            WHERE (
                excluded.updated_utc_ts > COALESCE(spotter_station_status.updated_utc_ts, 0)
                OR (
                    excluded.updated_utc_ts = COALESCE(spotter_station_status.updated_utc_ts, 0)
                    AND excluded.updated_ingested_ts >= COALESCE(spotter_station_status.updated_ingested_ts, 0)
                )
            )
            """,
            (
                call,
                fid,
                status_key,
                status_label,
                (response_code or "").strip(),
                float(utc_ts or 0.0),
                (utc_str or "").strip(),
                (raw_form or "").strip(),
                float(ingested_ts or 0.0),
                source,
                source_detail,
            ),
        )

    def _backfill_spotter_station_status(self, cur: sqlite3.Cursor) -> None:
        try:
            forms = sorted(self._mapped_status_form_ids())
            if not forms:
                return
            enabled_forms = set(forms)
            signature = "v2:" + ",".join(forms)
            if str(getattr(self, "_spotter_status_backfill_signature", "") or "") == signature:
                return
            placeholders = ",".join(["?"] * len(forms))
            cur.execute(
                f"""
                WITH ranked AS (
                    SELECT from_call, form_id, raw_text, utc_ts, utc_str, ingested_ts,
                           ROW_NUMBER() OVER (
                               PARTITION BY from_call
                               ORDER BY COALESCE(utc_ts, 0) DESC,
                                        COALESCE(ingested_ts, 0) DESC,
                                        id DESC
                           ) AS rank_number
                    FROM spotter_traffic
                    WHERE form_id IN ({placeholders})
                )
                SELECT from_call, form_id, raw_text, utc_ts, utc_str, ingested_ts
                FROM ranked
                WHERE rank_number = 1
                """,
                tuple(forms),
            )
            for from_call, form_id, raw_text, utc_ts, utc_str, ingested_ts in cur.fetchall():
                call = (from_call or "").strip().upper()
                if not call:
                    continue
                parsed_form_id, response_code, _ = self._parse_form_parts(str(raw_text or ""))
                self._upsert_spotter_station_status(
                    cur,
                    from_call=call,
                    form_id=str(form_id or parsed_form_id or ""),
                    response_code=response_code,
                    raw_form=str(raw_text or ""),
                    utc_ts=float(utc_ts or 0.0),
                    utc_str=str(utc_str or ""),
                    ingested_ts=float(ingested_ts or 0.0),
                    status_source=f"F!{str(form_id or parsed_form_id or '').strip()}",
                    mapped_status_form_ids=enabled_forms,
                )
            self._spotter_status_backfill_signature = signature
        except Exception as e:
            log.debug("MessageIngest: spotter status backfill failed: %s", e)

    def _ensure_spotter_table(self) -> None:
        db_path = self._db_path()
        if not db_path:
            return
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS spotter_traffic (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    utc_ts REAL,
                    utc_str TEXT,
                    from_call TEXT,
                    to_call TEXT,
                    form_id TEXT,
                    spotter_token TEXT,
                    raw_text TEXT,
                    decoded_text TEXT,
                    state TEXT,
                    read_ts REAL,
                    flag_state INTEGER DEFAULT 0,
                    relay_via TEXT,
                    source_radio_id TEXT,
                    js8_instance_id TEXT,
                    source_key TEXT,
                    ingested_ts REAL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS spotter_station_status (
                    from_call TEXT PRIMARY KEY,
                    form_id TEXT NOT NULL,
                    status_key TEXT NOT NULL,
                    status_label TEXT NOT NULL,
                    response_code TEXT,
                    updated_utc_ts REAL NOT NULL DEFAULT 0,
                    updated_utc_str TEXT,
                    raw_text TEXT,
                    updated_ingested_ts REAL,
                    status_source TEXT,
                    status_source_detail TEXT
                )
                """
            )
            try:
                cur.execute("ALTER TABLE spotter_traffic ADD COLUMN flag_state INTEGER DEFAULT 0")
            except Exception:
                pass
            for col_name, col_ddl in (
                ("relay_via", "TEXT"),
                ("source_radio_id", "TEXT"),
                ("js8_instance_id", "TEXT"),
                ("source_key", "TEXT"),
                ("ingested_ts", "REAL"),
            ):
                try:
                    cur.execute(f"ALTER TABLE spotter_traffic ADD COLUMN {col_name} {col_ddl}")
                except Exception:
                    pass
            for col_name, col_ddl in (
                ("status_source", "TEXT"),
                ("status_source_detail", "TEXT"),
            ):
                try:
                    cur.execute(f"ALTER TABLE spotter_station_status ADD COLUMN {col_name} {col_ddl}")
                except Exception:
                    pass
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_spotter_traffic_form_call_ts ON spotter_traffic(form_id, from_call, utc_ts DESC, id DESC)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_spotter_traffic_from_ts ON spotter_traffic(from_call, utc_ts DESC, id DESC)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_spotter_traffic_utc_ts ON spotter_traffic(utc_ts DESC, from_call, id DESC)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_spotter_status_key_ts ON spotter_station_status(status_key, updated_utc_ts DESC)"
            )
            self._ensure_projection_triggers_once(conn, "spotter_traffic")
            self._backfill_spotter_station_status(cur)
            conn.commit()
            conn.close()
        except Exception as e:
            log.debug("MessageIngest: failed to ensure spotter table: %s", e)

    def _inbox_path(self) -> Path | None:
        directed = (self.settings.get("js8_directed_path", "") or "").strip()
        if not directed:
            return None
        p = Path(directed)
        candidates = [
            p.parent / "inbox_v1",
            p.parent / "inbox_v1.sqlite",
            p.parent / "inbox_v1.db",
            p.parent / "inbox.db3",
        ]
        for c in candidates:
            if c.exists():
                return c
        for c in p.parent.glob("inbox*"):
            if c.is_file():
                return c
        return candidates[0]

    def _local_js8_db(self) -> Path | None:
        try:
            return get_config_dir() / "config" / "freqinout_nets.db"
        except Exception as e:
            log.debug("MessageIngest: failed to resolve local JS8 DB path: %s", e)
            return None

    def _load_js8_state_map(self, *, source_key: str = "") -> Dict[int, Tuple[str, float]]:
        db_path = self._local_js8_db()
        if not db_path or not db_path.exists():
            return {}
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute(
                "CREATE TABLE IF NOT EXISTS js8_inbox_state (id INTEGER PRIMARY KEY, state TEXT, last_seen REAL, read_ts REAL, last_ingested_id INTEGER)"
            )
            try:
                cur.execute("ALTER TABLE js8_inbox_state ADD COLUMN source_key TEXT")
            except Exception:
                pass
            try:
                cur.execute("ALTER TABLE js8_inbox_state ADD COLUMN source_id INTEGER")
            except Exception:
                pass
            try:
                cur.execute("UPDATE js8_inbox_state SET source_key='' WHERE source_key IS NULL")
            except Exception:
                pass
            if source_key:
                cur.execute("SELECT COALESCE(source_id, id), state, read_ts FROM js8_inbox_state WHERE COALESCE(source_key, '')=?", (source_key,))
            else:
                cur.execute("SELECT id, state, read_ts FROM js8_inbox_state WHERE COALESCE(source_key, '')=''")
            rows = cur.fetchall()
            conn.close()
            return {int(r[0]): ((r[1] or "").upper(), float(r[2] or 0.0)) for r in rows if r and r[0] is not None}
        except Exception as e:
            log.debug("MessageIngest: failed to load js8 state map: %s", e)
            return {}

    def _ensure_local_js8_tables(self) -> None:
        db_path = self._local_js8_db()
        if not db_path:
            return
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        conn = sqlite3.connect(db_path)
        ensure_js8_message_cache_schema(conn)
        self._ensure_projection_triggers_once(conn, "js8_messages")
        conn.commit()
        conn.close()

    def _local_max_js8_id(self, *, source_key: str = "") -> int:
        db_path = self._local_js8_db()
        if not db_path or not db_path.exists():
            return 0
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            if source_key:
                cur.execute("SELECT MAX(source_id) FROM js8_messages WHERE COALESCE(source_key, '')=?", (source_key,))
            else:
                cur.execute("SELECT MAX(id) FROM js8_messages WHERE COALESCE(source_key, '')=''")
            row = cur.fetchone()
            conn.close()
            return int(row[0]) if row and row[0] is not None else 0
        except Exception:
            return 0

    def _js8_ingest_checkpoint(self, *, source_key: str = "") -> int:
        db_path = self._local_js8_db()
        if not db_path or not db_path.exists():
            return 0
        try:
            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT last_source_id FROM js8_ingest_checkpoint WHERE source_key=?",
                (str(source_key or ""),),
            ).fetchone()
            conn.close()
            return int(row[0] or 0) if row else 0
        except Exception:
            return 0

    def _set_js8_ingest_checkpoint(self, *, source_key: str = "", last_source_id: int = 0) -> None:
        if int(last_source_id or 0) <= 0:
            return
        db_path = self._local_js8_db()
        if not db_path:
            return
        try:
            conn = sqlite3.connect(db_path)
            with conn:
                conn.execute(
                    """
                    INSERT INTO js8_ingest_checkpoint(source_key, last_source_id, updated_ts)
                    VALUES (?, ?, ?)
                    ON CONFLICT(source_key) DO UPDATE SET
                        last_source_id=MAX(js8_ingest_checkpoint.last_source_id, excluded.last_source_id),
                        updated_ts=excluded.updated_ts
                    """,
                    (str(source_key or ""), int(last_source_id), time.time()),
                )
            conn.close()
        except Exception as exc:
            log.debug("MessageIngest: failed to save JS8 ingest checkpoint: %s", exc)

    @staticmethod
    def _js8_source_key(*, source_key: str = "", source_radio_id: object = "", js8_instance_id: object = "") -> str:
        explicit = str(source_key or "").strip()
        if explicit:
            return explicit
        radio = str(source_radio_id or "").strip()
        js8 = str(js8_instance_id or "").strip()
        if radio or js8:
            digest_src = f"{radio}|{js8}"
            return f"js8:{hashlib.sha1(digest_src.encode('utf-8', errors='ignore')).hexdigest()[:16]}"
        return ""

    @staticmethod
    def _js8_local_row_id(native_id: object, source_key: str = "") -> int:
        try:
            native_int = int(native_id or 0)
        except Exception:
            native_int = 0
        if not source_key:
            return native_int
        digest_src = f"{source_key}|{native_int}"
        return int(hashlib.sha1(digest_src.encode("utf-8", errors="ignore")).hexdigest()[:15], 16)

    @staticmethod
    def _js8_signature_id(signature: object, source_key: str = "") -> int:
        digest_src = f"{str(source_key or '').strip()}|{str(signature or '').strip()}"
        if not digest_src.strip("|"):
            digest_src = "default"
        return int(hashlib.sha1(digest_src.encode("utf-8", errors="ignore")).hexdigest()[:15], 16)

    def _directed_js8_recipients(self) -> tuple[set[str], set[str]]:
        own_call = canonical_callsign(
            self.settings.get("operator_callsign", "")
            or self.settings.get("callsign", "")
            or ""
        )
        hf_groups, local_groups = configured_group_names(self.settings)
        context = load_operator_traffic_context(
            self._db_path(),
            callsign=own_call,
            configured_operating_groups=hf_groups,
            configured_local_groups=local_groups,
        )
        call_aliases: set[str] = {canonical_callsign(context.callsign)} if context.callsign else set()
        group_names: set[str] = {
            normalize_group_name(group)
            for group in context.groups
            if normalize_group_name(group)
        }
        db_path = self._db_path()
        if db_path and db_path.exists() and own_call:
            try:
                conn = sqlite3.connect(db_path)
                ensure_operator_identity_schema(conn, backfill_operator_rows=False)
                identity = resolve_operator_identity(conn, own_call)
                if identity is not None:
                    call_aliases.update(
                        canonical_callsign(call)
                        for call in callsigns_for_operator(conn, identity.operator_id)
                        if canonical_callsign(call)
                    )
                conn.close()
            except Exception:
                pass
        if own_call:
            call_aliases.add(own_call)
        return {call for call in call_aliases if call}, group_names

    @staticmethod
    def _split_directed_js8_text(text: str) -> tuple[str, str, str]:
        raw = str(text or "").strip()
        if not raw:
            return "", "", ""
        sender = ""
        dest = ""
        payload = raw
        if ":" in raw:
            sender, rest = raw.split(":", 1)
            sender = sender.strip().upper()
            tokens = rest.strip().split(None, 1)
            if tokens:
                dest_token = tokens[0].strip().strip(",")
                dest = dest_token.split(">", 1)[0].strip().upper()
                payload = tokens[1].strip().rstrip("\u2662").rstrip() if len(tokens) > 1 else ""
        return sender, dest, payload

    def _directed_js8_target_matches(self, dest: object, call_aliases: set[str], group_names: set[str]) -> bool:
        target = str(dest or "").strip().upper()
        if not target:
            return False
        normalized_group = normalize_group_name(target)
        if target.startswith("@") or normalized_group in group_names:
            return normalized_group in group_names
        return canonical_callsign(target) in call_aliases

    @staticmethod
    def _is_js8_directed_noise(text: object) -> bool:
        return not classify_js8_payload(text).inbox_visible

    def _parse_directed_js8_message_line(
        self,
        line: str,
        *,
        directed_callsigns: set[str],
        directed_groups: set[str],
        source_radio_id: object,
        js8_instance_id: object,
        source_key: object,
        source_path: Optional[Path],
    ) -> Optional[Dict[str, Any]]:
        if not line or not line.rstrip().endswith("\u2662"):
            return None
        parts = [part for part in line.strip().split("\t") if part]
        if len(parts) < 5:
            parts = re.split(r"\s+", line.strip(), maxsplit=4)
        if len(parts) < 5:
            return None
        try:
            timestamp = datetime.datetime.strptime(str(parts[0])[:19], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=datetime.timezone.utc
            )
        except Exception:
            return None
        raw_text = str(parts[4]).strip()
        sender, dest, payload = self._split_directed_js8_text(raw_text)
        rx_sender = sender
        if not sender or not dest:
            return None
        decision = classify_js8_payload(directed_js8_payload(raw_text))
        if not decision.inbox_visible:
            return None
        payload = decision.canonical_text
        if parse_dynamic_flamp_query(payload) is not None:
            return None
        if FORM_TOKEN_RE.search(raw_text):
            return None
        de_match = re.search(r"\*DE\*\s*([A-Z0-9/]+)", raw_text.upper())
        if de_match:
            sender = de_match.group(1).strip().upper()
        if not self._directed_js8_target_matches(dest, directed_callsigns, directed_groups):
            return None
        source_key_text = self._js8_source_key(
            source_key=str(source_key or ""),
            source_radio_id=source_radio_id,
            js8_instance_id=js8_instance_id,
        )
        signature = "|".join(
            (
                "directed-line",
                str(source_radio_id or "").strip(),
                str(js8_instance_id or "").strip(),
                str(source_path or ""),
                raw_text,
                timestamp.isoformat(),
            )
        )
        source_id = self._js8_signature_id(signature, source_key_text)
        return {
            "msg_id": source_id,
            "from_call": sender,
            "to_call": dest,
            "msg_type": "MSG",
            "utc_str": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "utc_ts": timestamp.timestamp(),
            "raw_text": payload,
            "decoded_text": payload,
            "state": "UNREAD",
            "read_ts": 0.0,
            "source_key": source_key_text,
            "source_id": source_id,
            "source_radio_id": source_radio_id,
            "js8_instance_id": js8_instance_id,
            "source_path": str(source_path or ""),
            **receive_context(
                sender=rx_sender,
                frequency_hz=(finite_number(parts[1]) * 1_000_000 if finite_number(parts[1]) is not None else None),
                snr=parts[3], kind="directed_txt", received_ts=time.time(),
            ),
        }

    def _parse_directed_js8_message_event(
        self,
        event: Dict[str, Any],
        *,
        directed_callsigns: set[str],
        directed_groups: set[str],
        source_radio_id: object,
        js8_instance_id: object,
        source_key: object,
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(event, dict):
            return None
        params = event.get("params") if isinstance(event.get("params"), dict) else {}
        text = str(params.get("TEXT") or event.get("value") or "").strip()
        if not text:
            return None
        sender = str(params.get("FROM") or "").strip().upper()
        dest = str(params.get("TO") or params.get("CALL") or "").strip().upper()
        parsed_sender, parsed_dest, payload = self._split_directed_js8_text(text)
        if not sender:
            sender = parsed_sender
        if not dest:
            dest = parsed_dest
        if not sender or not dest:
            return None
        rx_sender = sender
        decision = classify_js8_payload(directed_js8_payload(text))
        if not decision.inbox_visible:
            return None
        payload = decision.canonical_text
        if parse_dynamic_flamp_query(payload or text) is not None:
            return None
        if FORM_TOKEN_RE.search(text):
            return None
        de_match = re.search(r"\*DE\*\s*([A-Z0-9/]+)", text.upper())
        if de_match:
            sender = de_match.group(1).strip().upper()
        if not self._directed_js8_target_matches(dest, directed_callsigns, directed_groups):
            return None
        utc_str, utc_ts = parse_js8_api_utc(params.get("UTC") or event.get("time"))
        if utc_ts <= 0:
            utc_ts = float(time.time())
            utc_str = datetime.datetime.fromtimestamp(utc_ts, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        stable = str(params.get("ID") or params.get("MSG_ID") or event.get("id") or "").strip()
        if not stable:
            stable = hashlib.sha256(
                json.dumps(
                    {
                        "from": sender,
                        "to": dest,
                        "text": text,
                        "utc": utc_str,
                        "source_radio_id": str(source_radio_id or "").strip(),
                        "js8_instance_id": str(js8_instance_id or "").strip(),
                        "source_key": str(source_key or "").strip(),
                    },
                    sort_keys=True,
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
        source_key_text = self._js8_source_key(
            source_key=str(source_key or ""),
            source_radio_id=source_radio_id,
            js8_instance_id=js8_instance_id,
        )
        source_id = self._js8_signature_id(
            f"directed-event|{stable}|{sender}|{dest}|{utc_str}|{text}",
            source_key_text,
        )
        qualified_receipt = str(event.get("type") or "").upper() in {"RX.DIRECTED", "RX.DIRECTED.ME"}
        return {
            "msg_id": source_id,
            "from_call": sender,
            "to_call": dest,
            "msg_type": "MSG",
            "utc_str": utc_str,
            "utc_ts": utc_ts,
            "raw_text": payload or text,
            "decoded_text": payload or text,
            "state": "UNREAD",
            "read_ts": 0.0,
            "source_key": source_key_text,
            "source_id": source_id,
            "source_radio_id": source_radio_id,
            "js8_instance_id": js8_instance_id,
            "source_path": "",
            **receive_context(
                sender=rx_sender if qualified_receipt else "",
                frequency_hz=api_receive_frequency(params) if qualified_receipt else None,
                snr=params.get("SNR") if qualified_receipt else None,
                received_ts=time.time(), kind="rx_directed" if qualified_receipt else "",
            ),
        }

    def _store_directed_js8_message(self, row: Mapping[str, Any]) -> bool:
        try:
            # _insert_js8_local already performs the semantic duplicate check
            # and an atomic ON CONFLICT(source_key, source_id) guard.  A second
            # connection and lookup here doubled SQLite work for every line in
            # a JS8 catch-up scan and raced message projection unnecessarily.
            inserted = self._insert_js8_local(
                row.get("msg_id", 0),
                str(row.get("from_call") or "").strip().upper(),
                str(row.get("to_call") or "").strip().upper(),
                str(row.get("msg_type") or "MSG"),
                str(row.get("utc_str") or ""),
                float(row.get("utc_ts") or 0.0),
                str(row.get("raw_text") or ""),
                str(row.get("decoded_text") or row.get("raw_text") or ""),
                str(row.get("state") or "UNREAD"),
                float(row.get("read_ts") or 0.0),
                source_key=str(row.get("source_key") or ""),
                source_id=row.get("source_id", row.get("msg_id", 0)),
                source_radio_id=row.get("source_radio_id", ""),
                js8_instance_id=row.get("js8_instance_id", ""),
                source_path=row.get("source_path", ""),
                rx_freq_hz=row.get("rx_freq_hz"),
                rx_snr=row.get("rx_snr"),
                rx_sender=row.get("rx_sender"),
                receipt_kind=row.get("receipt_kind"),
                received_ts=row.get("received_ts"),
            )
            if inserted:
                try:
                    self._enqueue_next_msg_id(
                        str(row.get("from_call") or ""),
                        str(row.get("raw_text") or ""),
                        source_key=str(row.get("source_key") or ""),
                        source_radio_id=row.get("source_radio_id", ""),
                        js8_instance_id=row.get("js8_instance_id", ""),
                        source_path=row.get("source_path", ""),
                    )
                except Exception:
                    pass
            return inserted
        except Exception as exc:
            log.debug("MessageIngest: failed to store directed JS8 message: %s", exc)
            return False

    def _insert_js8_local(
        self,
        msg_id: int,
        from_call: str,
        to_call: str,
        msg_type: str,
        utc_str: str,
        utc_ts: float,
        raw_text: str,
        decoded_text: str,
        state: str,
        read_ts: float,
        *,
        source_key: str = "",
        source_id: object = 0,
        source_radio_id: object = "",
        js8_instance_id: object = "",
        source_path: object = "",
        rx_freq_hz: object = None,
        rx_snr: object = None,
        rx_sender: object = None,
        receipt_kind: object = None,
        received_ts: object = None,
    ) -> bool:
        db_path = self._local_js8_db()
        if not db_path:
            return False
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            native_id = int(source_id or msg_id or 0)
            local_id = self._js8_local_row_id(native_id, source_key)
            if float(utc_ts or 0.0) > 0.0:
                existing = cur.execute(
                    """
                    SELECT 1 FROM js8_messages
                    WHERE UPPER(COALESCE(from_call, ''))=UPPER(?)
                      AND UPPER(COALESCE(to_call, ''))=UPPER(?)
                      AND COALESCE(raw_text, '')=?
                      AND ABS(COALESCE(utc_ts, 0)-?) <= 1.0
                      AND COALESCE(source_key, '')=COALESCE(?, '')
                    LIMIT 1
                    """,
                    (from_call, to_call, raw_text, float(utc_ts), source_key),
                ).fetchone()
                if existing is not None:
                    conn.close()
                    return False
            cur.execute(
                """
                INSERT INTO js8_messages
                    (id, from_call, to_call, msg_type, utc_str, utc_ts, raw_text, decoded_text, state, read_ts,
                     source_key, source_id, source_radio_id, js8_instance_id, source_path,
                     rx_freq_hz, rx_band, rx_snr, rx_sender, receipt_kind, received_ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_key, source_id) DO NOTHING
                """,
                (
                    int(local_id),
                    from_call,
                    to_call,
                    msg_type,
                    utc_str,
                    float(utc_ts or 0.0),
                    raw_text,
                    decoded_text,
                    state,
                    float(read_ts or 0.0),
                    source_key,
                    native_id,
                    str(source_radio_id or ""),
                    str(js8_instance_id or ""),
                    str(source_path or ""),
                    *receive_context(
                        sender=str(rx_sender or ""), frequency_hz=rx_freq_hz,
                        snr=rx_snr, kind=str(receipt_kind or ""), received_ts=received_ts,
                    ).values(),
                ),
            )
            inserted = int(cur.rowcount or 0) > 0
            if inserted:
                candidate = received_pin_observation({
                    "receipt_kind": receipt_kind, "source_radio_id": source_radio_id,
                    "raw_text": raw_text, "to_call": to_call, "rx_sender": rx_sender,
                    "source_key": source_key, "source_id": native_id,
                    "received_ts": received_ts, "utc_str": utc_str,
                    "js8_instance_id": js8_instance_id,
                })
                if candidate is not None:
                    upsert_observation_conn(conn, candidate)
            conn.commit()
            conn.close()
            return inserted
        except Exception as e:
            log.debug("MessageIngest: failed to insert local js8 message: %s", e)
            return False
