"""Reviewed, exact-target BBS synchronization; no radio-control commands."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from freqinout.core.config_backup import create_config_backup, restore_config_backup
from freqinout.core.config_varac_managed import _stage_bytes, read_varac_ini_source
from freqinout.core.varac_bbs_config import (
    load_varac_bbs_config, render_varac_bbs_config_bytes, varac_path_to_host_path,
    parse_varac_bool,
)
from freqinout.core.varac_native_transaction import _backup_payload


@dataclass(frozen=True)
class BbsSyncTarget:
    radio_id: int
    radio_name: str
    path: Path
    source_digest: str
    payload: bytes
    before_json: str
    after_json: str


@dataclass(frozen=True)
class BbsSyncPlan:
    radio_id: int
    cluster: bool
    configuration_fingerprint: str
    targets: tuple[BbsSyncTarget, ...]


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _station_values(store):
    from freqinout.core.sqlite_utils import connect_sqlite
    with connect_sqlite(store.db_path) as conn:
        rows = dict(conn.execute("SELECT key,value FROM bbs_library_meta WHERE key IN "
                                 "('station_allowed_callsigns','station_limit_access_enabled')").fetchall())
    return {"limit_access": rows.get("station_limit_access_enabled", "0") == "1",
            "allowed_callsigns": str(rows.get("station_allowed_callsigns", "") or "").split(",")}


def _targets(store, radio_id, cluster):
    profiles = {int(p["id"]): p for p in store.list_device_profiles()}
    selected = profiles.get(int(radio_id))
    if not selected or not selected.get("use_varac"):
        raise ValueError("Select a configured VarAC radio")
    memberships = [m for m in store.list_varac_cluster_members(device_profile_id=radio_id) if m.get("enabled")]
    if len(memberships) > 1:
        raise ValueError("Radio has ambiguous cluster membership")
    members = memberships
    if cluster:
        if not memberships:
            raise ValueError("The selected radio has no enabled VarAC cluster")
        members = [m for m in store.list_varac_cluster_members(cluster_id=memberships[0]["cluster_id"]) if m.get("enabled")]
    ids = [int(m["device_profile_id"]) for m in members] if cluster else [int(radio_id)]
    if not ids or len(ids) > 32:
        raise ValueError("Cluster update scope is empty or exceeds 32 radios")
    result, seen = [], set()
    for rid in ids:
        profile = profiles.get(rid)
        if not profile or not profile.get("use_varac"):
            raise ValueError("Every enabled cluster member needs a configured VarAC radio")
        node = store.get_varac_node(int(profile.get("varac_node_id") or 0))
        ini = str((node or {}).get("ini_path") or "").strip()
        if not ini:
            raise ValueError(f"{profile['name']}: configure an exact VarAC INI in Radio Settings")
        path = Path(varac_path_to_host_path(ini, ini_path=profile.get("varac_install_path", ""))).expanduser()
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"{profile['name']}: VarAC INI is unavailable or is a symbolic link")
        path = path.resolve()
        if path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("VarAC INI exceeds the safe review size")
        identity = (path.stat().st_dev, path.stat().st_ino)
        if str(path).casefold() in seen or identity in seen:
            raise ValueError("Different radios resolve to the same VarAC INI")
        seen.update((str(path).casefold(), identity))
        member = next((m for m in members if int(m["device_profile_id"]) == rid), None)
        result.append((profile, path, member))
    return result


def _configuration(store, radio_id, cluster):
    targets = _targets(store, radio_id, cluster)
    station = _station_values(store)
    snapshot = [(p["id"], str(path), p.get("varac_node_id"),
                 p.get("varac_bbs_enabled"), p.get("varac_bbs_dir"), p.get("varac_bbs_announce_enabled"),
                 dict(member) if member else None) for p, path, member in targets]
    return targets, station, _digest(json.dumps([snapshot, station], sort_keys=True, default=str).encode())


def get_bbs_from_varac(store, radio_id):
    """Read selected member only; import of these values is a separate FIO save."""
    profile, path, _ = _targets(store, radio_id, False)[0]
    digest = _digest(path.read_bytes())
    values = load_varac_bbs_config(path)
    if _digest(path.read_bytes()) != digest:
        raise RuntimeError("VarAC source changed while reading; Get from VarAC again")
    return dict(values, radio_id=int(profile["id"]), radio_name=profile["name"], source_digest=digest)


def load_station_access(store):
    from freqinout.core.bbs_access_policy import normalize_access_policy
    from freqinout.core.sqlite_utils import connect_sqlite
    with connect_sqlite(store.db_path) as conn:
        rows = dict(conn.execute("SELECT key,value FROM bbs_library_meta WHERE key IN "
                                 "('station_allowed_callsigns','station_limit_access_enabled','station_access_policy_v1')").fetchall())
    return dict(policy=normalize_access_policy(rows.get("station_access_policy_v1"), rows.get("station_allowed_callsigns", "")),
                limit_access=rows.get("station_limit_access_enabled") == "1")


def save_station_access(store, policy, limit_access):
    from freqinout.core.bbs_access_policy import normalize_access_policy, effective_callsigns
    from freqinout.core.sqlite_utils import connect_sqlite
    p = normalize_access_policy(policy)
    calls = effective_callsigns(p)
    if limit_access and not calls:
        raise ValueError("Choose at least one allowed callsign before limiting native BBS connections")
    with connect_sqlite(store.db_path) as conn, conn:
        for key, value in {
            "station_allowed_callsigns": ",".join(calls),
            "station_limit_access_enabled": "1" if limit_access else "0",
            "station_access_policy_v1": json.dumps(p, sort_keys=True, separators=(",", ":")),
        }.items():
            conn.execute("INSERT INTO bbs_library_meta(key,value) VALUES (?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def import_bbs_from_varac(store, values):
    """Confirmed import: FIO transaction only; no INI file writes."""
    from freqinout.core.bbs_access_policy import normalize_access_policy
    current = get_bbs_from_varac(store, int(values["radio_id"]))
    if current["ini_path"] != values["ini_path"] or current["source_digest"] != values["source_digest"]:
        raise RuntimeError("VarAC source changed after review; Get from VarAC again")
    # Same SQLite connection for service and station admission intent.
    with store.guided_save_transaction() as transaction:
        checked = get_bbs_from_varac(store, int(values["radio_id"]))
        if checked["ini_path"] != values["ini_path"] or checked["source_digest"] != values["source_digest"]:
            raise RuntimeError("VarAC source changed before CommsPlan import")
        store.save_device_profile({"id": int(values["radio_id"]), "varac_bbs_enabled": values["enable_bbs"],
                                  "varac_bbs_dir": values["bbs_directory"], "varac_bbs_announce_enabled": values["announce"]})
        with store._connect() as conn:
            p = normalize_access_policy(None, values["allowed_callsigns"])
            for key, value in {"station_allowed_callsigns": ",".join(p["manual_callsigns"]),
                               "station_limit_access_enabled": "1" if values["limit_access"] else "0",
                               "station_access_policy_v1": json.dumps(p, sort_keys=True, separators=(",", ":"))}.items():
                conn.execute("INSERT INTO bbs_library_meta(key,value) VALUES (?,?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        if _digest(Path(values["ini_path"]).read_bytes()) != values["source_digest"]:
            raise RuntimeError("VarAC source changed during CommsPlan import; nothing was saved")
        transaction.complete()
    return values


def build_bbs_sync_plan(store, radio_id, *, cluster=False):
    targets, station, fingerprint = _configuration(store, radio_id, cluster)
    planned = []
    for profile, path, member in targets:
        if path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("VarAC INI exceeds the safe update size")
        source = read_varac_ini_source(path)
        native_cluster = next((v for k, v in source.values.items() if k.casefold() == "varac_cluster"), {})
        fields = {k.casefold(): v for k, v in native_cluster.items()}
        enabled = parse_varac_bool(fields.get("clusterenabled"), False)
        if enabled != bool(member):
            raise ValueError(f"{profile['name']}: CommsPlan and native VarAC cluster modes disagree; review cluster settings first")
        if member and str(fields.get("instancenumber", "")).strip() != str(member["instance_number"]):
            raise ValueError(f"{profile['name']}: native cluster instance number disagrees with CommsPlan")
        after = dict(station, enable_bbs=bool(profile.get("varac_bbs_enabled")),
                     bbs_directory=str(profile.get("varac_bbs_dir") or ""),
                     announce=bool(profile.get("varac_bbs_announce_enabled")))
        payload = render_varac_bbs_config_bytes(source.raw_bytes, **after)
        before = dict(load_varac_bbs_config(path))
        if _digest(path.read_bytes()) != source.digest:
            raise RuntimeError("VarAC INI changed while preparing review")
        planned.append(BbsSyncTarget(int(profile["id"]), str(profile["name"]), path, source.digest,
                                     payload, json.dumps(before, sort_keys=True), json.dumps(after, sort_keys=True)))
    return BbsSyncPlan(int(radio_id), bool(cluster), fingerprint, tuple(planned))


def varac_is_running():
    """Conservative global stopped-process gate, including Wine-launched VarAC."""
    import psutil
    for proc in psutil.process_iter(["name", "exe", "cmdline"]):
        try:
            name = str(proc.info.get("name") or "").casefold()
            exe = str(proc.info.get("exe") or "").replace("\\", "/").rsplit("/", 1)[-1].casefold()
            cmd = proc.info.get("cmdline")
            if name in {"varac", "varac.exe"} or exe in {"varac", "varac.exe"}:
                return True
            if cmd and any(str(arg).replace("\\", "/").rsplit("/", 1)[-1].casefold() in {"varac", "varac.exe"} for arg in cmd):
                return True
            if name.startswith("wine") and cmd is None:
                raise RuntimeError("Cannot verify whether Wine is running VarAC; close VarAC before updating")
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied as exc:
            raise RuntimeError("Cannot verify that VarAC is stopped") from exc
    return False


def apply_bbs_sync_plan(store, plan, *, backup_root, running_checker=varac_is_running, failure_injector=None):
    """Journal/backup/stage/promote/readback, compensating partial multi-INI writes."""
    if store.list_unfinished_varac_native_applies():
        raise RuntimeError("Unfinished native VarAC update requires recovery before another update")
    _, _, fingerprint = _configuration(store, plan.radio_id, plan.cluster)
    if fingerprint != plan.configuration_fingerprint:
        raise RuntimeError("Saved CommsPlan configuration changed after review; review again")
    if running_checker():
        raise RuntimeError("Close all VarAC instances before Update VarAC; CommsPlan will not stop them")
    for target in plan.targets:
        if target.path.is_symlink() or _digest(target.path.read_bytes()) != target.source_digest:
            raise RuntimeError("VarAC INI changed after review; Get from VarAC or review again")
    changed = tuple(t for t in plan.targets if _digest(t.payload) != t.source_digest)
    if not changed:
        return {"updated": 0, "backup_dir": "", "journal_id": ""}
    entry = str(uuid.uuid4())
    store.begin_varac_native_apply_journal(journal_id=entry, plan_fingerprint=plan.configuration_fingerprint,
        operation="bbs-sync", writer_key="varac:bbs-adapter-v1", targets=[str(t.path) for t in changed],
        desired={str(t.path): _digest(t.payload) for t in changed}, exclusive=True)
    state, backup, staged, promoted = "pending", None, {}, []
    def transition(next_state, **kwargs):
        nonlocal state
        store.update_varac_native_apply_journal(entry, state=next_state, expected_states=(state,), **kwargs)
        state = next_state
    try:
        backup = create_config_backup((t.path for t in changed), reason="varac-bbs-sync", backup_root=Path(backup_root))
        if any(item.status != "backed_up" for item in backup.items):
            raise OSError("Unable to back up every VarAC INI")
        transition("backup_ready", backup_manifest=_backup_payload(backup))
        # Verify backup reflects the reviewed bytes before any promotion.
        for item, target in zip(backup.items, changed):
            if _digest(Path(item.backup_path).read_bytes()) != target.source_digest:
                raise RuntimeError("VarAC INI changed while being backed up")
            staged[target.path] = _stage_bytes(target.path, target.payload)
            os.chmod(staged[target.path], target.path.stat().st_mode & 0o777)
        transition("promoting")
        for index, target in enumerate(changed):
            if failure_injector:
                failure_injector(index)
            if running_checker() or target.path.resolve() != target.path or target.path.is_symlink() or _digest(target.path.read_bytes()) != target.source_digest:
                raise RuntimeError("VarAC started or INI changed during update; update stopped")
            _, _, fingerprint = _configuration(store, plan.radio_id, plan.cluster)
            if fingerprint != plan.configuration_fingerprint:
                raise RuntimeError("CommsPlan configuration changed during update")
            os.replace(staged[target.path], target.path)
            promoted.append(target)
            if os.name != "nt":
                fd = os.open(target.path.parent, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            if target.path.read_bytes() != target.payload:
                raise OSError("VarAC INI readback did not match the reviewed update")
        transition("external_applied", observed={str(t.path): _digest(t.path.read_bytes()) for t in changed})
        # FIO intent was already saved; there is no additional configuration mutation.
        transition("fio_committed")
        transition("complete")
        return {"updated": len(changed), "backup_dir": backup.backup_dir, "journal_id": entry}
    except Exception as exc:
        if state == "fio_committed":
            # A fully verified update is recoverable as committed, not rolled back.
            raise RuntimeError("Update verified; journal completion needs recovery") from exc
        recovery = False
        if promoted:
            from dataclasses import replace
            selected_paths = {str(t.path) for t in promoted}
            partial = replace(backup, items=tuple(i for i in backup.items if i.original_path in selected_paths))
            recovery = not restore_config_backup(partial).ok
        transition("recovery_required" if recovery else "rolled_back", error=str(exc))
        raise
    finally:
        for path in staged.values():
            path.unlink(missing_ok=True)
