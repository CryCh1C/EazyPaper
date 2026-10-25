from __future__ import annotations

import shutil
import zipfile

from app.config import AppConfig
from app.pdf import cache as cache_module
from app.pdf.cache import DocumentCache
from app.pdf.extractor import extract_document
from app.translation_package import export_translation_package, import_translation_package
from app import project_package
from app.pdf.extractor import hash_file


def test_translation_package_roundtrip(sample_pdf, tmp_path, monkeypatch):
    monkeypatch.setattr(cache_module, "CACHE_DIR", tmp_path / "cache")
    config = AppConfig()
    config.ocr.enabled = False
    cache = DocumentCache(sample_pdf, "package-hash", config)
    cache.ensure()
    document = extract_document(
        sample_pdf,
        config,
        known_hash="package-hash",
        asset_dir=cache.root / "assets",
    )
    cache.save_structure(document)
    for block in document.translatable_blocks():
        block.translation = f"译文：{block.text}"
    translations = {b.id: b.translation for b in document.translatable_blocks()}
    cache.save_translation(document, translations, complete=True)

    package = tmp_path / "paper.zip"
    exported = export_translation_package(
        document,
        config,
        cache,
        package,
        markdown="# 测试\n",
    )
    assert exported.translations > 0
    assert exported.assets >= 2
    with zipfile.ZipFile(package) as archive:
        names = set(archive.namelist())
        assert {"meta.json", "translations.json", "translation.md"} <= names
        assert any(name.endswith("_table1.png") for name in names)
        assert any(name.endswith("_formula1.png") for name in names)

    shutil.rmtree(cache.root / "assets")
    for block in document.blocks:
        block.translation = ""
        block.hidden = False
    imported = import_translation_package(package, document, cache, config)
    assert imported.source_match is True
    assert imported.matched == len(document.blocks)
    assert imported.complete is True
    assert imported.assets >= 2
    assert all(block.translation for block in document.translatable_blocks())
    assert imported.warnings == []
    assert any((cache.root / "assets").glob("*.png"))


def test_project_package_contains_pdf_chat_and_assets(sample_pdf, tmp_path, monkeypatch):
    monkeypatch.setattr(cache_module, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(project_package, "IMPORT_DIR", tmp_path / "imports")
    config = AppConfig()
    config.ocr.enabled = False
    digest = hash_file(sample_pdf)
    cache = DocumentCache(sample_pdf, digest, config)
    cache.ensure()
    document = extract_document(
        sample_pdf,
        config,
        known_hash=digest,
        asset_dir=cache.root / "assets",
    )
    cache.save_structure(document)
    for block in document.translatable_blocks():
        block.translation = f"译文：{block.text}"
    translations = {b.id: b.translation for b in document.translatable_blocks()}
    cache.save_translation(document, translations, complete=True)
    chat = [
        {"role": "user", "content": "这篇论文讲什么？"},
        {"role": "assistant", "content": "它介绍 EasyPaper 的本地阅读流程。"},
    ]
    package = tmp_path / "project.zip"
    result = project_package.export_project(
        document,
        config,
        cache,
        sample_pdf,
        chat,
        package,
        markdown="# 项目\n",
    )
    assert result.bytes > 0
    assert result.assets >= 2
    with zipfile.ZipFile(package) as archive:
        names = set(archive.namelist())
        assert {"source.pdf", "structure.json", "translations.json", "chat.json"} <= names
        assert any(name.endswith("_table1.png") for name in names)

    bundle = project_package.extract_project(package)
    assert bundle.pdf_path.is_file()
    assert bundle.structure.source_hash == digest
    assert bundle.chat == chat
    assert bundle.translations
    imported_cache = project_package.prepare_project_cache(bundle, config)
    assert imported_cache.root.is_dir()
    assert (imported_cache.root / "structure.json").is_file()
    signature = imported_cache.translation_signature(bundle.structure)
    assert imported_cache.translation_path(signature).is_file()
