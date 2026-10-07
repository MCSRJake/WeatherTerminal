"""Process guard and bounded local diagnostics; no side effects on import."""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
from typing import Any, BinaryIO, Iterator


class AlreadyRunning(RuntimeError):
    pass


class SingleInstance:
    """Hold a Windows named-object handle for the lifetime of the process.

    Existence, not mutex ownership, is the guard. All startup/shutdown work occurs
    while the handle is held. Windows closes it after a crash, so no stale lock
    file needs deleting. The name is constant even if --data-dir differs.
    """
    WINDOWS_NAME = r"Local\ElementalInformatics.WeatherTerminal.v1"

    def __init__(self, runtime_root: Path) -> None:
        self.runtime_root = runtime_root
        self._handle: Any = None
        self._kernel32: Any = None
        self._file: BinaryIO | None = None

    def acquire(self) -> None:
        if self._handle is not None or self._file is not None:
            raise RuntimeError("This instance guard has already been acquired")
        if sys.platform == "win32":
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
            kernel.CreateMutexW.restype = wintypes.HANDLE
            kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel.CloseHandle.restype = wintypes.BOOL
            ctypes.set_last_error(0)
            handle = kernel.CreateMutexW(None, False, self.WINDOWS_NAME)
            error = ctypes.get_last_error()
            if not handle:
                raise ctypes.WinError(error)
            if error == 183:  # ERROR_ALREADY_EXISTS
                kernel.CloseHandle(handle)
                raise AlreadyRunning("Another Weather Terminal instance exists")
            self._kernel32 = kernel
            self._handle = handle
        else:
            # Developer/test fallback only. Same-root flock, no file deletion race.
            import fcntl
            self.runtime_root.mkdir(parents=True, exist_ok=True)
            lock = (self.runtime_root / ".instance.lock").open("a+b")
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                lock.close()
                raise AlreadyRunning("Another Weather Terminal instance exists") from exc
            except BaseException:
                lock.close()
                raise
            self._file = lock

    def release(self) -> None:
        if self._handle is not None:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None
            self._kernel32 = None
        if self._file is not None:
            # Closing the descriptor releases the OS lock even after a crash.
            self._file.close()
            self._file = None

    def __enter__(self) -> SingleInstance:
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


class _WeatherLogHandler(RotatingFileHandler):
    """Own only our file handler and the logger settings changed during its life."""

    def __init__(self, path: Path, logger: logging.Logger) -> None:
        # Initialize ownership before the base constructor: logging.shutdown()
        # may later close a partially initialized handler after an open failure.
        self._logger = logger
        self._previous_level = logger.level
        self._previous_propagate = logger.propagate
        self._owns_settings = False
        super().__init__(path, maxBytes=1_048_576, backupCount=3, encoding="utf-8")

    def attach(self) -> None:
        self._owns_settings = True
        try:
            self._logger.setLevel(logging.INFO)
            self._logger.propagate = False
            self._logger.addHandler(self)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        # Idempotent: a second close (including logging.shutdown) must not
        # overwrite changes made by a later logging session.
        try:
            if self._owns_settings:
                self._owns_settings = False
                self._logger.removeHandler(self)
                self._logger.setLevel(self._previous_level)
                self._logger.propagate = self._previous_propagate
        finally:
            super().close()


def configure_logging(path: Path) -> RotatingFileHandler:
    """Attach one owned file handler; preserve any caller-installed handlers.

    A foreign handler (for example a diagnostic/test capture handler) does not
    mean this application is already running. Only our active handler blocks a
    second configuration. close() removes ours and restores previous settings.
    """
    logger = logging.getLogger("weather_terminal")
    if any(isinstance(handler, _WeatherLogHandler) for handler in logger.handlers):
        raise RuntimeError("Weather Terminal logging is already configured")
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = _WeatherLogHandler(path, logger)
    try:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        handler.attach()
    except BaseException:
        handler.close()
        raise
    return handler


@contextmanager
def application_logging(path: Path) -> Iterator[logging.Logger]:
    """Pair log acquisition/cleanup even when configuration or UI startup fails."""
    handler = configure_logging(path)
    try:
        yield logging.getLogger("weather_terminal")
    finally:
        handler.close()
