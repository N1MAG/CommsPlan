"""Explicit, profile-owned manual forwarding. Called only by background jobs.

No autonomous dispatcher exists: loading, saving and restarting cannot send.
Transport owners are injected; immutable reviewed identities are re-resolved.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import re
import sqlite3
import threading
import time
from typing import Callable

from freqinout.core.manual_forward_payload import prepare_forward_payload
from freqinout.core.manual_forward_store import ForwardStore
from freqinout.core.mesh.settings import load_mesh_connection_configs, mesh_outbound_capability
from freqinout.core.mesh.store import list_mesh_channel_policies, list_mesh_nodes, mesh_send_policy_issue
from freqinout.core.mesh.models import MeshSendRequest
from freqinout.core.multi_radio_store import MultiRadioStore
from freqinout.core.sqlite_utils import connect_sqlite_readonly


def fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def _enabled(value: object) -> bool:
    return value is True or str(value).strip().lower() in {"1", "true", "yes", "on"}


def _js8_endpoint_identity(profile):
    host = str(profile.get("js8_host") or "").strip().casefold()
    host = "loopback" if host in {"127.0.0.1", "localhost", "::1"} else host
    return host, str(profile.get("js8_port") or "")


@dataclass(frozen=True)
class ForwardSettingsReader:
    """Worker-safe, fresh reads from the owner's explicit initialized profile.

    Carries no GUI SettingsManager or shared SQLite connection. Construction is
    I/O-free; each worker read closes its own read-only connection.
    """
    db_path: Path

    @staticmethod
    def _decode(value):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value

    def all(self):
        with closing(connect_sqlite_readonly(self.db_path)) as conn:
            return {key: self._decode(value) for key, value in conn.execute("SELECT key,value FROM kv")}

    def get(self, key, default=None):
        with closing(connect_sqlite_readonly(self.db_path)) as conn:
            row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            return self._decode(row[0]) if row else default


def validate_mesh_forward_at_io(request, config):
    """Run inside the native adapter/child process, after any queue or lock wait."""
    if not request.expected_connection_fingerprint:
        return
    if fingerprint(asdict(config)) != request.expected_connection_fingerprint:
        raise RuntimeError("The reviewed Mesh connection changed before adapter submission.")
    issue = forward_authorization_reason(request.forwarding_db_path, request.forwarding_relay_id, request.forwarding_attempt)
    age = (datetime.now(timezone.utc) - request.requested_at).total_seconds()
    if issue or age < 0 or age > request.timeout_sec:
        raise RuntimeError(issue or "The forwarding request expired in the adapter queue.")


def forward_authorization_reason(db_path, relay_id, attempt):
    """Final worker-side check of lease, source, policy and reviewed configuration."""
    try:
        with connect_sqlite_readonly(db_path, row_factory=sqlite3.Row) as conn:
            row = conn.execute("SELECT * FROM manual_forward_intents WHERE relay_id=?", (relay_id,)).fetchone()
            if row is None or row["state"] != "dispatching" or row["attempts"] != attempt or row["expires"] <= time.time():
                return "The forwarding authorization expired or changed before dispatch."
            if hashlib.sha256(row["request_json"].encode("utf-8")).hexdigest() != row["request_hash"]:
                return "The persisted forwarding review could not be verified."
            request = json.loads(row["request_json"])
        backend = ManualForwardBackend(db_path, None, guard=None, js8_submit=None, mesh_submit=None)
        source = backend.resolve_source(request["source"])
        if source["version"] != request["source"].get("version"):
            return "The source message or its policy changed before dispatch."
        reason = backend.source_policy_reason(source)
        if reason:
            return reason
        dest = request["destination"]
        settings = ForwardSettingsReader(Path(request["settings_db_path"]))
        if float(settings.get("schedule_suspend_until", 0) or 0) > time.time():
            return "The station is suspended. Review station controls before sending."
        if dest["transport"] == "js8":
            store = MultiRadioStore(Path(request["settings_db_path"]))
            profile = store.get_device_profile(dest["identity"]["radio_id"])
            if not profile or fingerprint(profile) != dest["identity"]["config_hash"]:
                return "The reviewed radio configuration changed before dispatch."
            endpoint = _js8_endpoint_identity(profile)
            owners = [p for p in store.list_device_profiles()
                      if _enabled(p.get("runtime_active")) and _enabled(p.get("enabled", True))
                      and _enabled(p.get("use_js8call")) and _js8_endpoint_identity(p) == endpoint]
            if len(owners) != 1 or int(owners[0]["id"]) != int(profile["id"]):
                return "The reviewed JS8Call endpoint ownership changed before dispatch."
        else:
            config = next((c for c in load_mesh_connection_configs(settings.all()) if c.adapter_id == dest["identity"]["adapter_id"]), None)
            if config is None or fingerprint(asdict(config)) != dest["identity"]["config_hash"]:
                return "The reviewed Mesh configuration changed before dispatch."
        if row["expires"] <= time.time():
            return "The forwarding authorization expired during its final safety check."
        return ""
    except Exception:
        return "The forwarding authorization could not be verified before dispatch."


class ManualForwardBackend:
    def __init__(self, db_path: Path, settings, *, guard: Callable[[dict], str],
                 js8_submit: Callable[[dict, str, Callable[[], str]], tuple[str, str]],
                 mesh_submit: Callable[[MeshSendRequest], tuple[str, str]],
                 stopped: threading.Event | None = None, describe=None, js8_selection=None):
        self.db_path, self.settings = Path(db_path), settings
        self.guard, self.js8_submit, self.mesh_submit = guard, js8_submit, mesh_submit
        self.stopped = stopped or threading.Event()
        self.describe = describe or (lambda destination: destination)
        self.js8_selection = js8_selection
        self._store = None

    @property
    def store(self):
        if self._store is None:
            self._store = ForwardStore(self.db_path)
            self._store.recover()
        return self._store

    def resolve_source(self, source: dict) -> dict:
        """Read original retained receipt, never the Inbox's truncated preview."""
        source = dict(source)
        for key in tuple(source):
            if key == "relay_id" or key.startswith("draft_"):
                source.pop(key)
        with connect_sqlite_readonly(self.db_path, row_factory=sqlite3.Row) as conn:
            message_id = str(source.get("message_id") or "")
            if message_id:
                projection = conn.execute("SELECT * FROM message_projection WHERE message_id=?", (message_id,)).fetchone()
                if projection is None or projection["deleted"] or not projection["inbox_visible"]:
                    raise ValueError("This source message is no longer available in the Inbox.")
                refs = conn.execute("SELECT * FROM message_external_refs WHERE message_id=? ORDER BY id LIMIT 50", (message_id,)).fetchall()
                mesh_ref = next((r for r in refs if r["external_kind"] == "mesh_message"), None)
                js8_ref = next((r for r in refs if r["external_kind"] == "js8_message"), None)
                if mesh_ref is not None:
                    row = conn.execute("SELECT * FROM mesh_messages WHERE source_ref=?", (mesh_ref["external_key"],)).fetchone()
                    if row is None:
                        raise ValueError("The full original Mesh receipt is unavailable.")
                    source.update(family=row["transport"], sender=row["from_node"], body=row["text"],
                                  adapter_id=row["adapter_id"], channel_id=row["channel"], source_ref=row["source_ref"],
                                  native_kind="mesh", native_id=row["source_ref"])
                elif js8_ref is not None:
                    metadata = json.loads(js8_ref["metadata_json"] or "{}")
                    source.update(native_kind="js8", native_id=metadata.get("row_id"))
                else:
                    raise ValueError("This message has no supported original Mesh or JS8Call receipt.")
            if source.get("native_kind") == "js8":
                row = conn.execute("SELECT * FROM js8_messages WHERE id=?", (source.get("native_id"),)).fetchone()
                if row is None:
                    raise ValueError("The full original JS8Call receipt is unavailable.")
                source.update(family="js8", sender=row["from_call"], body=row["raw_text"])
            if source.get("native_kind") not in {"mesh", "js8"}:
                raise ValueError("Original message provenance is unavailable; forwarding is held.")
            if source.get("native_kind") == "mesh" and not message_id:
                raise ValueError("Mesh forwarding requires its canonical Inbox receipt.")
        source["source_id"] = str(source.get("source_id") or message_id)
        family = source.get("family")
        if family not in {"meshcore", "meshtastic", "js8"}:
            raise ValueError("This message source does not support manual forwarding.")
        if not source.get("sender") or not str(source.get("body") or "").strip():
            raise ValueError("The original sender or complete message body is unavailable.")
        if re.search(r"\bCP\s+[A-F0-9]{12}\s+FROM\s", str(source["body"]), re.I):
            raise ValueError("This is already a CommsPlan forwarded message; another hop is not supported.")
        source["source_label"] = source.get("source_label") or str(family)
        if family != "js8":
            policies = list_mesh_channel_policies(self.db_path, adapter_id=source.get("adapter_id", ""), read_only=True)
            policy = next((p for p in policies if p.channel_id == str(source.get("channel_id") or "")), None)
            if policy is None or not policy.is_accepted or not policy.inbox_enabled:
                raise ValueError("The source channel is no longer accepted for Inbox reading. Review its Local Mesh policy.")
            policy_values = asdict(policy)
            policy_values.pop("updated_utc", None)
            source["policy_fingerprint"] = fingerprint(policy_values)
        source["version"] = fingerprint({k: source.get(k) for k in ("source_id", "family", "sender", "body", "adapter_id", "channel_id", "native_kind", "native_id", "policy_fingerprint")})
        return source

    def source_policy_reason(self, source: dict) -> str:
        if source.get("family") == "js8":
            return ""
        policies = list_mesh_channel_policies(self.db_path, adapter_id=source.get("adapter_id", ""), read_only=True)
        policy = next((p for p in policies if p.channel_id == str(source.get("channel_id") or "")), None)
        if policy is None or not policy.is_accepted or not policy.inbox_enabled:
            return "Review and accept the source channel's Inbox policy in Local Mesh before forwarding."
        if policy.channel_privacy != "public" or policy.channel_role != "public":
            return "This private, direct or unknown Mesh feed has no reviewed permission for HF forwarding."
        return ""

    def destinations(self, source: dict) -> list[dict]:
        out = []
        if source.get("family") in {"meshcore", "meshtastic"}:
            store = MultiRadioStore(Path(self.settings.db_path))
            profiles = store.list_device_profiles()
            owners = {}
            for profile in profiles:
                if _enabled(profile.get("runtime_active")) and _enabled(profile.get("enabled", True)) and _enabled(profile.get("use_js8call")):
                    key = _js8_endpoint_identity(profile)
                    owners[key] = owners.get(key, 0) + 1
            for profile in profiles:
                if not _enabled(profile.get("enabled", True)) or not _enabled(profile.get("runtime_active")):
                    continue
                if profile.get("device_class") == "observer" or not profile.get("js8_instance_id"):
                    continue
                if not _enabled(profile.get("use_js8call")) or not profile.get("js8_instance_name"):
                    continue
                host, port = str(profile.get("js8_host") or ""), profile.get("js8_port")
                if not host or not port:
                    continue
                identity = {"radio_id": int(profile["id"]), "instance_id": str(profile["js8_instance_id"]), "host": host, "port": int(port), "config_hash": fingerprint(profile)}
                reason = "This JS8Call endpoint has multiple active owners. Review radio configuration before forwarding." if owners.get(_js8_endpoint_identity(profile), 0) > 1 else ""
                out.append(dict(id="js8:" + str(profile["id"]), label=f"{profile.get('name') or 'Radio'} · JS8Call {profile['js8_instance_name']}",
                                transport="js8", max_bytes=512, identity=identity, fingerprint=fingerprint(identity), reason=reason,
                                connection_id="js8:" + str(profile["id"]), connection_label=f"{profile.get('name') or 'Radio'} · {profile['js8_instance_name']}"))
        else:
            for config in load_mesh_connection_configs(self.settings.all()):
                if not config.enabled or not config.send_enabled or not mesh_outbound_capability(config.protocol, config.connection_type).supported:
                    continue
                config_hash = fingerprint(asdict(config))
                policies = list_mesh_channel_policies(self.db_path, adapter_id=config.adapter_id, read_only=True)
                channels = [p for p in policies if p.is_accepted and p.key_available
                            and str(p.channel_id).isdigit() and 0 <= int(p.channel_id) <= 7
                            and p.channel_role != "direct"]
                for policy in channels:
                    identity = dict(adapter_id=config.adapter_id, protocol=config.protocol, config_hash=config_hash,
                                    kind="channel", channel_id=policy.channel_id, destination_id="")
                    out.append(self._mesh_destination(config, identity, f"Channel {policy.channel_id} · {policy.display_name}"))
                identity = dict(adapter_id=config.adapter_id, protocol=config.protocol, config_hash=config_hash,
                                kind="connection", channel_id="0", destination_id="")
                out.append(self._mesh_destination(config, identity, "Find a direct node"))
        return out

    @staticmethod
    def _mesh_destination(config, identity, label):
        digest = fingerprint(identity)
        protocol_label = "MeshCore" if config.protocol == "meshcore" else "Meshtastic"
        return dict(id="mesh:" + digest, label=f"{protocol_label} · {config.display_name} · {label}", transport=config.protocol,
                    max_bytes=160 if config.protocol == "meshcore" else 220, identity=identity, fingerprint=digest,
                    connection_id=f"mesh:{config.adapter_id}:{identity['config_hash']}",
                    connection_label=f"{protocol_label} · {config.display_name}",
                    recipient_kind=identity["kind"], recipient_label=label,
                    search_text=label + " " + identity["destination_id"])

    def lookup_nodes(self, source, chosen, *, query="", offset=0, target=""):
        if source.get("family") != "js8":
            return [], False
        ident = chosen.get("identity") or {}
        config = next((c for c in load_mesh_connection_configs(self.settings.all())
                       if c.adapter_id == ident.get("adapter_id") and c.enabled and c.send_enabled
                       and fingerprint(asdict(c)) == ident.get("config_hash")
                       and mesh_outbound_capability(c.protocol, c.connection_type).supported), None)
        if config is None:
            raise ValueError("The selected connection changed or is unavailable. Open a fresh review.")
        channel = str(ident.get("channel_id") or "0")
        if config.protocol == "meshtastic":
            policies = list_mesh_channel_policies(self.db_path, adapter_id=config.adapter_id, read_only=True)
            if not any(p.channel_id == channel and p.is_accepted and p.key_available and p.channel_role != "direct"
                       and channel.isdigit() and 0 <= int(channel) <= 7 for p in policies):
                raise ValueError("The selected channel policy changed. Refresh and review the channel before finding nodes.")
        rows = list_mesh_nodes(self.db_path, adapter_id=config.adapter_id, query=str(query)[:200],
                               offset=max(0, int(offset)), destination_id=target, native_identity=config.protocol, transport=config.protocol, limit=101, read_only=True)
        options = []
        for row in rows[:100]:
            native = str(row.get("public_key_or_hash") if config.protocol == "meshcore" else row.get("node_id") or "").strip()
            if not native or (config.protocol == "meshcore" and (not re.fullmatch(r"[A-Fa-f0-9]{12,64}", native) or len(native) % 2)):
                continue
            identity = dict(adapter_id=config.adapter_id, protocol=config.protocol, config_hash=fingerprint(asdict(config)),
                            kind="direct", channel_id=channel if config.protocol == "meshtastic" else "0", destination_id=native)
            label = f"{row.get('short_name') or row.get('long_name') or row.get('callsign') or native} · {native}"
            option = self._mesh_destination(config, identity, label)
            option["search_text"] += " " + " ".join(str(row.get(k) or "") for k in ("callsign", "long_name", "short_name"))
            options.append(option)
        return options, len(rows) > 100

    def js8_suggestions(self):
        suggestions = {}
        def add(value, group=""):
            value = str(value or "").strip().upper()
            if not value:
                return
            kind = "group" if value.startswith("@") else "callsign"
            checked = prepare_forward_payload(transport="js8", sender="SOURCE", body="Body", relay_id="REFERENCE", recipient=value)
            if checked.issues:
                return
            item = suggestions.setdefault(value, dict(value=value, kind=kind, groups=[]))
            if group and group not in item["groups"]:
                item["groups"].append(group)
        values = self.settings.all()
        for key in ("operating_groups", "local_net_profiles"):
            for record in values.get(key, []) or []:
                if isinstance(record, dict):
                    group = str(record.get("group") or record.get("group_name") or record.get("name") or "").strip().lstrip("@")
                    if group:
                        add("@" + group)
        with closing(connect_sqlite_readonly(self.db_path)) as conn:
            columns = {r[1] for r in conn.execute("PRAGMA table_info(js8_messages)")}
            if {"from_call", "to_call"} <= columns:
                for sender, recipient in conn.execute("SELECT from_call,to_call FROM js8_messages ORDER BY id DESC LIMIT 2000"):
                    recipient = str(recipient or "").strip().upper()
                    add(recipient)
                    add(sender, recipient if recipient.startswith("@") else "")
        return sorted(suggestions.values(), key=lambda x: x["value"])

    def __call__(self, action: str, data: dict) -> dict:
        if self.stopped.is_set():
            return dict(ok=False, detail="Forwarding stopped with the application.")
        if action == "activity":
            return dict(ok=True, records=self.store.list(source_id=data.get("source_id", "")))
        if action == "cancel":
            return self.store.cancel(data.get("relay_id", ""))
        source = self.resolve_source(data.get("source") or {})
        reason = self.source_policy_reason(source)
        if action == "lookup":
            options, more = self.lookup_nodes(source, data.get("destination") or {}, query=data.get("query", ""), offset=data.get("offset", 0))
            return dict(ok=True, options=options, has_more=more, next_offset=max(0, int(data.get("offset", 0))) + 100)
        choices = [self.describe(d) for d in self.destinations(source)]
        if action == "context":
            draft = (data.get("source") or {}).get("draft_destination") or {}
            if draft and source.get("family") == "js8" and (draft.get("identity") or {}).get("kind") == "direct":
                exact, _ = self.lookup_nodes(source, draft, target=str(draft["identity"].get("destination_id") or ""))
                choices.extend(self.describe(d) for d in exact if d["id"] not in {c["id"] for c in choices})
            return dict(ok=True, source=source, destinations=choices, js8_suggestions=self.js8_suggestions() if source.get("family") != "js8" else [], reason=reason or ("No send-enabled destination is configured." if not choices else ""))
        request = dict(data)
        reviewed = data.get("source") or {}
        if reviewed.get("version") != source.get("version"):
            return dict(ok=False, detail="The source message changed. Close this review and open it again.")
        request["source"] = source
        mode = data.get("recipient_type")
        chosen = data.get("destination") or {}
        destination = next((d for d in choices if d["id"] == chosen.get("id") and d["fingerprint"] == chosen.get("fingerprint")), None)
        if destination is None and source.get("family") == "js8" and (chosen.get("identity") or {}).get("kind") == "direct":
            exact, _more = self.lookup_nodes(source, chosen, target=str(chosen["identity"].get("destination_id") or ""))
            destination = next((d for d in exact if d["id"] == chosen.get("id") and d["fingerprint"] == chosen.get("fingerprint")), None)
            if destination is not None:
                destination = self.describe(destination)
        if destination is None:
            return dict(ok=False, detail="The selected destination changed or is unavailable. Open a fresh review.")
        if action in {"target_check", "target_clear"}:
            if destination["transport"] != "js8" or destination.get("reason") or not self.js8_selection:
                return dict(ok=False, detail=destination.get("reason") or "JS8 selection control is unavailable for this destination.")
            def cancelled():
                if self.stopped.is_set():
                    return True
                try:
                    return not any(d["id"] == destination["id"] and d["fingerprint"] == destination["fingerprint"] and not d.get("reason") for d in self.destinations(source))
                except Exception:
                    return True
            return self.js8_selection(destination, action == "target_clear", str(data.get("expected_target") or ""), cancelled)
        kind = (destination.get("identity") or {}).get("kind")
        if destination["transport"] == "js8":
            if mode is not None and (mode not in {"group", "callsign"} or (mode == "group") != str(data.get("recipient") or "").strip().startswith("@")):
                return dict(ok=False, detail="Recipient does not match the selected Group/Callsign type.")
        elif kind not in {"channel", "direct"} or (mode is not None and mode != kind) or (kind == "direct" and not (destination.get("identity") or {}).get("destination_id")):
            return dict(ok=False, detail="Recipient type does not match the selected Mesh destination.")
        if chosen.get("review_frequency_hz") != destination.get("review_frequency_hz"):
            return dict(ok=False, detail="The selected radio's tuning changed. Open a fresh review before forwarding.")
        request["destination"] = destination
        request["settings_db_path"] = str(self.settings.db_path)
        request["recipient"] = str(data.get("recipient") or "").strip().upper() if destination["transport"] == "js8" else ""
        preview = prepare_forward_payload(transport=destination["transport"], sender=source["sender"], body=source["body"],
                                          relay_id=str(data.get("relay_id") or ""), recipient=str(data.get("recipient") or ""), max_bytes=destination["max_bytes"])
        request["wire_text"] = preview.wire_text
        request["payload_evidence"] = dict(wire_sha256=hashlib.sha256(preview.wire_text.encode("utf-8")).hexdigest(),
                                            wire_bytes=preview.wire_bytes, max_bytes=preview.max_bytes,
                                            notices=list(preview.notices), policy_fingerprint=source.get("policy_fingerprint", ""))
        if action == "save":
            result = self.store.save(request)
            result["detail"] = result.get("detail") or "Draft saved. It will not transmit automatically."
            return result
        if action != "send":
            return dict(ok=False, detail="Unknown forwarding action.")
        if reason or not preview.ok:
            return dict(ok=False, detail=reason or "; ".join(preview.issues))
        if destination.get("reason"):
            return dict(ok=False, detail=destination["reason"])
        guard_reason = self.guard(destination)
        if guard_reason:
            return dict(ok=False, detail=guard_reason)
        saved = self.store.save(request)
        if not saved.get("ok"):
            return saved
        claim = self.store.claim(request)
        if not claim.get("ok"):
            return claim
        relay_id = str(request["relay_id"])
        attempt = int(claim["attempts"])
        def fence():
            if self.stopped.is_set():
                return "Application stopped before dispatch."
            return forward_authorization_reason(self.db_path, relay_id, attempt)
        try:
            if self.stopped.is_set():
                return self.store.finish(relay_id, "failed", "Application stopped before dispatch.", attempt=attempt)
            # Transport callbacks own the final guard/identity check at I/O boundary.
            if destination["transport"] == "js8":
                state, detail = self.js8_submit(destination, preview.wire_text, fence)
            else:
                ident = destination["identity"]
                mesh_request = MeshSendRequest(adapter_id=ident["adapter_id"], text=preview.wire_text,
                                               destination_kind=ident["kind"], destination_id=ident["destination_id"],
                                               channel_id=ident["channel_id"], request_id="cp-forward-" + relay_id,
                                               expected_connection_fingerprint=ident["config_hash"],
                                               forwarding_db_path=str(self.db_path), forwarding_relay_id=relay_id, forwarding_attempt=attempt,
                                               require_ack=ident["kind"] == "direct")
                issue = mesh_send_policy_issue(self.db_path, mesh_request, transport=destination["transport"])
                if issue:
                    return self.store.finish(relay_id, "failed", issue, attempt=attempt)
                issue = fence()
                if issue:
                    return self.store.finish(relay_id, "failed", issue, attempt=attempt)
                state, detail = self.mesh_submit(mesh_request)
        except Exception:
            # A transport exception may follow RF submission; never authorize a retry.
            state, detail = "unknown", "The send result is uncertain. No automatic retry is allowed; review the destination application."
        result = self.store.finish(relay_id, state, detail, attempt=attempt)
        result["ok"] = state in {"submitted", "unknown", "sent", "acked"}
        return result
