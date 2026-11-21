"""Log file handlers that recreate their file if it is deleted while running."""
import logging
import os
from logging.handlers import RotatingFileHandler


class _ReopenOnDeleteMixin:
    """Reopen the log file if it was deleted or replaced while the app runs.

    A plain FileHandler keeps writing to its open file descriptor, so if the
    file is deleted (or the logs folder is wiped) output silently goes nowhere
    until the process restarts. Before each write, check that the path still
    points at the file we have open; if not, recreate it.
    """

    _dev_ino = None

    def _open(self):
        stream = super()._open()
        try:
            st = os.fstat(stream.fileno())
            self._dev_ino = (st.st_dev, st.st_ino)
        except OSError:
            self._dev_ino = None
        return stream

    def _reopen_if_needed(self):
        if self.stream is None:
            return
        try:
            st = os.stat(self.baseFilename)
            current = (st.st_dev, st.st_ino)
        except FileNotFoundError:
            current = None
        if current is not None and current == self._dev_ino:
            return
        try:
            self.stream.close()
        except OSError:
            pass
        self.stream = None
        os.makedirs(os.path.dirname(self.baseFilename) or '.', exist_ok=True)
        self.stream = self._open()

    def emit(self, record):
        try:
            self._reopen_if_needed()
        except Exception:
            self.handleError(record)
            return
        super().emit(record)


class SelfHealingRotatingFileHandler(_ReopenOnDeleteMixin, RotatingFileHandler):
    """RotatingFileHandler that recreates its file if deleted externally."""


class SelfHealingFileHandler(_ReopenOnDeleteMixin, logging.FileHandler):
    """FileHandler that recreates its file if deleted externally."""
