"""Canonical, local-only review of attributed JS8 PIN receipts."""
from __future__ import annotations

from dataclasses import replace
import datetime
import json
import sqlite3
from pathlib import Path
from urllib.parse import quote
from typing import Mapping

from freqinout.core.js8_pin import parse_pin
from freqinout.core.observation_projection import observation_from_rf_pin
from freqinout.core.logger import log


def received_pin_observation(row: Mapping[str, object]):
    """Build a pending candidate after the ingest layer accepts its recipient."""
    if row.get("receipt_kind") not in {"directed_txt", "rx_directed"} or not row.get("source_radio_id"):
        return None
    parsed = parse_pin(row.get("raw_text"), target_hint=row.get("to_call"), sender_hint=row.get("rx_sender"))
    if not parsed.valid or not parsed.sender:
        if parsed.recognized and not parsed.valid:
            log.debug("JS8 PIN retained as a message without map projection: %s", parsed.reason)
        return None
    source_key, native_id = str(row.get("source_key") or ""), str(row.get("source_id") or row.get("msg_id") or "")
    if not source_key or not native_id:
        return None
    received_ts = row.get("received_ts")
    try:
        received_utc = datetime.datetime.fromtimestamp(float(received_ts), datetime.timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    pin = observation_from_rf_pin({
        "raw_reference": f"js8-receipt:{source_key}:{native_id}",
        "source_radio_id": row.get("source_radio_id"),
        "source_app": row.get("js8_instance_id"),
        "callsign": parsed.sender, "to_target": parsed.target,
        "group": parsed.target if parsed.target.startswith("@") else "",
        "grid": parsed.grid, "label": parsed.category.title(),
        "summary": parsed.description, "pin_type": parsed.category.lower(),
        "created_utc": received_utc, "event_utc": row.get("utc_str"),
        "status": "PENDING", "confirmed_state": "UNREVIEWED",
    })
    return replace(pin, provenance={
        **pin.provenance,
        "received_pin": {
            "raw_text": parsed.raw, "category": parsed.category,
            "reporter": parsed.sender, "target": parsed.target,
            "source_key": source_key, "source_id": native_id,
            "source_radio_id": str(row.get("source_radio_id") or ""),
            "js8_instance_id": str(row.get("js8_instance_id") or ""),
            "receipt_kind": row.get("receipt_kind"),
            "reported_location": parsed.grid,
            "location_verified": False,
        },
    })


def review_received_pin(db_path, observation_id: str, *, accept: bool) -> bool:
    """Compare-and-set a pending local decision; never authorize RF/publication."""
    conn = None
    try:
        # Review cannot create a missing database or assure/migrate a schema.
        uri = "file:" + quote(str(Path(db_path).resolve()), safe="/") + "?mode=rw"
        conn = sqlite3.connect(uri, uri=True, timeout=0.5)
        with conn:
            row = conn.execute(
                "SELECT provenance_json FROM observation_projection "
                "WHERE observation_id=? AND source_family='rf_pin' AND status='PENDING'",
                (str(observation_id),),
            ).fetchone()
            if row is None:
                return False
            provenance = json.loads(row[0] or "{}")
            if not isinstance(provenance.get("received_pin"), dict):
                return False
            provenance["pin_review"] = {
                "decision": "accepted" if accept else "dismissed",
                "reviewed_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "local_only": True,
                "location_verified": False,
            }
            cursor = conn.execute(
                "UPDATE observation_projection SET status=?, confirmed_state=?, provenance_json=? "
                "WHERE observation_id=? AND source_family='rf_pin' AND status='PENDING'",
                ("PIN" if accept else "DISMISSED", "REVIEWED" if accept else "DISMISSED",
                 json.dumps(provenance, sort_keys=True), str(observation_id)),
            )
            return cursor.rowcount == 1
    except (sqlite3.Error, ValueError, TypeError, AttributeError):
        return False
    finally:
        if conn is not None:
            conn.close()
