"""Explicit, cold Linux profile copy. Never changes the active installation.

Only schema-owned whole absolute paths are remapped. Finalized journals retain
their historical evidence. Unrecognized references block; message text is never rewritten.
"""
from __future__ import annotations

import argparse
from collections import Counter
import base64
from contextlib import closing
import hashlib
import json
import re
from pathlib import Path
import sqlite3
import shlex
import tempfile
import sys

from freqinout.core import brand_migration as cold

GUARD = '.commsplan-copy-incomplete'
RECORD = '.commsplan-profile-copy.json'
RETIRED_MANIFESTS = '.commsplan-retired-manifests.json'
FORMAT = 'commsplan-profile-copy-v1'
HISTORY_STATES = {
    'varac_native_apply_journal': {'complete', 'rolled_back'},
    'launch_bundle_migration_audit': {'confirmed', 'confirmed_existing_bundle'},
}

# Established kinds emitted by guided_launch_recipes/software_instance_assistant.
PATH_CLAIM_KINDS = {
    'path', 'working_directory', 'varac_executable', 'varac_database', 'varac_bbs',
    'varac_bbs_archive', 'varac_ini', 'vara_runtime', 'vara_ini', 'varac_incoming',
    'varac_outbox', 'settings_profile', 'message_storage', 'flrig_configuration',
    'fldigi_configuration', 'fldigi_logs', 'fldigi_checkins', 'flmsg_root',
    'flmsg_messages', 'flmsg_templates', 'flmsg_auto', 'flamp_root',
    'flamp_receive', 'flamp_outgoing', 'flmsg_application', 'flamp_application',
    'config_root', 'ini_path', 'js8_profile',
}

SQLITE_SUFFIXES = ('.db', '.db3', '.s3db', '.sqlite', '.sqlite3')


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _path_field(name):
    name = name.lower()
    return (name in {'path', 'paths', 'message_paths', 'folder', 'directory', 'current_overlay_file', 'unmanaged_live_files', 'endpoint_or_path', 'path_display'}
            or name.startswith('path_') or name.endswith(('_path', '_paths', '_dir', '_root', '_folder', '_directory')))


def _native_display_identity(component, source, regular_files, *, key, filename, argument_count=1):
    """Validate the native repair schema before rebuilding display-only metadata."""
    arguments = component.get('arguments')
    if (component.get('component_key') != key or component.get('executable') != 'wine'
            or component.get('operator_starts') is not False
            or not isinstance(arguments, list) or len(arguments) != argument_count
            or not isinstance(arguments[0], str)
            or component.get('effective_command') != ['wine', *arguments]):
        return False
    path = Path(arguments[0])
    try:
        relative = path.relative_to(source)
    except ValueError:
        return False
    return (path.is_absolute() and arguments[0] == str(path) and '..' not in path.parts
            and len(relative.parts) >= 3 and relative.parts[0] == 'managed-instances'
            and path.name == filename and relative.as_posix() in regular_files
            and component.get('working_directory') == str(path.parent))


def _derived_vara_display(component, source, regular_files):
    return _native_display_identity(component, source, regular_files, key='vara', filename='VARA.exe')


def _derived_varac_display(component, source, regular_files):
    if not _native_display_identity(component, source, regular_files, key='varac', filename='VarAC.exe', argument_count=2):
        return False
    ini = component['arguments'][1]
    if not isinstance(ini, str):
        return False
    path = Path(ini)
    try:
        relative = path.relative_to(source)
    except ValueError:
        return False
    if (not path.is_absolute() or ini != str(path) or '..' in path.parts
            or len(relative.parts) < 3 or relative.parts[0] != 'managed-instances'
            or path.suffix.lower() != '.ini' or relative.as_posix() not in regular_files):
        return False
    command = component['effective_command']
    legacy = ' '.join(f'"{item}"' if any(ch.isspace() for ch in item) else item for item in command)
    return component.get('effective_command_text') in (None, '', legacy)


