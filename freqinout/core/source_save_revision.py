"""Revision fence for a source save that requires an operator confirmation."""
from pathlib import Path

from freqinout.core.config_paths import get_config_dir
from freqinout.core.schedule_configuration_cache import ConfigurationChanged, ScheduleRevisionTracker


def source_save_revision(config_dir: Path):
    root = Path(config_dir)
    if get_config_dir() / "config" != root:
        raise ConfigurationChanged("The active configuration changed. Reopen the source before saving.")
    tracker = ScheduleRevisionTracker(root, allow_missing=True)
    try:
        return tracker.key()
    finally:
        tracker.close()


def require_source_save_revision(config_dir: Path, expected):
    if source_save_revision(config_dir) != expected:
        raise ConfigurationChanged("The schedule or radio settings changed during review. Review the source again before saving.")


def require_locked_source_save_revision(config_dir: Path, expected):
    """Compare the already installed counters without taking another write lock."""
    from contextlib import closing
    from freqinout.core.sqlite_utils import connect_sqlite_readonly

    root = Path(config_dir)
    if get_config_dir() / "config" != root:
        raise ConfigurationChanged("The active configuration changed. Reopen the source before saving.")
    actual = []
    for filename in ("freqinout.db", "freqinout_nets.db"):
        path = root / filename
        try:
            stat = path.stat()
        except FileNotFoundError:
            actual.append(("missing",))
            continue
        with closing(connect_sqlite_readonly(path)) as connection:
            schema = connection.execute("PRAGMA schema_version").fetchone()[0]
            counter = connection.execute("SELECT revision FROM _fio_schedule_revision WHERE id=1").fetchone()
        actual.append((stat.st_dev, stat.st_ino, schema, int(counter[0])))
    if tuple(actual) != expected:
        raise ConfigurationChanged("The schedule or radio settings changed during review. Review the source again before saving.")
