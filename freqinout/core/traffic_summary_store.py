"""Immutable report revisions; never writes a traffic/source database."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from freqinout.core.traffic_summary import (
    REPORT_MARKER, SummarySnapshot, protected_counts, render_summary,
    snapshot_dict, snapshot_digest, snapshot_from_dict,
)


def validate_review(snapshot: SummarySnapshot, text: str, excluded_ids=(), *,
                    report_kind: str = "legacy", target_minutes: int = 3) -> None:
    if snapshot.content_hash != snapshot_digest(snapshot):
        raise ValueError("Report snapshot has changed. Generate or load a verified snapshot.")
    if not text.strip() or len(text.encode("utf-8")) > 4 * 1024 * 1024:
        raise ValueError("Report narrative must be nonempty and smaller than 4 MiB.")
    expected = render_summary(snapshot, excluded_ids=excluded_ids, report_kind=report_kind, target_minutes=target_minutes)
    if protected_counts(text) != protected_counts(expected):
        raise ValueError("Calculated counts were edited. Change evidence inclusion and regenerate counts before saving or staging.")
    marker = REPORT_MARKER + snapshot.report_id
    if len(re.findall(r"(?m)^" + re.escape(marker) + r"$", text)) != 1:
        raise ValueError("Keep the report identity/lineage marker intact.")
    if snapshot.schema_version == 2 or report_kind != "legacy":
        if snapshot.station_callsign and not re.fullmatch(r"(?=[A-Z0-9/]*[A-Z])(?=[A-Z0-9/]*[0-9])[A-Z0-9/]{3,32}", snapshot.station_callsign):
            raise ValueError("The report station callsign is invalid. Generate a new report.")
        header = expected.split("[COMPUTED COUNTS]", 1)[0]
        if not text.startswith(header):
            raise ValueError("Keep the station callsign, report identity and period intact.")
        footer = "end report de " + (snapshot.station_callsign or "Unknown")
        if text.rstrip().splitlines()[-1] != footer or text.splitlines().count(footer) != 1:
            raise ValueError("Keep the station callsign end-report line intact.")
        count_pattern = r"(?m)^- (F\d+) [^\n:]+: (\d+) reports?;"
        expected_counts = dict(re.findall(count_pattern, expected))
        actual_counts = re.findall(count_pattern, text)
        if len({key for key, _ in actual_counts}) != len(actual_counts):
            raise ValueError("A finding count identity was repeated.")
        for key, count in actual_counts:
            if expected_counts.get(key) != count:
                raise ValueError("A finding report count was edited. Change evidence inclusion to recalculate counts.")
        for line in text.splitlines():
            if re.match(r"^- .*?: \d+ reports?;", line) and not re.match(count_pattern.removeprefix("(?m)"), line):
                raise ValueError("Keep finding count identities intact or remove the complete finding when editing prose.")
        if report_kind == "rf":
            for prefix in ("Routine/other traffic:", "Additional detail:"):
                expected_line = next((line for line in expected.splitlines() if line.startswith(prefix)), None)
                actual_lines = [line for line in text.splitlines() if line.startswith(prefix)]
                if actual_lines != ([expected_line] if expected_line is not None else []):
                    raise ValueError("Keep calculated RF coverage counts intact. Change evidence inclusion to recalculate.")
        return
    # Header identity and interval remain structured as well as the totals.
    before = expected.split("Generated UTC:", 1)[0]
    if not text.startswith(before):
        raise ValueError("Keep the report identity, scope and receipt period intact. Edit the narrative below the counts.")


def _atomic_bytes(path: Path, data: bytes) -> None:
    """Exclusive publication; identical retry succeeds without another file."""
    if path.is_symlink():
        raise ValueError("Report files cannot be symbolic links.")
    if path.exists():
        if path.read_bytes() != data:
            raise FileExistsError("An existing report artifact has different content.")
        return
    fd, name = tempfile.mkstemp(prefix=".summary-", dir=str(path.parent))
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != data:
                raise
        except OSError:
            # Exclusive creation is still no-overwrite on filesystems without links.
            try:
                with path.open("xb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
            except FileExistsError:
                if path.read_bytes() != data:
                    raise
    finally:
        temporary.unlink(missing_ok=True)


def revision_content(snapshot: SummarySnapshot, edited_text: str, excluded_ids=()) -> tuple[bytes, str]:
    excluded = tuple(sorted(set(excluded_ids)))
    validate_review(snapshot, edited_text, excluded)
    data = {"format": "commsplan-traffic-summary", "version": 1,
            "snapshot": snapshot_dict(snapshot), "edited_text": edited_text,
            "excluded_ids": excluded}
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    return payload, hashlib.sha256(payload).hexdigest()


def save_summary_revision(report_root: str | Path, snapshot: SummarySnapshot, edited_text: str,
                          *, excluded_ids=()) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", snapshot.report_id):
        raise ValueError("Invalid report identity.")
    payload, digest = revision_content(snapshot, edited_text, excluded_ids)
    root = Path(report_root).expanduser().resolve()
    directory = root / snapshot.report_id
    if directory.is_symlink() or not directory.resolve().is_relative_to(root):
        raise ValueError("Report revision directory leaves persistent report storage.")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (digest + ".json")
    _atomic_bytes(path, payload)
    _atomic_bytes(directory / (digest + ".txt"), edited_text.encode("utf-8"))
    return path


def _load_legacy_revision(path: str | Path) -> tuple[SummarySnapshot, str, tuple[str, ...]]:
    source = Path(path)
    if source.stat().st_size > 128 * 1024 * 1024:
        raise ValueError("Report draft is too large.")
    raw = source.read_bytes()
    data = json.loads(raw)
    if data.get("format") != "commsplan-traffic-summary" or data.get("version") != 1:
        raise ValueError("Unsupported report draft format.")
    snapshot = snapshot_from_dict(data["snapshot"])
    text = data["edited_text"]
    excluded = tuple(data["excluded_ids"])
    validate_review(snapshot, text, excluded)
    _, digest = revision_content(snapshot, text, excluded)
    if source.stem != digest:
        raise ValueError("Report revision integrity check failed.")
    return snapshot, text, excluded


@dataclass(frozen=True)
class SummaryBundle:
    snapshot: SummarySnapshot
    rf_text: str
    detailed_text: str
    target_minutes: int = 3
    excluded_ids: tuple[str, ...] = ()
    active_kind: str = "rf"


def _bundle_content(snapshot, rf_text, detailed_text, *, target_minutes=3,
                    excluded_ids=(), active_kind="rf"):
    if type(target_minutes) is not int or target_minutes not in (3, 5, 10):
        raise ValueError("Choose a 3, 5 or 10 minute MFSK32 planning target.")
    if active_kind not in {"rf", "detailed"}:
        raise ValueError("Choose RF Brief or Detailed Report.")
    excluded = tuple(sorted(set(excluded_ids)))
    for kind, text in (("rf", rf_text), ("detailed", detailed_text)):
        validate_review(snapshot, text, excluded, report_kind=kind, target_minutes=target_minutes)
    data = {"format": "commsplan-traffic-summary", "version": 2,
            "snapshot": snapshot_dict(snapshot), "rf_text": rf_text,
            "detailed_text": detailed_text, "target_minutes": target_minutes,
            "excluded_ids": excluded, "active_kind": active_kind}
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    return payload, hashlib.sha256(payload).hexdigest()


def save_summary_bundle(report_root, snapshot, rf_text, detailed_text, *,
                        target_minutes=3, excluded_ids=(), active_kind="rf") -> Path:
    payload, digest = _bundle_content(snapshot, rf_text, detailed_text,
        target_minutes=target_minutes, excluded_ids=excluded_ids, active_kind=active_kind)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", snapshot.report_id):
        raise ValueError("Invalid report identity.")
    root = Path(report_root).expanduser().resolve()
    directory = root / snapshot.report_id
    if directory.is_symlink() or not directory.resolve().is_relative_to(root):
        raise ValueError("Report revision directory leaves persistent report storage.")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (digest + ".json")
    _atomic_bytes(path, payload)
    _atomic_bytes(directory / (digest + ".rf.txt"), rf_text.encode("utf-8"))
    _atomic_bytes(directory / (digest + ".detailed.txt"), detailed_text.encode("utf-8"))
    return path


def load_summary_bundle(path) -> SummaryBundle:
    source = Path(path)
    if source.stat().st_size > 128 * 1024 * 1024:
        raise ValueError("Report draft is too large.")
    data = json.loads(source.read_bytes())
    if data.get("format") != "commsplan-traffic-summary":
        raise ValueError("Unsupported report draft format.")
    if data.get("version") == 1:
        snapshot, text, excluded = _load_legacy_revision(path)
        return SummaryBundle(snapshot, text, text, 3, excluded, "legacy")
    if data.get("version") != 2:
        raise ValueError("Unsupported report draft format.")
    snapshot = snapshot_from_dict(data["snapshot"])
    bundle = SummaryBundle(snapshot, data["rf_text"], data["detailed_text"],
                           data["target_minutes"], tuple(data["excluded_ids"]), data["active_kind"])
    _, digest = _bundle_content(snapshot, bundle.rf_text, bundle.detailed_text,
        target_minutes=bundle.target_minutes, excluded_ids=bundle.excluded_ids,
        active_kind=bundle.active_kind)
    if source.stem != digest:
        raise ValueError("Report revision integrity check failed.")
    return bundle


def load_summary_revision(path) -> tuple[SummarySnapshot, str, tuple[str, ...]]:
    bundle = load_summary_bundle(path)
    text = bundle.detailed_text if bundle.active_kind == "detailed" else bundle.rf_text
    return bundle.snapshot, text, bundle.excluded_ids


def export_summary_text(path, snapshot, text, *, report_kind="rf", target_minutes=3,
                        excluded_ids=()) -> Path:
    validate_review(snapshot, text, excluded_ids, report_kind=report_kind, target_minutes=target_minutes)
    destination = Path(path).expanduser().absolute()
    if not destination.parent.is_dir():
        raise ValueError("Choose an existing export folder.")
    _atomic_bytes(destination, text.encode("utf-8"))
    return destination
