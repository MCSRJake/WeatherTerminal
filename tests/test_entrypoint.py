import logging
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import uuid

import pytest

import main
from weather_terminal.runtime import SingleInstance

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def isolated_guard(monkeypatch):
    monkeypatch.setattr(SingleInstance, "WINDOWS_NAME", rf"Local\WeatherTerminal.Entry.Tests.{uuid.uuid4().hex}")


def test_imports_do_not_start_services_or_create_runtime_files(tmp_path):
    script = '''
import logging, socket, sys
from pathlib import Path

def blocked(*args, **kwargs):
    raise AssertionError("Unexpected network access on import")
socket.socket.connect = blocked
socket.socket.connect_ex = blocked
sys.argv = ["main.py", "--this-must-not-be-parsed-during-import"]
import main
import weather_terminal.app
import weather_terminal.clock
import weather_terminal.config
import weather_terminal.models
import weather_terminal.storage
import weather_terminal.freshness
import weather_terminal.offline
import weather_terminal.hourly
import weather_terminal.units
import weather_terminal.formatting
import weather_terminal.conditions
import weather_terminal.daily
import weather_terminal.alerts
import weather_terminal.services.observations
import weather_terminal.slice
import weather_terminal.services.http
import weather_terminal.services.geocoding
import weather_terminal.services.nws
import weather_terminal.runtime
import weather_terminal.services.http
import weather_terminal.text
assert not logging.getLogger("weather_terminal").handlers
assert not list(Path(ROOT).iterdir())
'''
    # ROOT is explicitly supplied as a Python literal; no shell is involved.
    script = "ROOT = " + repr(str(tmp_path)) + "\n" + script
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=PROJECT_ROOT,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_old_python_is_rejected_before_runtime_io(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "version_info", (3, 11, 0))
    assert main.main(["--data-dir", str(tmp_path)]) == 2
    assert "Python 3.12" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())


def test_invalid_config_gives_message_and_releases_resources(tmp_path, capsys, isolated_guard):
    (tmp_path / "config.toml").write_text("[refresh]\nalerts_seconds = 2\n", encoding="utf-8")
    assert main.main(["--data-dir", str(tmp_path)]) == 2
    assert "Configuration error" in capsys.readouterr().err
    assert not logging.getLogger("weather_terminal").handlers
    with SingleInstance(tmp_path):
        pass


def test_valid_startup_lifecycle_with_fake_ui(tmp_path, monkeypatch, isolated_guard):
    calls = []

    class FakeApp:
        return_code = 0

        def __init__(self, *, config, paths):
            calls.append((config, paths))

        def run(self):
            calls.append("run")

    monkeypatch.setitem(sys.modules, "weather_terminal.app", SimpleNamespace(WeatherApp=FakeApp))
    assert main.main(["--data-dir", str(tmp_path)]) == 0
    assert calls[-1] == "run"
    assert calls[0][1].root == tmp_path.resolve()
    assert (tmp_path / "config.toml").is_file()
    assert (tmp_path / "cache").is_dir()
    assert (tmp_path / "logs" / "weather-terminal.log").is_file()
    assert not (tmp_path / "state.json").exists()  # No confirmed location yet.
    assert not logging.getLogger("weather_terminal").handlers
    with SingleInstance(tmp_path):
        pass


def test_second_instance_is_actionable(tmp_path, capsys, isolated_guard):
    with SingleInstance(tmp_path):
        assert main.main(["--data-dir", str(tmp_path)]) == 2
    assert "already running" in capsys.readouterr().err
    assert not (tmp_path / "config.toml").exists()


def test_unexpected_failure_is_logged_and_resources_released(tmp_path, monkeypatch, capsys, isolated_guard):
    class BrokenApp:
        def __init__(self, **kwargs):
            pass

        def run(self):
            raise RuntimeError("simulated-ui-failure")

    monkeypatch.setitem(sys.modules, "weather_terminal.app", SimpleNamespace(WeatherApp=BrokenApp))
    assert main.main(["--data-dir", str(tmp_path)]) == 1
    output = capsys.readouterr()
    assert "Unexpected application failure" in output.err
    assert "Traceback" not in output.err
    log = (tmp_path / "logs" / "weather-terminal.log").read_text(encoding="utf-8")
    assert "simulated-ui-failure" in log
    assert not logging.getLogger("weather_terminal").handlers
    with SingleInstance(tmp_path):
        pass


@pytest.mark.parametrize("failure_kind", ["config", "ui", "interrupt", "missing-package"])
def test_failed_start_then_successful_start_restores_logging(
    tmp_path, monkeypatch, capsys, isolated_guard, failure_kind,
):
    logger = logging.getLogger("weather_terminal")
    original_handlers = tuple(logger.handlers)
    original_settings = (logger.level, logger.propagate)
    foreign_handler = logging.NullHandler()
    logger.addHandler(foreign_handler)

    class HealthyApp:
        return_code = 0

        def __init__(self, **kwargs):
            pass

        def run(self):
            pass

    class FailingApp(HealthyApp):
        def run(self):
            if failure_kind == "interrupt":
                raise KeyboardInterrupt
            raise RuntimeError("simulated-ui-failure")

    config_path = tmp_path / "config.toml"
    try:
        if failure_kind == "config":
            config_path.write_text("[refresh]\nalerts_seconds = 2\n", encoding="utf-8")
            first_app = SimpleNamespace(WeatherApp=HealthyApp)
            expected_code = 2
        elif failure_kind == "missing-package":
            first_app = None  # An explicit None entry makes this import fail.
            expected_code = 2
        else:
            first_app = SimpleNamespace(WeatherApp=FailingApp)
            expected_code = 0 if failure_kind == "interrupt" else 1
        monkeypatch.setitem(sys.modules, "weather_terminal.app", first_app)
        assert main.main(["--data-dir", str(tmp_path)]) == expected_code
        assert tuple(logger.handlers) == (*original_handlers, foreign_handler)
        assert (logger.level, logger.propagate) == original_settings
        if failure_kind == "ui":
            assert "Unexpected application failure" in capsys.readouterr().err
        if failure_kind == "config":
            config_path.unlink()
        monkeypatch.setitem(
            sys.modules, "weather_terminal.app", SimpleNamespace(WeatherApp=HealthyApp),
        )
        assert main.main(["--data-dir", str(tmp_path)]) == 0
        assert tuple(logger.handlers) == (*original_handlers, foreign_handler)
        assert (logger.level, logger.propagate) == original_settings
        with SingleInstance(tmp_path):
            pass
    finally:
        logger.removeHandler(foreign_handler)
        foreign_handler.close()
