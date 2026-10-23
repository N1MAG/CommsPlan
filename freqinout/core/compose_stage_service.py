from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from freqinout.core.gpg_tools import (
    clearsign_file,
    gpg_detail_indicates_passphrase_needed,
    verify_file_with_discovery,
)
from freqinout.core.nbems_compose import ComposeDestinationPlan, unique_destination
from freqinout.core.secret_store import load_gpg_signing_passphrase
from freqinout.core.sqlite_utils import connect_sqlite
from freqinout.core.varac_bbs_library_store import (
    ensure_bbs_library_schema,
    set_bbs_artifact_locations,
    upsert_bbs_artifact_path,
)


@dataclass(frozen=True)
class ComposeStageRequest:
    """Immutable snapshot consumed by the background compose-stage worker."""

    payload: str
    unsigned_name: str
    plans: tuple[ComposeDestinationPlan, ...]
    radio_id: str
    radio_label: str
    sign_flamp: bool = False
    signer_fingerprint: str = ""
    gpg_path: str = ""
    trusted_fingerprints: tuple[str, ...] = ()
    publish_to_bbs: bool = False
    bbs_db_path: str = ""
    bbs_location_ids: tuple[str, ...] = ()
    bbs_location_names: tuple[str, ...] = ()
    source_id: str = ""
    metadata: tuple[tuple[str, str], ...] = ()
    generation: int = 0


@dataclass(frozen=True)
class ComposeStageResult:
    generation: int
    outputs: tuple[str, ...] = ()
    output_by_key: tuple[tuple[str, str], ...] = ()
    skipped: tuple[str, ...] = ()
    signature_notes: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()
    bbs_artifact_id: str = ""
    bbs_publish_path: str = ""
    bbs_location_ids: tuple[str, ...] = ()
    bbs_location_names: tuple[str, ...] = ()


def _unique_runtime_destination(path: Path) -> Path:
    if not path.exists():
        return path
    alternate = unique_destination(path)
    if alternate is None:
        raise FileExistsError(f"Could not reserve a unique filename for {path.name}")
    return alternate


def _publish_temp_file(temp_path: Path, requested_path: Path) -> Path:
    """Publish a completed sibling temp file without overwriting user data."""

    destination = _unique_runtime_destination(requested_path)
    while True:
        try:
            os.link(temp_path, destination)
            temp_path.unlink(missing_ok=True)
            return destination
        except FileExistsError:
            destination = _unique_runtime_destination(destination)
            continue
        except OSError:
            # Some filesystems/platforms do not allow hard links. Exclusive
            # creation retains the no-overwrite guarantee; cleanup prevents a
            # failed write from looking like a successful staged artifact.
            try:
                with destination.open("xb") as output, temp_path.open("rb") as source:
                    shutil.copyfileobj(source, output, length=128 * 1024)
                    output.flush()
                    os.fsync(output.fileno())
            except FileExistsError:
                destination = _unique_runtime_destination(destination)
                continue
            except Exception:
                destination.unlink(missing_ok=True)
                raise
            temp_path.unlink(missing_ok=True)
            return destination


def _write_payload(path: Path, payload: str) -> Path:
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        return _publish_temp_file(temp_path, path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temp_path.unlink(missing_ok=True)
        raise


def _write_signed_payload(request: ComposeStageRequest, path: Path) -> tuple[Path | None, str, str]:
    source_fd, source_name = tempfile.mkstemp(prefix="fio-compose-source-", suffix=Path(request.unsigned_name).suffix)
    os.close(source_fd)
    source_path = Path(source_name)
    output_fd, output_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".signed", dir=str(path.parent))
    os.close(output_fd)
    output_path = Path(output_name)
    output_path.unlink(missing_ok=True)
    passphrase = ""
    try:
        source_path.write_text(request.payload, encoding="utf-8")
        ok, detail = clearsign_file(
            source_path,
            output_path=output_path,
            configured_path=request.gpg_path,
            signer_fingerprint=request.signer_fingerprint,
        )
        if not ok and gpg_detail_indicates_passphrase_needed(detail):
            passphrase, secret_error = load_gpg_signing_passphrase(request.signer_fingerprint)
            if not passphrase:
                return None, "", secret_error or (
                    "Selected signing key requires a passphrase. Save it in Settings > Message Auth."
                )
            ok, detail = clearsign_file(
                source_path,
                output_path=output_path,
                configured_path=request.gpg_path,
                signer_fingerprint=request.signer_fingerprint,
                passphrase=passphrase,
            )
        if not ok:
            return None, "", detail
        verification = verify_file_with_discovery(
            output_path,
            configured_path=request.gpg_path,
            trusted_fingerprints=request.trusted_fingerprints,
            allow_inline_clearsigned=True,
        )
        if verification.status != "valid":
            return None, "", f"signature verification failed: {verification.detail}"
        published = _publish_temp_file(output_path, path)
        return published, f"FLAmp signed file verified: {published.name}", ""
    finally:
        passphrase = ""
        source_path.unlink(missing_ok=True)
        output_path.unlink(missing_ok=True)


