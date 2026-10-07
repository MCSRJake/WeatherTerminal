import logging
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

from weather_terminal.runtime import (
    AlreadyRunning, SingleInstance, application_logging, configure_logging,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def guard_name(monkeypatch):
    # Do not collide with a running production preview or concurrent test process.
    name = rf"Local\WeatherTerminal.Tests.{uuid.uuid4().hex}"
    monkeypatch.setattr(SingleInstance, "WINDOWS_NAME", name)
    return name


def test_guard_rejects_second_instance_and_releases(tmp_path, guard_name):
    with SingleInstance(tmp_path):
        with pytest.raises(AlreadyRunning):
            SingleInstance(tmp_path).acquire()
    with SingleInstance(tmp_path):
        pass


def test_guard_releases_after_exception(tmp_path, guard_name):
    with pytest.raises(RuntimeError, match="simulated"):
        with SingleInstance(tmp_path):
            raise RuntimeError("simulated")
    with SingleInstance(tmp_path):
        pass


def test_guard_reentry_and_idempotent_release(tmp_path, guard_name):
    guard = SingleInstance(tmp_path)
    guard.acquire()
    try:
        with pytest.raises(RuntimeError, match="already been acquired"):
            guard.acquire()
    finally:
        guard.release()
        guard.release()


def test_other_process_cannot_acquire(tmp_path, guard_name):
    script = '''
import sys
from pathlib import Path
from weather_terminal.runtime import SingleInstance, AlreadyRunning
SingleInstance.WINDOWS_NAME = sys.argv[2]
try:
    with SingleInstance(Path(sys.argv[1])):
        pass
except AlreadyRunning:
    raise SystemExit(3)
raise SystemExit(0)
'''
    with SingleInstance(tmp_path):
        result = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path), guard_name],
            cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=15,
        )
    assert result.returncode == 3, result.stderr


def test_crashed_process_does_not_leave_stale_guard(tmp_path, guard_name):
    script = '''
import os, sys
from pathlib import Path
from weather_terminal.runtime import SingleInstance
SingleInstance.WINDOWS_NAME = sys.argv[2]
guard = SingleInstance(Path(sys.argv[1]))
guard.acquire()
os._exit(7)
'''
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), guard_name],
        cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 7, result.stderr
    with SingleInstance(tmp_path):
        pass


@pytest.mark.skipif(sys.platform != "win32", reason="Windows named object is session-wide")
def test_windows_guard_covers_different_data_dirs(tmp_path, guard_name):
    with SingleInstance(tmp_path / "first"):
        with pytest.raises(AlreadyRunning):
            SingleInstance(tmp_path / "second").acquire()


def test_logging_rotation_is_bounded_and_not_propagated(tmp_path):
    logger = logging.getLogger("weather_terminal")
    path = tmp_path / "logs" / "weather-terminal.log"
    previous_level, previous_propagate = logger.level, logger.propagate
    handler = configure_logging(path)
    try:
        assert handler.maxBytes == 1_048_576
        assert handler.backupCount == 3
        assert not logger.propagate
        handler.maxBytes = 200
        for index in range(20):
            logger.info("Test diagnostic %s %s", index, "x" * 100)
        handler.flush()
        assert path.exists()
        assert path.with_name(path.name + ".1").exists()
        assert path.with_name(path.name + ".3").exists()
        assert not path.with_name(path.name + ".4").exists()
        with pytest.raises(RuntimeError, match="already configured"):
            configure_logging(path)
    finally:
        handler.close()
    assert handler not in logger.handlers
    assert logger.level == previous_level
    assert logger.propagate is previous_propagate


def test_logging_preserves_foreign_handlers_and_restores_settings(tmp_path):
    logger = logging.getLogger("weather_terminal")
    original_handlers = tuple(logger.handlers)
    original_level, original_propagate = logger.level, logger.propagate
    foreign_handler = logging.NullHandler()
    logger.addHandler(foreign_handler)
    logger.setLevel(logging.WARNING)
    logger.propagate = True
    try:
        with application_logging(tmp_path / "logs" / "first.log") as active:
            assert active is logger
            assert foreign_handler in logger.handlers
            assert logger.level == logging.INFO
            assert logger.propagate is False
            with pytest.raises(RuntimeError, match="already configured"):
                configure_logging(tmp_path / "logs" / "second.log")
        assert tuple(logger.handlers) == (*original_handlers, foreign_handler)
        assert logger.level == logging.WARNING
        assert logger.propagate is True
    finally:
        logger.removeHandler(foreign_handler)
        foreign_handler.close()
        logger.setLevel(original_level)
        logger.propagate = original_propagate


def test_logging_exception_cleanup_allows_another_session(tmp_path):
    logger = logging.getLogger("weather_terminal")
    original_handlers = tuple(logger.handlers)
    original_level, original_propagate = logger.level, logger.propagate
    with pytest.raises(RuntimeError, match="simulated"):
        with application_logging(tmp_path / "first.log"):
            raise RuntimeError("simulated")
    assert tuple(logger.handlers) == original_handlers
    assert (logger.level, logger.propagate) == (original_level, original_propagate)
    with application_logging(tmp_path / "second.log") as active:
        active.info("second session started")
    assert "second session started" in (tmp_path / "second.log").read_text(encoding="utf-8")
    assert tuple(logger.handlers) == original_handlers


def test_logging_failed_file_open_does_not_change_logger(tmp_path):
    logger = logging.getLogger("weather_terminal")
    original_handlers = tuple(logger.handlers)
    original_level, original_propagate = logger.level, logger.propagate
    directory = tmp_path / "not-a-file"
    directory.mkdir()
    with pytest.raises(OSError):
        configure_logging(directory)
    assert tuple(logger.handlers) == original_handlers
    assert (logger.level, logger.propagate) == (original_level, original_propagate)
    with application_logging(tmp_path / "valid.log"):
        pass


def test_closing_old_handler_does_not_reset_later_session(tmp_path):
    logger = logging.getLogger("weather_terminal")
    original_handlers = tuple(logger.handlers)
    first = configure_logging(tmp_path / "first.log")
    first.close()
    second = configure_logging(tmp_path / "second.log")
    try:
        first.close()
        assert second in logger.handlers
        assert logger.level == logging.INFO
        assert not logger.propagate
    finally:
        second.close()
    assert tuple(logger.handlers) == original_handlers
