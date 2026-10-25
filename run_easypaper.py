"""Frozen-application entry point.

PyInstaller executes its entry script without a package context, so relative
imports in app/main.py cannot be used directly.  This launcher imports the
real entry point with an absolute package import.
"""

from __future__ import annotations

import multiprocessing
import os
import sys
import traceback
from pathlib import Path

_STREAM_REFS = []


def _boot_log(message: str) -> None:
    try:
        home = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
        log_dir = home / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "windowed-boot.log", "a", encoding="utf-8") as handle:
            handle.write(message.rstrip() + "\n")
    except Exception:
        pass


def _prepare_windowed_environment() -> None:
    """PyInstaller windowed apps have no stdio; logging must not crash."""

    try:
        log_dir = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    # Windowed PyInstaller apps have no stdio.  Use os.devnull; the rotating
    # file logger remains the source of truth.
    if sys.stdout is None:
        try:
            _STREAM_REFS.append(open(os.devnull, "w", encoding="utf-8"))
            sys.stdout = _STREAM_REFS[-1]
        except OSError:
            pass
    if sys.stderr is None:
        try:
            _STREAM_REFS.append(open(os.devnull, "w", encoding="utf-8"))
            sys.stderr = _STREAM_REFS[-1]
        except OSError:
            pass


_prepare_windowed_environment()

try:
    from app.main import main  # noqa: E402
except Exception:
    _boot_log("import app.main failed:\n" + traceback.format_exc())
    raise

if __name__ == "__main__":
    multiprocessing.freeze_support()
    try:
        raise SystemExit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception:
        _boot_log("main failed:\n" + traceback.format_exc())
        raise
