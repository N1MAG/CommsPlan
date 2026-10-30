"""Bounded read-only net/radio validation. Call only on the serial handoff worker."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sqlite3
from dataclasses import replace
from dataclasses import dataclass
from pathlib import Path

from freqinout.core.net_preparation import (
    NetSource, NetValues, NetSelection, PreparationError, PreparationRequest, PreparedNet, Protocol,
    RadioScope, encode_value, mode_protocols,
)
from freqinout.core.net_schedule_occurrences import next_net_occurrence
from freqinout.core.ncs_record_files import generated_checkin_paths
from freqinout.core.sqlite_utils import connect_sqlite_readonly
from freqinout.core.multi_radio_store import (
    _resolve_device_profile_links_conn, _legacy_settings_projection_from_device,
    _effective_assignment_for_device,
)


@dataclass(frozen=True)
class RadioInputs:
    scope: RadioScope
    missing: tuple[str, ...]
    directed_size: int = 0
    all_size: int = 0


@dataclass(frozen=True)
class PreparationInputs:
    request: PreparationRequest
    radios: tuple[RadioInputs, ...]
    checked_utc: dt.datetime


class PreparationReader:
    """No construction I/O; no SettingsManager/store/schema side effects.

    Compare bounded independent snapshots before/after file checks. This detects
    observed edits, not atomicity across external DB/files or same-value ID reuse.
    Runtime checkpoints and session metadata do not change endpoint identity.
    """
    def __init__(self, config_dir):
        self.config_dir = Path(config_dir)

    def _read(self, request, *, include_source=True):
        row_id = request.row_id if isinstance(request, NetSelection) else request.source.row_id
        output = {}
        for filename, names in (
            ('freqinout.db', ('kv', 'device_profiles', 'operating_profiles',
                             'operating_profile_assignments', 'runtime_policies',
                             'js8_instances', 'fast_light_configs')),
            ('freqinout_nets.db', ('net_schedule_tab',)),
        ):
            if filename == 'freqinout_nets.db' and not include_source:
                output['net_schedule_tab'] = ()
                continue
            connection = connect_sqlite_readonly(self.config_dir / filename, timeout=.5,
                                                  busy_timeout_ms=500, row_factory=sqlite3.Row)
            try:
                connection.execute('BEGIN')
                existing = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for name in names:
                    if name not in existing:
                        if name in {'kv', 'device_profiles', 'net_schedule_tab'}:
                            raise PreparationError('Net or radio configuration is unavailable; review setup')
                        output[name] = ()
                        continue
                    columns = [r[1] for r in connection.execute(f'PRAGMA table_info("{name}")')]
                    lengths = '+'.join('COALESCE(length(CAST("' + c.replace('"', '""') + '" AS BLOB)),0)' for c in columns)
                    where, params = (' WHERE id=?', (row_id,)) if name == 'net_schedule_tab' else ('', ())
                    if connection.execute(f'SELECT COALESCE(SUM({lengths}),0) FROM (SELECT * FROM "{name}"{where} LIMIT 10001)', params).fetchone()[0] > 8 * 1024 * 1024:
                        raise PreparationError('Net preparation input exceeds supported size')
                    if name == 'net_schedule_tab':
                        rows = connection.execute('SELECT * FROM net_schedule_tab WHERE id=?',
                                                  (row_id,)).fetchall()
                    else:
                        rows = connection.execute(f'SELECT * FROM "{name}" LIMIT 10001').fetchall()
                    if len(rows) > 10000:
                        raise PreparationError('Net preparation input exceeds supported capacity')
                    output[name] = tuple(dict(row) for row in rows)
                if filename == 'freqinout.db':
                    output['resolved'] = tuple(_resolve_device_profile_links_conn(connection, row)
                                               for row in output['device_profiles'])
            finally:
                connection.close()
        if len(encode_value(output).encode()) > 8 * 1024 * 1024:
            raise PreparationError('Net preparation configuration exceeds supported size')
        return output

    @staticmethod
    def _settings(raw, *, include_scoped=False):
        values = {}
        for row in raw['kv']:
            key = str(row['key'])
            if ((key.startswith('ncs_scope_') and not include_scoped) or key.startswith(('js8_ncs_', 'spotter_directed_offset', 'expect_directed_offset')) or key == 'ncs_session_snapshots_v1'):
                continue
            try:
                values[key] = json.loads(row['value'])
            except (TypeError, ValueError):
                values[key] = row['value']
        return values

    def _identity(self, raw):
        values = {key: value for key, value in raw.items() if key != 'kv'}
        values['settings'] = self._settings(raw)
        values['radio_macro_selections'] = {k: v for k, v in self._settings(raw, include_scoped=True).items()
                                            if k.startswith('ncs_scope_FLDIGI_') and k.endswith(('_fldigi_selected_macro_profile', '_fldigi_macro_profiles_v1'))}
        return hashlib.sha256(encode_value(values).encode()).hexdigest()

    @staticmethod
    def _complete_mapping(mapping):
        return (isinstance(mapping, dict) and mapping.get('enabled') and not mapping.get('read_only')
                and str(mapping.get('scope') or '').strip() and str(mapping.get('function') or '').strip()
                and (str(mapping.get('source_file') or '').strip() or str(mapping.get('macro_id') or '').strip()))

    @staticmethod
    def _same_file(left, right):
        if not left or not right:
            return False
        a, b = Path(left).expanduser(), Path(right).expanduser()
        try:
            if a.exists() and b.exists() and a.samefile(b):
                return True
            return a.resolve() == b.resolve()
        except OSError as exc:
            raise PreparationError(f'Cannot establish exclusive file ownership: {exc}') from exc

    @staticmethod
    def _same_endpoint(left, right):
        def host(value):
            text = str(value).strip().lower()
            return 'loopback' if text in {'localhost', '127.0.0.1', '::1'} else text
        return (host(left[0]), left[1]) == (host(right[0]), right[1])

    def select(self, selection: NetSelection, *, now=None):
        from freqinout.core.net_source_identity import net_source_identity
        raw = self._read(selection)
        rows = raw['net_schedule_tab']
        if len(rows) != 1 or net_source_identity(rows[0]) != selection.identity:
            raise PreparationError('Source changed or was removed; refresh or review the selected schedule')
        row = rows[0]
        if not selection.values_identity or hashlib.sha256(NetValues.from_row(row).payload.encode()).hexdigest() != selection.values_identity:
            raise PreparationError('Source values changed or are unavailable; refresh and select the net again')
        occurrence = next_net_occurrence(row, selection.when_utc)
        if not occurrence or occurrence['start_dt'] != selection.when_utc:
            raise PreparationError('Source occurrence changed; refresh or review the selected schedule')
        source = NetSource.from_row(row, revision=selection.revision, publication=selection.publication,
                                    start=occurrence['start_dt'], end=occurrence['end_dt'])
        explicit_radio = selection.explicit_radio_id if source.target.scope != 'device_profile' else source.target.device_id
        request = PreparationRequest(selection.request_id, selection.lifecycle, selection.navigation,
                                     selection.origin, selection.return_context, source, explicit_radio)
        # The full verified row, not an Ops label, supplies all prepared values.
        try:
            return self.load(request, now=now)
        except PreparationError as exc:
            if explicit_radio and source.target.scope != 'device_profile' and 'no eligible' in str(exc):
                # An old initiating context is a preference, not permission to
                # silently choose a different radio. Offer compatible choices.
                return self.load(replace(request, explicit_radio_id=None), now=now)
            raise

    def load(self, request: PreparationRequest, *, for_start=False, expected: RadioScope | None = None,
             now=None, for_manual=False, review_configuration=False) -> PreparationInputs:
        now = now or dt.datetime.now(dt.timezone.utc)
        raw = self._read(request, include_source=False) if for_manual else self._read(request)
        if for_manual:
            if expected is None:
                raise PreparationError('The intended manual radio context is unavailable')
            row = request.source.values.values()
            row.update(id=request.source.row_id, target_scope='device_profile', target_device_profile_id=expected.radio_id)
            target = request.source.target.__class__.from_row(row)
        else:
            rows = raw['net_schedule_tab']
            if len(rows) != 1:
                raise PreparationError('Source changed or was removed; review the selected schedule')
            row = rows[0]
            request.source.verify_row(row)
            occurrence = next_net_occurrence(row, request.source.occurrence_start)
            if (not occurrence or occurrence['start_dt'] != request.source.occurrence_start
                    or occurrence['end_dt'] != request.source.occurrence_end):
                raise PreparationError('Source occurrence changed; review the selected schedule')
            if now >= request.source.occurrence_end:
                raise PreparationError('This scheduled occurrence has ended; review it or explicitly use an ad hoc net')
            target = request.source.target
        choices = mode_protocols(row.get('mode'))
        if not choices:
            raise PreparationError('This saved mode has no supported net-control workspace; review its schedule')
        settings = self._settings(raw)
        scoped_preferences = self._settings(raw, include_scoped=True)
        policies = {int(r['radio_profile_id']): r for r in raw['runtime_policies']}
        operating = {int(r['id']): r for r in raw['operating_profiles']}
        radios = []
        for profile in raw['resolved']:
            ident = int(profile['id'])
            if not profile.get('enabled', 1) or not profile.get('runtime_active', 0):
                continue
            if str(profile.get('device_class') or '').lower() in {'sdr', 'observer', 'receive_only'} or profile.get('receive_only', 0):
                continue
            policy = policies.get(ident, {})
            if not policy.get('net_control_enabled', 1) or policy.get('operator_suppressed', 0):
                continue
            op_id = profile.get('operating_profile_id')
            op = operating.get(int(op_id or 0), {})
            if op and (not op.get('enabled', 1) or not op.get('use_net_control_tabs', 1) or op.get('receive_only', 0)):
                continue
            if not target.accepts(ident, op_id):
                continue
            if request.explicit_radio_id is not None and request.explicit_radio_id != ident:
                continue
            values = {**settings, **_legacy_settings_projection_from_device(profile, settings)}
            # Every runtime path uses the authoritative root, never default-profile paths.
            values['_preparation_config_dir'] = str(self.config_dir)
            values['_preparation_profile'] = {key: value for key, value in profile.items()
                                                if key not in {'runtime_primary', 'display_order', 'updated_utc', 'created_utc'}}
            values['net_schedule'] = [row]
            for protocol in choices:
                if request.protocol_choice is not None and protocol != request.protocol_choice:
                    continue
                voice = str(row.get('mode') or '').strip().upper() in {'SSB', 'USB', 'LSB', 'VOICE'}
                if protocol == Protocol.JS8 and not profile.get('use_js8call', 0):
                    continue
                if protocol == Protocol.FLDIGI and not voice and not profile.get('use_fldigi', 0):
                    continue
                prefix = 'js8' if protocol == Protocol.JS8 else 'fldigi'
                link_key, table = ('js8_instance_id', 'js8_instances') if prefix == 'js8' else ('fast_light_config_id', 'fast_light_configs')
                if profile.get(link_key):
                    linked = next((r for r in raw[table] if r['id'] == profile[link_key]), None)
                    if linked is None or not linked.get('enabled', 1):
                        continue
                endpoint = (str(values.get(prefix + '_host') or '127.0.0.1'),
                            int(values.get(prefix + '_port') or (2442 if prefix == 'js8' else 7362)))
                directed = str(profile.get('js8_directed_path') or '') if prefix == 'js8' else ''
                all_path = str(profile.get('js8_all_path') or (str(Path(directed).with_name('ALL.TXT')) if directed else ''))
                checkins = str(profile.get('fldigi_checkin_dir') or '') if prefix == 'fldigi' else ''
                # Voice is a manual receiver; require an explicit local record root,
                # not a hidden fallback to another radio's FLDigi files.
                instance = profile.get('js8_instance_id' if prefix == 'js8' else 'fast_light_config_id')
                instance_name = f'{prefix}:{instance}' if instance else f'{prefix}:device:{ident}'
                missing = []
                if not str(values.get('operator_callsign') or values.get('callsign') or '').strip():
                    missing.append('operator callsign')
                directed_size = all_size = 0
                if prefix == 'js8':
                    if not directed or not Path(directed).is_file():
                        missing.append('JS8 DIRECTED.TXT')
                    else:
                        directed_size = Path(directed).stat().st_size
                    if all_path and Path(all_path).is_file():
                        all_size = Path(all_path).stat().st_size
                    values['js8_all_path'] = all_path
                elif not checkins or not Path(checkins).is_dir() or not os.access(checkins, os.W_OK):
                    missing.append('radio check-in records folder')
                # Only endpoint/file ownership belonging unambiguously to this radio
                # may be offered. Existing endpoint guards still run for commands.
                for peer in raw['resolved']:
                    if int(peer['id']) == ident or not peer.get('enabled', 1) or not peer.get('runtime_active', 0):
                        continue
                    if prefix == 'js8' and peer.get('use_js8call', 0):
                        other_endpoint = (str(peer.get('js8_host') or '127.0.0.1'), int(peer.get('js8_port') or 2442))
                        if self._same_endpoint(other_endpoint, endpoint) or directed and self._same_file(peer.get('js8_directed_path'), directed) or all_path and self._same_file(peer.get('js8_all_path') or (str(Path(peer['js8_directed_path']).with_name('ALL.TXT')) if peer.get('js8_directed_path') else ''), all_path):
                            missing.append('exclusive JS8 endpoint and message folder')
                    if prefix == 'fldigi' and checkins and self._same_file(peer.get('fldigi_checkin_dir'), checkins):
                        missing.append('exclusive radio check-in records folder')
                scope_values = dict(values)
                if prefix == 'js8':
                    from freqinout.core.js8_spotter_forms import discover_effective_spotter_forms, resolve_net_form_options, MAPPER_SETTINGS_KEY
                    definitions = discover_effective_spotter_forms(values.get('js8_forms_path', ''))
                    options = resolve_net_form_options(definitions, values.get(MAPPER_SETTINGS_KEY, []),
                                                       group_key=row.get('operating_group_key', ''))
                    scope_values['_preparation_forms'] = list(options.options)
                    scope_values['_preparation_preferred_form'] = options.preferred
                if prefix == 'fldigi':
                    selected_key = f'ncs_scope_FLDIGI_{ident}_fldigi_selected_macro_profile'
                    selected = str(scoped_preferences.get(selected_key, settings.get('fldigi_selected_macro_profile', '')) or '')
                    store = scoped_preferences.get(f'ncs_scope_FLDIGI_{ident}_fldigi_macro_profiles_v1', settings.get('fldigi_macro_profiles_v1', {}))
                    canonical = str(Path(selected).expanduser().resolve()) if selected else ''
                    record = next((v for k, v in store.items() if str(Path(k).expanduser().resolve()) == canonical), {}) if isinstance(store, dict) else {}
                    mappings = [dict(m) for m in record.get('mappings', []) if self._complete_mapping(m)] if isinstance(record, dict) else []
                    scope_values['fldigi_selected_macro_profile'] = canonical
                    scope_values['fldigi_macro_profiles_v1'] = store
                    frozen = []
                    for mapping in mappings:
                        path = str(mapping.get('source_file') or '')
                        if not path:
                            frozen.append(mapping)
                            continue
                        path = str(Path(path).expanduser().resolve())
                        mapping['source_file'] = path
                        if not Path(path).parent.is_dir() or not os.access(Path(path).parent, os.W_OK):
                            missing.append('mapped records folder')
                        for peer in raw['resolved']:
                            peer_id = int(peer['id'])
                            if peer_id == ident or not peer.get('enabled', 1) or not peer.get('runtime_active', 0) or str(peer.get('device_class') or '').lower() in {'observer', 'receive_only', 'sdr'}:
                                continue
                            peer_root = str(peer.get('fldigi_checkin_dir') or '')
                            if peer_root and (Path(path).resolve().is_relative_to(Path(peer_root).expanduser().resolve())
                                              or any(self._same_file(path, other) for other in generated_checkin_paths(peer_root))):
                                missing.append('exclusive mapped and generated records folders')
                            peer_selected = str(scoped_preferences.get(f'ncs_scope_FLDIGI_{peer_id}_fldigi_selected_macro_profile', settings.get('fldigi_selected_macro_profile', '')) or '')
                            peer_store = scoped_preferences.get(f'ncs_scope_FLDIGI_{peer_id}_fldigi_macro_profiles_v1', settings.get('fldigi_macro_profiles_v1', {}))
                            peer_record = next((v for k, v in peer_store.items() if peer_selected and str(Path(k).expanduser().resolve()) == str(Path(peer_selected).expanduser().resolve())), {}) if isinstance(peer_store, dict) else {}
                            for other in peer_record.get('mappings', []) if isinstance(peer_record, dict) else []:
                                if self._complete_mapping(other) and other.get('source_file') and self._same_file(path, other['source_file']):
                                    missing.append('exclusive mapped files; select a radio-specific macro profile')
                        frozen.append(mapping)
                    for peer in raw['resolved']:
                        peer_id = int(peer['id'])
                        if peer_id == ident or not peer.get('enabled', 1) or not peer.get('runtime_active', 0) or str(peer.get('device_class') or '').lower() in {'observer', 'receive_only', 'sdr'}:
                            continue
                        peer_selected = str(scoped_preferences.get(f'ncs_scope_FLDIGI_{peer_id}_fldigi_selected_macro_profile', settings.get('fldigi_selected_macro_profile', '')) or '')
                        peer_store = scoped_preferences.get(f'ncs_scope_FLDIGI_{peer_id}_fldigi_macro_profiles_v1', settings.get('fldigi_macro_profiles_v1', {}))
                        peer_record = next((v for k, v in peer_store.items() if peer_selected and str(Path(k).expanduser().resolve()) == str(Path(peer_selected).expanduser().resolve())), {}) if isinstance(peer_store, dict) else {}
                        for other in peer_record.get('mappings', []) if isinstance(peer_record, dict) else []:
                            if not self._complete_mapping(other) or not other.get('source_file') or not checkins:
                                continue
                            other_path = Path(other['source_file']).expanduser().resolve()
                            if other_path.is_relative_to(Path(checkins).expanduser().resolve()) or any(self._same_file(other_path, path) for path in generated_checkin_paths(checkins)):
                                missing.append('exclusive mapped and generated records folders')
                        peer_root = str(peer.get('fldigi_checkin_dir') or '')
                        if peer_root and checkins and any(self._same_file(a, b) for a in generated_checkin_paths(checkins) for b in generated_checkin_paths(peer_root)):
                            missing.append('exclusive generated records files')
                    scope_values['_preparation_macro_mappings'] = frozen
                    scope_values['_preparation_custom_content'] = {}
                    for mapping in frozen:
                        if str(mapping.get('function') or '').upper() != 'CUSTOM' or not mapping.get('source_file'):
                            continue
                        path = Path(mapping['source_file'])
                        if path.is_file():
                            if path.stat().st_size > 1024 * 1024:
                                raise PreparationError('Custom bucket exceeds supported size; review its file')
                            scope_values['_preparation_custom_content'][str(path)] = path.read_text(encoding='utf-8', errors='replace')
                scope = RadioScope(ident, str(profile.get('name') or f'Radio {ident}'), op_id,
                                   protocol, instance_name, encode_value(scope_values),
                                   endpoint, directed, all_path, checkins,
                                   hashlib.sha256(encode_value((values['_preparation_profile'],
                                       {k: v for k, v in op.items() if k not in {'updated_utc', 'created_utc'}},
                                       {k: v for k, v in policy.items() if k not in {'updated_utc', 'created_utc'}})).encode()).hexdigest())
                radios.append(RadioInputs(scope, tuple(dict.fromkeys(missing)), directed_size, all_size))
        if self._identity(raw) != self._identity(self._read(request, include_source=False) if for_manual else self._read(request)):
            raise PreparationError('Source or radio configuration changed during checking; review and retry')
        if not radios:
            raise PreparationError('The selected net has no eligible active radio/service; review schedule or radio setup')
        if expected is not None:
            if for_manual and review_configuration and not for_start:
                # A setup review may publish a changed configuration for the
                # same radio/service. Confirmation and Start remain strict.
                match = next((item for item in radios if item.scope.radio_id == expected.radio_id
                              and item.scope.protocol == expected.protocol), None)
            else:
                match = next((item for item in radios if (item.scope.configuration_identity == expected.configuration_identity if for_manual else item.scope.identity == expected.identity)), None)
            if match is None:
                raise PreparationError('The intended radio or service configuration changed; review before Start')
            if for_start and match.missing:
                raise PreparationError('Needs setup: ' + ', '.join(match.missing))
            radios = [match]
        return PreparationInputs(request, tuple(radios), now)
