"""Consistent per-database support snapshots, atomically packaged off the UI thread."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import sqlite3
import tempfile
import threading
import time
import zipfile

from freqinout.core.sqlite_utils import connect_sqlite_readonly


class DiagnosticExportCancelled(RuntimeError):
    pass


def export_diagnostics(output_path, *, settings_db_path, log_path=None, version='',
                       log_level='', cancel_event=None, timeout_seconds=60):
    """Never mutate/checkpoint sources; keep any prior archive until full success.

    Each database has its own pinned read transaction. The two databases and log
    are captured sequentially and do not form a station-wide atomic snapshot.
    """
    cancel = cancel_event if cancel_event is not None else threading.Event()
    deadline = time.monotonic() + max(0.01, float(timeout_seconds))
    def utc():
        return datetime.now(timezone.utc).isoformat()
    def check():
        if cancel.is_set():
            raise DiagnosticExportCancelled('Diagnostic export was cancelled.')
        if time.monotonic() >= deadline:
            raise TimeoutError('Diagnostic export timed out. Retry when database activity is lower.')
    def interrupt():
        return int(cancel.is_set() or time.monotonic() >= deadline)
    def identity(path):
        stat = path.stat()
        return (stat.st_dev, stat.st_ino)
    output = Path(output_path).expanduser().absolute()
    settings = Path(settings_db_path).expanduser().absolute()
    databases = (settings, settings.with_name('freqinout_nets.db'))
    log = Path(log_path).expanduser().absolute() if log_path else None
    protected = tuple(path for database in databases for path in (database, *(Path(str(database)+suffix) for suffix in ('-wal','-shm','-journal'))))
    for source in (*protected, log):
        if source is None:
            continue
        if output.resolve() == source.resolve() or (output.exists() and source.exists() and os.path.samefile(output, source)):
            raise ValueError('Choose a ZIP destination different from the source databases and log.')
    manifest = {'format_version': 1, 'snapshot_method': 'sqlite-online-backup',
        'application_version': str(version), 'platform': platform.platform(), 'sqlite_version': sqlite3.sqlite_version,
        'log_level': str(log_level), 'export_started_utc': utc(), 'profile_directory_name': settings.parent.name,
        'profile_path_id': hashlib.sha256(str(settings.parent.resolve()).encode()).hexdigest()[:16],
        'consistency': 'Each database is transaction-consistent independently; databases and log are not one atomic snapshot.',
        'files': []}
    staged = None
    try:
        check()
        with tempfile.TemporaryDirectory(prefix='commsplan-diagnostics-', ignore_cleanup_errors=True) as temporary:
            snapshots = []
            captured_identities = []
            for index, source in enumerate(databases):
                check()
                name = 'freqinout.db' if index == 0 else 'freqinout_nets.db'
                entry = {'archive_name': name, 'source_name': source.name, 'capture_started_utc': utc()}
                if not source.exists():
                    if index == 0:
                        raise FileNotFoundError('The active settings database is unavailable. No diagnostic archive was replaced.')
                    entry.update(status='missing', capture_completed_utc=utc())
                    manifest['files'].append(entry)
                    continue
                before = identity(source)
                target = Path(temporary) / name
                with closing(connect_sqlite_readonly(source, timeout=.1, busy_timeout_ms=100)) as reader:
                    reader.set_progress_handler(interrupt, 1000)
                    reader.execute('BEGIN')
                    reader.execute('SELECT name FROM sqlite_master LIMIT 1').fetchone()
                    check()
                    if identity(source) != before:
                        raise RuntimeError('A source database was replaced during export. Retry with the current profile.')
                    entry['snapshot_utc'] = utc()
                    with closing(sqlite3.connect(target)) as destination:
                        def progress(_status, _remaining, _total):
                            check()
                        reader.backup(destination, pages=256, progress=progress, sleep=.01)
                        check()
                        destination.set_progress_handler(interrupt, 1000)
                        rows = destination.execute('PRAGMA quick_check').fetchall()
                        check()
                        if rows != [('ok',)]:
                            raise RuntimeError(f'The {name} snapshot failed its integrity check. No archive was replaced.')
                if identity(source) != before:
                    raise RuntimeError('A source database was replaced during export. Retry with the current profile.')
                entry.update(status='included', capture_completed_utc=utc(), source_identity=list(before))
                manifest['files'].append(entry)
                snapshots.append((target, name, entry))
                captured_identities.append((source, before))
            check()
            handle, staged_name = tempfile.mkstemp(prefix='.' + output.name + '-', suffix='.tmp', dir=output.parent)
            os.close(handle)
            staged = Path(staged_name)
            def add_file(archive, source, name, entry):
                digest = hashlib.sha256()
                copied = 0
                captured = datetime.fromisoformat(entry.get('snapshot_utc') or entry['capture_started_utc']).astimezone()
                member = zipfile.ZipInfo(name, date_time=captured.timetuple()[:6])
                member.compress_type = zipfile.ZIP_DEFLATED
                with source.open('rb') as reader, archive.open(member, 'w', force_zip64=True) as writer:
                    # A growing log is bounded to its size when opened.
                    remaining = os.fstat(reader.fileno()).st_size
                    while remaining:
                        check()
                        block = reader.read(min(1024 * 1024, remaining))
                        if not block:
                            break
                        writer.write(block)
                        digest.update(block)
                        copied += len(block)
                        remaining -= len(block)
                entry.update(bytes=copied, sha256=digest.hexdigest())
                if remaining:
                    entry['note'] = 'Source was truncated during capture; available bytes included.'
            with zipfile.ZipFile(staged, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                for source, name, entry in snapshots:
                    add_file(archive, source, name, entry)
                entry = {'archive_name': 'freqinout.log', 'source_name': log.name if log else '', 'capture_started_utc': utc()}
                if log is not None and log.exists():
                    add_file(archive, log, 'freqinout.log', entry)
                    entry['status'] = 'included'
                else:
                    entry['status'] = 'missing'
                entry['capture_completed_utc'] = utc()
                manifest['files'].append(entry)
                manifest['capture_completed_utc'] = utc()
                archive.writestr('diagnostics_manifest.json', json.dumps(manifest, indent=2))
                archive.writestr('diagnostics_info.txt',
                    f'CommsPlan version: {version}\nExported: {manifest["capture_completed_utc"]}\n'
                    f'Platform: {manifest["platform"]}\nLog level: {log_level}\n'
                    'Database capture: SQLite online backup, verified per database.\n'
                    + manifest['consistency'] + '\n'
                    + 'Missing files: ' + (', '.join(row['archive_name'] for row in manifest['files'] if row['status']=='missing') or 'none') + '\n')
            # CRC validation is bounded/cancellable, unlike an uninterruptible testzip().
            with zipfile.ZipFile(staged) as archive:
                for name in archive.namelist():
                    with archive.open(name) as member:
                        while member.read(1024 * 1024):
                            check()
            for source, before in captured_identities:
                if identity(source) != before:
                    raise RuntimeError('A source database was replaced before publication. Retry export.')
            check()
            os.replace(staged, output)
            staged = None
        return {'path': str(output), 'included': [row['archive_name'] for row in manifest['files'] if row['status']=='included'],
                'missing': [row['archive_name'] for row in manifest['files'] if row['status']=='missing']}
    except sqlite3.OperationalError:
        check()  # Translate a progress-handler interruption into its useful reason.
        raise
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
