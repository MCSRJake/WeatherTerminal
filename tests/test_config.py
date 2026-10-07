from pathlib import Path
import tomllib

import pytest

from weather_terminal.config import (
    AppConfig, ConfigError, DEFAULT_CONFIG, RuntimePaths, config_from_mapping, load_config,
)


def test_default_template_matches_defaults():
    assert config_from_mapping(tomllib.loads(DEFAULT_CONFIG)) == AppConfig()


def test_missing_file_uses_defaults_without_writing(tmp_path):
    path = tmp_path / "config.toml"
    assert load_config(path) == AppConfig()
    assert not path.exists()


def test_create_preserves_existing_user_settings(tmp_path):
    path = tmp_path / "config.toml"
    assert load_config(path, create=True) == AppConfig()
    original = '[http]\ncontact = "operator@example.invalid"\n'
    path.write_text(original, encoding="utf-8")
    assert load_config(path, create=True).http.contact == "operator@example.invalid"
    assert path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("values", [
    {"refresh": {"alerts_seconds": 29}},
    {"refresh": {"conditions_seconds": 0}},
    {"refresh": {"metadata_seconds": 100}},
    {"refresh": {"daily_seconds": float("inf")}},
    {"freshness": {"alerts_check_seconds": float("nan")}},
    {"freshness": {"observation_seconds": True}},
    {"freshness": {"forecast_check_seconds": "2700"}},
    {"http": {"connect_timeout_seconds": -1}},
    {"http": {"operation_timeout_seconds": 2}},
    {"http": {"user_agent": ""}},
    {"http": {"contact": "x\r\nInjected: header"}},
    {"http": {"contact": "x\u202e"}},
    {"geocoding": {"min_interval_seconds": 0.99}},
    {"geocoding": {"endpoint": "http://example.invalid/search"}},
    {"geocoding": {"endpoint": "https://user:secret@example.invalid/search"}},
    {"geocoding": {"endpoint": "https://example.invalid/search?x=1"}},
    {"geocoding": {"endpoint": "https://example.invalid/search#fragment"}},
    {"geocoding": {"endpoint": "https://example.invalid:bad/search"}},
    {"geocoding": {"endpoint": "https://example.invalid/ search"}},
    {"geocoding": {"endpoint": 123}},
    {"refresh": {"unknown": 1}},
    {"unknown": {}},
    {"refresh": "not a table"},
])
def test_invalid_settings_are_rejected(values):
    with pytest.raises(ConfigError):
        config_from_mapping(values)


def test_provider_minimum_boundaries():
    config = config_from_mapping({
        "refresh": {"alerts_seconds": 30},
        "geocoding": {"min_interval_seconds": 1},
    })
    assert config.refresh.alerts_seconds == 30
    assert config.geocoding.min_interval_seconds == 1


def test_custom_geocoder_and_real_contact():
    config = config_from_mapping({
        "geocoding": {"endpoint": "https://example.invalid:8443/nominatim/search"},
        "http": {"contact": "operator@example.invalid"},
    })
    assert "contact: operator@example.invalid" in config.http.identifying_user_agent


def test_invalid_toml_does_not_echo_secret(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[http]\ncontact = "private@example.invalid', encoding="utf-8")
    with pytest.raises(ConfigError) as raised:
        load_config(path)
    assert "private@example.invalid" not in str(raised.value)


def test_invalid_encoding_is_actionable(tmp_path):
    path = tmp_path / "config.toml"
    path.write_bytes(b"\xff\xfe")
    with pytest.raises(ConfigError, match="UTF-8"):
        load_config(path)


def test_override_paths_are_absolute_and_cwd_independent(tmp_path, monkeypatch):
    root = tmp_path / "local"
    paths = RuntimePaths.discover(override=root)
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    assert paths.config_file == root / "config.toml"
    assert paths.log_file == root / "logs" / "weather-terminal.log"
    paths.ensure_directories()
    assert paths.cache_dir.is_dir()
    assert paths.log_file.parent.is_dir()


def test_windows_missing_localappdata_is_actionable():
    with pytest.raises(ConfigError, match="LOCALAPPDATA"):
        RuntimePaths.discover(environ={}, platform="win32")


def test_localappdata_paths(tmp_path):
    paths = RuntimePaths.discover(environ={"LOCALAPPDATA": str(tmp_path)}, platform="win32")
    assert paths.root == tmp_path / "WeatherTerminal"


def test_non_windows_development_paths(tmp_path):
    paths = RuntimePaths.discover(environ={"XDG_STATE_HOME": str(tmp_path)}, platform="linux")
    assert paths.root == tmp_path / "WeatherTerminal"


def test_relative_environment_path_rejected():
    with pytest.raises(ConfigError, match="absolute"):
        RuntimePaths.discover(environ={"LOCALAPPDATA": "relative-path"}, platform="win32")
