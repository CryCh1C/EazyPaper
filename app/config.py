"""JSON configuration stored next to the executable."""

from __future__ import annotations

import copy
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .paths import CONFIG_PATH, default_model_dir

CONFIG_VERSION = 1


REASONING_OPTIONS = [
    ("自动（推荐）", "auto"),
    ("关闭思考", "off"),
    ("最低", "minimal"),
    ("低", "low"),
    ("中", "medium"),
    ("高", "high"),
]
REASONING_LABELS = {value: label for label, value in REASONING_OPTIONS}


@dataclass
class LLMConfig:
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o-mini"
    timeout: int = 120
    temperature: float = 0.2
    max_tokens: int = 5000
    use_json_mode: bool = True


@dataclass
class TranslationConfig:
    target_language: str = "简体中文"
    chunk_chars: int = 3600
    max_workers: int = 3
    skip_references: bool = True
    review_pass: bool = True
    reasoning_effort: str = "auto"  # auto | off | minimal | low | medium | high
    prompt_version: str = "translate-v3"


@dataclass
class ChatConfig:
    context_chars: int = 90000
    history_turns: int = 4
    temperature: float = 0.3
    max_tokens: int = 3500
    reasoning_effort: str = "auto"  # auto | off | minimal | low | medium | high


@dataclass
class OcrConfig:
    enabled: bool = True
    mode: str = "auto"  # auto | always | off
    model_dir: str = field(default_factory=default_model_dir)
    python_path: str = ""
    device: str = "auto"  # auto | cpu | cuda
    dpi: int = 160
    max_pixels: int = 800000
    max_new_tokens: int = 1536
    batch_size: int = 2
    prewarm: bool = True
    timeout_seconds: int = 3600
    force_refresh: bool = False


@dataclass
class UIConfig:
    left_zoom: float = 1.0
    middle_zoom: float = 1.0
    right_zoom: float = 1.0
    window_width: int = 1600
    window_height: int = 980
    appearance: str = "system"  # system | light | dark
    color_theme: str = "teal"  # teal | blue | red | violet | amber
    splitter_sizes: list[int] = field(default_factory=lambda: [590, 600, 410])


