"""Validated settings and runtime paths. No filesystem work on import."""
from __future__ import annotations

from dataclasses import dataclass, field, fields
import math
import os
from pathlib import Path
import sys
import tomllib
from typing import Any, Mapping
from urllib.parse import urlsplit


class ConfigError(ValueError):
    """Invalid local settings; safe to explain without printing setting values."""


def _seconds(name: str, value: Any, minimum: float, maximum: float = 604800) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} must be a number of seconds")
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum:g} and {maximum:g} seconds")


def _header_text(name: str, value: Any, *, optional: bool = False) -> None:
    if not isinstance(value, str) or len(value) > 200:
        raise ConfigError(f"{name} must be a string no longer than 200 characters")
    if (not optional and not value.strip()) or any(not 32 <= ord(c) <= 126 for c in value):
        raise ConfigError(f"{name} must contain printable ASCII text only")


@dataclass(frozen=True)
class RefreshSettings:
    alerts_seconds: float = 60
    conditions_seconds: float = 300
    hourly_seconds: float = 900
    daily_seconds: float = 900
    metadata_seconds: float = 86400

    def __post_init__(self) -> None:
        # Alerts: documented provider limit. Other minima: conservative app guardrails.
        for name, minimum in (
            ("alerts_seconds", 30), ("conditions_seconds", 60),
            ("hourly_seconds", 60), ("daily_seconds", 60), ("metadata_seconds", 3600),
        ):
            _seconds(f"refresh.{name}", getattr(self, name), minimum)


@dataclass(frozen=True)
class FreshnessSettings:
    observation_seconds: float = 5400
    conditions_check_seconds: float = 900
    alerts_check_seconds: float = 180
    forecast_check_seconds: float = 2700

    def __post_init__(self) -> None:
        for item in fields(self):
            _seconds(f"freshness.{item.name}", getattr(self, item.name), 1)


@dataclass(frozen=True)
class HttpSettings:
    connect_timeout_seconds: float = 5
    read_timeout_seconds: float = 15
    operation_timeout_seconds: float = 60
    user_agent: str = "WeatherTerminal/0.1 (personal terminal weather dashboard)"
    contact: str = ""

    def __post_init__(self) -> None:
        _seconds("http.connect_timeout_seconds", self.connect_timeout_seconds, 1, 120)
        _seconds("http.read_timeout_seconds", self.read_timeout_seconds, 1, 120)
        _seconds("http.operation_timeout_seconds", self.operation_timeout_seconds, 1, 300)
        if self.operation_timeout_seconds < max(self.connect_timeout_seconds, self.read_timeout_seconds):
            raise ConfigError("http.operation_timeout_seconds must cover each configured timeout")
        _header_text("http.user_agent", self.user_agent)
        _header_text("http.contact", self.contact, optional=True)

    @property
    def identifying_user_agent(self) -> str:
        return self.user_agent + (f"; contact: {self.contact.strip()}" if self.contact.strip() else "")


@dataclass(frozen=True)
class GeocodingSettings:
    endpoint: str = "https://nominatim.openstreetmap.org/search"
    min_interval_seconds: float = 1

    def __post_init__(self) -> None:
        _seconds("geocoding.min_interval_seconds", self.min_interval_seconds, 1, 60)
        if not isinstance(self.endpoint, str) or any(ord(c) <= 32 or ord(c) == 127 for c in self.endpoint):
            raise ConfigError("geocoding.endpoint must be an HTTPS URL without whitespace")
        try:
            parsed = urlsplit(self.endpoint)
            port = parsed.port
        except ValueError as exc:
            raise ConfigError("geocoding.endpoint is not a valid URL") from exc
        if (
            parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or (port is not None and not 1 <= port <= 65535)
        ):
            raise ConfigError("geocoding.endpoint must be HTTPS without credentials, query, or fragment")


