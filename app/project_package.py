"""Portable EasyPaper project archives: PDF + structure + translations + chat."""

from __future__ import annotations

import hashlib
import json
import os
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .config import AppConfig
from .logging_setup import get_logger
from .models import DocumentStructure
from .paths import APP_VERSION, IMPORT_DIR
from .pdf.cache import DocumentCache

log = get_logger("project_package")

PROJECT_SCHEMA = "easypaper.project/1"
MAX_BYTES = 500 * 1024 * 1024
MAX_MEMBER = 200 * 1024 * 1024


class ProjectError(RuntimeError):
    pass


@dataclass
class ProjectExportResult:
    path: Path
    bytes: int
    blocks: int
    translations: int
    assets: int


@dataclass
class ProjectBundle:
    pdf_path: Path
    structure: DocumentStructure
    translations: dict[str, str]
    chat: list[dict[str, Any]]
    complete: bool
    assets_dir: Path
    source_hash: str
    warnings: list[str] = field(default_factory=list)


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _sha_text(text: str) -> str:
    return _sha_bytes(text.encode("utf-8", "ignore"))


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _safe_name(name: str) -> PurePosixPath | None:
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts:
        return None
    if any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path


def _clean_chat(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    clean: list[dict[str, Any]] = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role", ""))
        content = str(item.get("content", ""))
        if role not in {"user", "assistant"} or not content.strip():
            continue
        row: dict[str, Any] = {"role": role, "content": content}
        if item.get("quote"):
            row["quote"] = str(item.get("quote"))
        if item.get("quote_page"):
            try:
                row["quote_page"] = int(item.get("quote_page"))
            except (TypeError, ValueError):
                pass
        if item.get("meta"):
            row["meta"] = str(item.get("meta"))
        clean.append(row)
    return clean


def export_project(
    document: DocumentStructure,
    config: AppConfig,
    cache: DocumentCache,
    pdf_path: Path,
    chat_messages: list[dict[str, Any]],
    target: Path,
    markdown: str = "",
) -> ProjectExportResult:
    """Write a self-contained project. The API key is never included."""

    pdf_path = Path(pdf_path)
    if not pdf_path.is_file():
        raise ProjectError("源 PDF 不存在，无法导出项目")
    actual_hash = _sha_file(pdf_path)
    if actual_hash != document.source_hash:
        raise ProjectError("PDF 指纹与当前结构缓存不一致，请先重新解析")
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    meta_info = cache.translation_meta(document) or {}
    complete = bool(meta_info.get("complete"))
    blocks: list[dict[str, Any]] = []
    assets: set[str] = set()
    for block in document.blocks:
        blocks.append(
            {
                "id": block.id,
                "page": block.page,
                "order": block.order,
                "kind": block.kind,
                "source_sha256": _sha_text(block.text),
                "source_text": block.text,
                "translation": block.translation,
                "hidden": bool(block.hidden),
            }
        )
        asset = _safe_name(str(block.meta.get("asset") or ""))
        if asset is not None and (cache.root / asset).is_file():
            assets.add(asset.as_posix())

    meta = {
        "schema": PROJECT_SCHEMA,
        "app_version": APP_VERSION,
        "created_at": _now(),
        "extractor_version": document.extractor_version,
        "source": {
            "file_name": document.file_name,
            "source_hash": document.source_hash,
            "page_count": document.page_count,
            "title": document.title,
            "authors": document.authors,
        },
        "target_language": config.translation.target_language,
        "model": config.llm.model,
        "base_url": config.llm.base_url.rstrip("/"),
        "prompt_version": config.translation.prompt_version,
        "complete": complete,
        "block_count": len(blocks),
        "translated_count": sum(1 for block in document.blocks if block.translation.strip()),
        "assets": sorted(assets),
        "review_summary": document.review_summary or {},
    }
    payload = {"schema": PROJECT_SCHEMA, "blocks": blocks}
    temp = target.with_name(target.name + ".tmp")
    try:
        with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "meta.json",
                json.dumps(meta, ensure_ascii=False, indent=1).encode("utf-8"),
            )
            archive.writestr(
                "structure.json",
                json.dumps(document.to_dict(), ensure_ascii=False, indent=1).encode("utf-8"),
            )
            archive.writestr(
                "translations.json",
                json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8"),
            )
            archive.writestr(
                "chat.json",
                json.dumps(_clean_chat(chat_messages), ensure_ascii=False, indent=1).encode("utf-8"),
            )
            archive.write(pdf_path, "source.pdf")
            if markdown.strip():
                archive.writestr("translation.md", markdown.encode("utf-8"))
            for relative in sorted(assets):
                source = cache.root / relative
                if source.is_file():
                    archive.write(source, relative)
        os.replace(temp, target)
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass
    return ProjectExportResult(
        path=target,
        bytes=target.stat().st_size,
        blocks=len(blocks),
        translations=sum(1 for block in document.blocks if block.translation.strip()),
        assets=len(assets),
    )