def _transform(value, field, source, destination, files, changes, blockers, location, external, *, regular_files=None):
    """Return transformed JSON, recording only path details (never arbitrary values)."""
    regular_files = files if regular_files is None else regular_files
    if isinstance(value, dict):
        result = {}
        component = (':software_instance_manifests:' in location
                     and '.launch_recipe.components[' in location
                     and location.endswith(']'))
        command = value.get('effective_command') if component else None
        generated = (isinstance(command, list) and all(isinstance(x, str) for x in command)
                     and (value.get('effective_command_text') == shlex.join(command)
                          or _derived_vara_display(value, source, regular_files)
                          or _derived_varac_display(value, source, regular_files)))
        if component and isinstance(command, list) and any(
                isinstance(x, str) and str(source) in x for x in command) and not generated:
            blockers.append(location + ': command display does not match generated argument vector')
        for key, item in value.items():
            if str(source) in key:
                blockers.append(location + ': profile reference in JSON key')
            if component and key == 'effective_command_text' and generated:
                continue
            claim_path = (':software_instance_manifests:' in location and
                          ':resource_claims_json[' in location and key == 'value' and
                          value.get('kind') in PATH_CLAIM_KINDS)
            child_field = field if field in {'message_paths', 'paths', 'unmanaged_live_files'} else key
            if claim_path:
                child_field = 'path'
            result[key] = _transform(item, child_field, source, destination, files, changes, blockers, location + '.' + key, external, regular_files=regular_files)
        if component and generated:
            result['effective_command_text'] = shlex.join(result['effective_command'])
            if result['effective_command_text'] != value.get('effective_command_text'):
                changes.append({'location': location + '.effective_command_text',
                                'from': value.get('effective_command_text'),
                                'to': result['effective_command_text']})
        return result
    if isinstance(value, list):
        return [_transform(item, field, source, destination, files, changes, blockers, location + f'[{i}]', external, regular_files=regular_files)
                for i, item in enumerate(value)]
    if not isinstance(value, str):
        return value
    # The scanner uses base64 path identities: never silently leave these stale.
    if field in {'path_key', 'path_b64'}:
        try:
            decoded = base64.urlsafe_b64decode(value + '=' * (-len(value) % 4)).decode('utf-8')
            if decoded == str(source) or decoded.startswith(str(source) + '/'):
                blockers.append(location + ': encoded profile path requires review')
        except (ValueError, UnicodeError):
            pass
    if str(source) not in value:
        if value and _path_field(field):
            if Path(value).is_absolute():
                classification = 'external-existing' if Path(value).exists() else 'external-missing'
                external.append({'location': location, 'path': value, 'classification': classification})
            else:
                external.append({'location': location, 'classification': 'relative-or-unresolved; unchanged'})
        return value
    # These fields are schema-owned launch facts, not arbitrary message text.
    launch_field = (':software_instance_manifests:' in location and
                    '.launch_recipe.' in location and
                    field in {'configuration_roots', 'data_roots', 'managed_directories',
                              'profile_selector', 'arguments', 'effective_command', 'executable'})
    profile_evidence = (':software_instance_manifests:' in location and
                        '.launch_recipe.' in location and
                        (location.endswith('.evidence.profile.root') or
                         location.endswith(('.evidence.checkins', '.evidence.logs'))))
    if not (_path_field(field) or launch_field or profile_evidence) or not Path(value).is_absolute():
        blockers.append(location + ': embedded/unrecognized profile reference')
        return value
    path = Path(value)
    try:
        relative = path.relative_to(source)
    except ValueError:
        # A similarly named external path is not the selected profile.
        return value
    if '..' in path.parts or value != str(path):
        blockers.append(location + ': noncanonical profile path')
        return value
    relative_name = relative.as_posix()
    if relative_name != '.' and relative_name not in files and not any(
            name.startswith(relative_name + '/') for name in files):
        blockers.append(location + ': path target is absent or excluded from the copy: ' + value)
        return value
    new = str(destination / relative)
    changes.append({'location': location, 'from': value, 'to': new})
    return new


def _database_files(profile, files):
    result = []
    for name in files:
        with (profile / name).open('rb') as handle:
            sqlite_header = handle.read(16) == b'SQLite format 3\x00'
        if name.lower().endswith(SQLITE_SUFFIXES) or sqlite_header:
            result.append(name)
    return result


