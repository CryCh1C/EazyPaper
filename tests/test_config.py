from __future__ import annotations

import json

from app.config import AppConfig


def test_roundtrip(tmp_path):
    path = tmp_path / "config.json"
    config = AppConfig()
    config.llm.base_url = "http://localhost:1234/v1"
    config.llm.api_key = "secret-key"
    config.llm.model = "local-model"
    config.translation.target_language = "日本語"
    config.ocr.mode = "always"
    config.save(path)
    loaded = AppConfig.load(path)
    assert loaded.llm.base_url == "http://localhost:1234/v1"
    assert loaded.llm.api_key == "secret-key"
    assert loaded.llm.model == "local-model"
    assert loaded.translation.target_language == "日本語"
    assert loaded.ocr.mode == "always"


def test_env_override_is_not_persisted(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setenv("EASYPAPER_API_KEY", "from-env")
    monkeypatch.setenv("EASYPAPER_MODEL", "env-model")
    config = AppConfig.load(path)
    assert config.llm.api_key == "from-env"
    assert config.llm.model == "env-model"
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["llm"]["api_key"] == ""
    assert raw["llm"]["model"] == "gpt-4o-mini"


def test_env_override_is_not_written_back_on_save(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    base = AppConfig()
    base.llm.base_url = "https://disk.example/v1"
    base.llm.api_key = "disk-key"
    base.llm.model = "disk-model"
    base.save(path)
    monkeypatch.setenv("EASYPAPER_BASE_URL", "https://env.example/v1")
    monkeypatch.setenv("EASYPAPER_API_KEY", "env-key")
    monkeypatch.setenv("EASYPAPER_MODEL", "env-model")
    loaded = AppConfig.load(path)
    assert loaded.llm.api_key == "env-key"
    loaded.save(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["llm"]["base_url"] == "https://disk.example/v1"
    assert raw["llm"]["api_key"] == "disk-key"
    assert raw["llm"]["model"] == "disk-model"


def test_invalid_values_are_clamped():
    config = AppConfig()
    config.translation.max_workers = 999
    config.llm.timeout = 1
    config.ocr.dpi = 2
    config.validate()
    assert config.translation.max_workers == 6
    assert config.llm.timeout == 10
    assert config.ocr.dpi == 96


def test_bad_json_is_backed_up(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")
    config = AppConfig.load(path)
    assert config.llm.model == "gpt-4o-mini"
    assert (tmp_path / "config.json.bad").is_file()
    assert path.is_file()
