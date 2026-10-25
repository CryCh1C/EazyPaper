"""Portable path layout.

Every mutable artifact (config, caches, logs, OCR scratch data, model lookup)
lives below the application home, never in the user profile.  The OCR helper
runs as a separate Python process and inherits the same environment.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "EasyPaper"
APP_VERSION = "1.0.0"


def app_home() -> Path:
    """Return the portable application root.

    * ``EASYPAPER_HOME`` wins when present.
    * Frozen (PyInstaller): directory containing EasyPaper.exe.
    * Source checkout: project root (the parent of ``app``).
    """

    override = os.environ.get("EASYPAPER_HOME", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


HOME = app_home()
CONFIG_PATH = HOME / "config.json"
CACHE_DIR = HOME / "cache"
LOG_DIR = HOME / "logs"
EXPORT_DIR = HOME / "exports"
IMPORT_DIR = HOME / "imports"
TMP_DIR = HOME / ".tmp"
MODEL_DIR = HOME / "paddle_ocr"
RUNTIME_DIR = HOME / ".easypaper-runtime"
OCR_RUNTIME_DIR = HOME / "ocr-runtime"


def ensure_directories() -> None:
    for path in (HOME, CACHE_DIR, LOG_DIR, EXPORT_DIR, IMPORT_DIR, TMP_DIR):
        path.mkdir(parents=True, exist_ok=True)


def configure_process_environment() -> None:
    """Pin third-party caches to the project directory (idempotent)."""

    ensure_directories()
    local_env = {
        "HF_HOME": HOME / ".hf-cache",
        "HUGGINGFACE_HUB_CACHE": HOME / ".hf-cache" / "hub",
        "TRANSFORMERS_CACHE": HOME / ".hf-cache" / "transformers",
        "HF_HUB_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1",
        "TORCH_HOME": HOME / ".torch-cache",
        "TORCHINDUCTOR_CACHE_DIR": HOME / ".torch-cache" / "inductor",
        "TRITON_CACHE_DIR": HOME / ".torch-cache" / "triton",
        "PADDLE_HOME": HOME / ".paddle-cache",
        "PADDLEOCR_HOME": HOME / ".paddle-cache",
        "MPLCONFIGDIR": HOME / ".mpl-cache",
        "PYTHONPYCACHEPREFIX": HOME / ".pycache",
        "TMP": TMP_DIR,
        "TEMP": TMP_DIR,
        "TMPDIR": TMP_DIR,
        "TOKENIZERS_PARALLELISM": "false",
        "HF_HUB_DISABLE_SYMLINKS_WARNING": "1",
    }
    for key, value in local_env.items():
        os.environ[key] = str(value)
        if key in {"HF_HOME", "TORCH_HOME", "PADDLE_HOME", "MPLCONFIGDIR"}:
            Path(value).mkdir(parents=True, exist_ok=True)

    # QtWebEngine runs Chromium; keep its profile/cache local and use software
    # compositing so it does not compete with the OCR process for the GPU.
    web_dir = HOME / ".webengine"
    (web_dir / "cache").mkdir(parents=True, exist_ok=True)
    flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
    for flag in (
        "--enable-gpu-rasterization",
        "--ignore-gpu-blocklist",
        f'--user-data-dir="{web_dir}"',
        f'--disk-cache-dir="{web_dir / "cache"}"',
    ):
        if flag.split("=", 1)[0] not in flags:
            flags += " " + flag
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = flags.strip()

    # Never let Python write __pycache__ into the installation tree.
    sys.pycache_prefix = str(HOME / ".pycache")


def model_candidates() -> list[Path]:
    """Likely locations for the local PaddleOCR-VL checkpoint."""

    candidates = [
        MODEL_DIR,
        HOME / "models" / "paddle_ocr",
        HOME / "paddleocr",
        Path.cwd() / "paddle_ocr",
    ]
    unique: list[Path] = []
    for item in candidates:
        resolved = item.expanduser().resolve()
        if resolved not in unique:
            unique.append(resolved)
    return unique


def default_model_dir() -> str:
    for candidate in model_candidates():
        if (candidate / "config.json").is_file():
            try:
                return str(candidate.relative_to(HOME))
            except ValueError:
                return str(candidate)
    return "paddle_ocr"


def find_ocr_python(explicit: str = "") -> Path | None:
    """Find the Python that has torch/transformers installed."""

    candidates: list[Path] = []
    if explicit.strip():
        path = Path(explicit.strip()).expanduser()
        candidates.append(path if path.is_absolute() else HOME / path)
    env_python = os.environ.get("EASYPAPER_OCR_PYTHON", "").strip()
    if env_python:
        path = Path(env_python).expanduser()
        candidates.append(path if path.is_absolute() else HOME / path)
    candidates.extend(
        [
            HOME / ".venv" / "Scripts" / "python.exe",
            HOME / "ocr-runtime" / "python.exe",
            OCR_RUNTIME_DIR / "Scripts" / "python.exe",
        ]
    )
    if not getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable))
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate.resolve()
        except OSError:
            continue
    return None


def resource_path(name: str) -> Path:
    """Locate a bundled UI resource in source and frozen builds."""

    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidate = Path(meipass) / "app" / "resources" / name
            if candidate.exists():
                return candidate
    return Path(__file__).resolve().parent / "resources" / name


def ocr_worker_script() -> Path:
    """The sidecar worker is shipped as data in the frozen application."""

    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            bundled = Path(meipass) / "tools" / "ocr_worker.py"
            if bundled.is_file():
                return bundled
    return HOME / "tools" / "ocr_worker.py"
