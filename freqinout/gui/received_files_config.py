"""Bounded daemon workers for optional received-files directory checks."""
import os
import stat
import threading
import weakref

from PySide6.QtCore import QEvent, QObject, Signal

# A stalled network mount must neither hold shutdown open nor accumulate work.
_LANES = threading.BoundedSemaphore(2)


def _count_entries(path):
    fd = None
    try:
        path = os.path.normpath(path)
        if stat.S_ISLNK(os.lstat(path).st_mode):
            return 0, False, "Choose a directory that is not a symbolic link."
        # On platforms supporting it, open without following a replacement link
        # and scan the opened directory rather than looking up the path again.
        if hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY") and os.scandir in os.supports_fd:
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        count = 0
        with os.scandir(fd if fd is not None else path) as entries:
            for index, entry in enumerate(entries):
                if index == 100:
                    return count, True, ""
                if entry.is_file(follow_symlinks=False):
                    count += 1
        return count, False, ""
    except OSError as exc:
        return 0, False, str(exc)
    finally:
        if fd is not None:
            os.close(fd)


class ReceivedFilesCounter(QObject):
    finished = Signal(int, str, int, bool, str)
    _result_ready = Signal(int, str, int, bool, str, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state = {"active": True, "serial": 0}
        self._lock = threading.Lock()
        self._result_ready.connect(self._publish)
        if parent is not None:
            parent.installEventFilter(self)
            parent.destroyed.connect(self.cancel)

    def cancel(self, *_args):
        with self._lock:
            self._state["active"] = False
            self._state["serial"] += 1

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Close:
            self.cancel()
        return False

    def _publish(self, generation, path, count, more, error, serial):
        with self._lock:
            current = self._state["active"] and self._state["serial"] == serial
        if current:
            self.finished.emit(generation, path, count, more, error)

    def request(self, generation, path):
        with self._lock:
            if not self._state["active"]:
                return
            self._state["serial"] += 1
            serial = self._state["serial"]
        if not _LANES.acquire(blocking=False):
            self.finished.emit(generation, path, 0, False,
                               "Directory checks are busy. Try again when a check finishes.")
            return
        state, lock, ref = self._state, self._lock, weakref.ref(self)

        def run():
            try:
                count, more, error = _count_entries(path)
                with lock:
                    current = state["active"] and state["serial"] == serial
                counter = ref()
                if current and counter is not None:
                    try:
                        counter._result_ready.emit(generation, path, count, more, error, serial)
                    except RuntimeError:
                        pass  # Qt destroyed the editor during the check.
            finally:
                _LANES.release()

        try:
            threading.Thread(target=run, name="received-files-count", daemon=True).start()
        except Exception:
            _LANES.release()
            raise
