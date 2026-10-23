"""Durable, idempotent summary staging around the existing Compose services."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
from pathlib import Path

from freqinout.core.compose_stage_service import (
    ComposeStageRequest, ComposeStageResult, _publish_to_managed_bbs,
    _write_payload, _write_signed_payload,
)
from freqinout.core.gpg_tools import verify_file_with_discovery
from freqinout.core.traffic_summary import SummarySnapshot
from freqinout.core.traffic_summary_store import _atomic_bytes, save_summary_revision, save_summary_bundle, validate_review


@dataclasses.dataclass(frozen=True)
class SummaryStageContext:
    snapshot: SummarySnapshot
    edited_text: str
    excluded_ids: tuple[str, ...]
    report_root: str
    report_kind: str = "legacy"
    companion_text: str = ""
    target_minutes: int = 3


def _recover_signed(path: Path, request: ComposeStageRequest) -> None:
    verified = verify_file_with_discovery(path, configured_path=request.gpg_path,
                                          trusted_fingerprints=request.trusted_fingerprints,
                                          allow_inline_clearsigned=True)
    if verified.status != "valid" or verified.signer_fingerprint.upper() != request.signer_fingerprint.upper():
        raise ValueError("Existing signed report failed signature/signer verification; no replacement was created.")
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    if not text.startswith("-----BEGIN PGP SIGNED MESSAGE-----\n"):
        raise ValueError("Existing report is not a clearsigned file.")
    _, sep, body = text.partition("\n\n")
    clear, boundary, _ = body.partition("-----BEGIN PGP SIGNATURE-----")
    clear = "\n".join(line[2:] if line.startswith("- ") else line for line in clear.split("\n"))
    if not sep or not boundary or clear.rstrip("\n") != request.payload.replace("\r\n", "\n").rstrip("\n"):
        raise ValueError("Existing signed report content differs from this revision; no replacement was created.")


def stage_summary_request(request: ComposeStageRequest, context: SummaryStageContext) -> ComposeStageResult:
    """Worker-only. A separate report ledger serializes retries across processes.

    Persistent artifacts are BBS sources; destination files are replaceable copies.
    No traffic-source schema, version metadata or automatic RF action is touched.
    """
    validate_review(context.snapshot, context.edited_text, context.excluded_ids,
                    report_kind=context.report_kind, target_minutes=context.target_minutes)
    if (context.snapshot.schema_version == 2 or context.report_kind != "legacy") and not context.snapshot.station_callsign:
        raise ValueError("Set the station callsign and generate the report before staging.")
    normalized_text = context.edited_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if normalized_text not in request.payload:
        raise ValueError("Compose payload does not contain this reviewed report.")
    root = Path(context.report_root).expanduser().resolve()
    if context.report_kind == "legacy":
        revision = save_summary_revision(root, context.snapshot, context.edited_text, excluded_ids=context.excluded_ids)
    else:
        rf_text = context.edited_text if context.report_kind == "rf" else context.companion_text
        detailed_text = context.edited_text if context.report_kind == "detailed" else context.companion_text
        revision = save_summary_bundle(root, context.snapshot, rf_text, detailed_text,
            target_minutes=context.target_minutes, excluded_ids=context.excluded_ids, active_kind=context.report_kind)
    if not request.plans or not any(p.requested and p.ready for p in request.plans):
        return ComposeStageResult(request.generation, problems=("Choose at least one ready file destination.",))
    plans = tuple(p for p in request.plans if p.requested)
    spec = {"revision": revision.stem, "payload": request.payload,
            "destinations": sorted((p.key, str(Path(p.directory).expanduser().resolve())) for p in plans),
            "unsigned_name": request.unsigned_name, "sign_flamp": request.sign_flamp,
            "signer": request.signer_fingerprint, "radio": request.radio_id}
    action_key = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    artifact_dir = revision.parent / "artifacts" / action_key
    if not artifact_dir.resolve().is_relative_to(root):
        raise ValueError("Report artifact path leaves persistent report storage.")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = root / "stage-actions.sqlite"
    if ledger_path.is_symlink():
        raise ValueError("The report action ledger cannot be a symbolic link.")
    ledger = sqlite3.connect(ledger_path, timeout=1.0)
    outputs, notes, problems, skipped = [], [], [], []
    durable, copies = {}, {}
    artifact_id, publish_path = "", ""
    try:
        ledger.execute("CREATE TABLE IF NOT EXISTS actions (action_key TEXT PRIMARY KEY, state_json TEXT NOT NULL)")
        ledger.commit()
        ledger.execute("BEGIN IMMEDIATE")
        row = ledger.execute("SELECT state_json FROM actions WHERE action_key=?", (action_key,)).fetchone()
        state = json.loads(row[0]) if row else {}
        for plan in plans:
            if not plan.ready:
                skipped.append(plan.note or f"{plan.label} is unavailable.")
                continue
            if plan.key not in {"flmsg", "flamp", "varac_outbox"}:
                problems.append(f"{plan.label}: summaries use station Managed BBS publication, not direct live-folder writes.")
                continue
            # Keep existing naming policy, but make ownership and retry identity explicit.
            # Do not use the planner's collision-dependent suffix in the action identity.
            base = Path(request.unsigned_name).name
            if plan.key == "flamp" and request.sign_flamp:
                # The destination plan already reflects the configured signing suffix.
                base = Path(plan.path).name
            suffixes = "".join(Path(base).suffixes) or ".b2s"
            stem = base[:-len(suffixes)] if base.endswith(suffixes) else base
            filename = stem + "-STS-" + action_key[:16] + suffixes
            record = state.get(plan.key, {})
            if record:
                persistent = Path(record["persistent"])
                destination = Path(record["destination"])
            else:
                persistent = artifact_dir / (plan.key + "-" + filename)
                destination = Path(plan.directory).expanduser().resolve() / filename
                record = {"persistent": str(persistent), "destination": str(destination)}
            if not persistent.resolve().is_relative_to(artifact_dir.resolve()):
                raise ValueError("Saved report action contains an invalid persistent path.")
            expected_destination = Path(plan.directory).expanduser().resolve() / persistent.name.removeprefix(plan.key + "-")
            if (persistent.parent != artifact_dir or not persistent.name.startswith(plan.key + "-")
                    or "-STS-" + action_key[:16] not in persistent.name
                    or destination != expected_destination):
                raise ValueError("Saved report action does not match its selected destination.")
            try:
                if persistent.exists():
                    if request.sign_flamp and plan.key == "flamp":
                        _recover_signed(persistent, request)
                    elif persistent.read_bytes() != request.payload.encode("utf-8"):
                        raise ValueError("Existing persistent report differs; no overwrite or duplicate was created.")
                elif request.sign_flamp and plan.key == "flamp":
                    result, note, error = _write_signed_payload(request, persistent)
                    if result is None:
                        raise ValueError("Signing failed; no unsigned fallback. " + error)
                    if result != persistent:
                        raise ValueError("Unexpected report filename collision; review persistent artifacts.")
                    notes.append(note)
                else:
                    result = _write_payload(persistent, request.payload)
                    if result != persistent:
                        raise ValueError("Unexpected report filename collision; review persistent artifacts.")
                data = persistent.read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                if record.get("sha256") and record["sha256"] != digest:
                    raise ValueError("Saved report artifact hash changed; staging stopped.")
                record["sha256"] = digest
                state[plan.key] = record
                # Exclusive copy is idempotent, never overwrites unrelated station files.
                _atomic_bytes(destination, data)
                outputs.append(str(destination))
                durable[plan.key] = persistent
                copies[plan.key] = destination
            except Exception as exc:
                problems.append(f"{plan.label}: {exc}")
        ledger.execute("INSERT INTO actions VALUES (?,?) ON CONFLICT(action_key) DO UPDATE SET state_json=excluded.state_json",
                       (action_key, json.dumps(state, sort_keys=True)))
        # Publication can be retried from the same verified persistent artifact.
        if request.publish_to_bbs:
            try:
                metadata = dict(request.metadata)
                metadata.update({"derived_report": "true", "report_id": context.snapshot.report_id,
                                 "report_revision": revision.stem, "snapshot_hash": context.snapshot.content_hash,
                                 "receipt_basis": "stored_receipt_window"})
                publish_request = dataclasses.replace(request, source_id="traffic-summary:" + context.snapshot.report_id + ":" + revision.stem,
                                                      metadata=tuple(metadata.items()))
                artifact_id, publish_path = _publish_to_managed_bbs(publish_request, durable)
            except Exception as exc:
                problems.append(f"Managed BBS: {exc}")
        ledger.commit()
    finally:
        ledger.close()
    return ComposeStageResult(request.generation, tuple(outputs), tuple((k, str(v)) for k, v in sorted(copies.items())),
                              tuple(skipped), tuple(notes), tuple(problems), artifact_id, publish_path,
                              request.bbs_location_ids if artifact_id else (), request.bbs_location_names if artifact_id else ())
