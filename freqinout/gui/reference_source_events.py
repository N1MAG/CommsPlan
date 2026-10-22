"""GUI-thread publication notice; no polling, filesystem watcher or source reads."""
from PySide6.QtCore import QObject, Signal


class _ReferenceSourceEvents(QObject):
    changed = Signal(str)


_events = None


def reference_source_events():
    global _events
    if _events is None:
        _events = _ReferenceSourceEvents()
    return _events
