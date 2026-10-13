"""FreqInOut compatibility package; importing metadata never opens a profile."""

from freqinout.version import __version__


# Keep the public convenience API, but defer SettingsManager/logger imports until
# a timezone/time helper is actually called. Migration inspection must be able
# to import its modules without creating a default profile or log directory.
def get_timezone():
    from freqinout.utils import get_timezone as shared_get_timezone
    return shared_get_timezone()


def get_utc_time():
    from freqinout.utils import get_utc_time as shared_get_utc_time
    return shared_get_utc_time()


def get_local_time():
    from freqinout.utils import get_local_time as shared_get_local_time
    return shared_get_local_time()