@dataclass(frozen=True)
class AppConfig:
    refresh: RefreshSettings = field(default_factory=RefreshSettings)
    freshness: FreshnessSettings = field(default_factory=FreshnessSettings)
    http: HttpSettings = field(default_factory=HttpSettings)
    geocoding: GeocodingSettings = field(default_factory=GeocodingSettings)


def config_from_mapping(values: Mapping[str, Any]) -> AppConfig:
    sections = {
        "refresh": RefreshSettings, "freshness": FreshnessSettings,
        "http": HttpSettings, "geocoding": GeocodingSettings,
    }
    unknown = set(values) - set(sections)
    if unknown:
        raise ConfigError("Unknown configuration section(s): " + ", ".join(sorted(unknown)))
    built: dict[str, Any] = {}
    for name, cls in sections.items():
        section = values.get(name, {})
        if not isinstance(section, dict):
            raise ConfigError(f"{name} must be a TOML table")
        allowed = {item.name for item in fields(cls)}
        unknown_keys = set(section) - allowed
        if unknown_keys:
            raise ConfigError(f"Unknown key(s) in {name}: " + ", ".join(sorted(unknown_keys)))
        built[name] = cls(**section)
    return AppConfig(**built)


DEFAULT_CONFIG = '''# Weather Terminal configuration. Times are in seconds.
# Changes take effect on the next launch. All four weather components are retrieved.

[refresh]
alerts_seconds = 60
conditions_seconds = 300
hourly_seconds = 900
daily_seconds = 900
metadata_seconds = 86400

[freshness]
observation_seconds = 5400
conditions_check_seconds = 900
alerts_check_seconds = 180
forecast_check_seconds = 2700

[http]
connect_timeout_seconds = 5
read_timeout_seconds = 15
operation_timeout_seconds = 60
user_agent = "WeatherTerminal/0.1 (personal terminal weather dashboard)"
# Optional real operator contact; do not insert private details into shared examples.
contact = ""

[geocoding]
endpoint = "https://nominatim.openstreetmap.org/search"
min_interval_seconds = 1
'''


def load_config(path: Path, *, create: bool = False) -> AppConfig:
    if create:
        try:
            # Exclusive creation: never overwrite the user's existing configuration.
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(DEFAULT_CONFIG)
        except FileExistsError:
            pass
    try:
        with path.open("rb") as stream:
            values = tomllib.load(stream)
    except FileNotFoundError:
        if not create:
            return AppConfig()
        raise
    except tomllib.TOMLDecodeError as exc:
        # Do not echo lines that might contain private contact configuration.
        raise ConfigError(f"Invalid TOML in {path}. Correct it or back it up and recreate it.") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"Configuration {path} must use UTF-8 encoding") from exc
    return config_from_mapping(values)


@dataclass(frozen=True)
class RuntimePaths:
    root: Path

    @classmethod
    def discover(
        cls, *, override: Path | None = None, environ: Mapping[str, str] | None = None,
        platform: str | None = None,
    ) -> RuntimePaths:
        env = os.environ if environ is None else environ
        platform = sys.platform if platform is None else platform
        if override is not None:
            return cls(override.expanduser().resolve())
        if env.get("LOCALAPPDATA"):
            base = Path(env["LOCALAPPDATA"])
        elif platform == "win32":
            raise ConfigError("LOCALAPPDATA is not set. Supply a writable absolute --data-dir.")
        else:
            # Test/development fallback; Windows remains the supported deployment.
            base = Path(env.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
        if not base.is_absolute():
            raise ConfigError("The runtime-directory base must be an absolute path")
        return cls(base / "WeatherTerminal")

    @property
    def config_file(self) -> Path:
        return self.root / "config.toml"

    @property
    def state_file(self) -> Path:
        return self.root / "state.json"

    @property
    def cache_dir(self) -> Path:
        return self.root / "cache"

    @property
    def log_file(self) -> Path:
        return self.root / "logs" / "weather-terminal.log"

    def ensure_directories(self) -> None:
        for directory in (self.root, self.cache_dir, self.log_file.parent):
            directory.mkdir(parents=True, exist_ok=True)