def _publish_to_managed_bbs(
    request: ComposeStageRequest,
    output_by_key: Mapping[str, Path],
) -> tuple[str, str]:
    preferred_key = "flamp" if any(plan.key == "flamp" and plan.requested for plan in request.plans) else "flmsg"
    source_path = output_by_key.get(preferred_key)
    if source_path is None:
        raise RuntimeError(
            "Managed BBS publication was not applied because the selected "
            f"{preferred_key.upper()} artifact was not staged successfully."
        )
    if not request.bbs_db_path:
        raise RuntimeError("Managed BBS database is not configured.")
    if not request.bbs_location_ids:
        raise RuntimeError("Select at least one enabled Managed BBS location.")
    metadata = {str(key): str(value) for key, value in request.metadata}
    metadata.update({"origin": "compose", "radio_id": request.radio_id, "file_type": preferred_key})
    with connect_sqlite(Path(request.bbs_db_path)) as conn:
        ensure_bbs_library_schema(conn)
        with conn:
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            for location_id in request.bbs_location_ids:
                row = conn.execute("SELECT enabled FROM bbs_locations WHERE location_id=?", (location_id,)).fetchone()
                if row is None or not row[0]:
                    raise ValueError("A selected Managed BBS location is missing or disabled. Review destinations and retry.")
            artifact_id = upsert_bbs_artifact_path(
                conn,
                source_path=source_path,
                source_kind="compose_file",
                source_id=request.source_id,
                display_name=source_path.name,
                metadata=metadata,
            )
            set_bbs_artifact_locations(
                conn,
                artifact_id=artifact_id,
                location_ids=request.bbs_location_ids,
            )
    return artifact_id, str(source_path)


def stage_compose_request(request: ComposeStageRequest) -> ComposeStageResult:
    """Stage one immutable request; safe to call from a non-GUI worker."""

    outputs: list[str] = []
    output_by_key: dict[str, Path] = {}
    skipped = [plan.note for plan in request.plans if plan.requested and not plan.ready and plan.note]
    signature_notes: list[str] = []
    problems: list[str] = []

    if request.sign_flamp and not request.signer_fingerprint:
        problems.append("Select a private signing key before staging a signed FLAmp copy.")
        return ComposeStageResult(
            generation=request.generation,
            skipped=tuple(skipped),
            problems=tuple(problems),
        )

    for plan in request.plans:
        if not plan.requested or not plan.ready:
            continue
        destination = Path(plan.path)
        try:
            if plan.key == "flamp" and request.sign_flamp:
                published, note, error = _write_signed_payload(request, destination)
                if published is None:
                    problems.append(f"FLAmp signing failed; no unsigned FLAmp fallback was staged. {error}")
                    continue
                output_path = published
                signature_notes.append(note)
            else:
                output_path = _write_payload(destination, request.payload)
            outputs.append(str(output_path))
            output_by_key[plan.key] = output_path
        except Exception as exc:
            problems.append(f"{plan.label}: {exc}")

    bbs_artifact_id = ""
    bbs_publish_path = ""
    if request.publish_to_bbs:
        try:
            bbs_artifact_id, bbs_publish_path = _publish_to_managed_bbs(request, output_by_key)
        except Exception as exc:
            problems.append(f"Managed BBS: {exc}")

    return ComposeStageResult(
        generation=request.generation,
        outputs=tuple(outputs),
        output_by_key=tuple((key, str(value)) for key, value in sorted(output_by_key.items())),
        skipped=tuple(skipped),
        signature_notes=tuple(signature_notes),
        problems=tuple(problems),
        bbs_artifact_id=bbs_artifact_id,
        bbs_publish_path=bbs_publish_path,
        bbs_location_ids=request.bbs_location_ids if bbs_artifact_id else (),
        bbs_location_names=request.bbs_location_names if bbs_artifact_id else (),
    )