@dataclass
class AppConfig:
    version: int = CONFIG_VERSION
    llm: LLMConfig = field(default_factory=LLMConfig)
    translation: TranslationConfig = field(default_factory=TranslationConfig)
    chat: ChatConfig = field(default_factory=ChatConfig)
    ocr: OcrConfig = field(default_factory=OcrConfig)
    ui: UIConfig = field(default_factory=UIConfig)

    def __post_init__(self) -> None:
        # (section, field) pairs populated from environment variables.  These
        # are never written back to the portable JSON file.
        object.__setattr__(self, "_env_overrides", set())

    # ------------------------------------------------------------------ I/O
    @classmethod
    def load(cls, path: Path | None = None) -> "AppConfig":
        target = Path(path or CONFIG_PATH)
        defaults = cls()
        if not target.is_file():
            # Persist clean defaults; environment overrides stay in memory only
            # so a CI/API key never leaks into a portable config file.
            defaults.save(target)
            defaults.apply_env()
            return defaults
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("config root must be an object")
        except Exception:
            # Keep the unreadable file for inspection, then fall back to defaults.
            try:
                target.replace(target.with_suffix(".json.bad"))
            except OSError:
                pass
            defaults.save(target)
            defaults.apply_env()
            return defaults

        config = cls()
        config._merge(raw)
        config.validate()
        config.apply_env()
        return config

    def _merge(self, raw: dict[str, Any]) -> None:
        section_types = {
            "llm": LLMConfig,
            "translation": TranslationConfig,
            "chat": ChatConfig,
            "ocr": OcrConfig,
            "ui": UIConfig,
        }
        for name, klass in section_types.items():
            value = raw.get(name)
            if not isinstance(value, dict):
                continue
            current = getattr(self, name)
            valid = {f for f in klass.__dataclass_fields__}
            for key, item in value.items():
                if key in valid:
                    current_key = getattr(current, key)
                    try:
                        if isinstance(current_key, bool):
                            setattr(current, key, bool(item))
                        elif isinstance(current_key, int) and not isinstance(current_key, bool):
                            setattr(current, key, int(item))
                        elif isinstance(current_key, float):
                            setattr(current, key, float(item))
                        elif isinstance(current_key, list):
                            setattr(current, key, list(item))
                        else:
                            setattr(current, key, str(item))
                    except (TypeError, ValueError):
                        pass

    def validate(self) -> None:
        self.llm.base_url = self.llm.base_url.strip() or "https://api.openai.com/v1"
        self.llm.model = self.llm.model.strip()
        self.llm.timeout = max(10, min(self.llm.timeout, 600))
        self.llm.max_tokens = max(256, min(self.llm.max_tokens, 32000))
        self.llm.temperature = max(0.0, min(self.llm.temperature, 2.0))

        self.translation.chunk_chars = max(800, min(self.translation.chunk_chars, 12000))
        self.translation.max_workers = max(1, min(self.translation.max_workers, 6))
        if self.translation.reasoning_effort not in {
            "auto", "off", "default", "minimal", "low", "medium", "high"
        }:
            self.translation.reasoning_effort = "auto"
        self.chat.context_chars = max(8000, min(self.chat.context_chars, 400000))
        self.chat.history_turns = max(0, min(self.chat.history_turns, 20))
        if self.chat.reasoning_effort not in {
            "auto", "off", "default", "minimal", "low", "medium", "high"
        }:
            self.chat.reasoning_effort = "auto"

        self.ocr.mode = self.ocr.mode if self.ocr.mode in {"auto", "always", "off"} else "auto"
        self.ocr.device = self.ocr.device if self.ocr.device in {"auto", "cpu", "cuda"} else "auto"
        self.ocr.dpi = max(96, min(self.ocr.dpi, 300))
        self.ocr.max_pixels = max(200000, min(self.ocr.max_pixels, 4000000))
        self.ocr.max_new_tokens = max(128, min(self.ocr.max_new_tokens, 8192))
        self.ocr.batch_size = max(1, min(self.ocr.batch_size, 4))
        self.ocr.timeout_seconds = max(60, min(self.ocr.timeout_seconds, 14400))
        if not self.ocr.model_dir.strip():
            self.ocr.model_dir = default_model_dir()

        for name in ("left_zoom", "middle_zoom", "right_zoom"):
            value = float(getattr(self.ui, name))
            setattr(self.ui, name, max(0.5, min(value, 3.5)))
        self.ui.window_width = max(1000, min(self.ui.window_width, 5000))
        self.ui.window_height = max(700, min(self.ui.window_height, 3500))
        if self.ui.appearance not in {"system", "light", "dark"}:
            self.ui.appearance = "system"
        if self.ui.color_theme not in {"teal", "blue", "red", "violet", "amber"}:
            self.ui.color_theme = "teal"
        if len(self.ui.splitter_sizes) != 3 or sum(self.ui.splitter_sizes) <= 0:
            self.ui.splitter_sizes = [590, 600, 410]

    def apply_env(self) -> None:
        """Environment overrides are handy for CI and packaging tests."""

        overrides = getattr(self, "_env_overrides", set())
        if os.environ.get("EASYPAPER_BASE_URL"):
            self.llm.base_url = os.environ["EASYPAPER_BASE_URL"]
            overrides.add(("llm", "base_url"))
        if os.environ.get("EASYPAPER_API_KEY"):
            self.llm.api_key = os.environ["EASYPAPER_API_KEY"]
            overrides.add(("llm", "api_key"))
        if os.environ.get("EASYPAPER_MODEL"):
            self.llm.model = os.environ["EASYPAPER_MODEL"]
            overrides.add(("llm", "model"))
        if os.environ.get("EASYPAPER_OCR_MODEL"):
            self.ocr.model_dir = os.environ["EASYPAPER_OCR_MODEL"]
            overrides.add(("ocr", "model_dir"))
        object.__setattr__(self, "_env_overrides", overrides)

    def save(self, path: Path | None = None) -> None:
        target = Path(path or CONFIG_PATH)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(self)
        payload["version"] = CONFIG_VERSION
        overrides = getattr(self, "_env_overrides", set())
        if overrides and target.is_file():
            try:
                existing = json.loads(target.read_text(encoding="utf-8"))
            except Exception:
                existing = {}
            for section, key in overrides:
                if isinstance(existing.get(section), dict) and key in existing[section]:
                    payload.setdefault(section, {})[key] = existing[section][key]
        fd, tmp_name = tempfile.mkstemp(prefix="config-", suffix=".tmp", dir=str(target.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, target)
        finally:
            if os.path.exists(tmp_name):
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass

    def copy(self) -> "AppConfig":
        duplicate = AppConfig.load_from_dict(copy.deepcopy(asdict(self)))
        object.__setattr__(duplicate, "_env_overrides", set(getattr(self, "_env_overrides", set())))
        return duplicate

    @classmethod
    def load_from_dict(cls, raw: dict[str, Any]) -> "AppConfig":
        config = cls()
        config._merge(raw)
        config.validate()
        return config

    @property
    def api_ready(self) -> bool:
        return bool(self.llm.base_url.strip() and self.llm.api_key.strip() and self.llm.model.strip())


def redact_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"
