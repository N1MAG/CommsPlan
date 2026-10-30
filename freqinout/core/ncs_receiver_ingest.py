"""Pinned passive API ingest using the existing canonical message importer."""
from pathlib import Path
import copy


def ingest_ncs_events(db_path, settings_values, events, source):
    from freqinout.core.message_ingest import MessageIngestor
    class Snapshot:
        def __init__(self, values):
            self._data = values
        def get(self, key, default=None):
            return copy.deepcopy(self._data.get(key, default))
        def all(self):
            return copy.deepcopy(self._data)
    class PinnedIngestor(MessageIngestor):
        def _db_path(self):
            return Path(db_path)
        def _local_js8_db(self):
            # The legacy importer has a separate ordinary-message cache resolver.
            # Forms and ordinary traffic must share this pinned profile.
            return Path(db_path)
        def _directed_js8_recipients(self):
            from freqinout.core.group_utils import normalize_group_name
            calls, groups = super()._directed_js8_recipients()
            groups.update(normalize_group_name(value) for value in self.settings.get('primary_js8_groups', ()) if normalize_group_name(value))
            return calls, groups
        def _enqueue_next_msg_id(self, *args, **kwargs):
            # This receiver adds passive evidence only. The station coordinator
            # owns all automatic query backlog evaluation.
            return None
    if not Path(db_path).is_file():
        raise FileNotFoundError('Pinned traffic database is unavailable')
    # Automatic Expect evaluation remains owned by the station coordinator.
    return PinnedIngestor(Snapshot(settings_values)).ingest_spotter_from_js8_events(
        events, source_radio_id=source.get('source_radio_id', ''),
        js8_instance_id=source.get('js8_instance_id', ''),
        source_key=source.get('source_id', ''), evaluate_expect=False)
