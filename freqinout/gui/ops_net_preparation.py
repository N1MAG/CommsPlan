"""Ops→NCS handoff coordinator. Readers own I/O; Qt owns intent and drafts."""
from __future__ import annotations

import uuid
import hashlib
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QTimer, Signal
from freqinout.core.net_preparation import NetSelection, PreparationError, PreparedNet, Protocol
from freqinout.core.net_preparation_inputs import PreparationReader
from freqinout.core.scheduler_serial_executor import DaemonSerialExecutor
from freqinout.gui.net_prepared_receiver import ScopeSettings
from freqinout.gui.net_preparation_panel import NetPreparationPanel


class OpsNetPreparation(QObject):
    completed = Signal(object, object)

    def __init__(self, ops):
        super().__init__(ops)
        self.ops = ops
        self.reader = PreparationReader(Path(ops.settings.db_path).parent)
        self.executor = DaemonSerialExecutor(thread_name_prefix='fio-net-prepare')
        self.generation = 0
        self.lifecycle = 0
        self.context = None
        self.inflight = False
        self.latest = None
        self.closed = False
        self.receiver_contexts = {}
        self.panels = {}
        self.origin_panel = NetPreparationPanel(ops.settings, ops)
        self.origin_panel.hide()
        layout = getattr(ops, 'controlfreq_root_layout', None) or ops.layout()
        layout.insertWidget(0, self.origin_panel)
        self.origin_panel.action_requested.connect(self._origin_action)
        ops.installEventFilter(self)
        self.completed.connect(self._completed)
        self.deadline = QTimer(self)
        self.deadline.setSingleShot(True)
        self.deadline.timeout.connect(self._timeout)
        ops.destroyed.connect(self.close)

    def _window(self):
        return self.ops.window()

    def _epoch(self):
        return int(getattr(self._window(), '_navigation_epoch', 0))

    def _stopping(self):
        if self.closed:
            return True
        win = self._window()
        return getattr(self.ops, '_closing', False) or getattr(win, '_closing', False) or getattr(win, '_shutting_down', False)

    def request(self, entry):
        if self._stopping():
            return
        outlook = getattr(self.ops, '_outlook', None)
        if outlook is not None and not outlook.current(entry):
            outlook.tick()
            return
        self.cancel()
        epoch = self._epoch()
        try:
            pub = outlook.publication if outlook is not None else None
            row_id = int(entry.get('source_row_id') or 0)
            seed = NetSelection(uuid.uuid4().hex, row_id, tuple(entry['source_identity']), entry['when_utc'],
                                pub.inputs.revision if pub is not None else (),
                                pub.action_token if pub is not None else (), self.lifecycle, epoch,
                                return_context=self._screen_label(self.ops),
                                explicit_radio_id=getattr(self._window(), '_station_command_explicit_profile_id', None),
                                values_identity=str(entry.get('source_values_identity') or ''))
        except (KeyError, TypeError, ValueError) as exc:
            self._show('Source changed', f'The selected net cannot be identified: {exc}. Refresh and select it again.',
                       actions=[('Dismiss', 'dismiss', self.generation, 'secondary')])
            return
        self.context = dict(generation=self.generation, epoch=epoch, entry=dict(entry), seed=seed,
                            request=None, prepared=None, receiver=None, retain=False, repair=False)
        self._show('Checking selected net', 'Checking its saved schedule and eligible radio/service.')
        self._submit('select', seed)

    def _screen_label(self, widget):
        return next((label for label, item in getattr(self._window(), '_screens', ()) if item is widget), 'Operating Status')

    def _current(self, context, *, source=True):
        if self._stopping() or self.context is not context or context['generation'] != self.generation or self._epoch() != context['epoch']:
            return False
        if source and context.get('source_guard', True):
            outlook = getattr(self.ops, '_outlook', None)
            if outlook is not None and not outlook.current(context['entry']):
                self._show('Source changed', 'The upcoming schedule refreshed during checking. Refresh and select the net again.')
                return False
        return True

    def _show(self, title, detail='', *, choices=(), actions=None):
        ctx = self.context
        token = ctx['generation'] if ctx else self.generation
        if actions is None:
            actions = [('Review schedule', 'review', token, 'secondary'), ('Cancel preparation', 'cancel', token, 'secondary')]
        self.origin_panel.display(title, detail, actions=actions, choices=choices)
        self.origin_panel.show()

    def _submit(self, phase, value):
        context = self.context
        if context is None:
            return
        job = (context, phase, value)
        self.deadline.start(10000)
        if self.inflight:
            self.latest = job
            return
        self._start(job)

    def _start(self, job):
        self.inflight = True
        context, phase, value = job
        if phase == 'select':
            future = self.executor.submit(self.reader.select, value)
        elif phase == 'repair':
            future = self.executor.submit(self.reader.load, value.request,
                                          for_manual=context['manual'], expected=value.scope if context['manual'] else None,
                                          review_configuration=context['manual'])
        else:
            future = self.executor.submit(self.reader.load, value.request, expected=value.scope,
                                          for_manual=context.get('manual', False))
        def done(result):
            try:
                payload = result.result(), None
            except Exception as exc:
                payload = None, str(exc)
            if not self.closed:
                try:
                    self.completed.emit(job, payload)
                except RuntimeError:
                    pass
        future.add_done_callback(done)

    def _completed(self, job, payload):
        self.inflight = False
        try:
            self._accept_completed(job, payload)
        except Exception as exc:
            context = job[0]
            if self._current(context, source=False):
                self.deadline.stop()
                if context.get('receiver_mode'):
                    context['receiver']._prepared_receiver._set_status('Existing draft retained', str(exc))
                    self.context = None
                else:
                    self._show('Existing draft retained', str(exc))
        latest, self.latest = self.latest, None
        if latest is not None and self._current(latest[0]):
            self._start(latest)

    def _accept_completed(self, job, payload):
        context, phase, value = job
        result, error = payload
        if self._current(context):
            self.deadline.stop()
            if context.get('receiver_mode'):
                self._repair_completed(context, phase, value, result, error)
            elif error:
                self._show('Source changed' if 'Source' in error or 'occurrence' in error else 'Needs setup', error)
            elif phase == 'select':
                context['request'] = result.request
                options = result.radios
                if len(options) != 1 or not result.request.allows(options[0].scope):
                    context['options'] = options
                    choices = [(f'{item.scope.radio_name} — {"JS8Call" if item.scope.protocol == Protocol.JS8 else "FLDigi / SSB"}' + (' — needs setup' if item.missing else ''),
                                (context['generation'], index)) for index, item in enumerate(options)]
                    self._show('Choose radio and service', 'Confirm the intended context; nothing starts or changes frequency.', choices=choices)
                else:
                    self._destination(context, PreparedNet(result.request, options[0].scope), options[0])
            elif phase == 'destination':
                receiver = context['receiver']
                co = receiver._prepared_receiver
                signature = self._signature(receiver)
                if signature != context['receiver_signature']:
                    self._show('Existing draft retained', 'Receiving inputs changed during checking. Review them and select Prepare again.')
                else:
                    self._commit(context, value, result.radios[0])
            elif phase == 'retain':
                receiver = context['receiver']
                co = receiver._prepared_receiver
                signature = self._signature(receiver)
                if signature != context['receiver_signature']:
                    self._show('Existing draft retained', 'Receiving work changed after your choice; review before retaining it.')
                else:
                    self._commit(context, value, result.radios[0])

    def _destination(self, context, prepared, inputs):
        label = 'NCS-JS8' if prepared.scope.protocol == Protocol.JS8 else 'NCS-FLDigi/SSB'
        win = self._window()
        index = next((i for i, (name, _) in enumerate(getattr(win, '_screens', ())) if name == label), None)
        if index is None or bool(getattr(win, '_screen_is_runtime_suppressed', lambda _label: False)(label)):
            self._show('Needs setup', 'The intended net-control workspace is unavailable. Review the radio operating policy.')
            return
        settings = ScopeSettings(prepared.scope.settings(), self.reader.config_dir, writer=win.settings)
        previous = getattr(win, '_ncs_preparation_settings', None)
        win._ncs_preparation_settings = settings
        try:
            win._ensure_lazy_tab_loaded(label, index)
        finally:
            win._ncs_preparation_settings = previous
        receiver = win._screens[index][1]
        co = getattr(receiver, '_prepared_receiver', None)
        if co is None or not self._current(context):
            self._show('Needs setup', 'The net-control workspace could not open; existing work is retained.')
            return
        context.update(receiver=receiver, label=label, index=index, prepared=prepared, inputs=inputs,
                       receiver_signature=self._signature(receiver))
        self._show('Checking selected net', 'Rechecking the source and radio after opening the workspace.')
        self._submit('destination', prepared)

    def _commit(self, context, prepared, inputs):
        receiver = context['receiver']
        co = receiver._prepared_receiver
        outcome = co.prepare(prepared, inputs, retain_current=context['retain'])
        if outcome == 'conflict':
            context['prepared'], context['inputs'] = prepared, inputs
            context['receiver_signature'] = self._signature(receiver)
            if receiver._net_in_progress:
                actions = [('Open current net', 'open_current', self.generation, 'primary'),
                           ('Keep request for later', 'keep_pending', self.generation, 'secondary'),
                           ('Cancel preparation', 'cancel', self.generation, 'secondary')]
                self._show('Another net active', 'The active session and its records are retained. End it through its own workflow.', actions=actions)
            else:
                if context['retain']:
                    self._show('Existing draft retained', co.detail,
                               actions=[('Review retained drafts', 'open_current', self.generation, 'primary'),
                                        ('Keep request for later', 'keep_pending', self.generation, 'secondary'),
                                        ('Cancel preparation', 'cancel', self.generation, 'secondary')])
                    return
                actions = [('Keep current draft', 'cancel', self.generation, 'primary'),
                           ('Keep draft and prepare', 'retain', self.generation, 'secondary'),
                           ('Review differences', 'compare', self.generation, 'secondary')]
                self._show('Existing draft retained', f'Current: {self._name(receiver)}. Incoming: {prepared.request.source.values.values().get("net_name") or "selected net"} on {prepared.scope.radio_name}.', actions=actions)
            return
        if outcome not in {'accepted', 'needs attention'}:
            self._show(co.status, co.detail)
            return
        self._install_receiver(receiver, context)
        self._show(co.status, co.detail)
        self._window()._set_screen(context['index'])
        self._render_receiver(receiver)
        self.context = None

    @staticmethod
    def _name(receiver):
        control = getattr(receiver, 'net_name_edit', None) or getattr(receiver, 'net_name_combo', None)
        return control.text() if hasattr(control, 'text') else control.currentText()

    def _origin_action(self, action, payload):
        try:
            self._origin_action_impl(action, payload)
        except Exception as exc:
            if self.context is not None and self._current(self.context, source=False):
                self.deadline.stop()
                self._show('Existing draft retained', str(exc))

    def _origin_action_impl(self, action, payload):
        context = self.context
        if action == 'dismiss' and payload == self.generation:
            self.cancel()
            return
        if action in {'cancel', 'review'} and context is not None and payload == context['generation'] and self._current(context, source=False):
            if action == 'cancel':
                self.cancel()
            else:
                self.cancel(hide=False)
                self.ops._request_net_review(context['entry'], from_preparation=True)
            return
        token = payload[0] if action == 'choose' and isinstance(payload, (tuple, list)) else payload
        if context is None or token != context['generation'] or not self._current(context):
            return
        if action == 'cancel':
            self.cancel()
        elif action == 'review':
            self.cancel(hide=False)
            self.ops._request_net_review(context['entry'])
        elif action == 'choose':
            item = context['options'][int(payload[1])]
            request = replace(context['request'], explicit_radio_id=item.scope.radio_id,
                              protocol_choice=item.scope.protocol, choice_is_explicit=True)
            self._destination(context, PreparedNet(request, item.scope), item)
        elif action == 'retain':
            context['retain'] = True
            self._show('Checking selected net', 'Rechecking before retaining the current draft.')
            self._submit('retain', context['prepared'])
        elif action == 'compare':
            receiver = context['receiver']
            co = receiver._prepared_receiver
            incoming = context['prepared']
            values = incoming.request.source.values.values()
            current_scope = co.prepared.scope if co.prepared else None
            current_service = 'JS8Call' if co.protocol == Protocol.JS8 else 'FLDigi / SSB'
            detail = (f'Current: {self._name(receiver) or "unnamed draft"} · {current_scope.radio_name if current_scope else "existing radio context"} · {current_service} · {receiver.role_combo.currentText()}\n'
                      f'Incoming: {values.get("net_name") or "unnamed net"} · {incoming.scope.radio_name} · {current_service}\n'
                      f'Incoming operating group: {values.get("group_name") if "group_name" in values else "not supplied"}\n'
                      f'Incoming JS8 call group: {values.get("primary_js8call_group") if "primary_js8call_group" in values else "not supplied"}\n'
                      'All current fields, notes and roster entries are retained together in memory. No restart recovery is promised. The incoming net keeps your current role; Start remains separate.')
            self._show('Review differences', detail,
                       actions=[('Keep current draft', 'cancel', self.generation, 'primary'), ('Keep draft and prepare', 'retain', self.generation, 'secondary')])
        elif action in {'open_current', 'keep_pending'}:
            receiver = context['receiver']
            self._install_receiver(receiver, context, pending=action == 'keep_pending')
            self._window()._set_screen(context['index'])
            self._render_receiver(receiver)
            self.context = None

    def _install_receiver(self, receiver, context, *, pending=False):
        key = id(receiver)
        if key not in self.panels:
            panel = NetPreparationPanel(receiver.settings, receiver)
            if hasattr(receiver, '_ncs_scroll_content'):
                layout = receiver._ncs_scroll_content.layout()
            else:
                layout = receiver.js8_ncs_scroll_area.widget().layout()
            layout.insertWidget(0, panel)
            self.panels[key] = panel
            panel.action_requested.connect(lambda action, payload, target=receiver: self._receiver_action(target, action, payload))
            receiver._prepared_receiver.changed.connect(lambda target=receiver: self._render_receiver(target))
            receiver.installEventFilter(self)
            receiver.destroyed.connect(lambda *_args, ident=key: self.receiver_contexts.pop(ident, None))
        prior = self.receiver_contexts.get(key)
        if receiver._net_in_progress and prior is not None:
            if pending:
                prior['pending'] = context['prepared']
                prior['pending_entry'] = dict(context['entry'])
            return
        self.receiver_contexts[key] = dict(receiver=receiver, entry=dict(context['entry']), origin=context.get('origin') or context['seed'].return_context,
                                          pending=context['prepared'] if pending else None,
                                          pending_entry=dict(context['entry']) if pending else None, nonce=0)

    def _render_receiver(self, receiver):
        state = self.receiver_contexts.get(id(receiver))
        if state is None or self._stopping():
            return
        co, panel = receiver._prepared_receiver, self.panels[id(receiver)]
        state['nonce'] += 1
        nonce = state['nonce']
        prepared = co.prepared
        context = ''
        if prepared:
            values = prepared.request.source.values.values()
            context = f'{values.get("net_name") or "Selected net"} · {prepared.scope.radio_name} · {"JS8Call" if prepared.scope.protocol == Protocol.JS8 else "FLDigi / SSB"} · {receiver.role_combo.currentText()}'
            if co.manual:
                context = f'Manual / ad hoc · {prepared.scope.radio_name} · {"JS8Call" if prepared.scope.protocol == Protocol.JS8 else "FLDigi / SSB"} · {receiver.role_combo.currentText()}'
        actions = [('Review schedule', 'review', nonce, 'secondary'), ('Inspect source', 'inspect', nonce, 'secondary'), ('Return to Ops', 'return', nonce, 'secondary')]
        if prepared and not receiver._net_in_progress:
            actions += [('Check setup', 'recheck', nonce, 'secondary'), ('Repair setup', 'repair', nonce, 'secondary'),
                        ('Use manual / ad hoc context', 'manual', nonce, 'secondary')]
        drafts = list(co.retained.values())
        if co.pre_start_recovery is not None:
            drafts.append(co.pre_start_recovery)
        for index, draft in enumerate(drafts):
            name = next((c.value().get('text') if c.kind == 'combo' else c.value() for c in draft.controls if c.name in {'net_name_edit', 'net_name_combo'}), 'draft')
            actions.append((f'Restore {name or "retained draft"}', 'restore', (nonce, index), 'secondary'))
        state['drafts'] = drafts
        if state['pending'] is not None:
            actions.append(('Continue pending preparation', 'continue', nonce, 'secondary'))
        if state.get('repair_candidate') is not None and not receiver._net_in_progress:
            actions.append(('Use updated setup', 'use_setup', nonce, 'secondary'))
        panel.settings = receiver.settings
        panel.apply_theme()
        panel.display(co.status, co.detail, context, actions=actions)
        panel.show()

    def _receiver_action(self, receiver, action, payload):
        try:
            self._receiver_action_impl(receiver, action, payload)
        except Exception as exc:
            if not self._stopping() and self._window().stack.currentWidget() is receiver:
                receiver._prepared_receiver._set_status('Existing draft retained', str(exc))

    def _receiver_action_impl(self, receiver, action, payload):
        state = self.receiver_contexts.get(id(receiver))
        nonce = payload[0] if action == 'restore' and isinstance(payload, (tuple, list)) else payload
        if (state is None or nonce != state['nonce'] or self._stopping() or
                self._window().stack.currentWidget() is not receiver):
            return
        co = receiver._prepared_receiver
        if action == 'return':
            co.cancel_start()
            self.ops._navigate_to_tab(state['origin'])
        elif action == 'review':
            if co.prepared is not None and not co.manual:
                source = co.prepared.request.source
                entry = dict(state['entry'], source_row_id=source.row_id, source_key=f'NET:{source.row_id}',
                             source_identity=source.identity, when_utc=source.occurrence_start)
                self.ops._request_net_review(entry, from_preparation=True)
            else:
                co._set_status('Existing net retained', 'This manual session has no current scheduled association. Review its entered context here.')
        elif action == 'inspect' and co.prepared:
            values = co.prepared.request.source.values.values()
            source = co.prepared.request.source
            detail = '\n'.join(f'{label}: {values.get(key) if key in values else "not supplied"}' for label, key in [('Operating group','group_name'), ('JS8 call group','primary_js8call_group'), ('Frequency context','frequency'), ('Band','band'), ('Mode','mode'), ('VFO','vfo'), ('Offset','fldigi_offset'), ('Auto-tune context','auto_tune'), ('Early check-in minutes','early_checkin')])
            self.panels[id(receiver)].display('Saved schedule context', detail + f'\nUTC occurrence: {source.occurrence_start.isoformat()} → {source.occurrence_end.isoformat()}', actions=[('Back to preparation', 'keep', nonce, 'secondary')])
        elif action == 'repair' and co.prepared:
            win = self._window()
            section = 'js8call' if co.protocol == Protocol.JS8 else 'fldigi'
            if hasattr(win, 'open_settings_section'):
                co.cancel_start()
                win.open_settings_section(section, radio_id=co.prepared.scope.radio_id, settings_nav_context='software')
                self._install_repair_return(receiver, state)
        elif action == 'manual' and not receiver._net_in_progress:
            # Explicitly leave schedule association, preserving radio scope and
            # work. The ordinary validators still apply on the next Start.
            co.cancel_start()
            co.manual = True
            co.generation += 1
            co._set_status('Manual net context', 'Schedule association removed by your choice; intended radio and entered work retained. Start remains explicit.')
        elif action == 'restore':
            draft = state['drafts'][int(payload[1])]
            outcome = co.restore(draft)
            if outcome == 'conflict':
                state['restore'] = draft
                state['nonce'] += 1
                token = state['nonce']
                self.panels[id(receiver)].display('Existing draft retained', 'Restore needs a place for the current entered work.',
                    actions=[('Keep current draft', 'keep', token, 'primary'), ('Keep current and restore', 'restore_confirm', token, 'secondary')])
        elif action == 'restore_confirm':
            co.restore(state['restore'], retain_current=True)
            self._render_receiver(receiver)
        elif action == 'keep':
            self._render_receiver(receiver)
        elif action == 'continue':
            pending = state['pending']
            if pending is not None and not receiver._net_in_progress:
                self.ops._navigate_to_tab(state['origin'])
                self.cancel()
                self.context = dict(generation=self.generation, epoch=self._epoch(), source_guard=False,
                                    entry=dict(state.get('pending_entry') or state['entry']), seed=None,
                                    origin=state['origin'], request=pending.request, prepared=pending,
                                    receiver=None, retain=False, repair=False)
                self._show('Checking selected net', 'Checking the explicitly retained request; its saved occurrence must still match.')
                self._destination(self.context, pending, None)
        elif action == 'recheck':
            self._recheck_receiver(receiver, state)
        elif action == 'use_setup' and state.get('repair_candidate') is not None:
            candidate, signature = state['repair_candidate']
            if self._signature(receiver) != signature:
                state.pop('repair_candidate', None)
                co._set_status('Existing draft retained', 'Inputs changed after setup review. Check setup again.')
                return
            self._begin_receiver_check(receiver, state, candidate, 'repair_confirm')

    def _recheck_receiver(self, receiver, state):
        co = receiver._prepared_receiver
        if co.prepared is not None and not receiver._net_in_progress:
            state.pop('repair_candidate', None)
            self._begin_receiver_check(receiver, state, co.prepared, 'repair')

    @staticmethod
    def _signature(receiver):
        if receiver._net_in_progress:
            return ('active', receiver._ncs_session_id)
        snapshot = receiver._prepared_receiver.capture()
        return receiver._prepared_receiver.generation, snapshot.controls, snapshot.state_payload, snapshot.operator_edited

    def _begin_receiver_check(self, receiver, state, prepared, phase):
        self.cancel()
        co = receiver._prepared_receiver
        co.cancel_start()
        co._set_status('Checking selected net', 'Checking setup without starting; all entered work stays in this workspace.')
        self.context = dict(generation=self.generation, epoch=self._epoch(), source_guard=False, receiver_mode=True,
                            entry=dict(state['entry']), receiver=receiver, prepared=prepared, manual=co.manual,
                            receiver_signature=self._signature(receiver))
        self._submit(phase, prepared)

    def _repair_completed(self, context, phase, prepared, result, error):
        receiver = context['receiver']
        co = receiver._prepared_receiver
        state = self.receiver_contexts[id(receiver)]
        self.context = None
        if self._signature(receiver) != context['receiver_signature']:
            co._set_status('Existing draft retained', 'Inputs changed during checking. Review them and check setup again.')
            return
        if error:
            co._set_status('Source changed' if 'Source' in error or 'occurrence' in error else 'Needs setup', error)
            return
        inputs = next((item for item in result.radios if item.scope.radio_id == prepared.scope.radio_id and item.scope.protocol == co.protocol), None)
        if inputs is None:
            co._set_status('Needs setup', 'The intended radio/service is unavailable. Repair it or review the schedule; your work is retained.')
            return
        candidate = PreparedNet(prepared.request, inputs.scope)
        same = (candidate.scope.configuration_identity == co.prepared.scope.configuration_identity if co.manual
                else candidate.scope.identity == co.prepared.scope.identity)
        if phase == 'repair_confirm':
            state.pop('repair_candidate', None)
            co.rebind(candidate, inputs)
        elif not same:
            state['repair_candidate'] = candidate, self._signature(receiver)
            co._set_status('Source changed', f'Setup for {inputs.scope.radio_name} changed. Review it in Repair setup, then choose Use updated setup to keep this draft on that radio. Start remains separate.')
        else:
            co._set_status('Needs setup' if inputs.missing else ('Manual net context' if co.manual else 'Prepared'),
                           ', '.join(inputs.missing) if inputs.missing else 'Setup checked; entered work retained. Start remains explicit.')

    def _install_repair_return(self, receiver, state):
        win = self._window()
        settings = next((widget for label, widget in getattr(win, '_screens', ()) if label == 'Settings'), None)
        if settings is None or settings.layout() is None or win.stack.currentWidget() is not settings:
            return
        panel = getattr(self, 'repair_return_panel', None)
        if panel is None:
            panel = self.repair_return_panel = NetPreparationPanel(win.settings, settings)
            settings.layout().insertWidget(0, panel)
            settings.installEventFilter(self)
            panel.action_requested.connect(self._repair_return)
        self.repair_return_context = receiver, state, receiver._prepared_receiver.prepared, self._epoch()
        panel.display('Net draft retained', 'Repair the selected radio/service here, then return to check it. Returning never starts the net.',
                      actions=[('Return to retained net', 'return', self._epoch(), 'secondary')])
        panel.show()

    def _repair_return(self, action, epoch):
        if not getattr(self, 'repair_return_context', None):
            return
        receiver, state, prepared, expected_epoch = self.repair_return_context
        win = self._window()
        if (action != 'return' or self._stopping() or epoch != expected_epoch or self._epoch() != expected_epoch
                or win.stack.currentWidget() is not self.repair_return_panel.parentWidget()
                or receiver._prepared_receiver.prepared is not prepared):
            return
        self.repair_return_panel.hide()
        index = next((i for i, (_, widget) in enumerate(win._screens) if widget is receiver), None)
        if index is not None:
            win._set_screen(index)
            self._recheck_receiver(receiver, state)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Hide and not self.closed:
            panel = getattr(self, 'repair_return_panel', None)
            if panel is not None and watched is panel.parentWidget():
                panel.hide()
                self.repair_return_context = None
            context = self.context
            if context is not None:
                if watched is self.ops and not context.get('receiver_mode'):
                    self.cancel()
                elif context.get('receiver_mode') and watched is context.get('receiver'):
                    self.cancel()
                    watched._prepared_receiver._set_status('Existing draft retained', 'Checking cancelled after navigation; check setup again when ready.')
        return super().eventFilter(watched, event)

    def label_for(self, entry):
        for state_key, state in self.receiver_contexts.items():
            panel = self.panels.get(state_key)
            receiver = state.get('receiver')
            co = getattr(receiver, '_prepared_receiver', None)
            prepared = co.prepared if co else None
            if not prepared or co.manual or not receiver._net_in_progress or not receiver._ncs_session_id:
                continue
            source = prepared.request.source
            explicit_radio = getattr(self._window(), '_station_command_explicit_profile_id', None)
            if source.target.scope != 'device_profile' and explicit_radio != prepared.scope.radio_id:
                continue
            if (source.row_id == entry.get('source_row_id') and source.identity == tuple(entry.get('source_identity') or ())
                    and source.occurrence_start == entry.get('when_utc')
                    and hashlib.sha256(source.values.payload.encode()).hexdigest() == entry.get('source_values_identity')):
                return 'Open active net'
        return 'Prepare net'

    def cancel(self, *, hide=True):
        self.generation += 1
        self.context = None
        self.latest = None
        self.deadline.stop()
        if hide:
            self.origin_panel.hide()

    def _timeout(self):
        if self.context is not None and self._current(self.context, source=False):
            context = self.context
            self.cancel(hide=False)
            if context.get('receiver_mode'):
                context['receiver']._prepared_receiver._set_status('Needs setup', 'Checking timed out. Work is retained; check setup again when available.')
            else:
                self.origin_panel.display('Checking timed out', 'Work is retained. Refresh the schedule or review setup, then select Prepare again.',
                                          actions=[('Dismiss', 'dismiss', self.generation, 'secondary')])

    def close(self, *_):
        self.closed = True
        self.lifecycle += 1
        self.cancel()
        self.executor.shutdown(wait=False, cancel_futures=True)