def _inspect_paths(profile, source, destination, files, *, write=False, ignored_manifest_rows=()):
    changes, blockers, external = [], [], []
    targets = set(files) | set(cold._directories(profile, profile=True))
    database_files = _database_files(profile, files)
    for name in database_files:
        path = profile / name
        cold._database_summary(path)
        uri = path.as_uri() + ('?mode=rw' if write else '?mode=ro&immutable=1')
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            if conn.execute('PRAGMA foreign_key_check').fetchone():
                blockers.append(name + ': foreign-key consistency requires review')
            for kind, object_name, sql in conn.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL"):
                if str(source) in sql:
                    blockers.append(f'{name}:{kind}:{object_name}: schema profile reference requires review')
            tables = conn.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
            with conn:
                for table, definition in tables:
                    if 'VIRTUAL TABLE' in (definition or '').upper():
                        blockers.append(f'{name}:{table}: virtual table requires review')
                        continue
                    columns = [r[1] for r in conn.execute(f'PRAGMA table_info({_quote(table)})')]
                    # WITHOUT ROWID tables are scanned but cannot be remapped here.
                    rowid = 'WITHOUT ROWID' not in (definition or '').upper() and not any(c.lower() == 'rowid' for c in columns)
                    query = ('rowid, ' if rowid else '') + ', '.join(map(_quote, columns))
                    for record in conn.execute(f'SELECT {query} FROM {_quote(table)}').fetchall():
                        if (name == 'config/freqinout.db' and table == 'software_instance_manifests'
                                and rowid and record[0] in ignored_manifest_rows):
                            continue
                        values = record[1:] if rowid else record
                        if name == 'config/freqinout.db' and table in HISTORY_STATES:
                            state = values[columns.index('state')] if 'state' in columns else None
                            if state not in HISTORY_STATES[table]:
                                blockers.append(f'{name}:{table}:row:{record[0] if rowid else "key"}: unfinished/unknown migration journal state requires recovery')
                            # Terminal journals are immutable evidence of the original
                            # operation. Never retarget their backup paths/hash keys.
                            continue
                        for i, (column, raw) in enumerate(zip(columns, values)):
                            if not isinstance(raw, str):
                                continue
                            field = str(values[columns.index('key')]) if table == 'kv' and column == 'value' and 'key' in columns else column
                            try:
                                value = json.loads(raw)
                                encoded = True
                            except (ValueError, TypeError):
                                value, encoded = raw, False
                            before = len(changes)
                            new = _transform(value, field, source, destination, targets, changes, blockers, f'{name}:{table}:row:{record[0] if rowid else "key"}:{column}', external, regular_files=files)
                            if len(changes) > before:
                                if not rowid or any(r[0] for r in conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,))):
                                    blockers.append(f'{name}:{table}: triggers/row identity require review')
                                elif write:
                                    if blockers:
                                        raise cold.MigrationError('Path blockers found during write; database transaction rolled back.')
                                    conn.execute(f'UPDATE {_quote(table)} SET {_quote(column)}=? WHERE rowid=?',
                                                 (json.dumps(new, ensure_ascii=False) if encoded else new, record[0]))
                if write and conn.execute('PRAGMA foreign_key_check').fetchone():
                    raise cold.MigrationError('Path changes violate foreign-key consistency; transaction rolled back.')
    for name in files:
        if name in database_files or not name.startswith('config/'):
            continue
        path = profile / name
        if name.startswith('config/backups/varac-native-repair/'):
            # Retained repair artifacts describe the old station; hashes bind
            # their exact bytes. They are not active launch configuration.
            continue
        if name.endswith('.json'):
            try:
                value = json.loads(path.read_text(encoding='utf-8'))
            except (ValueError, UnicodeError):
                blockers.append(name + ': configuration JSON is unreadable')
                continue
            before = len(changes)
            new = _transform(value, '', source, destination, targets, changes, blockers, name, external, regular_files=files)
            if write and len(changes) > before:
                path.write_text(json.dumps(new, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        elif str(source).encode() in path.read_bytes():
            blockers.append(name + ': non-JSON configuration reference requires review')
    return sorted(changes, key=lambda item: (item['location'], item['from'], item['to'])), sorted(set(blockers)), sorted(external, key=lambda item: item['location'])


def _row_digest(rowid, values):
    return hashlib.sha256(json.dumps({'rowid': rowid, 'values': values}, sort_keys=True,
                                     separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def _retirable_draft_manifests(profile, origin, blockers):
    """Identify unassigned Fast Light drafts; every blocked field must be a missing draft path."""
    by_row = {}
    for blocker in blockers:
        match = re.match(r'config/freqinout\.db:software_instance_manifests:row:(\d+):', blocker)
        if match:
            by_row.setdefault(int(match[1]), []).append(blocker)
    if not by_row:
        return []
    result = []
    path = profile / 'config/freqinout.db'
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {'software_instance_manifests', 'fast_light_configs', 'device_profiles',
                    'radio_software_identity_records', 'radio_launch_bundle_items'}
        if not required <= tables:
            return []
        required_columns = {
            'fast_light_configs': {'id', 'system_key'},
            'device_profiles': {'fast_light_config_id'},
            'radio_software_identity_records': {'bundle_id'},
            'radio_launch_bundle_items': {'instance_key'},
        }
        if any(not columns <= {column[1] for column in conn.execute(f'PRAGMA table_info({_quote(table)})')}
               for table, columns in required_columns.items()):
            return []
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name='software_instance_manifests' LIMIT 1").fetchone():
            return []
        for rowid, reasons in sorted(by_row.items()):
            if not all(': path target is absent or excluded from the copy: ' in reason for reason in reasons):
                continue
            row = conn.execute('SELECT rowid,* FROM software_instance_manifests WHERE rowid=?', (rowid,)).fetchone()
            if row is None:
                continue
            values = {key: row[key] for key in row.keys() if key != 'rowid'}
            key = str(values.get('instance_key') or '')
            application_key = str(values.get('application_system_key') or '')
            configuration_path = str(values.get('configuration_path') or '')
            try:
                parts = Path(configuration_path).relative_to(origin).parts
            except ValueError:
                continue
            if len(parts) < 3 or parts[0] != 'managed-instances' or not parts[1].startswith('draft-fast_light-'):
                continue
            draft_root = origin / parts[0] / parts[1]
            missing_targets = [reason.partition(': path target is absent or excluded from the copy: ')[2]
                               for reason in reasons]
            if not all(target.startswith(str(draft_root) + '/') for target in missing_targets):
                continue
            if (values.get('family_key') != 'fast_light' or values.get('management_mode') != 'operator'
                    or values.get('verification_state') != 'configured'
                    or not key.startswith('fast_light:') or not application_key
                    or not configuration_path.startswith(str(draft_root) + '/')):
                continue
            application = conn.execute('SELECT id FROM fast_light_configs WHERE system_key=?',
                                       (application_key,)).fetchall()
            if len(application) > 1:
                continue
            application_id = application[0][0] if application else None
            if application_id is None and conn.execute(
                    'SELECT 1 FROM device_profiles d LEFT JOIN fast_light_configs f '
                    'ON f.id=d.fast_light_config_id WHERE d.fast_light_config_id IS NOT NULL '
                    'AND d.fast_light_config_id<>0 AND f.id IS NULL LIMIT 1').fetchone():
                # An unresolved radio assignment could still refer to this
                # missing application; do not infer ownership from its key.
                continue
            if conn.execute('SELECT COUNT(*) FROM software_instance_manifests WHERE family_key=? AND application_system_key=?',
                            ('fast_light', application_key)).fetchone()[0] != 1:
                continue
            if application_id is not None and conn.execute(
                    'SELECT 1 FROM device_profiles WHERE fast_light_config_id=? LIMIT 1',
                    (application_id,)).fetchone():
                continue
            if conn.execute('SELECT 1 FROM radio_software_identity_records WHERE bundle_id=? LIMIT 1',
                            (key,)).fetchone():
                continue
            if conn.execute('SELECT 1 FROM radio_launch_bundle_items WHERE instance_key=? OR substr(instance_key,1,length(?) + 1)=? || ":" LIMIT 1',
                            (key, key, key)).fetchone():
                continue
            result.append({'rowid': rowid, 'instance_key': key,
                           'application_system_key': application_key,
                           'application_id': application_id, 'sha256': _row_digest(rowid, values),
                           'values': values})
    return result


def _archive_retired_manifests(destination, retired):
    if not retired:
        return None
    archive = destination / RETIRED_MANIFESTS
    pending = destination / (RETIRED_MANIFESTS + '.pending')
    if archive.exists() or archive.is_symlink() or pending.exists() or pending.is_symlink():
        raise cold.MigrationError('Retired-manifest archive already exists; copied profile remains guarded.')
    pending.write_text(json.dumps({'format': 'commsplan-retired-manifests-v1',
                                   'policy': 'Exact rows removed only from this prepared copy; original and verified backup retain them.',
                                   'rows': retired}, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    path = destination / 'config/freqinout.db'
    with closing(sqlite3.connect(path)) as conn:
        with conn:
            for item in retired:
                row = conn.execute('SELECT rowid,* FROM software_instance_manifests WHERE rowid=?',
                                   (item['rowid'],)).fetchone()
                if row is None:
                    raise cold.MigrationError('Draft manifest changed in copy; copy remains guarded.')
                columns = [entry[1] for entry in conn.execute('PRAGMA table_info(software_instance_manifests)')]
                values = dict(zip(columns, row[1:]))
                if _row_digest(item['rowid'], values) != item['sha256'] or values != item['values']:
                    raise cold.MigrationError('Draft manifest changed in copy; copy remains guarded.')
                deleted = conn.execute('DELETE FROM software_instance_manifests WHERE rowid=? AND instance_key=?',
                                       (item['rowid'], item['instance_key']))
                if deleted.rowcount != 1:
                    raise cold.MigrationError('Draft manifest retirement failed; copy remains guarded.')
            if conn.execute('PRAGMA foreign_key_check').fetchone():
                raise cold.MigrationError('Draft manifest retirement violates database references; copy remains guarded.')
    pending.replace(archive)
    return {'path': RETIRED_MANIFESTS, 'sha256': cold._hash(archive), 'count': len(retired)}


def _blocked_instances(profile, blockers):
    """Small, read-only context for unresolved active manifest records."""
    rows = {}
    for blocker in blockers:
        match = re.match(r'config/freqinout\.db:software_instance_manifests:row:(\d+):', blocker)
        if match:
            row = rows.setdefault(int(match[1]), set())
            _, marker, target = blocker.partition(': path target is absent or excluded from the copy: ')
            if marker:
                row.add(target)
    if not rows:
        return []
    result = []
    path = profile / 'config/freqinout.db'
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        for identity, targets in sorted(rows.items()):
            raw = conn.execute('SELECT * FROM software_instance_manifests WHERE rowid=?', (identity,)).fetchone()
            if raw is None:
                continue
            values = dict(raw)
            context = {key: values[key] for key in ('instance_key', 'family_key', 'management_mode', 'verification_state') if key in values}
            context.update(rowid=identity, missing_targets=sorted(targets))
            try:
                evidence = json.loads(values.get('evidence_json') or '{}')
                recipe = evidence.get('launch_recipe', {}) if isinstance(evidence, dict) else {}
                status = recipe.get('status') if isinstance(recipe, dict) else None
                context['recipe_status'] = status if isinstance(status, str) else None
            except (ValueError, TypeError):
                context['recipe_status'] = None
            result.append(context)
    return result


def _history_inventory(profile):
    result = []
    path = profile / 'config/freqinout.db'
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in HISTORY_STATES:
            if table not in tables:
                continue
            columns = [r[1] for r in conn.execute(f'PRAGMA table_info({_quote(table)})')]
            for row in conn.execute(f'SELECT * FROM {_quote(table)}'):
                state = row[columns.index('state')] if 'state' in columns else None
                result.append({'table': table, 'state': state,
                               'identity': row[columns.index('id' if table == 'varac_native_apply_journal' else 'migration_key')],
                               'row_sha256': hashlib.sha256(json.dumps(row, ensure_ascii=False, default=str).encode()).hexdigest()})
    return sorted(result, key=lambda x: (x['table'], x['row_sha256']))


def _recovery_digest(source):
    record = source / '.commsplan-journal-recovery.json'
    return cold._hash(record) if record.exists() else None


def _plan(source, destination):
    from freqinout.core.journal_recovery import path_origin
    origin = path_origin(source)
    if (source / '.commsplan-journal-recovery.json').exists():
        recovery_record = json.loads((source / '.commsplan-journal-recovery.json').read_text())
        if 'directories' not in recovery_record:
            raise cold.MigrationError('This recovery predates directory preservation. Keep it for evidence; repeat profile recover from the original into a new workspace.')
    report = cold._inspect_profile(source)
    if (source / RECORD).exists():
        raise cold.MigrationError('Source already has a profile-copy record; this relocation needs separate review.')
    if (source / GUARD).exists():
        raise cold.MigrationError('Source profile is an incomplete copy; use the original station.')
    if (source / RETIRED_MANIFESTS).exists() or (source / RETIRED_MANIFESTS).is_symlink():
        raise cold.MigrationError('Source has a retired-manifest archive; a second relocation needs separate review.')
    if destination.exists() or destination.is_symlink():
        raise cold.MigrationError('Destination must not exist; profiles are never merged or overwritten.')
    if any(root == destination or root in destination.parents or destination in root.parents for root in {source, origin}):
        raise cold.MigrationError('Source and destination must be separate directories.')
    if not destination.parent.is_dir():
        raise cold.MigrationError('Destination parent must already exist.')
    if report['migration_marker'] < 1:
        raise cold.MigrationError('Complete and verify the existing station upgrade before relocating its profile.')
    files = cold._files(source, profile=True)
    directories = cold._directories(source, profile=True)
    changes, blockers, external = _inspect_paths(source, origin, destination, files)
    retired = _retirable_draft_manifests(source, origin, blockers)
    if retired:
        prefixes = tuple(f"config/freqinout.db:software_instance_manifests:row:{item['rowid']}:"
                         for item in retired)
        changes = [item for item in changes if not item['location'].startswith(prefixes)]
        blockers = [item for item in blockers if not item.startswith(prefixes)]
        external = [item for item in external if not item['location'].startswith(prefixes)]
    # Prove read-only inspection did not race a companion writer.
    if cold._files(source, profile=True) != files or cold._directories(source, profile=True) != directories or cold._sidecar_snapshot(source) != report['sqlite_sidecars']:
        raise cold.MigrationError('Source changed during preview; close all writers and retry.')
    plan = {'format': FORMAT, 'source': str(source), 'path_origin': str(origin),
            'recovery_provenance_sha256': _recovery_digest(source), 'destination': str(destination),
            'files': files, 'directories': directories, 'databases': report['databases'], 'sqlite_sidecars': report['sqlite_sidecars'], 'path_changes': changes,
            'blockers': blockers, 'excluded_roots': sorted(cold.EXCLUDED_ROOTS),
            'retired_manifests': retired,
            'retirement_policy': 'Unreferenced Fast Light draft manifest rows with only missing draft-path blockers are archived exactly and removed from the prepared copy. If an unassigned application configuration row exists it is retained. Original and verified backup retain all rows.',
            'sqlite_sidecar_policy': 'Checkpointed SHM/empty journal residue excluded; pending WAL/journal refused. Original files untouched.',
            'blocked_instances': _blocked_instances(source, blockers),
            'preserved_history': _history_inventory(source),
            'history_policy': 'Finalized migration journals and config/backups/varac-native-repair artifacts retain original bytes and paths; no replay or automatic rollback.',
            'external_paths': external, 'external_policy': 'Unchanged. External companion data and OS keyring are not copied.',
            'ready_to_copy': not blockers}
    plan['digest'] = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    return plan


def preview(source: Path, destination: Path):
    source, destination = cold._plain_path(source), cold._plain_path(destination)
    if not source.is_dir():
        raise cold.MigrationError('Select an existing profile root containing config/.')
    with cold._locked_profile(source):
        return _plan(source, destination)


def copy_profile(source: Path, destination: Path, backup_directory: Path, digest: str):
    source, destination = cold._plain_path(source), cold._plain_path(destination)
    backup_directory = cold._plain_path(backup_directory)
    if backup_directory == destination or destination in backup_directory.parents:
        raise cold.MigrationError('Backup directory must be outside the new profile.')
    plan = preview(source, destination)
    if plan['digest'] != digest:
        raise cold.MigrationError('Preview changed; inspect a fresh preview before copying.')
    if plan['blockers']:
        raise cold.MigrationError('Preview has unresolved path references; no copy or activation is allowed.')
    # Profile-only backup is intentionally independent of runtime version stamping.
    backup = cold._create_backup(source, backup_directory, lambda: cold._inspect_profile(source))
    manifest = cold.verify_backup(backup)
    if manifest['files'] != plan['files'] or manifest.get('directories') != plan['directories'] or manifest['sqlite_sidecars'] != plan['sqlite_sidecars']:
        raise cold.MigrationError(f'Source changed since preview; verified backup retained at {backup}. Preview again.')
    with cold._locked_profile(source):
        origin = Path(plan['path_origin'])
        from freqinout.core.journal_recovery import path_origin
        if path_origin(source) != origin or _recovery_digest(source) != plan['recovery_provenance_sha256']:
            raise cold.MigrationError('Recovery provenance changed; preview again.')
        if cold._files(source, profile=True) != plan['files'] or cold._directories(source, profile=True) != plan['directories'] or cold._sidecar_snapshot(source) != plan['sqlite_sidecars']:
            raise cold.MigrationError(f'Source changed after backup; retained at {backup}. Preview again.')
        cold.restore_copy(backup, destination, guard_filename=GUARD)
        with cold._locked_profile(destination):
            ignored_rows = {item['rowid'] for item in plan['retired_manifests']}
            reviewed_changes, reviewed_blockers, _ = _inspect_paths(
                destination, origin, destination, plan['files'], ignored_manifest_rows=ignored_rows)
            if reviewed_blockers or reviewed_changes != plan['path_changes']:
                raise cold.MigrationError('Destination path preflight differs; copy remains guarded.')
            changes, blockers, _ = _inspect_paths(
                destination, origin, destination, plan['files'], write=True,
                ignored_manifest_rows=ignored_rows)
            if blockers or changes != plan['path_changes']:
                raise cold.MigrationError('Path review changed; copy remains guarded. Do not activate it.')
            retired_archive = _archive_retired_manifests(destination, plan['retired_manifests'])
            for name in _database_files(destination, plan['files']):
                cold._database_summary(destination / name)
            remaining, unresolved, _ = _inspect_paths(destination, origin, destination, plan['files'])
            if remaining or unresolved:
                raise cold.MigrationError('Old-profile path references remain; copy is guarded. Do not activate it.')
            if any(cold._hash(destination / name) != digest for name, digest in plan['files'].items()
                   if name.startswith('config/backups/varac-native-repair/')):
                raise cold.MigrationError('Historical repair artifacts changed; copy remains guarded.')
            if cold._directories(destination, profile=True) != plan['directories']:
                raise cold.MigrationError('Destination directory inventory changed; copy remains guarded.')
            if _history_inventory(destination) != plan['preserved_history']:
                raise cold.MigrationError('Historical journal evidence changed; do not activate the guarded copy.')
            for name in cold.DATABASE_NAMES:
                expected_tables = dict(plan['databases'][name])
                if name == 'freqinout.db' and plan['retired_manifests']:
                    expected_tables['software_instance_manifests'] -= len(plan['retired_manifests'])
                if cold._database_summary(destination / 'config' / name) != expected_tables:
                    raise cold.MigrationError('Copied table counts changed; do not activate the guarded copy.')
            if cold._files(source, profile=True) != plan['files'] or cold._directories(source, profile=True) != plan['directories'] or cold._sidecar_snapshot(source) != plan['sqlite_sidecars'] or path_origin(source) != origin or _recovery_digest(source) != plan['recovery_provenance_sha256']:
                raise cold.MigrationError('Source changed while preparing copy; do not activate it.')
            report = {'format': FORMAT, 'stage': 'prepared-not-activated', 'source': str(source), 'path_origin': str(origin),
                      'destination': str(destination), 'backup': str(backup), 'preview_digest': digest,
                      'backup_manifest_sha256': cold._hash(backup / 'manifest.json'),
                      'original_files': plan['files'], 'directories': plan['directories'], 'original_sqlite_sidecars': plan['sqlite_sidecars'],
                      'path_changes_sha256': hashlib.sha256(json.dumps(changes, sort_keys=True).encode()).hexdigest(),
                      'path_changes': changes, 'preserved_history': plan['preserved_history'], 'files': {name: cold._hash(destination / name) for name in plan['files']},
                      'retired_manifests': [{key: item[key] for key in ('rowid', 'instance_key', 'application_system_key', 'sha256')}
                                            for item in plan['retired_manifests']],
                      'retired_manifest_archive': retired_archive,
                      'next': 'Keep the original closed. Verify this copy with a one-shot FREQINOUT_CONFIG_DIR override and start-commsplan.sh. Close it, then run install-commsplan.sh setup --config-root with this destination to remember it. Keep original profile/backup. Never run both profiles concurrently.'}
            (destination / RECORD).write_text(json.dumps(report, indent=2) + '\n')
            (destination / GUARD).unlink()
            return report


def journal_inventory(source: Path):
    source = cold._plain_path(source)
    if not source.is_dir():
        raise cold.MigrationError('Select an existing profile root containing config/.')
    with cold._locked_profile(source):
        databases = []
        for name in cold.DATABASE_NAMES:
            path = source / 'config' / name
            if path.is_symlink() or not path.is_file():
                raise cold.MigrationError('Expected a regular config/' + name)
            databases.append({'database': name, 'bytes': path.stat().st_size,
                              'sidecars': cold._journal_inventory(path)})
        return {'profile': str(source), 'stage': 'journal-inventory', 'databases': databases,
                'all_profile_sidecars': cold._sidecar_snapshot(source, validate=False),
                'policy': 'Read-only filesystem inventory; no SQLite open, checkpoint, recovery or deletion. Nonempty WAL/journal requires review; SHM alone carries no database content.'}


def _publish_report(report, parent):
    # A new private directory prevents overwrites/symlink attacks and keeps
    # reports outside profile data/provenance. Full evidence remains available.
    directory = Path(tempfile.mkdtemp(prefix='CommsPlan-profile-report-', dir=parent))
    path = directory / 'report.json'
    path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    summary = {key: report[key] for key in
               ('stage', 'source', 'original_profile', 'profile', 'destination',
                'path_origin', 'backup', 'ready_to_copy', 'digest', 'next') if key in report}
    summary['report_file'] = str(path)
    summary['counts'] = {key: len(report[key]) for key in
                         ('files', 'directories', 'path_changes', 'blockers', 'external_paths', 'preserved_history', 'retired_manifests') if key in report}
    summary['retired_manifests'] = [{key: item[key] for key in ('rowid', 'instance_key', 'application_system_key', 'sha256')}
                                    for item in report.get('retired_manifests', [])[:4]]
    summary['retired_manifest_archive'] = report.get('retired_manifest_archive')
    groups = Counter(':'.join(item.split(':')[:2]) for item in report.get('blockers', []))
    summary['blocker_groups'] = dict(groups.most_common(12))
    summary['blocker_groups_omitted'] = max(0, len(groups) - 12)
    summary['blocked_instances'] = []
    for item in report.get('blocked_instances', [])[:4]:
        context = {key: (value[:256] if isinstance(value, str) else value)
                   for key, value in item.items() if key != 'missing_targets'}
        targets = item.get('missing_targets', [])
        context['missing_target_count'] = len(targets)
        context['missing_targets'] = [value[:512] for value in targets[:4]]
        summary['blocked_instances'].append(context)
    summary['blocked_instances_omitted'] = max(0, len(report.get('blocked_instances', [])) - 4)
    summary['blocker_samples'] = [item[:768] for item in report.get('blockers', [])[:8]]
    if report.get('stage') == 'journal-inventory':
        summary['databases'] = report['databases']
        summary['counts']['all_profile_sidecars'] = len(report['all_profile_sidecars'])
    summary['external_classifications'] = dict(Counter(
        item['classification'] for item in report.get('external_paths', [])))
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description='Preview or prepare a new CommsPlan profile. Original data and launch selection remain unchanged.')
    commands = parser.add_subparsers(dest='command', required=True)
    recover = commands.add_parser('profile-recover', help='Preserve raw bytes and consolidate SQLite only in a new staging copy')
    recover.add_argument('--source', type=Path, required=True)
    recover.add_argument('--backup-directory', type=Path, required=True)
    recover.add_argument('--companions-closed', action='store_true', required=True)
    journals = commands.add_parser('profile-journals', help='Read-only file/sidecar size inventory; does not open SQLite')
    journals.add_argument('--source', type=Path, required=True)
    for name in ('profile-plan', 'profile-copy'):
        sub = commands.add_parser(name)
        sub.add_argument('--source', type=Path, required=True, help='Actual existing profile ROOT containing config/')
        sub.add_argument('--destination', type=Path, required=True, help='NEW physical directory, usually ~/.commsplan')
        if name == 'profile-copy':
            sub.add_argument('--backup-directory', type=Path, required=True, help='Existing directory outside both profiles')
            sub.add_argument('--companions-closed', action='store_true', required=True, help='Confirm all companion writers are closed; the app lock cannot stop them')
            sub.add_argument('--reviewed-digest', required=True, help='Digest from the reviewed profile-plan output')
    for sub in (recover, journals, *[commands.choices[name] for name in ('profile-plan', 'profile-copy')]):
        sub.add_argument('--json', action='store_true', help='Print full evidence JSON instead of a compact summary with a private report file')
    args = parser.parse_args(argv)
    try:
        if args.command == 'profile-recover':
            from freqinout.core.journal_recovery import recover_profile
            report = recover_profile(args.source, args.backup_directory, companions_closed=args.companions_closed)
        elif args.command == 'profile-journals':
            report = journal_inventory(args.source)
        elif args.command == 'profile-plan':
            report = preview(args.source, args.destination)
        else:
            backup_dir = cold._plain_path(args.backup_directory)
            target = cold._plain_path(args.destination)
            if backup_dir == target or target in backup_dir.parents:
                raise cold.MigrationError('Backup directory must be outside the new profile.')
            report = copy_profile(args.source, target, backup_dir, args.reviewed_digest)
        parent = args.backup_directory if hasattr(args, 'backup_directory') else (
            args.destination.parent if hasattr(args, 'destination') else args.source.parent)
        output = report if args.json else _publish_report(report, cold._plain_path(parent))
        print(json.dumps(output, indent=2))
        return 0 if not report.get('blockers') else 2
    except (cold.MigrationError, OSError, ValueError, sqlite3.Error) as exc:
        print(f'Profile copy stopped: {exc}', file=sys.stderr)
        return 1
