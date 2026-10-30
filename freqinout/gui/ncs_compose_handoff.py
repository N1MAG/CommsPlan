"""NCS working-copy handoff using the existing shared Compose editor/codec.

Helpers touch cached widgets only. The NCS host owns worker persistence and
source-scoped send history. No form decoding, database or transport lives here.
"""
import copy
import json


def is_ncs_intent(intent):
    return isinstance(intent, dict) and intent.get('source') == 'js8_ncs' and isinstance(intent.get('ncs_context'), dict)


def draft_key(intent):
    context = intent.get('ncs_context', {})
    return json.dumps([context.get('run_id'), context.get('entry_id'),
                       intent.get('expect_entry_id'), intent.get('ncs_target') or intent.get('recipient_callsign')], separators=(',', ':'))


def capture(view):
    mode = str(getattr(view, '_compose_mode', 'spotter'))
    view._store_compose_form_draft()
    view._store_compose_mode_draft(mode)
    return copy.deepcopy({'mode': mode, 'intent': getattr(view, '_compose_intent', {}),
                          'draft': view._compose_mode_drafts.get(mode, {}),
                          'fields': view._compose_field_values(),
                          'working_response': getattr(view, '_compose_spotter_working_response', ''),
                          'response_dirty': getattr(view, '_compose_spotter_working_response_dirty', False)})


def restore(view, snapshot):
    mode = snapshot['mode']
    view._compose_mode_drafts[mode] = copy.deepcopy(snapshot['draft'])
    intent = copy.deepcopy(snapshot['intent'])
    intent.setdefault('mode', 'commstat' if mode == 'commstat_rf' else mode)
    intent.setdefault('transport', intent['mode'])
    view._apply_prefill_compose_intent(intent)
    view._restore_compose_mode_draft(mode)
    for key, value in snapshot.get('fields', {}).items():
        view._compose_set_widget_value(key, value)
    view._compose_spotter_working_response = snapshot.get('working_response', '')
    view._compose_spotter_working_response_dirty = bool(snapshot.get('response_dirty'))
    view._update_compose_preview()


def retain(view):
    if getattr(view, '_is_shutting_down', False):
        return
    intent = dict(getattr(view, '_compose_intent', {}) or {})
    if not is_ncs_intent(intent):
        return
    snapshot = capture(view)
    bank = getattr(view, '_ncs_compose_drafts', None)
    if bank is None:
        bank = view._ncs_compose_drafts = {}
    key = draft_key(intent)
    bank[key] = snapshot
    callback = getattr(view.window(), 'retain_ncs_compose_draft', None)
    if callable(callback):
        callback(dict(intent['ncs_context']), key, snapshot)


def begin_handoff(view, intent):
    """Retain current work before a new selection; return an existing NCS copy."""
    bank = getattr(view, '_ncs_compose_drafts', None)
    if bank is None:
        bank = view._ncs_compose_drafts = {}
    old = dict(getattr(view, '_compose_intent', {}) or {})
    if is_ncs_intent(old):
        retain(view)
    elif not hasattr(view, '_ncs_previous_compose_draft'):
        view._ncs_previous_compose_draft = capture(view)
    key = draft_key(intent)
    if key not in bank and len(bank) >= 16:
        raise ValueError('Retained Net Message draft limit reached; finish or explicitly clear a draft before opening another')
    callback = getattr(view.window(), 'ncs_compose_draft', None)
    if key not in bank and callable(callback):
        retained = callback(intent['ncs_context'], key)
        if retained:
            bank[key] = retained
    return copy.deepcopy(bank.get(key))


def restore_previous(view):
    previous = getattr(view, '_ncs_previous_compose_draft', None)
    if previous is None:
        return False
    retain(view)
    restore(view, previous)
    del view._ncs_previous_compose_draft
    return True


def publish_saved_entry_snapshot(view, intent):
    entry = intent.get('ncs_expect_entry')
    if not is_ncs_intent(intent) or not isinstance(entry, dict):
        return
    if int(entry.get('id') or 0) != int(intent.get('expect_entry_id') or 0):
        raise ValueError('Saved form reference changed; working copy retained')
    if str(entry.get('expect_key') or '').upper() == 'Q' or not entry.get('response_text'):
        raise ValueError('A static saved form response is required')
    entries = [row for row in getattr(view, '_compose_spotter_expect_entries', [])
               if int(row.get('id') or 0) != int(entry['id'])]
    view._compose_spotter_expect_entries = entries[:199] + [copy.deepcopy(entry)]
    view._compose_spotter_expect_loaded = True
    view._apply_compose_spotter_source_options()


def queue_retention(view):
    if getattr(view, '_is_shutting_down', False):
        return
    if not is_ncs_intent(dict(getattr(view, '_compose_intent', {}) or {})):
        return
    from PySide6.QtCore import QTimer
    timer = getattr(view, '_ncs_compose_retention_timer', None)
    if timer is None:
        timer = view._ncs_compose_retention_timer = QTimer(view)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: retain(view))
    timer.start(300)


def discard_current(view):
    """Explicitly clear one NCS working copy without retaining it again."""
    intent = dict(getattr(view, '_compose_intent', {}) or {})
    if not is_ncs_intent(intent):
        return None
    key = draft_key(intent)
    timer = getattr(view, '_ncs_compose_retention_timer', None)
    if timer is not None:
        timer.stop()
    previous = getattr(view, '_ncs_previous_compose_draft', None)
    if previous is not None:
        restore(view, previous)
        del view._ncs_previous_compose_draft
    else:
        restore(view, {'mode': 'js8', 'intent': {'mode': 'js8', 'transport': 'js8'}, 'draft': {}, 'fields': {}})
        view.compose_js8_plain_text_edit.clear()
        view.compose_js8_target_edit.clear()
    getattr(view, '_ncs_compose_drafts', {}).pop(key, None)
    return key
