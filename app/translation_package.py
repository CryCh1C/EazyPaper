"""Portable translation packages (metadata + block translations + assets)."""

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
from .models import Block, DocumentStructure
from .paths import APP_VERSION
from .pdf.cache import DocumentCache

log = get_logger("package")

PACKAGE_SCHEMA = "easypaper.translation/1"
MAX_PACKAGE_BYTES = 200 * 1024 * 1024
MAX_MEMBER_BYTES = 50 * 1024 * 1024


class PackageError(RuntimeError):
    pass


@dataclass
class ExportResult:
    path: Path
    blocks: int
    translations: int
    assets: int
    bytes: int


@dataclass
class ImportResult:
    matched: int = 0
    total: int = 0
    skipped: int = 0
    assets: int = 0
    source_match: bool = False
    complete: bool = False
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _safe_name(name: str) -> PurePosixPath | None:
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts:
        return None
    if any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path


def export_translation_package(
    document: DocumentStructure,
    config: AppConfig,
    cache: DocumentCache,
    target: Path,
    markdown: str = "",
) -> ExportResult:
    """Write a ZIP that can be imported later without calling an LLM."""

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
                "source_sha256": _sha(block.text),
                "source_text": block.text,
                "translation": block.translation,
                "hidden": bool(block.hidden),
            }
        )
        asset = str(block.meta.get("asset") or "")
        asset_path = _safe_name(asset)
        if asset_path is not None and (cache.root / asset_path).is_file():
            assets.add(asset_path.as_posix())

    meta = {
        "schema": PACKAGE_SCHEMA,
        "app_version": APP_VERSION,
        "created_at": _now(),
        "source": {
            "file_name": document.file_name,
            "source_hash": document.source_hash,
            "page_count": document.page_count,
            "title": document.title,
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
    payload = {"schema": PACKAGE_SCHEMA, "blocks": blocks}

    temp = target.with_name(target.name + ".tmp")
    try:
        with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "meta.json",
                json.dumps(meta, ensure_ascii=False, indent=1).encode("utf-8"),
            )
            archive.writestr(
                "translations.json",
                json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8"),
            )
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
    return ExportResult(
        path=target,
        blocks=len(blocks),
        translations=sum(1 for block in document.blocks if block.translation.strip()),
        assets=len(assets),
        bytes=target.stat().st_size,
    )


def _read_json(archive: zipfile.ZipFile, name: str) -> Any:
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise PackageError(f"翻译包缺少 {name}") from exc
    if info.file_size > MAX_MEMBER_BYTES:
        raise PackageError(f"{name} 过大")
    try:
        return json.loads(archive.read(info).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackageError(f"{name} 不是有效 JSON") from exc


def _looks_like_html(value: str) -> bool:
    head = value.lstrip()[:80].lower()
    return head.startswith("<div") or head.startswith("<table") or head.startswith("<script")


def import_translation_package(
    archive_path: Path,
    document: DocumentStructure,
    cache: DocumentCache,
    config: AppConfig,
) -> ImportResult:
    """Import block translations/hidden states and referenced assets.

    Matching uses block id + source hash, then a unique source hash.  A package
    from a different PDF can still import text when block hashes match, but its
    page images are not copied.
    """

    path = Path(archive_path)
    if not path.is_file():
        raise PackageError("翻译包不存在")
    if path.stat().st_size > MAX_PACKAGE_BYTES:
        raise PackageError("翻译包过大")
    result = ImportResult()
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            total = sum(info.file_size for info in infos)
            if total > MAX_PACKAGE_BYTES:
                raise PackageError("翻译包解压后过大")
            for info in infos:
                if info.file_size > MAX_MEMBER_BYTES:
                    raise PackageError(f"翻译包成员过大：{info.filename}")
                if _safe_name(info.filename) is None:
                    raise PackageError(f"翻译包包含非法路径：{info.filename}")
            meta = _read_json(archive, "meta.json")
            payload = _read_json(archive, "translations.json")
            if not isinstance(meta, dict) or meta.get("schema") != PACKAGE_SCHEMA:
                raise PackageError("不支持的翻译包格式")
            source = meta.get("source") if isinstance(meta.get("source"), dict) else {}
            result.source_match = str(source.get("source_hash", "")) == document.source_hash
            if not result.source_match:
                result.warnings.append("源 PDF 指纹不一致，仅按块文本 hash 匹配；页面图片不会导入。")
            if str(meta.get("target_language", "")) != config.translation.target_language:
                result.warnings.append(
                    f"翻译包目标语言为 {meta.get('target_language') or '未知'}，"
                    f"当前为 {config.translation.target_language}。"
                )

            items = payload.get("blocks") if isinstance(payload, dict) else None
            if not isinstance(items, list):
                raise PackageError("translations.json 缺少 blocks")
            result.total = len(items)
            by_id = {block.id: block for block in document.blocks}
            by_hash: dict[str, list[Block]] = {}
            for block in document.blocks:
                by_hash.setdefault(_sha(block.text), []).append(block)

            for item in items:
                if not isinstance(item, dict):
                    result.skipped += 1
                    continue
                source_sha = str(item.get("source_sha256", ""))
                block = by_id.get(str(item.get("id", "")))
                if block is not None and source_sha and _sha(block.text) != source_sha:
                    block = None
                if block is None and source_sha:
                    candidates = by_hash.get(source_sha, [])
                    if len(candidates) == 1:
                        block = candidates[0]
                if block is None:
                    result.skipped += 1
                    continue
                value = item.get("translation")
                if not isinstance(value, str):
                    result.skipped += 1
                    continue
                if _looks_like_html(value):
                    # Never trust arbitrary HTML from an imported package.
                    result.warnings.append(f"{block.id} 的 HTML 译文已忽略")
                    value = ""
                block.translation = value.strip()
                block.hidden = bool(item.get("hidden"))
                result.matched += 1

            if result.source_match:
                for info in infos:
                    name = _safe_name(info.filename)
                    if name is None or not name.parts or name.parts[0] != "assets":
                        continue
                    relative = name.as_posix()
                    target = (cache.root / relative).resolve()
                    try:
                        target.relative_to(cache.root.resolve())
                    except ValueError:
                        continue
                    if target.exists():
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as source_file, target.open("wb") as output:
                        while True:
                            chunk = source_file.read(1024 * 1024)
                            if not chunk:
                                break
                            output.write(chunk)
                    result.assets += 1

            review = meta.get("review_summary")
            if result.source_match and isinstance(review, dict):
                document.review_summary = review
            result.complete = bool(meta.get("complete"))
            translations = {
                block.id: block.translation.strip()
                for block in document.blocks
                if block.translation.strip()
            }
            missing = [
                block
                for block in document.translatable_blocks()
                if block.text.strip() and block.id not in translations
            ]
            if missing:
                result.complete = False
            cache.save_structure(document)
            cache.save_translation(document, translations, complete=result.complete)
            return result
    except zipfile.BadZipFile as exc:
        raise PackageError("不是有效的 ZIP 翻译包") from exc
    except OSError as exc:
        raise PackageError(f"读取翻译包失败：{exc}") from exc
