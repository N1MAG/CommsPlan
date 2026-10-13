"""Preserve raw journal-bearing files, then consolidate SQLite in copies only."""
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile

from freqinout.core import brand_migration as cold

FORMAT = 'commsplan-journal-capture-v1'
RECORD = '.commsplan-journal-recovery.json'
GUARD = '.commsplan-copy-incomplete'


def _raw_files(profile):
    return cold._files(profile, profile=True, include_sidecars=True)


def _copy_file(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination, follow_symlinks=False)


def recover_profile(source: Path, backup_directory: Path, *, companions_closed: bool = False):
    if not companions_closed:
        raise cold.MigrationError('Confirm all companion writers are closed before journal recovery.')
    # Import only the pure filesystem/SQLite preparation module, never runtime.
    from freqinout.core.profile_transition import _database_files
    source, backup_directory = cold._plain_path(source), cold._plain_path(backup_directory)
    if not source.is_dir() or not backup_directory.is_dir():
        raise cold.MigrationError('Source profile and backup directory must already exist.')
    if source == backup_directory or source in backup_directory.parents:
        raise cold.MigrationError('Backup directory must be outside the original profile.')
    if (source / GUARD).exists() or (source / RECORD).exists():
        raise cold.MigrationError('Use the original station, not an incomplete or previously recovered copy.')
    for name in cold.DATABASE_NAMES:
        if not (source / 'config' / name).is_file():
            raise cold.MigrationError('Missing config/' + name)
    with cold._locked_profile(source):
        before = _raw_files(source)
        directories = cold._directories(source, profile=True)
        sidecars = cold._sidecar_snapshot(source, validate=False)
        if any(item['state'] != 'regular' for item in sidecars):
            raise cold.MigrationError('Linked or irregular sidecars require review.')
        databases = _database_files(source, before)
        if any(item['path'].rsplit('-', 1)[0] not in databases for item in sidecars):
            raise cold.MigrationError('Orphan sidecars require review; original files are untouched.')
        needed = sum((source / name).stat().st_size for name in before) * 3 + 64 * 1024 * 1024
        if shutil.disk_usage(backup_directory).free < needed:
            raise cold.MigrationError('Insufficient space for raw capture, recovery inputs and consolidated copy (about three profile copies).')
        workspace = Path(tempfile.mkdtemp(prefix='CommsPlan-journal-recovery-', dir=backup_directory))
        raw = workspace / 'raw-profile'
        raw.mkdir(mode=0o700)
        profile = workspace / 'recovered-profile'
        profile.mkdir(mode=0o700)
        (profile / GUARD).write_text('Journal recovery copy is incomplete. Do not launch it.\n')
        try:
            for name in directories:
                (raw / name).mkdir(parents=True, exist_ok=True)
                (profile / name).mkdir(parents=True, exist_ok=True)
            for name in before:
                _copy_file(source / name, raw / name)
            if (_raw_files(source) != before or cold._sidecar_snapshot(source, validate=False) != sidecars
                    or cold._files(raw, profile=False) != before
                    or cold._directories(source, profile=True) != directories
                    or cold._directories(raw, profile=False) != directories
                    or cold._directories(profile, profile=True) != directories):
                raise cold.MigrationError('Source changed during raw capture. Close all writers and retry.')
            capture = {'format': FORMAT, 'source': str(source), 'files': before, 'directories': directories,
                       'stage': 'raw-capture-verified', 'sqlite_sidecars': sidecars,
                       'excluded_roots': sorted(cold.EXCLUDED_ROOTS)}
            manifest = workspace / 'raw-manifest.json'
            manifest.write_text(json.dumps(capture, indent=2) + '\n')
            inputs = workspace / 'sqlite-input'
            inputs.mkdir(mode=0o700)
            for name in before:
                if name not in databases and not any(name == db + suffix for db in databases for suffix in ('-wal', '-shm', '-journal')):
                    _copy_file(raw / name, profile / name)
            for name in databases:
                input_path = inputs / name
                _copy_file(raw / name, input_path)
                for suffix in ('-wal', '-shm', '-journal'):
                    sidecar = name + suffix
                    if sidecar in before:
                        _copy_file(raw / sidecar, inputs / sidecar)
                output = profile / name
                output.parent.mkdir(parents=True, exist_ok=True)
                # SQLite may recover/checkpoint this disposable input. The raw
                # capture and original are never opened by SQLite.
                with closing(sqlite3.connect(input_path.as_uri() + '?mode=rw', uri=True, timeout=1)) as reader:
                    if reader.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                        raise cold.MigrationError('Copied SQLite input failed integrity: ' + name)
                    tables = [row[0] for row in reader.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
                    counts = {table: reader.execute('SELECT COUNT(*) FROM "' + table.replace('"', '""') + '"').fetchone()[0] for table in tables}
                    with closing(sqlite3.connect(output)) as writer:
                        reader.backup(writer, pages=1024)
                        if writer.execute('PRAGMA journal_mode=DELETE').fetchone()[0].lower() != 'delete':
                            raise cold.MigrationError('Copied output could not leave WAL mode: ' + name)
                    if any(item['state'] != 'absent' for item in cold._journal_inventory(output)):
                        raise cold.MigrationError('Consolidated output still has sidecars: ' + name)
                    if cold._database_summary(output) != counts:
                        raise cold.MigrationError('Consolidated database table counts differ: ' + name)
            if (_raw_files(source) != before or cold._sidecar_snapshot(source, validate=False) != sidecars
                    or cold._files(raw, profile=False) != before
                    or cold._directories(source, profile=True) != directories
                    or cold._directories(raw, profile=False) != directories
                    or cold._directories(profile, profile=True) != directories):
                raise cold.MigrationError('Source or raw capture changed during recovery; do not use the guarded copy.')
            cold._inspect_profile(profile)
            files = {name: cold._hash(profile / name) for name in before if (profile / name).is_file()}
            report = {'format': FORMAT, 'stage': 'recovered-not-activated', 'original_profile': str(source),
                      'profile': str(profile), 'raw_manifest': str(manifest),
                      'raw_manifest_sha256': cold._hash(manifest), 'files': files, 'directories': directories,
                      'next': 'Use this recovered profile as --source for profile plan/copy. Stored original paths are retargeted using verified capture provenance. Keep original and workspace; do not launch or edit this staging copy.'}
            (profile / RECORD).write_text(json.dumps(report, indent=2) + '\n')
            (profile / GUARD).unlink()
            return report
        except Exception as exc:
            (workspace / 'INCOMPLETE.txt').write_text('Journal recovery did not finish. Original untouched; preserve this workspace for review.\n')
            raise cold.MigrationError(f'Journal recovery copy stopped at {workspace}; original untouched: {exc}') from exc


def path_origin(profile: Path) -> Path:
    """Derive original path root only from a verified local recovery capture."""
    record = profile / RECORD
    if not record.exists():
        return profile
    try:
        if record.is_symlink():
            raise ValueError('linked record')
        report = json.loads(record.read_text())
        manifest = cold._plain_path(Path(report['raw_manifest']))
        if (report['format'] != FORMAT or report['stage'] != 'recovered-not-activated'
                or report['profile'] != str(profile) or manifest.parent != profile.parent
                or report['raw_manifest_sha256'] != cold._hash(manifest)):
            raise ValueError('recovery identity mismatch')
        capture = json.loads(manifest.read_text())
        if not Path(report['original_profile']).is_absolute():
            raise ValueError('original root must be absolute')
        original = cold._plain_path(Path(report['original_profile']))
        if capture['format'] != FORMAT or capture['stage'] != 'raw-capture-verified' or capture['source'] != str(original):
            raise ValueError('capture identity mismatch')
        if cold._files(manifest.parent / 'raw-profile', profile=False) != capture['files']:
            raise ValueError('raw capture changed')
        if 'directories' in report or 'directories' in capture:
            directories = report.get('directories')
            if (directories != capture.get('directories')
                    or cold._directories(manifest.parent / 'raw-profile', profile=False) != directories
                    or cold._directories(profile, profile=True) != directories):
                raise ValueError('captured directory tree changed')
        actual = cold._files(profile, profile=True)
        actual.pop(RECORD, None)
        if actual != report['files']:
            raise ValueError('recovered staging data changed')
        return original
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise cold.MigrationError('Journal recovery provenance is invalid; do not use this staging copy.') from exc
