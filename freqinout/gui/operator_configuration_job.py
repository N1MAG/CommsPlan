"""Bounded CP-only saves; worker owns SQLite and receives detached data only."""
from copy import deepcopy
from uuid import uuid4
from PySide6.QtCore import QThread, Signal
from freqinout.core.multi_radio_store import MultiRadioStore
from freqinout.core.operator_application_configuration import save_operator_application, load_operator_application_context
from freqinout.core.configuration_edit_session import ConfigurationConflict, load_unfinished_configuration, discard_unfinished_configuration

_LIVE_JOBS = set()


def live_configuration_jobs():
    """GUI-thread snapshot for application shutdown, including detached saves."""
    return tuple(_LIVE_JOBS)

class OperatorConfigurationJob(QThread):
    result_ready = Signal(object)

    def __init__(self, db_path, payload, *, action='save', parent=None):
        # Retain a running transaction independently of a closing editor. Save
        # has already been explicitly committed by the operator; no native or
        # radio action can occur here. Qt disconnects a destroyed receiver.
        super().__init__(None)
        _LIVE_JOBS.add(self)
        self.finished.connect(self._release)
        self.token = uuid4().hex
        self.db_path = db_path
        self.payload = deepcopy(dict(payload))
        self.action = action

    def _release(self):
        _LIVE_JOBS.discard(self)
        self.deleteLater()

    def run(self):
        response = {'job_token':self.token}
        try:
            store = MultiRadioStore(self.db_path)
            if self.action in {'station_review','schedule_enable','schedule_disable'}:
                from freqinout.core.operator_schedule_authorization import review_station_setup, set_schedule_authorization
                with store.connect():
                    pass
                if self.action != 'station_review':
                    response['review'] = set_schedule_authorization(store,self.payload['radio_id'],
                        enable=self.action == 'schedule_enable',expected_fingerprint=self.payload.get('fingerprint',''))
                response['setup'] = review_station_setup(store)
                response['new_radio_draft'] = load_unfinished_configuration(store, 'radio:new')
                self.result_ready.emit(response)
                return
            radio_action = self.action.startswith('radio_')
            if radio_action:
                from freqinout.core.operator_radio_configuration import load_operator_radio_context, save_operator_radio
                radio_id = self.payload.get('radio_id') or (self.payload.get('base') or {}).get('id')
                scope = f'radio:{radio_id or "new"}'
            else:
                scope = f"application:{int(self.payload['radio_id'])}:{self.payload['family_key']}"
            if self.action == 'radio_load':
                response.update(load_operator_radio_context(store,radio_id))
                # Catalog/cache and bounded rigctl discovery run off the GUI and
                # outside the radio read transaction. Failure cannot block manual setup.
                try:
                    from freqinout.core.radio_catalog import load_radio_catalog
                    response['radio_catalog'] = load_radio_catalog()
                except Exception:
                    response['radio_catalog'] = {'source': 'unavailable', 'entries': []}
                self.result_ready.emit(response)
                return
            if self.action == 'radio_discard':
                response['draft_revision'] = discard_unfinished_configuration(store,scope,expected_revision=self.payload['draft_revision'])
                response['discarded'] = True
                self.result_ready.emit(response)
                return
            if self.action in {'radio_save', 'radio_keep_draft'}:
                response.update(save_operator_radio(store, {**self.payload, 'keep_draft_only': self.action == 'radio_keep_draft'}))
                response['unfinished'] = load_unfinished_configuration(store,scope)
                self.result_ready.emit(response)
                return
            if self.action == 'discard':
                discard_unfinished_configuration(store,scope,expected_revision=self.payload['draft_revision'])
                response['discarded'] = True
            elif self.action == 'load':
                response.update(load_operator_application_context(store,self.payload))
                response['unfinished'] = load_unfinished_configuration(store,scope)
            else:
                response['result'] = save_operator_application(store,self.payload)
                response['profile'] = store.get_device_profile(int(self.payload['radio_id']))
                response['unfinished'] = load_unfinished_configuration(store,scope)
        except Exception as exc:
            response['error'] = str(exc) or exc.__class__.__name__
            if isinstance(exc, ConfigurationConflict) and self.action in {'save', 'radio_save', 'radio_keep_draft'}:
                try:
                    if self.action.startswith('radio_'):
                        response['conflict_context'] = load_operator_radio_context(store, radio_id)
                    else:
                        response['conflict_context'] = load_operator_application_context(store, self.payload)
                except Exception:
                    # Missing/removed targets retain the original error and draft.
                    pass
            try:
                response['unfinished'] = load_unfinished_configuration(store,scope)
            except Exception:
                pass
        self.result_ready.emit(response)
