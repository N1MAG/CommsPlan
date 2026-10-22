"""Dedicated serial map-reference mailbox; never shares the operational map lane."""
from dataclasses import dataclass
import threading
from PySide6.QtCore import QObject, Signal


@dataclass(frozen=True)
class ReferenceRequest:
    kind: str
    arguments: tuple


class MapReferenceController(QObject):
    ready = Signal(object)
    failed = Signal(str)
    progress = Signal(str)
    busy_changed = Signal(bool)
    invalidated = Signal()
    _completed = Signal(int, object, str)
    _progress = Signal(int, str)

    def __init__(self, parent=None, store=None):
        super().__init__(parent)
        self._store_holder = [store]
        self._active = True
        self._closed = False
        self._running = False
        self._generation = 0
        self._pending = None
        self._kind = ''
        self._cancel = threading.Event()
        self._completed.connect(self._finish)
        self._progress.connect(self._receive_progress)
        self.destroyed.connect(lambda *_args, event=self._cancel:event.set())
        from freqinout.gui.reference_source_events import reference_source_events
        reference_source_events().changed.connect(self._source_changed)

    def set_active(self,active):
        self._active=bool(active) and not self._closed
        self.invalidate()

    def invalidate(self):
        self._generation+=1
        self._pending=None
        self._cancel.set()

    def viewport_changed(self):
        if self._kind=='area' and (self._running or self._pending):
            self.invalidate()

    def _source_changed(self,_service):
        self.invalidate()
        self.invalidated.emit()

    def dispose(self):
        self._closed=True
        self._active=False
        self.invalidate()

    def request(self,kind,**kwargs):
        if not self._active or self._closed:
            return
        if kind not in ('build','area','callsign'):
            raise ValueError('Unknown reference request')
        self._generation+=1
        self._cancel.set()
        self._pending=(self._generation,ReferenceRequest(kind,tuple(kwargs.items())))
        self._kind=kind
        self.busy_changed.emit(True)
        self._dispatch()

    def _dispatch(self):
        if self._running or not self._pending or not self._active or self._closed:
            return
        generation,request=self._pending
        self._pending=None
        self._running=True
        self._cancel.clear()
        holder,cancel=self._store_holder,self._cancel
        done,progress=self._completed,self._progress
        def run():
            value,error=None,''
            try:
                if holder[0] is None:
                    from freqinout.core.license_map_reference import LicenseMapReferenceStore
                    holder[0]=LicenseMapReferenceStore()
                kwargs=dict(request.arguments)
                if request.kind=='build':
                    def report(message):
                        try:
                            progress.emit(generation,str(message))
                        except RuntimeError:
                            cancel.set()
                    kwargs.update(cancel=cancel,progress=report)
                value=getattr(holder[0],request.kind)(**kwargs)
            except Exception as exc:
                error=str(exc)
            try:
                done.emit(generation,value,error)
            except RuntimeError:
                pass
        threading.Thread(target=run,daemon=True,name='map-license-reference').start()

    def _receive_progress(self,generation,text):
        if generation==self._generation and self._active and not self._closed:
            self.progress.emit(text)

    def _finish(self,generation,result,error):
        self._running=False
        if generation==self._generation and self._active and not self._closed:
            if error:
                self.failed.emit(error)
            else:
                self.ready.emit(result)
        if self._pending:
            self._dispatch()
        else:
            self.busy_changed.emit(False)
