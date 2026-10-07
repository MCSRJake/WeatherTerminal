"""Launch Weather Terminal; importing this module performs no I/O."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Weather Terminal dashboard (NWS observations, forecasts, and alerts)")
    parser.add_argument(
        "--data-dir", type=Path, help="Override the local runtime directory, e.g. for testing"
    )
    args = parser.parse_args(argv)
    if sys.version_info < (3, 12):
        print("Weather Terminal requires Python 3.12 or later. Use your Python 3.12 venv.", file=sys.stderr)
        return 2

    from weather_terminal.config import ConfigError, RuntimePaths, load_config
    from weather_terminal.runtime import AlreadyRunning, SingleInstance, application_logging

    try:
        paths = RuntimePaths.discover(override=args.data_dir)
        paths.root.mkdir(parents=True, exist_ok=True)
        with SingleInstance(paths.root):
            paths.ensure_directories()
            # This context owns the file handler and temporary logger settings.
            # It exits before the instance guard, on every return/exception path.
            with application_logging(paths.log_file) as logger:
                try:
                    config = load_config(paths.config_file, create=True)
                    try:
                        from weather_terminal.app import WeatherApp
                    except ModuleNotFoundError:
                        print(
                            "A required package is missing. Run your venv's Python with "
                            "'-m pip install -r requirements.txt' from the project directory.",
                            file=sys.stderr,
                        )
                        return 2
                    app = WeatherApp(config=config, paths=paths)
                    logger.info("Starting cached-source preview (no network services)")
                    app.run()
                    return app.return_code or 0
                except (ConfigError, OSError):
                    raise
                except KeyboardInterrupt:
                    return 0
                except Exception:
                    # Record unexpected errors before closing logs/releasing the guard.
                    logger.exception("Unexpected application failure")
                    print(
                        "Unexpected application failure. Review the local diagnostic log before sharing it.",
                        file=sys.stderr,
                    )
                    return 1
    except AlreadyRunning:
        print("Weather Terminal is already running. Close the other copy before starting again.", file=sys.stderr)
        return 2
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print(
            "Unable to initialize local files or the instance guard. Check permissions for "
            "%LOCALAPPDATA%\\WeatherTerminal, or supply a writable --data-dir.", file=sys.stderr,
        )
        return 2
    except KeyboardInterrupt:
        return 0
    except Exception:
        # Startup failed before a usable logger existed; never emit a raw traceback.
        print("Unexpected startup failure. Check your environment and installed requirements.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