def _read_json(archive: zipfile.ZipFile, name: str) -> Any:
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise ProjectError(f"项目包缺少 {name}") from exc
    if info.file_size > MAX_MEMBER:
        raise ProjectError(f"{name} 过大")
    try:
        return json.loads(archive.read(info).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProjectError(f"{name} 不是有效 JSON") from exc


def extract_project(archive_path: Path) -> ProjectBundle:
    """Extract a project package to imports/ and return its in-memory data."""

    path = Path(archive_path)
    if not path.is_file():
        raise ProjectError("项目包不存在")
    if path.stat().st_size > MAX_BYTES:
        raise ProjectError("项目包过大")
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if sum(info.file_size for info in infos) > MAX_BYTES:
                raise ProjectError("项目包解压后过大")
            for info in infos:
                if info.file_size > MAX_MEMBER:
                    raise ProjectError(f"项目包成员过大：{info.filename}")
                if _safe_name(info.filename) is None:
                    raise ProjectError(f"项目包包含非法路径：{info.filename}")
            meta = _read_json(archive, "meta.json")
            if not isinstance(meta, dict) or meta.get("schema") != PROJECT_SCHEMA:
                raise ProjectError("不支持的 EasyPaper 项目包")
            source = meta.get("source") if isinstance(meta.get("source"), dict) else {}
            source_hash = str(source.get("source_hash", ""))
            if not source_hash:
                raise ProjectError("项目包缺少源 PDF 指纹")
            structure_raw = _read_json(archive, "structure.json")
            if not isinstance(structure_raw, dict):
                raise ProjectError("structure.json 无效")
            structure = DocumentStructure.from_dict(structure_raw)
            if structure.source_hash != source_hash:
                raise ProjectError("结构缓存与源 PDF 指纹不一致")
            translations_raw = _read_json(archive, "translations.json")
            items = translations_raw.get("blocks") if isinstance(translations_raw, dict) else None
            if not isinstance(items, list):
                raise ProjectError("translations.json 缺少 blocks")
            translations: dict[str, str] = {}
            for item in items:
                if not isinstance(item, dict):
                    continue
                block_id = str(item.get("id", ""))
                value = item.get("translation")
                if block_id and isinstance(value, str) and value.strip():
                    translations[block_id] = value
            chat_raw = _read_json(archive, "chat.json")
            chat = _clean_chat(chat_raw if isinstance(chat_raw, list) else [])

            import_root = IMPORT_DIR / source_hash[:12]
            import_root.mkdir(parents=True, exist_ok=True)
            raw_name = Path(str(source.get("file_name") or "paper.pdf")).name
            if not raw_name.lower().endswith(".pdf"):
                raw_name = "paper.pdf"
            pdf_path = import_root / raw_name
            temp_pdf = pdf_path.with_name(pdf_path.name + ".part")
            try:
                with archive.open("source.pdf") as src, temp_pdf.open("wb") as dst:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        dst.write(chunk)
                if _sha_file(temp_pdf) != source_hash:
                    raise ProjectError("项目包中的 PDF 已损坏或 hash 不一致")
                os.replace(temp_pdf, pdf_path)
            finally:
                if temp_pdf.exists():
                    try:
                        temp_pdf.unlink()
                    except OSError:
                        pass

            assets_dir = import_root / "assets"
            assets_dir.mkdir(parents=True, exist_ok=True)
            for info in infos:
                name = _safe_name(info.filename)
                if name is None or not name.parts or name.parts[0] != "assets":
                    continue
                relative = PurePosixPath(*name.parts[1:])
                if not relative.parts:
                    continue
                target = assets_dir.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as src, target.open("wb") as dst:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        dst.write(chunk)
            return ProjectBundle(
                pdf_path=pdf_path,
                structure=structure,
                translations=translations,
                chat=chat,
                complete=bool(meta.get("complete")),
                assets_dir=assets_dir,
                source_hash=source_hash,
                warnings=[],
            )
    except zipfile.BadZipFile as exc:
        raise ProjectError("不是有效的 ZIP 项目包") from exc
    except OSError as exc:
        raise ProjectError(f"读取项目包失败：{exc}") from exc


def prepare_project_cache(bundle: ProjectBundle, config: AppConfig) -> DocumentCache:
    """Copy extracted structure/assets into the normal cache before load_pdf."""

    cache = DocumentCache(bundle.pdf_path, bundle.source_hash, config)
    cache.ensure()
    bundle.structure.source_path = str(bundle.pdf_path)
    bundle.structure.file_name = bundle.pdf_path.name
    bundle.structure.apply_translations(bundle.translations)
    cache.save_structure(bundle.structure)
    cache.save_translation(
        bundle.structure,
        dict(bundle.translations),
        complete=bundle.complete,
    )
    target_assets = cache.root / "assets"
    target_assets.mkdir(parents=True, exist_ok=True)
    if bundle.assets_dir.is_dir():
        for source in bundle.assets_dir.rglob("*"):
            if not source.is_file():
                continue
            relative = source.relative_to(bundle.assets_dir)
            target = target_assets / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                try:
                    os.replace(source, target)
                except OSError:
                    import shutil

                    shutil.copy2(source, target)
    return cache
