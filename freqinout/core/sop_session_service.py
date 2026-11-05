"""Detached persistence for an HF SOP activation session.

This service owns the JSON session journal and the session bookkeeping around
temporary Net/SOP priority decisions. Call it from a worker with a worker-local
``SOPManager``; it never imports or calls GUI code.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import tempfile
import threading
import uuid
from typing import Any, Mapping, Sequence


log = logging.getLogger(__name__)
_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}


def _path_lock(path: Path) -> threading.RLock:
    key = str(path.expanduser().resolve())
    with _LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class SOPSessionService:
    """Read/write the SOP session journal and track temporary policy changes.

    ``manager`` is expected to be a worker-local, non-Qt ``SOPManager``. A
    single service instance serializes its read/modify/write journal operations
    with other instances targeting the same path in this process.
    """

    def __init__(self, journal_path: Path | str, manager: Any):
        self.journal_path = Path(journal_path)
        self.manager = manager
        self._lock = _path_lock(self.journal_path)

    @staticmethod
    def _empty_journal() -> dict[str, Any]:
        return {
            "version": 1,
            "session_id": "",
            "status": "idle",
            "started_utc": "",
            "updated_utc": _utc_now_iso(),
            "active_profile_ids": [],
            "profile_names": {},
            "hf_restore": {
                "available": False,
                "captured_utc": "",
                "pre_adjust_signature": "",
                "post_adjust_signature": "",
                "hf_rows_before": [],
                "restored_utc": "",
            },
            "temp_net_sop_policies": [],
        }

    def load_journal(self) -> dict[str, Any]:
        """Load and normalize the existing version-1 journal schema."""
        out = self._empty_journal()
        if self.journal_path.exists():
            try:
                raw = json.loads(self.journal_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    out.update(raw)
            except Exception as exc:
                log.debug("SOP session: failed loading journal %s: %s", self.journal_path, exc)

        hf_restore = out.get("hf_restore")
        if not isinstance(hf_restore, dict):
            hf_restore = {}
        base_restore = dict(self._empty_journal()["hf_restore"])
        base_restore.update(hf_restore)
        base_restore["available"] = bool(base_restore.get("available"))
        if not isinstance(base_restore.get("hf_rows_before"), list):
            base_restore["hf_rows_before"] = []
        out["hf_restore"] = base_restore

        if not isinstance(out.get("active_profile_ids"), list):
            out["active_profile_ids"] = []
        out["active_profile_ids"] = [
            int(value) for value in out.get("active_profile_ids", [])
            if str(value).strip().lstrip("-").isdigit() and int(value) > 0
        ]
        if not isinstance(out.get("profile_names"), dict):
            out["profile_names"] = {}
        if not isinstance(out.get("temp_net_sop_policies"), list):
            out["temp_net_sop_policies"] = []
        out["version"] = 1
        out["updated_utc"] = str(out.get("updated_utc") or "") or _utc_now_iso()
        return out

    def save_journal(self, journal: Mapping[str, Any]) -> dict[str, Any]:
        """Persist and return the version-1 journal, preserving its full schema."""
        data = deepcopy(dict(journal or {}))
        data["version"] = 1
        data["updated_utc"] = _utc_now_iso()
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.journal_path.parent,
                                             delete=False) as temporary:
                path = Path(temporary.name)
                try:
                    json.dump(data, temporary, indent=2, sort_keys=True)
                    temporary.close()
                    path.replace(self.journal_path)
                finally:
                    path.unlink(missing_ok=True)
        return data

    @staticmethod
    def _profile_id(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def _active_hf_profiles(self) -> list[dict[str, Any]]:
        """Match Daily's active HF filter using the manager's detached catalog."""
        rows = self.manager.list_profiles_with_category()
        return [dict(row) for row in rows if isinstance(row, Mapping)
                and bool(row.get("active"))
                and str(row.get("category") or "HF").strip().upper() == "HF"]

    def start_or_refresh(self, profiles: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        with self._lock:
            return self._start_or_refresh(profiles)

    def _start_or_refresh(self, profiles: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        active_profiles = [dict(profile) for profile in profiles if isinstance(profile, Mapping)]
        active_ids = sorted({self._profile_id(profile.get("id")) for profile in active_profiles
                             if self._profile_id(profile.get("id")) > 0})
        profile_names = {
            str(self._profile_id(profile.get("id"))): str(profile.get("name") or "").strip()
            for profile in active_profiles if self._profile_id(profile.get("id")) > 0
        }
        now_iso = _utc_now_iso()
        journal = self.load_journal()
        status = str(journal.get("status") or "idle").strip().lower()
        existing_ids = sorted({self._profile_id(value) for value in journal.get("active_profile_ids", [])
                               if self._profile_id(value) > 0})
        if status == "active" and existing_ids and existing_ids == active_ids:
            journal["profile_names"] = dict(journal.get("profile_names") or {})
            journal["profile_names"].update(profile_names)
            journal["updated_utc"] = now_iso
            return self.save_journal(journal)
        if active_ids:
            journal = self._empty_journal()
            journal["session_id"] = uuid.uuid4().hex
            journal["status"] = "active"
            journal["started_utc"] = now_iso
            journal["active_profile_ids"] = list(active_ids)
            journal["profile_names"] = dict(profile_names)
            return self.save_journal(journal)
        return journal

    def register_activation(self, profile_id: int, profile_name: str = "") -> dict[str, Any] | None:
        """Register one newly activated profile and return the persisted journal."""
        pid = self._profile_id(profile_id)
        if pid <= 0:
            return None
        with self._lock:
            profiles = self._active_hf_profiles()
            known = {self._profile_id(profile.get("id")) for profile in profiles}
            if pid not in known:
                profiles.append({"id": pid, "name": profile_name, "active": True, "category": "HF"})
            journal = self._start_or_refresh(profiles)
            if str(profile_name or "").strip():
                names = dict(journal.get("profile_names") or {})
                names[str(pid)] = str(profile_name).strip()
                journal["profile_names"] = names
                journal["status"] = "active"
                journal["active_profile_ids"] = sorted(
                    {self._profile_id(value) for value in journal.get("active_profile_ids", [])
                     if self._profile_id(value) > 0} | {pid}
                )
                journal = self.save_journal(journal)
            return journal

    def _policy_rows_by_key(self) -> dict[str, dict[str, Any]]:
        rows = self.manager.list_net_sop_conflict_policies(active_only=True)
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            key = self.manager._policy_conflict_key(
                str(row.get("net_row_signature") or "").strip(),
                str(row.get("sop_row_signature") or "").strip(),
                str(row.get("window_start_utc") or "").strip(),
                str(row.get("window_end_utc") or "").strip(),
            )
            if key and key not in result:
                result[key] = dict(row)
        return result

    def _record_temp_policy_decisions(
        self,
        decisions: Sequence[Mapping[str, Any]],
        *,
        before_by_key: Mapping[str, Mapping[str, Any]],
        origin: str,
        profiles_hint: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        active_profiles = self._active_hf_profiles()
        if profiles_hint:
            known_ids = {self._profile_id(profile.get("id")) for profile in active_profiles}
            for profile in profiles_hint:
                if not isinstance(profile, Mapping):
                    continue
                pid = self._profile_id(profile.get("id"))
                if pid > 0 and pid not in known_ids:
                    active_profiles.append(dict(profile))
                    known_ids.add(pid)
        if not active_profiles:
            return None

        journal = self._start_or_refresh(active_profiles)
        after_by_key = self._policy_rows_by_key()
        rows = list(journal.get("temp_net_sop_policies") or [])
        row_map: dict[str, dict[str, Any]] = {}
        for item in rows:
            if not isinstance(item, dict):
                continue
            key = str(item.get("conflict_key") or "").strip()
            if key and key not in row_map:
                row_map[key] = item

        changed = False
        for decision in decisions:
            if not isinstance(decision, Mapping):
                continue
            key = self.manager._policy_conflict_key(
                str(decision.get("net_row_signature") or "").strip(),
                str(decision.get("sop_row_signature") or "").strip(),
                str(decision.get("window_start_utc") or "").strip(),
                str(decision.get("window_end_utc") or "").strip(),
            )
            if not key:
                continue
            before = before_by_key.get(key) or {}
            after = after_by_key.get(key) or {}
            expected_policy = str(after.get("policy") or decision.get("policy") or "").strip().upper()
            if expected_policy not in {"SOP_PRIORITY", "NET_PRIORITY"}:
                continue
            item = row_map.get(key)
            if item is None:
                item = {
                    "conflict_key": key,
                    "net_row_signature": str(decision.get("net_row_signature") or "").strip(),
                    "sop_row_signature": str(decision.get("sop_row_signature") or "").strip(),
                    "window_start_utc": str(decision.get("window_start_utc") or "").strip(),
                    "window_end_utc": str(decision.get("window_end_utc") or "").strip(),
                    "sop_profile_id": self._profile_id(decision.get("sop_profile_id")),
                    "sop_layer_id": self._profile_id(decision.get("sop_layer_id")),
                    "prev_exists": bool(before),
                    "prev_policy": str(before.get("policy") or "").strip().upper(),
                    "prev_policy_id": self._profile_id(before.get("id")),
                    "prev_updated_utc": str(before.get("updated_utc") or "").strip(),
                    "expected_policy": expected_policy,
                    "expected_updated_utc": str(after.get("updated_utc") or "").strip(),
                    "origin": str(origin or "").strip(),
                    "saved_utc": _utc_now_iso(),
                    "reverted": False,
                    "reverted_utc": "",
                }
                rows.append(item)
                row_map[key] = item
            else:
                item["expected_policy"] = expected_policy
                item["expected_updated_utc"] = str(after.get("updated_utc") or "").strip()
                item["origin"] = str(origin or item.get("origin") or "").strip()
                item["saved_utc"] = _utc_now_iso()
                item["reverted"] = False
                item["reverted_utc"] = ""
            changed = True

        if changed:
            journal["temp_net_sop_policies"] = rows
            return self.save_journal(journal)
        return journal

    def save_net_policy_decisions(
        self,
        decisions: Sequence[Mapping[str, Any]],
        *,
        origin: str = "",
        session_profile_hint: Mapping[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any] | None]:
        """Save policies and journal their prior values for session restoration.

        The return count preserves ``SOPManager.save_net_sop_conflict_policies``
        semantics. Journal tracking remains best-effort, matching Daily's
        existing wrapper: a journal failure does not undo a saved policy.
        """
        valid = [dict(row) for row in decisions or () if isinstance(row, Mapping)]
        if not valid:
            return 0, None
        with self._lock:
            before_by_key = self._policy_rows_by_key()
            saved = int(self.manager.save_net_sop_conflict_policies(valid) or 0)
            if saved <= 0:
                return saved, None
            try:
                journal = self._record_temp_policy_decisions(
                    valid,
                    before_by_key=before_by_key,
                    origin=origin or "Net/SOP conflict decision",
                    profiles_hint=[dict(session_profile_hint)] if isinstance(session_profile_hint, Mapping) else None,
                )
            except Exception as exc:
                log.debug("SOP session: failed recording temporary Net/SOP policy decisions: %s", exc)
                journal = None
            return saved, journal
